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
from platform_support.common import (
    clean_env,
    download_page_fallback,
    find_claude,
    launch_argv_for,
    run_capture,
    safe_cwd,
)

# pyobjc 的处理器对象必须一直有人引用，否则会被回收、事件就没人接了
_URL_HANDLER_KEEP = []

VSCODE_INSTALL_TIMEOUT = 900
VSCODE_APP_NAME = "Visual Studio Code.app"


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


# ================= VSCode =================
# macOS 上没有注册表，靠的是 .app 包在不在。注意「Install 'code' command in PATH」
# 那一步是可选的、很多人不做，所以 PATH 只能当补充，不能当主路。

def _vscode_app_candidates() -> list[Path]:
    return [
        Path("/Applications") / VSCODE_APP_NAME,
        HOME / "Applications" / VSCODE_APP_NAME,
    ]


def _vscode_info(app: Path) -> dict:
    """把 .app 包补全成"怎么开窗 + 怎么跑 CLI"。"""
    cli = app / "Contents" / "Resources" / "app" / "bin" / "code"
    return {
        "exe": str(app),
        # CLI 脚本偶尔没有执行位（手工解压、剪贴板拷贝），退化成 sh 调用
        "cli_argv": ([str(cli)] if os.access(cli, os.X_OK)
                     else ["/bin/sh", str(cli)]) if cli.is_file() else [],
        "launch_argv": ["open", "-a", str(app)],
        "source": "app-bundle",
    }


def find_vscode() -> dict | None:
    for app in _vscode_app_candidates():
        if app.is_dir():
            return _vscode_info(app)

    # Spotlight：应用被挪到别的目录时还能找到。慢（几百毫秒），所以放第二位。
    if shutil.which("mdfind"):
        try:
            out, _err, timed_out, rc = run_capture(
                ["mdfind", "kMDItemCFBundleIdentifier == com.microsoft.VSCode"],
                timeout=10, env=clean_env(), stdin=subprocess.DEVNULL)
            if not timed_out and rc == 0:
                for line in out.decode("utf-8", "replace").splitlines():
                    line = line.strip()
                    if line.endswith(".app") and Path(line).is_dir():
                        return _vscode_info(Path(line))
        except Exception:
            pass

    cli = shutil.which("code")
    if cli:
        # /Applications/Visual Studio Code.app/Contents/Resources/app/bin/code
        resolved = Path(cli).resolve()
        for parent in resolved.parents:
            if parent.name == VSCODE_APP_NAME:
                return _vscode_info(parent)
    return None


def _find_brew() -> str:
    """GUI 启动的进程拿不到登录 shell 的 PATH，所以先按已知位置找 brew。"""
    for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(path).is_file():
            return path
    return shutil.which("brew") or ""


def install_vscode() -> str:
    """有 brew 就用 brew 装，没有/失败就打开下载页。"""
    found = find_vscode()
    if found:
        return f"已经装过 VSCode，不用重复安装（{found['exe']}）"

    brew = _find_brew()
    if not brew:
        return download_page_fallback("这台机器上没有 Homebrew")

    env = clean_env()
    # NONINTERACTIVE + DEVNULL：万一 brew 要 sudo，让它立刻失败，而不是挂在那里等回车
    env["NONINTERACTIVE"] = "1"
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    try:
        out, err, timed_out, rc = run_capture(
            [brew, "install", "--cask", "visual-studio-code"],
            timeout=VSCODE_INSTALL_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except Exception as e:
        return download_page_fallback(f"brew 跑不起来：{e}")

    if timed_out:
        return download_page_fallback(f"brew 超过 {VSCODE_INSTALL_TIMEOUT} 秒没装完")
    if rc != 0:
        detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
        return download_page_fallback(
            f"brew 返回 {rc}" + (f"：{detail[-1][:120]}" if detail else ""))

    found = find_vscode()
    if found:
        return f"已安装 VSCode（{found['exe']}）"
    return download_page_fallback("brew 说装成功了，但 /Applications 里没找到 VSCode")


def open_vscode(directory: str = "", new_window: bool = False) -> str:
    """用 VSCode 打开目录。new_window=True 时走 CLI 的 -n（`open -n` 是"再开一个
    app 实例"，会多出一个 Dock 图标，所以不用它）。"""
    info = find_vscode()
    if not info:
        return "找不到 VSCode，无法打开"
    cwd, note = safe_cwd(directory)
    try:
        subprocess.Popen(
            launch_argv_for(info, new_window, cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=clean_env(),               # 挡掉从 VSCode 终端继承来的 ELECTRON_RUN_AS_NODE
            cwd=str(HOME),
            start_new_session=True,        # 助手退出后 VSCode 继续活着
        )
    except Exception as e:
        return f"打开 VSCode 失败：{e}"
    return f"已打开 VSCode（{info['exe']}），工作目录：{cwd}{note}"


def open_uri(uri: str) -> str:
    """把 vscode://xxx 交给系统的协议处理程序。成功返回空串。"""
    try:
        subprocess.Popen(
            ["open", uri],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=clean_env(), start_new_session=True,
        )
    except Exception as e:
        return f"打不开链接 {uri}：{e}"
    return ""


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
