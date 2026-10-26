#!/bin/sh
# Claude Code 助手 · 服务器版一键安装
#
#   curl -fsSL https://aikaui.com/install.sh | sh
#
# 做的事：从官网读 latest.json → 下 Linux 包 → 校验 sha256 → 解到 ~/.local
#         → 跑一次自检 → 把 ~/.local/bin 写进 PATH → 直接进安装向导。
#
# 五条设计约束（改这个文件之前先看一遍）：
#   1. **不提权**。全程只写 $HOME，不调 sudo。root 直接拒绝（AIKAUI_ALLOW_ROOT=1 才行）。
#   2. **不写死版本号**。版本、下载地址、sha256、大小全部来自 latest.json。
#   3. **不假装成功**。sha256 不对、大小不对、包里有 `../`、跑不起来 —— 一律退出非零。
#   4. **幂等**。重跑就是升级：二进制原位置原子替换，PATH 块已经有过就不再写。
#   5. **POSIX sh**。服务器上可能是 dash / busybox sh，不能依赖 bash 的语法。
#
# 可选环境变量：
#   AIKAUI_SITE=https://aikaui.com   清单地址的前缀（测试时指向本地）
#   AIKAUI_VERSION=1.2.0             只装指定版本（默认取清单里的）
#   AIKAUI_NO_RUN=1                  装完不启动向导
#   AIKAUI_ALLOW_ROOT=1              允许 root 运行（不推荐）
#   AIKAUI_LIB=~/.local/lib/claude-code-assistant   安装目录
#
# 退出码：0 成功 / 1 失败 / 2 用法或环境 / 3 校验失败 / 4 装上了但跑不起来

set -eu

SITE="${AIKAUI_SITE:-https://aikaui.com}"
SITE="${SITE%/}"
MANIFEST="$SITE/latest.json"
PLATFORM="linux-x64"
START_MARK="# >>> aikaui >>>"
END_MARK="# <<< aikaui <<<"
PREFIX="${HOME}/.local"
LIB="${AIKAUI_LIB:-$PREFIX/lib/claude-code-assistant}"
BIN_LINK="$PREFIX/bin/claude-code-assistant"

say()  { printf '%s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*" >&2; }
die()  { printf '✖ %s\n' "$*" >&2; exit "${2:-1}"; }

TMP=""
cleanup() { if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT INT TERM HUP

# ---------------------------------------------------------------- 环境

say "▶ Claude Code 助手 · 服务器版安装器"

if [ "$(id -u 2>/dev/null || echo 1)" = "0" ] && [ "${AIKAUI_ALLOW_ROOT:-}" != "1" ]; then
  die "请不要用 root 运行。装到 /root 下面的东西，普通用户以后用不了。
   确实要这样请加：AIKAUI_ALLOW_ROOT=1" 2
fi

case "${HOME:-}" in
  ""|"/") die "HOME 没设置或等于 /，不敢往下装" 2 ;;
esac

command -v tar >/dev/null 2>&1 || die "找不到 tar" 2
command -v awk >/dev/null 2>&1 || die "找不到 awk" 2

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL --retry 3 --connect-timeout 15 --max-time "$2" -o "$3" "$1"; }
  fetch_text() { curl -fsSL --retry 3 --connect-timeout 15 --max-time 60 "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -T "$2" -O "$3" "$1"; }
  fetch_text() { wget -q -T 60 -O - "$1"; }
else
  die "需要 curl 或 wget 才能下载安装包" 2
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/aikaui.XXXXXX" 2>/dev/null || true)"
[ -n "$TMP" ] || die "创建临时目录失败" 2

# ---------------------------------------------------------------- 读清单
#
# latest.json 是 build_tools/make_manifest.py 用 json.dumps(indent=2) 写的，
# 形状固定成这样（平台名在第 4 列，字段在第 6 列）：
#
#     "latest": {
#       "linux-x64": {
#         "version": "1.2.0",
#         ...
#       },
#
# 所以用一段按缩进的 awk 状态机就够了 —— 不依赖 jq / python。
# ★ 只认"去掉前导空白之后正好是 "linux-x64": {" 的那一行：
#   notes 字段里可能原样带着 "linux-x64": { 这段文字（发版说明就写过），
#   那种行是 `"notes": "…"` 开头的，不会被误认成平台块的开头。

manifest_field() {
  # manifest_field <文件> <平台> <字段名>
  awk -v plat="$2" -v key="$3" '
    BEGIN { inplat = 0 }
    {
      line = $0
      sub(/\r$/, "", line)                      # 清单可能是在 Windows 上生成的（CRLF）
      sub(/^[ \t]+/, "", line)
      if (!inplat) {
        if (line == "\"" plat "\": {") { inplat = 1 }
        next
      }
      if (line ~ /^\}/) { exit }                 # 平台块结束
      if (index(line, "\"" key "\"") == 1) {
        val = line
        sub("^\"" key "\"[ \t]*:[ \t]*", "", val)
        sub(/,$/, "", val)
        gsub(/"/, "", val)
        print val
        exit
      }
    }
  ' "$1"
}

say "▶ 读取版本信息（$MANIFEST）"
fetch_text "$MANIFEST" > "$TMP/latest.json" 2>/dev/null \
  || die "拉不到 $MANIFEST —— 网络通不通？" 1

VERSION="$(manifest_field "$TMP/latest.json" "$PLATFORM" version)"
URL="$(manifest_field "$TMP/latest.json" "$PLATFORM" url)"
SHA="$(manifest_field "$TMP/latest.json" "$PLATFORM" sha256)"
SIZE="$(manifest_field "$TMP/latest.json" "$PLATFORM" size)"

[ -n "$VERSION" ] || die "清单里没有 $PLATFORM 的 version（清单格式变了？）" 1
[ -n "$URL" ]     || die "清单里没有 $PLATFORM 的 url" 1

NAME="$(printf '%s' "$URL" | sed 's#.*/##')"
[ -n "$NAME" ] || die "解析不出文件名：$URL" 1

case "$URL" in
  https://*) : ;;
  *) die "清单里的下载地址不是 https：$URL" 3 ;;
esac

if [ -n "${AIKAUI_VERSION:-}" ] && [ "$AIKAUI_VERSION" != "$VERSION" ]; then
  say "  （你指定了 $AIKAUI_VERSION，但清单里是 $VERSION；按清单装）"
fi
say "  版本 $VERSION"
say "  $NAME"

# ---------------------------------------------------------------- 下载 + 校验

PKG="$TMP/$NAME"

say "▶ 下载安装包"
fetch "$URL" 600 "$PKG" || die "下载失败：$URL" 1

ACTUAL_SIZE="$(wc -c < "$PKG" | tr -d '[:space:]')"
if [ -n "$SIZE" ] && [ "$ACTUAL_SIZE" != "$SIZE" ]; then
  die "下载的大小不对：清单说 $SIZE 字节，拿到 $ACTUAL_SIZE 字节" 3
fi

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$1" | awk '{print $NF}'
  else
    printf ''
  fi
}

if [ -n "$SHA" ]; then
  GOT="$(sha256_of "$PKG")"
  if [ -z "$GOT" ]; then
    warn "这台机器没有 sha256sum / shasum / openssl，跳过校验（大小已经对上了）"
  elif [ "$GOT" != "$SHA" ]; then
    die "校验失败：下载内容损坏或被替换过
   清单 $SHA
   实际 $GOT" 3
  fi
else
  warn "清单里没有 sha256，跳过校验"
fi

# ---------------------------------------------------------------- 解压

say "▶ 解压"
tar -tzf "$PKG" > "$TMP/list.txt" 2>/dev/null || die "压缩包读不了（不是 tar.gz？）" 3
[ -s "$TMP/list.txt" ] || die "压缩包是空的" 3

# ★ 逐条查路径：一个 ../../.ssh/authorized_keys 就够把整台机器交出去。
while IFS= read -r entry; do
  case "$entry" in
    /*|../*|*/../*|*/..|..)
      die "压缩包里有不安全的路径：$entry" 3 ;;
  esac
done < "$TMP/list.txt"

mkdir -p "$TMP/x"
tar -xzf "$PKG" -C "$TMP/x" || die "解压失败" 3

BIN="$(find "$TMP/x" -maxdepth 3 -type f -name ClaudeCodeAssistant | head -n 1)"
[ -n "$BIN" ] || die "压缩包里找不到 ClaudeCodeAssistant 可执行文件" 3
[ -s "$BIN" ] || die "解出来的 ClaudeCodeAssistant 是空文件" 3

# ---------------------------------------------------------------- 安装

say "▶ 安装到 $LIB"
mkdir -p "$LIB" "$PREFIX/bin" || die "建不了 $LIB（权限？磁盘满？）"

# 先拷成临时文件再 mv：正在运行的旧版本不会被写坏（mv 同目录是原子的）
STAGE="$LIB/.ClaudeCodeAssistant.tmp.$$"
cp "$BIN" "$STAGE" || die "拷贝到 $LIB 失败（磁盘满？权限？）"
chmod 755 "$STAGE"
mv -f "$STAGE" "$LIB/ClaudeCodeAssistant"

BIN="$LIB/ClaudeCodeAssistant"

ln -sfn "$BIN" "$BIN_LINK" 2>/dev/null || warn "建软链 $BIN_LINK 失败，可以直接用 $BIN"

# ---------------------------------------------------------------- 自检

say "▶ 自检"
if ! OUT="$("$BIN" --version 2>&1)"; then
  die "装上了但跑不起来：
   $OUT
   多半是 glibc 太老或架构不对（这个包要 x86_64 / glibc ≥ 2.14）" 4
fi
say "  $OUT"

# ---------------------------------------------------------------- PATH

add_path_block() {
  # 0 = 刚写进去 / 1 = 写失败 / 2 = 早就有了（一个字都不动）
  if [ -f "$1" ] && grep -qF "$START_MARK" "$1" 2>/dev/null; then
    return 2
  fi
  {
    if [ -f "$1" ] && [ -s "$1" ]; then printf '\n'; fi
    printf '%s\n' "$START_MARK"
    printf 'export PATH="%s/bin:$PATH"\n' "$PREFIX"
    printf '%s\n' "$END_MARK"
  } >> "$1" 2>/dev/null || return 1
  return 0
}

say "▶ 配置 PATH"
CHANGED=""
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
  rc_status=0
  add_path_block "$rc" || rc_status=$?
  if [ "$rc_status" = "0" ]; then
    CHANGED="$CHANGED $rc"
  elif [ "$rc_status" = "1" ]; then
    warn "写不进 $rc（只影响下次登录时的 PATH）"
  fi
done
case "${SHELL:-}" in
  *zsh*)
    if add_path_block "$HOME/.zshrc"; then
      CHANGED="$CHANGED $HOME/.zshrc"
    fi
    ;;
esac
if [ -n "$CHANGED" ]; then
  say "  已写入：$CHANGED"
else
  say "  PATH 之前就配好了，没动"
fi

# ---------------------------------------------------------------- 进向导

if [ "${AIKAUI_NO_RUN:-0}" = "1" ]; then
  say ""
  say "✅ 装好了（AIKAUI_NO_RUN=1，没有启动向导）。"
  say "   手动继续：$BIN"
  exit 0
fi

say ""
if [ -t 0 ] && [ -t 1 ]; then
  # 直接在终端里跑：让向导继承这个终端
  set +e
  "$BIN"
  code=$?
  set -e
  exit $code
fi

if [ -r /dev/tty ] && ( : < /dev/tty ) 2>/dev/null; then
  # `curl … | sh` 的情况：stdin 是脚本本身，把它接回终端
  say "▶ 启动安装向导"
  set +e
  "$BIN" < /dev/tty
  code=$?
  set -e
  exit $code
fi

say "✅ 装好了。这里没有终端，向导没启动。"
say "   在终端里执行这一句继续："
say "     $BIN"
exit 0
