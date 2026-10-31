#!/usr/bin/env bash
# 把微软官方的 App Installer（x64 的 msix）放到我们自己的 CDN 上 —— 客户端
# platform_support/windows.py 里的 WINGET_BASES 指的就是这个位置：
#
#   nginx  /var/www/aikaui/releases/winget/<版本>/  →  https://aikaui.com/releases/winget/<版本>/…
#   COS    releases/winget/<版本>/                →  https://dl.aikaui.com/releases/winget/<版本>/…
#
# ★ 为什么单独一个脚本：这份包**不属于任何一次发版**（版本号固定、内容永久不变，
#   客户端把它当 immutable 缓存），所以不放进 deploy_downloads.sh。换 winget 版本
#   时才跑一次，然后把 platform_support/windows.py 里的
#   WINGET_VERSION / WINGET_MSIX_SIZE / WINGET_MSIX_SHA256 一起改掉。
#
# 怎么拿到这个包（不需要装什么，也不需要改包）：
#   1. 下微软的 msixbundle：
#      https://github.com/microsoft/winget-cli/releases/download/v<版本>/Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle
#   2. 从里面取出 x64 的那份（bundle 只是个子包压缩包）：
#      python -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extract('AppInstaller_x64.msix', sys.argv[2])" \
#        bundle.msixbundle ./out
#
# 用法：
#   DEPLOY_HOST=… ./build_tools/deploy_winget.sh ./build/winget_assets/AppInstaller_x64.msix 1.11.430
#
#   DRY_RUN=1  只算 sha256 / 大小，不上传
#
# ★ 顺带把两个**依赖框架包**一起传（2026-10-31 加）：
#   App Installer 依赖 Microsoft.UI.Xaml.2.8 和 Microsoft.VCLibs.140.00.UWPDesktop，
#   少了它们 Add-AppxPackage 只回一个干巴巴的 `1`。客户端
#   platform_support/windows.py 的 WINGET_DEPS 从
#   releases/winget/<版本>/deps/<同名文件> 下它们，所以这两个也得在这儿。
#   本地放哪：DEPLOY_DEPS_DIR（默认 <脚本目录>/../build/winget_deps）；
#   里面没有就跳过（老机器上没解过 Dependencies.zip 时还能只发主包），并提醒一句。
set -euo pipefail

die() { echo "✖ $*" >&2; exit 1; }

SRC="${1:?用法: deploy_winget.sh <AppInstaller_x64.msix> <版本>}"
VERSION="${2:?用法: deploy_winget.sh <AppInstaller_x64.msix> <版本>}"
[ -f "$SRC" ] || die "找不到文件：$SRC"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "版本号必须是 X.Y.Z，收到：$VERSION"

HOST="${DEPLOY_HOST:?需要 DEPLOY_HOST（DRY_RUN=1 时也要给，只是不会真连）}"
USER_NAME="${DEPLOY_USER:-root}"
REMOTE_BASE="${REMOTE_BASE:-/var/www/aikaui}"
NAME="AppInstaller_x64.msix"
TARGET="$REMOTE_BASE/releases/winget/$VERSION"
COS_PREFIX="releases/winget/$VERSION"
SITE_BASE="${SITE_BASE:-https://aikaui.com}"
CDN_BASE="${CDN_BASE:-https://dl.aikaui.com}"
DRY_RUN="${DRY_RUN:-0}"

PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
command -v "$PY" >/dev/null 2>&1 || die "找不到 python / python3"
export PYTHONIOENCODING=utf-8

SIZE="$("$PY" -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$SRC")"
SHA="$("$PY" -c 'import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],"rb");
[h.update(b) for b in iter(lambda: f.read(1<<20), b"")]; print(h.hexdigest())' "$SRC")"
echo "▶ $NAME  $SIZE 字节  sha256 $SHA"

# ---------- 依赖框架包 ----------
# 名字 / 大小 / sha256 必须和 platform_support/windows.py 的 WINGET_DEPS 一模一样，
# 客户端的 _download 会逐字节校验，对不上就不装。
DEPS_DIR="${DEPLOY_DEPS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/build/winget_deps}"
DEP_NAMES=(
  "Microsoft.VCLibs.140.00.UWPDesktop_14.0.33728.0_x64.appx"
  "Microsoft.UI.Xaml.2.8_8.2310.30001.0_x64.appx"
)
DEPS=()
for n in "${DEP_NAMES[@]}"; do
  if [ -f "$DEPS_DIR/$n" ]; then
    DEPS+=("$DEPS_DIR/$n")
  else
    echo "⚠ 没找到依赖包 $DEPS_DIR/$n —— 跳过它（客户端会退到只装主包）" >&2
  fi
done
for d in "${DEPS[@]}"; do
  echo "▶ $(basename "$d")  $("$PY" -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$d") 字节"
done

if [ "$DRY_RUN" = "1" ]; then
  echo "▶ DRY_RUN=1：会执行"
  echo "   ssh $USER_NAME@$HOST mkdir -p $TARGET && scp $SRC → $TARGET/"
  echo "   ssh … coscmd upload -y $NAME $COS_PREFIX/$NAME"
  for d in "${DEPS[@]}"; do
    echo "   scp $(basename "$d") → $TARGET/deps/ && coscmd upload → $COS_PREFIX/deps/"
  done
  echo "▶ 记得把 platform_support/windows.py 里的 WINGET_VERSION / WINGET_MSIX_SIZE /"
  echo "  WINGET_MSIX_SHA256 改成上面这三个值（现在写的是另一份，客户端会校验不过）"
  exit 0
fi

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[ -n "${DEPLOY_SSH_KEY:-}" ] && SSH_OPTS+=(-i "$DEPLOY_SSH_KEY")
SSH=(ssh "${SSH_OPTS[@]}" "$USER_NAME@$HOST")

echo "▶ 传到 nginx（$TARGET/）"
"${SSH[@]}" "mkdir -p '$TARGET'"
scp -q "${SSH_OPTS[@]}" "$SRC" "$USER_NAME@$HOST:$TARGET/$NAME"
"${SSH[@]}" "chmod 644 '$TARGET/$NAME'"

DEPS_TARGET="$TARGET/deps"
if [ "${#DEPS[@]}" -gt 0 ]; then
  echo "▶ 依赖包传到 nginx（$DEPS_TARGET/）"
  "${SSH[@]}" "mkdir -p '$DEPS_TARGET'"
  for d in "${DEPS[@]}"; do
    scp -q "${SSH_OPTS[@]}" "$d" "$USER_NAME@$HOST:$DEPS_TARGET/$(basename "$d")"
    "${SSH[@]}" "chmod 644 '$DEPS_TARGET/$(basename "$d")'"
  done
fi

echo "▶ 上传到 COS（$COS_PREFIX/，dl.aikaui.com 的源）"
"${SSH[@]}" "command -v coscmd >/dev/null 2>&1" \
  || die "服务器上没有 coscmd"
"${SSH[@]}" "set -euo pipefail; cd '$TARGET'; coscmd upload -y '$NAME' '$COS_PREFIX/$NAME'"
if [ "${#DEPS[@]}" -gt 0 ]; then
  for d in "${DEPS[@]}"; do
    n="$(basename "$d")"
    "${SSH[@]}" "set -euo pipefail; cd '$DEPS_TARGET'; coscmd upload -y '$n' '$COS_PREFIX/deps/$n'"
  done
fi

echo "▶ 两边都验一遍（大小必须和本地一致）"
check() {
  local url="$1" label="$2" want="$3" got=""
  for _ in 1 2 3 4 5; do          # CDN 首次回源可能要几秒
    got="$(curl -fsSI --max-time 60 "$url" 2>/dev/null | tr -d '\r' \
           | awk 'tolower($1) == "content-length:" { v = $2 } END { print v }')"
    [ "$got" = "$want" ] && { echo "  ✔ $label  $got 字节"; return 0; }
    sleep 3
  done
  echo "  ✖ $label 期望 $want 字节，拿到 '${got:-取不到}'（$url）" >&2
  return 1
}
check "$SITE_BASE/releases/winget/$VERSION/$NAME" "aikaui.com" "$SIZE" || die "nginx 上取不到"
check "$CDN_BASE/releases/winget/$VERSION/$NAME" "dl.aikaui.com" "$SIZE" || die "CDN 上取不到"
for d in "${DEPS[@]}"; do
  n="$(basename "$d")"
  want="$("$PY" -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$d")"
  check "$SITE_BASE/releases/winget/$VERSION/deps/$n" "aikaui.com/deps/$n" "$want" \
    || die "依赖包在 nginx 上取不到：$n"
  check "$CDN_BASE/releases/winget/$VERSION/deps/$n" "dl.aikaui.com/deps/$n" "$want" \
    || die "依赖包在 CDN 上取不到：$n"
done

echo "✔ 上线了。把 platform_support/windows.py 里的三个常量改成："
echo "    WINGET_VERSION = \"$VERSION\""
echo "    WINGET_MSIX_SIZE = $SIZE"
echo "    WINGET_MSIX_SHA256 = \"$SHA\""
