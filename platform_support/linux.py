# -*- coding: utf-8 -*-
"""Linux 专属实现。

- aikaui:// 靠 XDG 的 .desktop + xdg-mime 注册。
- 桌面快捷方式也是一个 .desktop（Terminal=true）。
- 终端模拟器五花八门，按常见顺序探测，找不到就明确告诉用户手动开。
"""

import os
import shlex
import shutil
import subprocess
import sys
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


def platform_key() -> str:
    machine = os.uname().machine.lower()
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    return "linux-x64"


def path_dirs() -> list[Path]:
    return [
        HOME / ".local" / "bin",
        Path("/usr/local/bin"),
        HOME / ".npm-global" / "bin",
    ]


def taskbar_reserve_px() -> int:
    return 48                    # 面板通常在底部；最后还有屏幕边界钳制兜底


def desktop_dir() -> Path:
    """读 XDG 配置里的真实桌面目录（可能是中文的「桌面」）。"""
    cfg = HOME / ".config" / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("XDG_DESKTOP_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(HOME))
                return Path(value)
    except Exception:
        pass
    return HOME / "Desktop"


def find_claude_path() -> str | None:
    return find_claude([
        HOME / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/usr/bin/claude"),
        HOME / ".npm-global" / "bin" / "claude",
    ])


def _exec_quote(s: str) -> str:
    """Desktop Entry 的 Exec 参数转义（不是 shell 语法）。"""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') \
        .replace("$", "\\$").replace("`", "\\`") + '"'


def _app_command() -> str:
    """真正被 .desktop 启动的那条命令。"""
    if getattr(sys, "frozen", False):
        return _exec_quote(sys.executable)
    script = Path(sys.argv[0]).resolve()
    return f"{_exec_quote(sys.executable)} {_exec_quote(str(script))}"


def _write_desktop(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o755)
    except Exception:
        pass


# ================= aikaui:// 协议 =================

def register_url_protocol() -> str:
    """写一个带 MimeType 的 .desktop，并把 aikaui:// 默认处理器指过去。"""
    entry = HOME / ".local" / "share" / "applications" / "claude-code-assistant.desktop"
    try:
        _write_desktop(entry, [
            "[Desktop Entry]",
            "Type=Application",
            "Name=小克",
            "Comment=安装与配置 Claude Code",
            f"Exec={_app_command()} %u",
            "Icon=utilities-terminal",
            "Terminal=false",
            "NoDisplay=true",
            "Categories=Development;Utility;",
            "MimeType=x-scheme-handler/aikaui;",
        ])
    except Exception as e:
        return f"注册协议失败：{e}"

    # 尽力而为：没有 xdg-mime 的极简系统上，文件写好了也能被部分桌面识别
    try:
        if shutil.which("xdg-mime"):
            subprocess.run(
                ["xdg-mime", "default", "claude-code-assistant.desktop",
                 "x-scheme-handler/aikaui"],
                capture_output=True, timeout=10,
            )
        if shutil.which("update-desktop-database"):
            subprocess.run(
                ["update-desktop-database", str(entry.parent)],
                capture_output=True, timeout=10,
            )
    except Exception:
        pass

    return "已注册协议 aikaui://"


# ================= 快捷方式 / 启动终端 =================

def create_shortcut() -> str:
    """在桌面放一个 Claude Code 的启动器。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，可能没装好"

    target = desktop_dir() / "Claude Code.desktop"
    try:
        _write_desktop(target, [
            "[Desktop Entry]",
            "Type=Application",
            "Name=Claude Code",
            "Comment=启动 Claude Code",
            f"Exec={_exec_quote(claude_path)}",
            "Icon=utilities-terminal",
            "Terminal=true",
            "Categories=Development;Utility;",
        ])
        # GNOME 需要 trusted 标记才允许双击运行；没有 gio 就算了
        try:
            if shutil.which("gio"):
                subprocess.run(
                    ["gio", "set", str(target), "metadata::trusted", "true"],
                    capture_output=True, timeout=10,
                )
        except Exception:
            pass
        return f"已创建桌面快捷方式：{target}"
    except Exception as e:
        return f"创建桌面快捷方式失败：{e}"


def _terminals(cwd: Path, inner: str):
    """按顺序产出一组 (argv, 说明)。每个 argv 都能直接交给 Popen。"""
    shell = os.environ.get("SHELL") or "/bin/bash"
    yield (["x-terminal-emulator", "-e", shell, "-lc", inner], "x-terminal-emulator")
    yield (["gnome-terminal", f"--working-directory={cwd}", "--",
            shell, "-lc", inner], "gnome-terminal")
    yield (["konsole", "--workdir", str(cwd), "-e", shell, "-lc", inner], "konsole")
    yield (["xfce4-terminal", f"--working-directory={cwd}", "-x",
            shell, "-lc", inner], "xfce4-terminal")
    yield (["kitty", "--directory", str(cwd), shell, "-lc", inner], "kitty")
    yield (["alacritty", "--working-directory", str(cwd), "-e",
            shell, "-lc", inner], "alacritty")
    yield (["xterm", "-e", shell, "-lc", inner], "xterm")


def launch_claude(directory: str = "") -> str:
    """找一个可用的终端模拟器，在指定目录里启动 Claude Code。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，无法启动"

    cwd, note = safe_cwd(directory)
    inner = f"cd {shlex.quote(str(cwd))} && exec {shlex.quote(claude_path)}"

    candidates = []
    # 用户自己指定了终端就先用它（$TERMINAL 可能带参数）
    term = os.environ.get("TERMINAL")
    if term:
        try:
            shell = os.environ.get("SHELL") or "/bin/bash"
            candidates.append((shlex.split(term) + ["-e", shell, "-lc", inner], term))
        except Exception:
            pass
    candidates.extend(_terminals(cwd, inner))

    for argv, name in candidates:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.Popen(argv)
            return f"已打开新终端启动 Claude Code（{claude_path}），工作目录：{cwd}{note}"
        except Exception:
            continue

    return "启动失败：没找到可用的终端模拟器，请手动打开终端并输入 claude"


# ================= VSCode =================
# Linux 上装 VSCode 走 apt/snap 基本都要 sudo，助手不替用户提权：
# install_vscode 只负责把下载页打开，剩下让用户自己装。

def _vscode_cli_candidates() -> list[str]:
    candidates = [
        "/snap/bin/code",
        "/usr/share/code/bin/code",
        "/usr/share/code/code",
        "/opt/visual-studio-code/bin/code",
        "/opt/visual-studio-code/code",
        # flatpak 的导出入口，两个常见前缀
        str(HOME / ".local" / "share" / "flatpak" / "exports" / "bin" / "com.visualstudio.code"),
        "/var/lib/flatpak/exports/bin/com.visualstudio.code",
    ]
    found = shutil.which("code")
    if found:
        candidates.insert(0, found)
    return candidates


def _flatpak_vscode_argv() -> list[str]:
    """装了 flatpak 版但没导出命令的情况：问一下 flatpak 自己。"""
    if not shutil.which("flatpak"):
        return []
    try:
        out, _err, timed_out, rc = run_capture(
            ["flatpak", "list", "--app", "--columns=application"],
            timeout=10, env=clean_env(), stdin=subprocess.DEVNULL)
        if timed_out or rc != 0:
            return []
        apps = out.decode("utf-8", "replace").split()
        if "com.visualstudio.code" in apps:
            return ["flatpak", "run", "com.visualstudio.code"]
    except Exception:
        pass
    return []


def find_vscode() -> dict | None:
    for cli in _vscode_cli_candidates():
        if Path(cli).is_file():
            return {
                "exe": cli,
                "cli_argv": [cli],
                "launch_argv": [cli],
                "source": "cli",
            }
    flatpak_argv = _flatpak_vscode_argv()
    if flatpak_argv:
        return {
            "exe": "flatpak:com.visualstudio.code",
            "cli_argv": flatpak_argv,
            "launch_argv": flatpak_argv,
            "source": "flatpak",
        }
    return None


def install_vscode(on_progress=None, on_note=None) -> str:
    """Linux 上不代劳安装（要 sudo），只把下载页打开。

    on_progress / on_note 是 Windows 那边下 winget、下安装包用的，
    这里没有可报的进度，忽略。"""
    del on_progress, on_note
    found = find_vscode()
    if found:
        return f"已经装过 VSCode，不用重复安装（{found['exe']}）"
    return download_page_fallback("Linux 上装 VSCode 需要管理员权限，助手不替你提权")


def open_vscode(directory: str = "", new_window: bool = False) -> str:
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
    opener = shutil.which("xdg-open")
    if not opener:
        return "没有 xdg-open，打不开 vscode:// 链接"
    try:
        subprocess.Popen(
            [opener, uri],
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
        if not shutil.which("notify-send"):
            return
        subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
    except Exception:
        pass


# ================= 命令执行 =================

def shell_command(cmd: str) -> tuple[list[str], bool]:
    shell = os.environ.get("SHELL") or "/bin/bash"
    return [shell, "-lc", cmd], False
