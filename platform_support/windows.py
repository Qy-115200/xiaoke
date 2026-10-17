# -*- coding: utf-8 -*-
"""Windows 专属实现。

原来的代码直接写在 server/tools_fixed.py 里，并且顶层 import winreg——那会让
macOS/Linux 在 import 阶段就崩掉。现在 winreg / win32com 全部收进函数内部，
模块本身在任何平台上都能安全 import。
"""

import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from config import HOME
from platform_support.common import find_claude, safe_cwd


def platform_key() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "windows-arm64"
    return "windows-x64"


def path_dirs() -> list[Path]:
    """刚装完 Node 时需要手动补进 PATH 的目录。"""
    return [
        Path(r"C:\Program Files\nodejs"),
        HOME / "AppData" / "Roaming" / "npm",
    ]


def taskbar_reserve_px() -> int:
    """底部任务栏大概占多高（充值窗定位用）。"""
    return 48


def desktop_dir() -> Path:
    """真实桌面路径。可能是 OneDrive 重定向过的，所以查注册表而不是拼 ~/Desktop。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        return Path(winreg.QueryValueEx(key, "Desktop")[0])
    except Exception:
        return HOME / "Desktop"


def find_claude_path() -> str | None:
    return find_claude([
        HOME / "AppData" / "Roaming" / "npm" / "claude.cmd",                # npm 装的
        HOME / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",  # 官方原生装的
        Path("C:/Program Files/claude/claude.exe"),                          # 系统级装的
    ])


# ================= aikaui:// 协议 =================
# 支付页面付完款后，会打开 aikaui://recharge?user_id=7 这样的链接，
# Windows 靠注册表里的这个协议，把这个链接转成"启动我们的 exe 并带上参数"。

def register_url_protocol() -> str:
    """注册 aikaui:// 自定义协议（Windows）。已注册则覆盖成当前 exe 路径。"""
    import winreg

    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}" "%1"'
    else:
        # 开发环境（python main.py）：带上解释器和脚本路径，方便本地调试
        script = Path(sys.argv[0]).resolve()
        # python -c / -m 之类没有真实脚本，写进注册表只会在点链接时炸掉
        if not script.is_file():
            return "跳过协议注册：开发环境没有可执行的脚本路径"
        command = f'"{Path(sys.executable).resolve()}" "{script}" "%1"'

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:AI 管家")   # 描述
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")  # ★ 有这行才被认成协议
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui\shell\open\command"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except Exception as e:
        return f"注册协议失败：{e}"

    return "已注册协议 aikaui://"


# ================= 快捷方式 / 启动终端 =================

def create_shortcut() -> str:
    """在桌面创建 Claude Code 快捷方式。"""
    try:
        import win32com.client                  # Windows COM 接口（创建 .lnk 用）
    except ImportError:
        return "缺少 pywin32，无法创建快捷方式"

    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，可能没装好"

    shell = win32com.client.Dispatch("WScript.Shell")   # 创建 COM 对象
    lnk_path = desktop_dir() / "Claude Code.lnk"
    lnk = shell.CreateShortCut(str(lnk_path))

    if claude_path.endswith((".cmd", ".bat")):          # .cmd/.bat 要通过 cmd.exe 运行
        lnk.TargetPath = "cmd.exe"
        lnk.Arguments = f'/k "{claude_path}"'            # /k = 执行完后保留窗口
    else:
        lnk.TargetPath = claude_path

    lnk.WorkingDirectory = str(HOME)
    lnk.Description = "启动 Claude Code"
    lnk.Save()
    return f"已创建桌面快捷方式：{lnk_path}"


def launch_claude(directory: str = "") -> str:
    """打开新终端窗口，在指定目录里启动 Claude Code。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，无法启动"

    cwd, note = safe_cwd(directory)

    try:
        # start "Claude Code" cmd /k "xxx"：
        #   start = 打开新窗口；cmd /k = 执行完命令后保留窗口
        subprocess.Popen(
            f'start "Claude Code" cmd /k "{claude_path}"',
            shell=True,
            cwd=str(cwd),
        )
        return f"已打开新终端启动 Claude Code（{claude_path}），工作目录：{cwd}{note}"
    except Exception as e:
        return f"启动失败：{e}"


# ================= 通知 =================

def notify(title: str, message: str) -> None:
    """桌面通知：win10toast → plyer → PowerShell 的 WinRT toast，全失败就静默。"""
    # ① win10toast（requirements 里的首选）
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, message, duration=8, threaded=False)
        return
    except Exception:
        pass

    # ② plyer（跨平台，装了就用）
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=8)
        return
    except Exception:
        pass

    # ③ PowerShell 的 WinRT toast（Windows 10/11 自带，啥都不用装）
    try:
        app_id = str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) \
            else "Claude Code 助手"

        def _esc(s):
            """XML 转义，避免标题/正文里的特殊字符把模板弄坏"""
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))

        template = (
            '<toast><visual><binding template="ToastText02">'
            f'<text id="1">{_esc(title)}</text>'
            f'<text id="2">{_esc(message)}</text>'
            "</binding></visual></toast>"
        )
        ps = (
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
            'ContentType=WindowsRuntime] | Out-Null\n'
            '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
            'ContentType=WindowsRuntime] | Out-Null\n'
            f'$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n'
            f'$xml.LoadXml(@\'\n{template}\n\'@)\n'
            f'$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n'
            f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
            f'"{app_id}").Show($toast)\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as f:
            f.write(ps)
            script = f.name
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


# ================= 命令执行 =================

def shell_command(cmd: str) -> tuple[str, bool]:
    """返回 (真正交给 subprocess 的命令, 是否 shell=True)。

    chcp 65001 强制 UTF-8 输出（对程序输出有效），>nul 吞掉它自己的提示。"""
    return f"chcp 65001 >nul && {cmd}", True
