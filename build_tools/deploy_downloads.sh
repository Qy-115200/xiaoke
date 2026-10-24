#!/usr/bin/env bash
# 把四个平台的产物 + latest.json 发到生产，让**两边**都能取到：
#
#   nginx  /var/www/aikaui/releases/<版本>/  →  https://aikaui.com/releases/<版本>/…
#   COS    releases/<版本>/                  →  https://dl.aikaui.com/releases/<版本>/…（CDN）
#
# ★ 为什么要传两份：下载页的 HTML 用相对路径走 aikaui.com（nginx），而
#   latest.json 里给自动更新用的 url 指向 dl.aikaui.com（COS + CDN）。只传一边，
#   另一边就是 404 —— 客户端不报错，只在 update.log 里留一行，6 小时后再试一次，
#   比不发版更难发现。所以两边都传、两边都验。
#
# 用法：
#   DEPLOY_HOST=… DEPLOY_USER=root ./build_tools/deploy_downloads.sh 1.0.1 ./release
#
#   DRY_RUN=1   只做本地校验（生成清单 + 用客户端自己的 updater 验一遍），不碰服务器
#   SKIP_COS=1  明确知道这次不用推 COS 时才用（默认不跳）
#   NOTES="…"   写进清单的发布说明（客户端不会显示，但排查时有用）
#   SITE_BASE / CDN_BASE / BASE_URL / COS_PREFIX   可以覆盖，默认就是上面那套
#
# 顺序是刻意的：
#   传包到 nginx → 传包到 COS → 两边都验大小 → 才换 latest.json → 再改下载页链接
# 任何一步没验过就退出：清单永远不会指向一个取不到的文件。
#
# ★ 这是生产部署脚本，执行前先看清楚 HOST 和 TARGET。
set -euo pipefail

die() { echo "✖ $*" >&2; exit 1; }

VERSION="${1:?用法: deploy_downloads.sh <版本> <产物目录>}"
SRC="${2:?用法: deploy_downloads.sh <版本> <产物目录>}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "版本号必须是 X.Y.Z，收到：$VERSION"
[ -d "$SRC" ] || die "产物目录不存在：$SRC"

DRY_RUN="${DRY_RUN:-0}"
SKIP_COS="${SKIP_COS:-0}"
NOTES="${NOTES:-}"

HOST="${DEPLOY_HOST:?需要 DEPLOY_HOST（DRY_RUN=1 时也要给，只是不会真连）}"
USER_NAME="${DEPLOY_USER:-root}"
REMOTE_BASE="${REMOTE_BASE:-/var/www/aikaui}"
TARGET="$REMOTE_BASE/releases/$VERSION"
SITE_BASE="${SITE_BASE:-https://aikaui.com}"          # nginx：下载页走这个
CDN_BASE="${CDN_BASE:-https://dl.aikaui.com}"         # COS + CDN：自动更新走这个
BASE_URL="${BASE_URL:-$CDN_BASE/releases/$VERSION}"   # 清单里的 url 前缀
COS_PREFIX="${COS_PREFIX:-releases/$VERSION}"         # 桶内路径（不带开头的斜杠）
PAGES=(index.html download/index.html)                # 这两个页面里写死了 /releases/<版本>/

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ubuntu 的 CI runner 上不一定有 `python`（只有 `python3`），本地 Windows 反过来。
PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
command -v "$PY" >/dev/null 2>&1 || die "找不到 python / python3"

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
srcs=()
names=()
for item in "${FILES[@]}"; do
  key="${item%%:*}"
  name="${item#*:}"
  [ -f "$SRC/$name" ] || die "缺产物：$SRC/$name"
  file_args+=(--file "$key=$SRC/$name")
  srcs+=("$SRC/$name")
  names+=("$name")
done

# 让远端 for 循环用（文件名固定是 ASCII，可以直接拼进命令）
remote_names="${names[*]}"

echo "▶ 生成清单 latest.json（v$VERSION，url 指向 $BASE_URL）"
"$PY" "$ROOT/build_tools/make_manifest.py" \
  --version "$VERSION" \
  --base-url "$BASE_URL" \
  --notes "$NOTES" \
  "${file_args[@]}" \
  --out "$SRC/latest.json"

# ★ 用客户端自己的 updater 验一遍：形状、https、sha256、size、min_version。
#   这里能过，说明客户端能接受这份清单；过不了就别往上推。
echo "▶ 本地校验清单（用 updater.py，和客户端同一套规则）"
"$PY" - "$ROOT" "$SRC/latest.json" "$VERSION" <<'PY'
import json, sys
root, path, version = sys.argv[1:4]
sys.path.insert(0, root)
import updater                                    # 不 import tkinter
from config import APP_MIN_UPDATE_FROM

data = json.load(open(path, encoding="utf-8"))
if not updater.validate_manifest(data):
    sys.exit("清单形状不对（schema_version / latest）")
for key in ("windows-x64", "macos-arm64", "macos-x64", "linux-x64"):
    rel = updater.select_release(data, key, current=APP_MIN_UPDATE_FROM)
    if not rel or rel["version"] != version:
        sys.exit(f"{key}：从 {APP_MIN_UPDATE_FROM} 升不到 {version}")
    if updater.select_release(data, key, current=version) is not None:
        sys.exit(f"{key}：对已经是最新的用户也返回了更新")
print(f"  ✔ 四个平台都能从 {APP_MIN_UPDATE_FROM} 升到 {version}，已是最新的不会被重复更新")
PY

if [ "$DRY_RUN" = "1" ]; then
  echo "▶ DRY_RUN=1：清单已生成并验过，没有连服务器。真正发布要跑的步骤是："
  echo "   1. ssh $USER_NAME@$HOST mkdir -p $TARGET"
  echo "   2. 上传 ${#srcs[@]} 个产物到 $TARGET/"
  if [ "$SKIP_COS" = "1" ]; then
    echo "   3. （SKIP_COS=1）不推 COS —— dl.aikaui.com 不会更新，自动更新会 404"
  else
    echo "   3. 在服务器上 coscmd upload 到 $COS_PREFIX/"
  fi
  echo "   4. 两边 HEAD 校验大小"
  echo "   5. 原子换 $REMOTE_BASE/latest.json"
  echo "   6. 把 ${PAGES[*]} 里的 /releases/<旧版本>/ 改成 /releases/$VERSION/"
  echo "✔ 完成（DRY_RUN）"
  exit 0
fi

echo "▶ 建目录 $TARGET"
"${SSH[@]}" "mkdir -p '$TARGET'"

echo "▶ 上传产物到 nginx（$TARGET/）"
# 只传清单里点名的四个文件。不要用 ClaudeCodeAssistant-* 通配：
# 用 API 下载 artifact 时目录里会多出 *.zip 外壳，通配会把它们一起传上去。
if command -v rsync >/dev/null 2>&1; then
  rsync -av -e "$RSYNC_SSH" --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    "${srcs[@]}" "$USER_NAME@$HOST:$TARGET/"
else
  # Git Bash（Windows）通常没有 rsync；scp 效果一样，只是没有增量。
  echo "（没有 rsync，改用 scp）"
  scp -q "${KEY_OPTS[@]}" "${srcs[@]}" "$USER_NAME@$HOST:$TARGET/"
  "${SSH[@]}" "chmod 644 '$TARGET'/*"
fi

if [ "$SKIP_COS" = "1" ]; then
  echo "⚠ SKIP_COS=1：不推 COS。dl.aikaui.com 上还是旧的 —— 清单里的 url 会 404。"
else
  echo "▶ 上传产物到 COS（$COS_PREFIX/，dl.aikaui.com 的源）"
  # 从服务器上那个目录直接推，省得 150MB 再走一遍 CI 的带宽。
  # coscmd 的 cos_path 不带开头斜杠；-y 是"不确认直接覆盖"（重跑这个脚本时才用得到）。
  "${SSH[@]}" "command -v coscmd >/dev/null 2>&1" \
    || die "服务器上没有 coscmd —— 推不上去 COS。装上它，或者确认这次真的不用推再 SKIP_COS=1"
  "${SSH[@]}" "set -euo pipefail; cd '$TARGET'; for f in $remote_names; do echo \"  ↑ \$f\"; coscmd upload -y \"\$f\" '$COS_PREFIX/'\"\$f\"; done"
fi

# ★ 这一步就是上次踩的坑：只验了一边。两边都验，大小必须和本地一致。
check_url() {
  local url="$1" want="$2" label="$3" got="" attempt
  for attempt in 1 2 3; do
    got="$(curl -fsSI --max-time 60 "$url" 2>/dev/null | tr -d '\r' \
           | awk 'tolower($1) == "content-length:" { v = $2 } END { print v }')"
    if [ "$got" = "$want" ]; then
      echo "  ✔ $label  $got 字节"
      return 0
    fi
    sleep 3
  done
  echo "  ✖ $label  期望 $want 字节，拿到 '${got:-取不到}'（$url）" >&2
  return 1
}

echo "▶ 校验两边都能取到"
for i in "${!srcs[@]}"; do
  name="${names[$i]}"
  want="$(wc -c < "${srcs[$i]}" | tr -d '[:space:]')"
  check_url "$SITE_BASE/releases/$VERSION/$name" "$want" "aikaui.com/$name" \
    || die "aikaui.com 上没有这个文件 —— 清单不能发"
  if [ "$SKIP_COS" != "1" ]; then
    check_url "$CDN_BASE/releases/$VERSION/$name" "$want" "dl.aikaui.com/$name" \
      || die "dl.aikaui.com 上没有这个文件 —— 清单不能发（客户端会自动更新到 404）"
  fi
done

echo "▶ 原子替换清单"
# 先写 .tmp 再 mv：客户端永远读不到半个清单。
scp -q "${KEY_OPTS[@]}" "$SRC/latest.json" "$USER_NAME@$HOST:$REMOTE_BASE/latest.json.tmp"
"${SSH[@]}" "mv '$REMOTE_BASE/latest.json.tmp' '$REMOTE_BASE/latest.json'"

# 线上那份必须和本地这份一字不差（CDN 会缓存旧清单，这里如果拿到的是旧的，
# 说明前面某一步没生效 —— 别急着说"发完了"）。
live="$(mktemp)"
trap 'rm -f "$live"' EXIT
for attempt in 1 2 3 4 5; do
  curl -fsS --max-time 30 -o "$live" "$SITE_BASE/latest.json" || true
  cmp -s "$SRC/latest.json" "$live" && break
  sleep 3
done
cmp -s "$SRC/latest.json" "$live" \
  || die "线上 latest.json 和本地不一致（等了一会儿还是旧的）—— 去服务器上看一眼"

echo "▶ 改下载页里的版本号"
# 两个页面里写死了 /releases/1.0.0/ 这种链接，只换清单的话新用户下载到的还是旧包。
"${SSH[@]}" "VERSION='$VERSION' REMOTE_BASE='$REMOTE_BASE' bash -s" <<'REMOTE'
set -euo pipefail
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
for page in index.html download/index.html; do
  f="$REMOTE_BASE/$page"
  if [ ! -f "$f" ]; then
    echo "  ✖ 找不到页面：$f" >&2
    exit 1
  fi
  if ! grep -Eq '/releases/[0-9]+\.[0-9]+\.[0-9]+/' "$f"; then
    echo "  · $page 里没有版本号链接，跳过"
    continue
  fi
  stale="$(grep -Eo '/releases/[0-9]+\.[0-9]+\.[0-9]+/' "$f" | grep -v "/releases/$VERSION/" || true)"
  if [ -z "$stale" ]; then
    echo "  · $page 已经指向 /releases/$VERSION/，跳过"
    continue
  fi
  cp -p "$f" "$f.bak-$stamp"
  sed -E "s#/releases/[0-9]+\.[0-9]+\.[0-9]+/#/releases/$VERSION/#g" "$f.bak-$stamp" > "$f.new"
  mv "$f.new" "$f"          # 同目录 mv 是原子的：nginx 不会读到半个页面
  left="$(grep -Eo '/releases/[0-9]+\.[0-9]+\.[0-9]+/' "$f" | grep -v "/releases/$VERSION/" || true)"
  if [ -n "$left" ]; then
    echo "  ✖ $page 里还有旧版本链接：" >&2
    echo "$left" | sort -u >&2
    exit 1
  fi
  echo "  ✔ $page → /releases/$VERSION/（备份 $f.bak-$stamp）"
done
REMOTE

echo "▶ 最终确认（客户端的视角）"
"$PY" - "$SITE_BASE" "$VERSION" "$CDN_BASE" <<'PY'
import json, sys, urllib.request
site, version, cdn = sys.argv[1:4]
with urllib.request.urlopen(f"{site}/latest.json", timeout=30) as r:
    data = json.load(r)
entries = data.get("latest") or {}
bad = []
for key, entry in entries.items():
    if entry.get("version") != version:
        bad.append(f"{key} 版本是 {entry.get('version')}")
    if not str(entry.get("url", "")).startswith(f"{cdn}/releases/{version}/"):
        bad.append(f"{key} 的 url 没指向 {cdn}/releases/{version}/")
if bad:
    sys.exit("清单内容不对：\n  " + "\n  ".join(bad))
print(f"  ✔ latest.json 四个平台都是 {version}，url 指向 {cdn}")
PY

echo
echo "✔ 完成：v$VERSION 已同时上线"
echo "   下载页  $SITE_BASE/releases/$VERSION/"
echo "   更新源  $CDN_BASE/releases/$VERSION/"
echo "   latest.json 已指向 v$VERSION（客户端 6 小时内会看到）。"
