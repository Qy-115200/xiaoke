#!/usr/bin/env bash
# 把四个平台的产物和 latest.json 传到 aikaui.com（nginx 静态目录）。
#
# 用法：
#   DEPLOY_HOST=49.233.72.225 DEPLOY_USER=root \
#     ./build_tools/deploy_downloads.sh 1.0.1 ./release
#
# 约定：产物文件名必须是 CI 输出的那四个名字（见下方 FILES）。
# 上传顺序是刻意的：先传 releases/<版本>/ 下的包，最后才把 latest.json
# 以「先写 .tmp 再 mv」的方式换上去 —— 客户端永远读不到半个清单。
#
# ★ 这是生产部署脚本，执行前先看清楚 HOST 和 TARGET。
set -euo pipefail

VERSION="${1:?用法: deploy_downloads.sh <版本> <产物目录>}"
SRC="${2:?用法: deploy_downloads.sh <版本> <产物目录>}"
HOST="${DEPLOY_HOST:?需要 DEPLOY_HOST}"
USER_NAME="${DEPLOY_USER:-root}"
REMOTE_BASE="${REMOTE_BASE:-/var/www/aikaui}"
TARGET="$REMOTE_BASE/releases/$VERSION"
BASE_URL="${BASE_URL:-https://aikaui.com/releases/$VERSION}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# CI 里私钥是临时文件，用 DEPLOY_SSH_KEY 指过来；本机直接跑就让它走 ssh-agent
KEY_OPTS=()
RSYNC_SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
if [ -n "${DEPLOY_SSH_KEY:-}" ]; then
  KEY_OPTS=(-i "$DEPLOY_SSH_KEY")
  RSYNC_SSH="ssh -i $DEPLOY_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
fi
SSH=(ssh "${KEY_OPTS[@]}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$USER_NAME@$HOST")

declare -a FILES=(
  "windows-x64:ClaudeCodeAssistant-win-x64.exe"
  "macos-arm64:ClaudeCodeAssistant-macos-arm64.zip"
  "macos-x64:ClaudeCodeAssistant-macos-x64.zip"
  "linux-x64:ClaudeCodeAssistant-linux-x64.tar.gz"
)

file_args=()
for item in "${FILES[@]}"; do
  key="${item%%:*}"
  name="${item#*:}"
  if [ ! -f "$SRC/$name" ]; then
    echo "✖ 缺产物：$SRC/$name" >&2
    exit 1
  fi
  file_args+=(--file "$key=$SRC/$name")
done

echo "▶ 生成清单 latest.json（v$VERSION）"
python "$ROOT/build_tools/make_manifest.py" \
  --version "$VERSION" \
  --base-url "$BASE_URL" \
  "${file_args[@]}" \
  --out "$SRC/latest.json"

echo "▶ 建目录 $TARGET"
"${SSH[@]}" "mkdir -p '$TARGET'"

echo "▶ 上传产物"
if command -v rsync >/dev/null 2>&1; then
  rsync -av -e "$RSYNC_SSH" --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    "$SRC"/ClaudeCodeAssistant-* "$USER_NAME@$HOST:$TARGET/"
else
  # Git Bash（Windows）通常没有 rsync；scp 效果一样，只是没有增量。
  echo "（没有 rsync，改用 scp）"
  scp -q "${KEY_OPTS[@]}" "$SRC"/ClaudeCodeAssistant-* "$USER_NAME@$HOST:$TARGET/"
  "${SSH[@]}" "chmod 644 '$TARGET'/*"
fi

echo "▶ 原子替换清单"
scp -q "${KEY_OPTS[@]}" "$SRC/latest.json" "$USER_NAME@$HOST:$REMOTE_BASE/latest.json.tmp"
"${SSH[@]}" "mv '$REMOTE_BASE/latest.json.tmp' '$REMOTE_BASE/latest.json'"

echo "▶ 校验"
curl -fsSI "$BASE_URL/$(basename "${FILES[0]#*:}")" | head -n 1
curl -fsS "https://aikaui.com/latest.json" | head -c 300
echo
echo "✔ 完成：$BASE_URL"
