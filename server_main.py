#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器版入口：一条命令，把 Claude Code 装好、配上账号。

    curl -fsSL https://aikaui.com/install.sh | sh

它和桌面版 `main.py` 是**两个独立入口**：

  main.py       有界面（customtkinter），给 Windows / macOS 和装了桌面的 Linux 用
  server_main.py 只有终端，给没有图形界面的服务器用 —— 不跑 Agent、不问模型

★ 这个文件**不许 import tkinter / customtkinter / ui / agent_core / bus / llm**。
  打包用 `server.spec`，那边把这些整包 exclude 掉；这里只要有一条 import 链把
  tkinter 拖回来，在没有 X11 的机器上就会直接死掉。

做的事就四件：装 Node → 装 claude → 领/绑账号 → 写配置。
每一步都"能重跑"：已经有 Node 就不下载，已经有 claude 就不安装，
已经有身份就不重新注册（重新注册 = 开新号 = 用户余额丢了）。
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import APP_VERSION, CLAUDE_CONFIG, CLAUDE_SETTINGS
from server import claude_setup, console_ui, identity, node_setup
from server import ui_bridge
from server.cli import (
    EXIT_ACCOUNT, EXIT_ARGS, EXIT_CONFIG, EXIT_ENV, EXIT_FAILED, EXIT_NODE,
    EXIT_NPM, EXIT_OK, CliContext, CliError,
)

PROG = "ClaudeCodeAssistant"
LOG_FILE = Path.home() / ".claude-agent" / "cli.log"
SITE_URL = "https://aikaui.com"

TOTAL_STEPS = 5


# ================= 小工具 =================

def _is_tty(stream) -> bool:
    try:
        return bool(stream and stream.isatty())
    except Exception:
        return False


def _log_line(text: str) -> None:
    """往 ~/.claude-agent/cli.log 追加一行。写不进去就算了，不能因此失败。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except Exception:
        pass


def _step(n: int, text: str, total: int = TOTAL_STEPS) -> None:
    try:
        print(f"[{n}/{total}] {text}", flush=True)
    except Exception:
        pass


def _say(text: str = "") -> None:
    """打印一行。★ 吞掉编码错误：Windows 的 GBK 控制台连 ✅ 都会抛，
    不能因为装饰字符把整条流程判成崩溃。"""
    try:
        print(text, flush=True)
    except Exception:
        pass


def _say_err(text: str = "") -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _ask(question: str, default: bool = True) -> bool:
    """只有真终端才问。非终端直接回到 default。"""
    if not _is_tty(sys.stdin):
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes", "是", "好", "1")


# ================= 参数 =================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="在服务器上装好 Claude Code（不需要图形界面、不需要 root）。",
        epilog="装完之后直接输入 claude 开始用。",
    )
    p.add_argument("--dir", default="",
                   help="Claude Code 的工作目录（默认 $HOME）")
    p.add_argument("--account-code", default="",
                   help="已有账号的账户码：把本机绑到这个账号，而不是开新号")
    p.add_argument("--account-key", default="",
                   help="账户密码（2026-10-24 之后注册的账号需要）")
    p.add_argument("--node-version", default="",
                   help="指定 Node 版本（默认最新 LTS）")
    p.add_argument("--claude-version", default="",
                   help="指定 @anthropic-ai/claude-code 的版本（默认 latest）")
    p.add_argument("--new-account", action="store_true",
                   help="即使本机保险箱里还留着旧账号，也开一个新账号")
    p.add_argument("--repair", action="store_true",
                   help="只检查并修复 Claude Code 的配置（不装 Node、不装 claude、不注册）")
    p.add_argument("--yes", "-y", action="store_true",
                   help="不问任何问题，全按默认走")
    p.add_argument("--force", action="store_true",
                   help="忽略小问题（比如已经装好了也重装一次）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印要做什么，不下载、不写盘")
    p.add_argument("--allow-root", action="store_true",
                   help="允许以 root 身份运行（不推荐）")
    p.add_argument("--quiet", action="store_true", help="只打错误和结果")
    p.add_argument("--version", action="version",
                   version=f"{PROG} {APP_VERSION}")
    return p


# ================= 五步 =================

def _preflight(ctx, args) -> dict:
    """① 环境自检：Linux / 架构 / 可写 / 不是 root。"""
    if not sys.platform.startswith("linux"):
        raise CliError(
            f"这个安装器是给 Linux 服务器用的（当前系统：{sys.platform}）",
            EXIT_ENV,
            hint="Windows / macOS 请下载桌面版：https://aikaui.com")

    arch = node_setup.detect_node_arch()
    if not arch:
        raise CliError("只支持 x86_64 / aarch64 架构", EXIT_ENV)

    if hasattr(os, "geteuid") and os.geteuid() == 0 and not args.allow_root:
        raise CliError(
            "请用你自己的账号运行，不要用 root",
            EXIT_ENV,
            hint="用 root 装的东西会落在 /root 下，以后普通用户用不了；"
                 "确实要这样请加 --allow-root")

    if not ctx.dry_run:
        try:
            ctx.prefix.mkdir(parents=True, exist_ok=True)
            probe = ctx.prefix / f".write-test-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as e:
            raise CliError(f"{ctx.prefix} 不可写：{e}", EXIT_ENV,
                           hint="检查一下主目录的权限和磁盘空间")

    ctx.note(f"    {sys.platform} / {arch}，主目录 {ctx.home}")
    return {"arch": arch}


def _claude_step(ctx, args, node_info) -> dict:
    """③ claude 命令。"""
    if args.force and not ctx.dry_run:
        # 重装一次：把旧包删掉，免得 npm 认为"已经是最新"
        pkg = Path(ctx.prefix) / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
        if pkg.exists():
            shutil.rmtree(pkg, ignore_errors=True)
            ctx.note("    （--force）删掉旧的 claude-code 包，重新装")
    return claude_setup.ensure_claude(ctx, node_info,
                                      version=args.claude_version or "")


def _account_step(ctx, args) -> dict:
    """④ 账号：本地有就用，给了账户码就绑，都没有才注册新号。"""
    local = identity.load_identity()
    if local and local.get("api_key"):
        ctx.note(f"    本机已有账号：{local.get('account_code') or '（缺账户码）'}")
        return {"account_code": local.get("account_code") or "", "reused": True}

    if args.account_code:
        if ctx.dry_run:
            ctx.log("    （--dry-run）会用你给的账户码绑定本机")
            return {"account_code": "", "reused": False, "planned": True}
        ctx.log("    正在绑定你给的账号 …")
        data, err, _code, _wait = identity.bind_account(
            args.account_code, args.account_key)
        if not data:
            raise CliError(f"绑定账号失败：{err}", EXIT_ACCOUNT,
                           hint="账户码和账户密钥要跟截图上一字不差；"
                                "老账号（2026-10-24 之前）可能不需要密钥")
        identity.adopt_identity(data)
        return {"account_code": data.get("account_code") or "", "reused": False}

    # ★ 保险箱里还留着旧账号：绝不能默默开新号（那等于把余额扔了）
    hint = identity.pending_recovery() or {}
    if hint.get("account_code") and not args.new_account:
        code = hint.get("account_code")
        raise CliError(
            f"这台机器以前用过账号 {code}",
            EXIT_ACCOUNT,
            hint=f"想继续用它：加 --account-code {code} --account-key <账户密钥>\n"
                 f"确实要开一个全新账号（旧账号的余额不会跟过来）：加 --new-account")

    if ctx.dry_run:
        ctx.log("    （--dry-run）会向服务器领一个新账号")
        return {"account_code": "", "reused": False, "planned": True}

    if not args.yes and not _ask("这台机器还没有账号。现在领一个新账号吗？"):
        raise CliError("没有账号，安装停在这里", EXIT_ACCOUNT,
                       hint="想用已有账号：加 --account-code <账户码> --account-key <密钥>")

    ctx.log("    正在向服务器申请账号 …")
    result = identity.register_device()
    if not str(result).startswith("{"):
        raise CliError(f"领账号失败：{result}", EXIT_ACCOUNT,
                       hint="过一会儿再试；同一网络 24 小时内注册太多次会被限流")
    import json
    try:
        info = json.loads(result)
    except Exception:
        info = {}
    return {"account_code": info.get("account_code") or "", "reused": False}


def _config_step(ctx, args) -> dict:
    """⑤ 写 Claude Code 的配置文件。"""
    from server import tools_fixed

    if ctx.dry_run:
        ctx.log(f"    （--dry-run）会写 {CLAUDE_CONFIG} 和 {CLAUDE_SETTINGS}")
        return {"ok": True, "planned": True}

    notes = []
    try:
        notes.append(tools_fixed.bypass_region_check())
    except Exception as e:
        raise CliError(f"写 ~/.claude.json 失败：{e}", EXIT_CONFIG,
                       hint="检查主目录的权限")

    text = tools_fixed.set_model_config()
    if str(text).startswith("错误"):
        raise CliError(str(text), EXIT_CONFIG,
                       hint="身份文件可能坏了，删掉后加 --new-account 重跑")
    return {"ok": True, "notes": notes}


# ================= 只对账配置 =================

def _repair_only(args) -> int:
    """`--repair`：拿 identity.json 对一次账，只碰 Claude Code 的两份配置。

    ★ 不做平台/架构/root 预检、不碰 Node 和 claude：这条路径要能在 cron 里、
      在别的机器上都跑得起来，只要求"本机有账号 + 配置文件可写"。
    ★ 坏文件不覆盖（那是用户别处的配置）；配置指向别人家的中转时也不动，
      除非显式加 --force。"""
    from server import claude_config

    _step(1, "检查 Claude Code 配置", total=1)
    if args.dry_run:
        _say("  （--dry-run）只对账，不备份、不写盘")

    ident = identity.load_identity()
    if not ident:
        raise CliError(
            "这台机器还没有账号，没有可以对账的凭证",
            EXIT_ACCOUNT,
            hint="先装一遍：curl -fsSL https://aikaui.com/install.sh | sh\n"
                 "或者用 --account-code <账户码> 绑一个已有账号")

    result = claude_config.check_and_repair(
        identity=ident, force=args.force, dry_run=args.dry_run,
        create_missing=True)

    for line in result.lines():
        _say(line)

    if result.problem == "bad-identity":
        raise CliError("身份文件不完整（缺 api_base），没法判断该写什么",
                       EXIT_CONFIG,
                       hint="重新跑一遍安装器，或加 --new-account 领个新账号")
    if result.problem.startswith("error:"):
        raise CliError(f"对账时出错：{result.problem[6:].strip()}", EXIT_CONFIG)
    if not result.ok or result.hard_problems:
        # 坏文件/软链/写不进去：不能报"修好了"。部分写成功也算失败。
        raise CliError("配置没修好（见上面逐条结果）", EXIT_CONFIG,
                       hint="上一条写着「跳过」的文件需要你自己看一眼；"
                            "改坏了想强制按账号重写，加 --force")

    if result.changes:
        verb = "需要补齐（--dry-run 没写）" if result.planned else "已补齐"
        _say(f"\n✅ {verb}：{result.summary}")
    elif result.skipped:
        _say("\n✅ 没有要补的键；有文件按上面的原因跳过了，没动。")
    else:
        _say("\n✅ 配置和账号一致，什么都没动。")
    return EXIT_OK


# ================= 无 TTY 保护 =================

def _terminal_argv(inner: str):
    """照 platform_support/linux.py 那张表重开一个终端来跑向导。"""
    shell = os.environ.get("SHELL") or "/bin/bash"
    yield ["x-terminal-emulator", "-e", shell, "-lc", inner]
    yield ["gnome-terminal", "--", shell, "-lc", inner]
    yield ["konsole", "-e", shell, "-lc", inner]
    yield ["xfce4-terminal", "-x", shell, "-lc", inner]
    yield ["kitty", shell, "-lc", inner]
    yield ["alacritty", "-e", shell, "-lc", inner]
    yield ["xterm", "-e", shell, "-lc", inner]


def _relaunch_in_terminal(argv) -> bool:
    """有图形会话时，把向导丢进一个终端窗口里跑（那边才有 tty）。"""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    cmd = " ".join(shlex_quote(a) for a in argv)
    inner = f"{cmd}; echo; read -r -p '按回车关闭这个窗口…' _"
    for cand in _terminal_argv(inner):
        if not shutil.which(cand[0]):
            continue
        try:
            subprocess.Popen(cand, start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def shlex_quote(text: str) -> str:
    import shlex
    return shlex.quote(str(text))


def _no_tty_guard(argv, args) -> int:
    """双击 / 被老客户端自动更新顶掉时走这里：不阻塞、不假装成功。"""
    _log_line("no tty: " + " ".join(argv))
    if _relaunch_in_terminal([sys.executable, *argv]):
        _log_line("relaunched in terminal")
        return EXIT_OK

    msg = ("ClaudeCodeAssistant 需要在终端里运行。\n"
           "  在你自己的电脑上执行：\n"
           "    curl -fsSL https://aikaui.com/install.sh | sh\n"
           f"  日志：{LOG_FILE}")
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass
    # 有桌面通知就弹一下，没有就算了
    try:
        import platform_support
        platform_support.notify("Claude Code 安装", "请在终端里运行安装命令")
    except Exception:
        pass
    return EXIT_ENV


# ================= 主流程 =================

def _run(args) -> int:
    quiet = bool(args.quiet)

    ui = console_ui.make_console_ui()
    ui.verbose = not quiet
    ui_bridge.set_ui(ui)

    ctx = CliContext(home=Path.home(), env=os.environ.copy(),
                     ui=ui, dry_run=args.dry_run, quiet=quiet)

    if args.dry_run:
        ctx.log("（--dry-run）只打印计划，不下载、不写盘。\n")

    _step(1, "检查环境")
    env_info = _preflight(ctx, args)

    _step(2, "准备 Node.js（≥ 22）")
    node_info = node_setup.ensure_node(ctx, arch=env_info["arch"],
                                       pinned=(args.node_version or None))

    _step(3, "安装 claude 命令")
    claude_info = _claude_step(ctx, args, node_info)

    _step(4, "配置账号")
    account = _account_step(ctx, args)

    _step(5, "写入 Claude Code 配置")
    _config_step(ctx, args)

    _report(ctx, args, node_info, claude_info, account)
    return EXIT_OK


def _report(ctx, args, node_info, claude_info, account) -> None:
    if args.dry_run:
        ctx.log("\n（--dry-run 结束：什么都没改）")
        return

    workdir = Path(args.dir).expanduser() if args.dir else Path.home()
    lines = ["", "✅ 装好了。", ""]
    if account.get("account_code"):
        lines.append(f"  账户码    {account['account_code']}")
        lines.append(f"            （充值、换机器找回都用它，记一下）")
    if node_info.get("version"):
        where = "（新装到 ~/.local/node）" if node_info.get("installed") else ""
        lines.append(f"  Node      {node_info['version']}{where}")
    if claude_info.get("version"):
        lines.append(f"  claude    {claude_info['version']}")
    lines.append(f"  工作目录  {workdir}")
    lines.append("")
    lines.append("  开始用：")
    lines.append(f"    cd {workdir} && claude")
    if claude_setup.needs_path_reload(ctx):
        lines.append("")
        lines.append("  ★ 新开的终端才会认得 claude。当前这个窗口请先执行：")
        lines.append(f"    {claude_setup.claude_env_hint(ctx.prefix)}")
    lines.append("")
    lines.append(f"  充值 / 查余额：{SITE_URL}")
    try:
        print("\n".join(lines), flush=True)
    except Exception:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:                 # --help / --version / 参数错
        return int(e.code or 0)

    interactive = _is_tty(sys.stdin) and _is_tty(sys.stdout)
    if args.repair and (args.account_code or args.new_account):
        _say_err("✖ --repair 只对账配置，不能和 --account-code / --new-account 一起用")
        return EXIT_ARGS
    # --repair 本身就是非交互的：没终端也照跑，不走"重开终端"那套
    if not interactive and not args.yes and not args.dry_run and not args.repair:
        return _no_tty_guard(argv, args)

    try:
        if args.repair:
            return _repair_only(args)
        return _run(args)
    except CliError as e:
        _say_err(f"\n✖ {e.message}")
        if e.hint:
            _say_err("  " + str(e.hint).replace("\n", "\n  "))
        _say_err(f"  日志：{LOG_FILE}")
        _log_line(f"failed({e.exit_code}): {e.message}")
        return e.exit_code
    except KeyboardInterrupt:
        _say_err("\n已取消。")
        return 130
    except Exception as e:                  # 兜底：绝不吐一整屏 traceback 给用户
        import traceback
        _log_line("crash: " + traceback.format_exc())
        _say_err(f"\n✖ 意外的错误：{e}")
        _say_err(f"  详细堆栈在 {LOG_FILE}")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
