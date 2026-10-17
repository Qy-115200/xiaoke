# -*- coding: utf-8 -*-
"""macOS 专属实现。

几个和 Windows 不一样的地方：
- aikaui:// 不是注册表，而是打包时写进 Info.plist；链接由 LaunchServices 以
  Apple Event（kAEGetURL）送进来，必须用 pyobjc 装一个处理器才收得到。
- 桌面"快捷方式"用符号链接（或退化成 .command 脚本），没有 .lnk。
- 终端是 Terminal.app，用 osascript 让它执行命令。
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from config import HOME
from platform_support.common import find_claude, safe_cwd

# pyobjc 的处理器对象必须一直有人引用，否则会被回收、事件就没人接了
_URL_HANDLER_KEEP = []


def platform_key() -> str:
    if os.uname().machine.lower() in ("arm64", "aarch64"):
        return "macos-arm64"
    return "macos-x64"


def path_dirs() -> list[Path]:
    return [
        HOME / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        HOME / ".npm-global" / "bin",
    ]


def taskbar_reserve_px() -> int:
    return 0                    # 菜单栏在顶部，底部没有任务栏


def desktop_dir() -> Path:
    return HOME / "Desktop"


def find_claude_path() -> str | None:
    return find_claude([
        HOME / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
        HOME / ".npm-global" / "bin" / "claude",
        HOME / ".claude" / "local" / "claude",
    ])


# ================= aikaui:// 协议 =================

def register_url_protocol() -> str:
    """macOS 的协议声明写在 .app 的 Info.plist 里，运行时不需要注册。"""
    return "macOS 协议由 Info.plist 声明，无需运行时注册"


def install_url_handler(callback) -> bool:
    """装一个 kAEGetURL 处理器，收到 aikaui:// 链接就调用 callback(url)。

    必须在主线程调用（Apple Event 也只在主线程分发）。pyobjc 缺失时返回 False，
    调用方当作"协议暂时不可用"处理，绝不能让程序起不来。"""
    try:
        from Foundation import NSObject, NSAppleEventManager
    except Exception:
        return False

    class _URLHandler(NSObject):
        def handleGetURLEvent_withReplyEvent_(self, event, reply):
            try:
                desc = event.paramDescriptorForKeyword_(b"----")   # keyDirectObject
                url = desc.stringValue() if desc is not None else None
                if url:
                    callback(str(url))
            except Exception:
                pass

    try:
        handler = _URLHandler.alloc().init()
        manager = NSAppleEventManager.sharedAppleEventManager()
        # kInternetEventClass = kAEGetURL = 'GURL'
        manager.setEventHandlerAndSelector_forEventClass_andEventID_(
            handler, b"handleGetURLEvent:withReplyEvent:", b"GURL", b"GURL",
        )
        _URL_HANDLER_KEEP.append(handler)
        return True
    except Exception:
        return False


def _as_applescript_string(s: str) -> str:
    """把普通字符串转成 AppleScript 字面量（双引号、反斜杠要转义）。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ================= 快捷方式 / 启动终端 =================

def create_shortcut() -> str:
    """在桌面放一个 Claude Code 的入口。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，可能没装好"

    desktop = desktop_dir()
    try:
        desktop.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"创建桌面快捷方式失败：{e}"

    target = desktop / "Claude Code"
    try:
        if target.is_symlink() or target.exists():
            target.unlink()
        os.symlink(claude_path, target)
        return f"已创建桌面快捷方式：{target}"
    except Exception:
        pass

    # 符号链接失败（少见）→ 退化成双击可运行的 .command
    try:
        cmd = desktop / "Claude Code.command"
        cmd.write_text(
            "#!/bin/bash\nexec " + shlex.quote(claude_path) + "\n",
            encoding="utf-8",
        )
        cmd.chmod(0o755)
        return f"已创建桌面快捷方式：{cmd}"
    except Exception as e:
        return f"创建桌面快捷方式失败：{e}"


def launch_claude(directory: str = "") -> str:
    """用 Terminal.app 在指定目录里启动 Claude Code。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，无法启动"

    cwd, note = safe_cwd(directory)
    if not shutil.which("osascript"):
        return "启动失败：找不到 osascript"

    # 交给 shell 拼的一整行：先 cd，再 exec（终端一关就退出）
    line = f"cd {shlex.quote(str(cwd))} && exec {shlex.quote(claude_path)}"
    script = (
        f'tell application "Terminal" to do script {_as_applescript_string(line)}\n'
        'tell application "Terminal" to activate\n'
    )
    try:
        subprocess.Popen(["osascript", "-e", script])
        return f"已打开新终端启动 Claude Code（{claude_path}），工作目录：{cwd}{note}"
    except Exception as e:
        return f"启动失败：{e}"


# ================= 通知 =================

def notify(title: str, message: str) -> None:
    try:
        if not shutil.which("osascript"):
            return
        script = (
            f"display notification {_as_applescript_string(message)} "
            f"with title {_as_applescript_string(title)}"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except Exception:
        pass


# ================= 命令执行 =================

def shell_command(cmd: str) -> tuple[list[str], bool]:
    """用登录 shell 执行：GUI 启动的进程拿不到用户 profile 里的 PATH，
    加 -l 才能找到 brew / npm 装的东西。"""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    return [shell, "-lc", cmd], False
