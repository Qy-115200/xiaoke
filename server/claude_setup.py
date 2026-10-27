# -*- coding: utf-8 -*-
"""装 `claude` 命令本身（`@anthropic-ai/claude-code`），装进 `~/.local`。

三件容易踩的事，都在这儿处理掉了：

1. **不提权**：`npm install -g --prefix ~/.local`。root 由 `server_main.py`
   在入口处就拒掉，这里不碰 sudo。
2. **不继承用户的 npm 配置**：很多人的 `~/.npmrc` 里写着 `ignore-scripts=true`
   或 `omit=optional`。这个包的 npm 包只是个壳，真正的原生二进制由 postinstall
   从 optionalDependencies 里拷过去 —— 上面两条任意一条生效，装完就是一堆
   JS 没有可执行文件。所以把继承来的 `npm_config_*` 全部摘掉，再显式给值。
3. **认准 `claude` 在哪**：npm 的 bin 软链、包内 `bin/claude.exe` 都要试，
   最后用 `claude --version` 定论 —— 文件存在不等于能跑（架构/glibc 不对时
   报的是 `Exec format error`）。
4. **原生包单独兜一次**：主包只有 29KB，`bin/claude.exe` 是个 500 字节的占位，
   真正的二进制在 optionalDependencies 的平台子包里（linux-x64：132MB 下载 /
   293MB 解压）。★ npm 对 optional 依赖的失败**不报错**，退出码还是 0 —— 下载
   断一次，结果就是「装好了但没有 claude」。所以拿不到可执行文件时单独装一次
   平台包（这次是普通依赖，失败会真的报错），再手动跑一遍壳的 postinstall。
"""

import os
import subprocess
import time
from pathlib import Path

from server.cli import EXIT_NPM, CliError, atomic_symlink

CLAUDE_PACKAGE = "@anthropic-ai/claude-code"
NPM_REGISTRY = "https://registry.npmmirror.com"

INSTALL_TIMEOUT = 1800         # 原生包 132MB，慢网络下十几分钟；900 秒不够
VERSION_TIMEOUT = 60
POSTINSTALL_TIMEOUT = 300      # postinstall 要把 293MB 硬链/拷成 claude.exe
NATIVE_DOWNLOAD_MB = 130       # 给用户看的量级，别写精确值（会随版本变）


# ================= 纯函数（离线可测）=================

def npm_env(base_env, prefix) -> dict:
    """干净的 npm 环境：摘掉继承来的 npm_config_*，再给我们要的那几个。"""
    env = {k: v for k, v in (base_env or {}).items()
           if not k.lower().startswith("npm_config_")}
    prefix = Path(prefix)
    env["npm_config_prefix"] = str(prefix)
    env["npm_config_cache"] = str(prefix / ".npm-cache")
    env["npm_config_registry"] = NPM_REGISTRY
    # ★ 这两条是"防用户 .npmrc 把安装搞残"的最后一道闸：
    #   环境变量优先级高于 userconfig，低于命令行。
    env["npm_config_ignore_scripts"] = "false"
    env["npm_config_omit"] = ""
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    env["npm_config_update_notifier"] = "false"
    # ★ 132MB 的原生包是最容易断的一步，而 optional 依赖断了 npm 不报错。
    #   多给它几次机会：默认只重试 2 次、单次 5 分钟。
    env["npm_config_fetch_retries"] = "5"
    env["npm_config_fetch_timeout"] = "300000"
    env["npm_config_fetch_retry_mintimeout"] = "10000"
    env["npm_config_fetch_retry_maxtimeout"] = "120000"
    return env


def npm_argv_prefix(node_info) -> list:
    """怎么调 npm 最稳：`<node> <npm-cli.js>`，找不到再用 npm 自己的脚本。

    ★ npm 的 bin 脚本 shebang 是 `#!/usr/bin/env node`，会去 PATH 里找**第一个**
      node。系统里要是有个 18 的 node 排在前面，npm 就在 18 上跑 —— 所以能指定
      node 就指定。"""
    node = str((node_info or {}).get("node") or "")
    npm = str((node_info or {}).get("npm") or "")
    if node and npm:
        cli_js = Path(npm).parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if cli_js.is_file():
            return [node, str(cli_js)]
    return [npm] if npm else ["npm"]


def install_argv(node_info, prefix, version: str = "",
                 registry: str = NPM_REGISTRY) -> list:
    spec = f"{CLAUDE_PACKAGE}@{version}" if version else CLAUDE_PACKAGE
    return npm_argv_prefix(node_info) + [
        "install", "-g",
        "--prefix", str(Path(prefix)),
        spec,
        "--registry", registry,
        # ★ include=optional 不能省：原生二进制就在 optionalDependencies 里
        "--include=optional",
        "--no-audit", "--no-fund",
    ]


# ================= 原生包（那 130MB）=================
#
# 主包只有 29KB，`bin/claude.exe` 是个 500 字节的占位；真正的可执行文件在
# optionalDependencies 的平台子包里。★ npm 对 optional 依赖的失败**不报错**：
# 断网、超时、校验不过都只在 stderr 里留一句警告，退出码仍然是 0。
# 所以「npm 说装好了」根本不等于装好了。

# 判断本机要哪个平台包。逻辑必须和壳里 install.cjs 的 getPlatformKey() 一致：
# musl 靠 process.report 里有没有 glibcVersionRuntime 判断（容器里未必有 ldd）。
_ARCH_PROBE = (
    "const r=process.report&&process.report.getReport?process.report.getReport():null;"
    "process.stdout.write(process.arch+"
    "(r&&r.header&&r.header.glibcVersionRuntime!==undefined?'':'-musl'))"
)
_ARCH_TAGS = ("x64", "arm64", "x64-musl", "arm64-musl")
_MACHINE_ALIASES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}


def fallback_arch_tag(machine: str = "", musl=None) -> str:
    """问不到 node 时的退路：看内核报的机器名 + 有没有 musl 的 loader。"""
    if not machine:
        import platform as _platform
        machine = _platform.machine()
    tag = _MACHINE_ALIASES.get(machine.lower(), machine.lower())
    if tag not in ("x64", "arm64"):
        return ""
    if musl is None:
        try:
            musl = bool(list(Path("/lib").glob("ld-musl-*.so.1")))
        except OSError:
            musl = False
    return tag + ("-musl" if musl else "")


def platform_arch_tag(ctx, node_info) -> str:
    """x64 / arm64 / x64-musl / arm64-musl；认不出来返回空串。"""
    node = str((node_info or {}).get("node") or "")
    if node and ctx is not None:
        res = ctx.runner.run([node, "-e", _ARCH_PROBE],
                             timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL)
        tag = (res.out or "").strip()
        if res.ok and tag in _ARCH_TAGS:
            return tag
    return fallback_arch_tag()


def native_package_name(arch_tag: str) -> str:
    return f"{CLAUDE_PACKAGE}-linux-{arch_tag}" if arch_tag else ""


def wrapper_dir(prefix) -> Path:
    """壳（那个 29KB 的 npm 包）装在哪儿。"""
    return Path(prefix) / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"


def wrapper_version(prefix) -> str:
    """壳的版本 = 平台包的版本（同一批发布的），补装时按它钉版本。"""
    import json
    try:
        data = json.loads((wrapper_dir(prefix) / "package.json")
                          .read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except Exception:
        return ""


def native_install_argv(node_info, prefix, package: str, version: str = "",
                        registry: str = NPM_REGISTRY) -> list:
    """单独装平台子包。★ 这次是普通依赖，失败会真的报错 —— 这正是要的：
    别再把「132MB 没下下来」伪装成「装好了」。"""
    spec = f"{package}@{version}" if version else package
    return npm_argv_prefix(node_info) + [
        "install", "-g",
        "--prefix", str(Path(prefix)),
        spec,
        "--registry", registry,
        "--no-audit", "--no-fund",
    ]


def claude_candidates(prefix) -> list:
    """装完之后 `claude` 可能出现的几个位置（按可能性排序）。"""
    prefix = Path(prefix)
    pkg = prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    return [
        prefix / "bin" / "claude",
        pkg / "bin" / "claude.exe",      # 这个包的 bin 字段就叫 claude.exe
        pkg / "bin" / "claude",
        pkg / "cli.js",
        pkg / "bin" / "claude.js",
    ]


def looks_like_script(path) -> bool:
    """文件是不是 JS（用 shebang 判断），决定要不要退回 `node <file>` 来跑。"""
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        return head == b"#!"
    except Exception:
        return False


# ================= 主流程 =================

def claude_version(ctx, path) -> str:
    """跑一次 `--version`。能跑起来才算装好。"""
    res = ctx.runner.run([str(path), "--version"], timeout=VERSION_TIMEOUT,
                         stdin=subprocess.DEVNULL)
    text = (res.out or "").strip() or (res.err or "").strip()
    if res.ok and text:
        return text.splitlines()[0].strip()
    # 有些包里 bin 只是个 JS 文件、没有可执行位 —— 退回用 node 跑
    if looks_like_script(path):
        node = str((getattr(ctx, "_node_info", {}) or {}).get("node") or "node")
        res2 = ctx.runner.run([node, str(path), "--version"],
                              timeout=VERSION_TIMEOUT, stdin=subprocess.DEVNULL)
        text2 = (res2.out or "").strip()
        if res2.ok and text2:
            return text2.splitlines()[0].strip()
    return ""


def find_installed_claude(ctx) -> str:
    """在 ~/.local 里找一个能跑的 claude，找不到返回空串。"""
    for cand in claude_candidates(ctx.prefix):
        if not cand.is_file():
            continue
        if claude_version(ctx, cand):
            return str(cand)
    return ""


def claude_on_path(env) -> str:
    import shutil
    return shutil.which("claude", path=(env or {}).get("PATH", "")) or ""


def ensure_claude(ctx, node_info, version: str = "") -> dict:
    """保证能跑 `claude`。返回 {"claude": 路径, "version": 版本, "installed": bool}。

    出错抛 CliError(exit_code=EXIT_NPM)。"""
    ctx._node_info = node_info or {}

    # 1) 已经在 PATH 里且能跑 → 不重复装（用户的机器也许早装过）
    existing = claude_on_path(ctx.env)
    if existing:
        ver = claude_version(ctx, existing)
        if ver:
            ctx.note(f"    claude {ver}（{existing}）")
            return {"claude": existing, "version": ver, "installed": False}

    # 2) 之前用 --prefix ~/.local 装过、只是当前 shell 没读 PATH
    found = find_installed_claude(ctx)
    if found:
        ver = claude_version(ctx, found)
        ctx.note(f"    claude {ver}（{found}，上次装的）")
        _ensure_bin_link(ctx, found)
        _prepend_bin(ctx)
        return {"claude": found, "version": ver, "installed": False}

    argv = install_argv(node_info, ctx.prefix, version=version)

    if ctx.dry_run:
        ctx.log(f"    （--dry-run）会执行：{' '.join(argv)}")
        return {"claude": "", "version": "", "installed": False, "planned": True}

    if node_info and not node_info.get("npm"):
        raise CliError("找不到 npm", EXIT_NPM,
                       hint="Node 是装好了，但里面没有 npm；重新跑一次安装器试试")

    ctx.log(f"    安装 {CLAUDE_PACKAGE}{('@' + version) if version else ''} …")
    ctx.note(f"    要下约 {NATIVE_DOWNLOAD_MB}MB 的原生包，慢网络要几分钟；"
             f"这一步不刷进度是正常的")
    env = npm_env(ctx.env, ctx.prefix)
    old_env = getattr(ctx.runner, "env", None)
    ctx.runner.env = env
    started = time.monotonic()
    try:
        res = ctx.runner.run(argv, timeout=INSTALL_TIMEOUT,
                             stdin=subprocess.DEVNULL)
    finally:
        if old_env is not None:
            ctx.runner.env = old_env
    elapsed = int(time.monotonic() - started)

    if res.timed_out:
        raise CliError(f"npm 安装超时（{INSTALL_TIMEOUT} 秒）", EXIT_NPM,
                       hint="换个网络或者稍后重跑；已下载的部分会留在缓存里")
    tail = res.text(limit=600)
    if not res.ok:
        raise CliError(f"npm 安装失败（退出码 {res.code}）：\n{tail}", EXIT_NPM,
                       hint="检查一下网络能不能连 registry.npmmirror.com")

    found = find_installed_claude(ctx)
    if not found:
        # ★ 走到这里千万别直接说「装好了」：npm 退出码 0 只代表**它自己**没出错，
        #   optional 依赖（也就是那 132MB 的原生包）下载失败它是不会报错的。
        ctx.note(f"    npm 跑完了（{elapsed} 秒），但没有可用的 claude —— 补装原生包")
        found = _repair_native(ctx, node_info, tail)
    if not found:
        raise CliError(
            "npm 说装好了，但找不到能跑的 claude\n" + (tail or "（npm 没有输出）"),
            EXIT_NPM,
            hint=f"看看 {Path(ctx.prefix) / 'lib' / 'node_modules' / CLAUDE_PACKAGE} 在不在；"
                 f"多半是 npm 的 ignore-scripts/omit=optional 或原生包没下完。"
                 f"重跑一次安装器通常就能补上")
    ver = claude_version(ctx, found)
    _ensure_bin_link(ctx, found)
    _prepend_bin(ctx)
    ctx.note(f"    claude {ver}（{found}，{elapsed} 秒）")
    return {"claude": found, "version": ver, "installed": True}


def _repair_native(ctx, node_info, npm_text: str) -> str:
    """壳装上了、但壳里没有可执行文件时，单独把原生包补上。

    三步，任一步失败就抛错并把 npm 的原话带出来：
      1. 认出本机要哪个平台包（x64 / arm64 / -musl）；
      2. `npm install -g` 那个平台包 —— 普通依赖，失败会真的报错；
      3. 跑一遍壳里的 postinstall，它会把 `bin/claude.exe` 那个占位换成真二进制。
    返回能找到的 claude 路径；还是找不到就返回空串（由调用方报错）。"""
    pkg_dir = wrapper_dir(ctx.prefix)
    if not pkg_dir.is_dir():
        return ""

    tag = platform_arch_tag(ctx, node_info)
    package = native_package_name(tag)
    if not package:
        ctx.note("    （认不出这台机器的架构，没法单独补装原生包）")
        return ""

    version = wrapper_version(ctx.prefix)
    spec = f"{package}{('@' + version) if version else ''}"
    ctx.note(f"    补装 {spec} …")
    res = ctx.runner.run(native_install_argv(node_info, ctx.prefix, package,
                                             version=version),
                         timeout=INSTALL_TIMEOUT, stdin=subprocess.DEVNULL)
    if res.timed_out or not res.ok:
        reason = "超时" if res.timed_out else f"退出码 {res.code}"
        raise CliError(
            f"{spec} 装不上（{reason}）：\n" + res.text(limit=600),
            EXIT_NPM,
            hint="这多半是网络问题：原生包一百多兆，断一次就装不完整。"
                 "换个网络或者稍后重跑安装器（已下载的部分会留在缓存里）"
                 + (f"\n第一次 npm 的输出：\n{npm_text}" if npm_text else ""))

    node = str((node_info or {}).get("node") or "")
    install_cjs = pkg_dir / "install.cjs"
    if node and install_cjs.is_file():
        ctx.note("    跑一遍 postinstall，把占位换成真二进制 …")
        ran = ctx.runner.run([node, str(install_cjs)], timeout=POSTINSTALL_TIMEOUT,
                             stdin=subprocess.DEVNULL)
        if not ran.ok:
            ctx.note("    postinstall 没跑通：" + ran.text(limit=300))
    return find_installed_claude(ctx)


def _ensure_bin_link(ctx, path: str) -> None:
    """让 `~/.local/bin/claude` 存在。装完就有的情况下这里什么都不做。"""
    dst = Path(ctx.bin_dir) / "claude"
    src = Path(path)
    try:
        if dst.exists() or dst.is_symlink():
            if dst.resolve() == src.resolve():
                return
        atomic_symlink(src, dst)
    except Exception:
        pass


def _prepend_bin(ctx) -> None:
    from platform_support.common import prepend_paths
    prepend_paths(ctx.env, [ctx.bin_dir])
    if getattr(ctx.runner, "env", None) is not None:
        ctx.runner.env = ctx.env


def claude_env_hint(prefix=None) -> str:
    """给用户看的一句 PATH 提示（install.sh 里也有同样的写法）。"""
    prefix = Path(prefix or (Path.home() / ".local"))
    return f'export PATH="{prefix / "bin"}:$PATH"'


# 供 server_main 判断是否需要提醒重启 shell
def needs_path_reload(ctx) -> bool:
    return str(Path(ctx.bin_dir)) not in (os.environ.get("PATH") or "").split(os.pathsep)
