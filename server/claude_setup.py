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
"""

import os
import subprocess
from pathlib import Path

from server.cli import EXIT_NPM, CliError, atomic_symlink

CLAUDE_PACKAGE = "@anthropic-ai/claude-code"
NPM_REGISTRY = "https://registry.npmmirror.com"

INSTALL_TIMEOUT = 900          # 原生二进制几十兆，慢网络下要几分钟
VERSION_TIMEOUT = 60


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
    env = npm_env(ctx.env, ctx.prefix)
    old_env = getattr(ctx.runner, "env", None)
    ctx.runner.env = env
    try:
        res = ctx.runner.run(argv, timeout=INSTALL_TIMEOUT,
                             stdin=subprocess.DEVNULL)
    finally:
        if old_env is not None:
            ctx.runner.env = old_env

    if res.timed_out:
        raise CliError(f"npm 安装超时（{INSTALL_TIMEOUT} 秒）", EXIT_NPM,
                       hint="换个网络或者稍后重跑；已下载的部分会留在缓存里")
    if not res.ok:
        tail = res.text(limit=600)
        raise CliError(f"npm 安装失败（退出码 {res.code}）：\n{tail}", EXIT_NPM,
                       hint="检查一下网络能不能连 registry.npmmirror.com")

    found = find_installed_claude(ctx)
    if not found:
        raise CliError(
            "npm 说装好了，但找不到能跑的 claude",
            EXIT_NPM,
            hint=f"看看 {Path(ctx.prefix) / 'lib' / 'node_modules' / CLAUDE_PACKAGE} 在不在；"
                 f"多半是 npm 的 ignore-scripts/omit=optional 把原生二进制省了")
    ver = claude_version(ctx, found)
    _ensure_bin_link(ctx, found)
    _prepend_bin(ctx)
    ctx.note(f"    claude {ver}（{found}）")
    return {"claude": found, "version": ver, "installed": True}


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
