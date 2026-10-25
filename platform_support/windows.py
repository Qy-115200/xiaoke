# -*- coding: utf-8 -*-
"""Windows 专属实现。

原来的代码直接写在 server/tools_fixed.py 里，并且顶层 import winreg——那会让
macOS/Linux 在 import 阶段就崩掉。现在 winreg / win32com 全部收进函数内部，
模块本身在任何平台上都能安全 import。
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from config import HOME
from platform_support.common import (
    clean_env,
    download_page_fallback,
    exe_from_command,
    find_claude,
    launch_argv_for,
    run_capture,
    safe_cwd,
)

# 装 VSCode 可能要一两分钟（下载 + 安装 + 杀软扫一遍）。这个超时只给 install_vscode
# 自己的子进程用，跟 bash 工具的 600 秒上限无关。
VSCODE_INSTALL_TIMEOUT = 900


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


# ================= VSCode =================
# 找 VSCode 不能只看 PATH。真机上最常见的两种情况都靠不住 PATH：
#   1) 用户装的时候没勾「添加到 PATH」——安装目录里明明有 bin\code.cmd，PATH 里却没有；
#   2) 装到了非默认盘（本机就是 D:\vs code\Microsoft VS Code），不在任何标准目录里。
# 所以顺序是「注册表 → 标准目录 → PATH」，注册表排最前面，它一定是最准的。

def _read_reg_value(hive_name: str, subkey: str, value_name: str = "") -> str:
    """读一个注册表字符串值，读不到就返回空串（任何异常都不许冒出去）。"""
    try:
        import winreg
        hive = (winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM"
                else winreg.HKEY_CURRENT_USER)
        flags = winreg.KEY_READ
        if hive_name == "HKLM":
            # 32 位冻结包默认会被重定向到 WOW6432Node，显式要 64 位视图
            flags |= getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(hive, subkey, 0, flags) as key:
            return str(winreg.QueryValueEx(key, value_name)[0] or "")
    except Exception:
        return ""


def _vscode_registry_candidates() -> list[str]:
    """注册表里能给出 Code.exe 路径的几处，按可靠程度排。

    App Paths 最好：值本身就是干净的完整路径，不用解析。
    后面两条是"命令行"，形如 `"...\\Code.exe" "%1"`，得用 exe_from_command 抠。"""
    subkeys = [
        r"Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe",
        r"Software\Classes\Applications\Code.exe\shell\open\command",
        r"Software\Classes\vscode\shell\open\command",
    ]
    out = []
    for hive in ("HKCU", "HKLM"):
        for sub in subkeys:
            value = _read_reg_value(hive, sub)
            if value:
                out.append(value)
    return out


def _vscode_standard_paths() -> list[Path]:
    """几个常见安装位置（用户级安装器默认就落在 LOCALAPPDATA 那条）。"""
    roots = [HOME / "AppData" / "Local" / "Programs"]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    return [root / "Microsoft VS Code" / "Code.exe" for root in roots]


def _vscode_uninstall_candidates() -> list[str]:
    """从卸载表里找。★ 只有兜底价值：本机两个卸载表都匹配不到 "Visual Studio Code"，
    所以它排在最后，不能当主路。"""
    found = []
    try:
        import winreg
    except Exception:
        return found
    sub = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    # (读值时用的 hive 名, 真正的 hive 句柄, 视图标志)
    tables = [
        ("HKCU", winreg.HKEY_CURRENT_USER, 0),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0)),
    ]
    for hive_name, hive, view in tables:
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as key:
                count = winreg.QueryInfoKey(key)[0]
                names = []
                for i in range(count):
                    try:
                        names.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
        except Exception:
            continue
        for name in names:
            base = sub + "\\" + name
            display = _read_reg_value(hive_name, base, "DisplayName")
            if "visual studio code" not in " ".join(display.lower().split()):
                continue
            for value_name in ("DisplayIcon", "InstallLocation", "Inno Setup: App Path"):
                value = _read_reg_value(hive_name, base, value_name)
                if not value:
                    continue
                path = Path(value.rstrip(",0").strip().strip('"'))
                if path.suffix.lower() != ".exe":
                    path = path / "Code.exe"
                found.append(str(path))
    return found


def _resolve_vscode_exe(raw: str) -> Path | None:
    """把一条候选（可能是路径、命令行、也可能是 code.cmd）变成 Code.exe 的路径。

    返回 None 表示这条候选不可用 —— 注册表键在卸载后经常残留，所以要真验文件存在。"""
    candidate = exe_from_command(raw)
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.name.lower() != "code.exe":
        # 拿到的是 bin\code.cmd（或某个 code 包装脚本）→ 反推安装根目录
        path = path.parent.parent / "Code.exe"
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _vscode_info(exe: Path, source: str) -> dict:
    """把 Code.exe 路径补全成"怎么开窗 + 怎么跑 CLI"。

    ★ CLI 必须是 bin\\code.cmd，不能用 Code.exe：本机实测 `Code.exe --list-extensions`
      输出是空的，而 code.cmd 能正常列出扩展（Code.exe 是把参数转交给已运行实例的
      GUI 进程，不适合当命令行用）。"""
    cli = exe.parent / "bin" / "code.cmd"
    return {
        "exe": str(exe),
        "cli_argv": [str(cli)] if cli.is_file() else [],
        "launch_argv": [str(exe)],
        "source": source,
    }


def find_vscode() -> dict | None:
    """找 VSCode。只读、不起进程、不看 PATH 也基本能找到。

    返回 {"exe", "cli_argv", "launch_argv", "source"}，找不到返回 None。"""
    # (原始值, 来源标签)
    candidates: list[tuple[str, str]] = []
    candidates += [(v, "registry") for v in _vscode_registry_candidates()]
    candidates += [(str(p), "standard") for p in _vscode_standard_paths()]
    for name in ("code", "code.cmd", "Code.exe"):
        found = shutil.which(name)
        if found:
            candidates.append((found, "PATH"))
    candidates += [(v, "uninstall") for v in _vscode_uninstall_candidates()]

    seen = set()
    for raw, source in candidates:
        exe = _resolve_vscode_exe(raw)
        if exe is None:
            continue
        key = os.path.normcase(str(exe))
        if key in seen:          # 同一个 exe 被多条路径认出来，只用第一条（也就是最可靠的那条）
            continue
        seen.add(key)
        return _vscode_info(exe, source)
    return None


def install_vscode() -> str:
    """用 winget 装 VSCode（用户级安装，不提权）。装不上就打开下载页。"""
    found = find_vscode()
    if found:
        return f"已经装过 VSCode，不用重复安装（{found['exe']}）"

    winget = (shutil.which("winget") or shutil.which("winget.exe")
              or str(HOME / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe"))
    if not Path(winget).is_file():
        return download_page_fallback("这台机器上没有 winget")

    argv = [
        winget, "install", "-e", "--id", "Microsoft.VisualStudioCode",
        "--scope", "user",                     # 用户级：不弹 UAC
        "--accept-package-agreements", "--accept-source-agreements",
    ]
    try:
        out, err, timed_out, rc = run_capture(
            argv, timeout=VSCODE_INSTALL_TIMEOUT, env=clean_env(),
            stdin=subprocess.DEVNULL)
    except Exception as e:
        return download_page_fallback(f"winget 跑不起来：{e}")

    if timed_out:
        return download_page_fallback(f"winget 超过 {VSCODE_INSTALL_TIMEOUT} 秒没装完")
    if rc != 0:
        # 只取最后一行有用信息，别把整段 winget 日志倒给模型
        detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
        return download_page_fallback(
            f"winget 返回 {rc}" + (f"：{detail[-1][:120]}" if detail else ""))

    # 装是装完了，但 PATH 还是旧的（winget 写的是用户环境变量，本进程读不到），
    # 所以再走一遍注册表/标准目录。杀软和安装器收尾会拖一点，重试几次。
    for _ in range(3):
        found = find_vscode()
        if found:
            return f"已安装 VSCode（{found['exe']}）"
        time.sleep(2)
    return download_page_fallback("winget 说装成功了，但找不到 VSCode 的安装位置")


def open_vscode(directory: str = "", new_window: bool = False) -> str:
    """用 VSCode 打开指定目录（目录规则跟 launch_claude 一样：只在用户主目录里）。

    new_window=True 时强制开新窗口 —— 刚装完插件必须这样：已开的窗口是在装插件
    之前加载的，里面没有这个插件。"""
    info = find_vscode()
    if not info:
        return "找不到 VSCode，无法打开"
    cwd, note = safe_cwd(directory)
    try:
        subprocess.Popen(
            launch_argv_for(info, new_window, cwd),
            stdin=subprocess.DEVNULL,          # 别让子进程抢走终端的标准输入
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # ★ clean_env 不能省：带着 ELECTRON_RUN_AS_NODE 会让 Code.exe 变成
            #   一个 node 解释器，跑完就退，既不弹窗也不报错。
            env=clean_env(),
            cwd=str(HOME),
            creationflags=subprocess.DETACHED_PROCESS,   # 助手退出后 VSCode 继续活着
        )
    except Exception as e:
        return f"打开 VSCode 失败：{e}"
    return f"已打开 VSCode（{info['exe']}），工作目录：{cwd}{note}"


def open_uri(uri: str) -> str:
    """把 vscode://xxx 这样的链接交给系统（VS Code 自己注册的协议）。

    成功返回空串，失败返回一句要转告用户的话。用 os.startfile 而不是拼
    `cmd /c start`：链接里带 `&` 之类的话，交给命令行会被拆开。"""
    try:
        os.startfile(uri)                    # noqa: S606 - ShellExecute，走系统协议处理
    except Exception as e:
        return f"打不开链接 {uri}：{e}"
    return ""


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
