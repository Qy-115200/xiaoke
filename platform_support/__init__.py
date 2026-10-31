# -*- coding: utf-8 -*-
"""平台适配层。

调用方只 import 这个包，不直接 import windows/macos/linux 子模块——
具体实现按 sys.platform 延迟加载，这样 Windows 的 winreg 不会在 macOS 上被导入。

约定：每个平台的实现都必须提供下面这些函数，返回的文字尽量保持一致
（agent_core 靠 launch_claude() 以「已打开新终端」开头、open_vscode() 以「已打开 VSCode」
开头，来判断"安装流程真的把用户领进门了"）。

"""

import sys


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


_IMPL = None


def _impl():
    global _IMPL
    if _IMPL is None:
        if is_windows():
            from platform_support import windows as m
        elif is_macos():
            from platform_support import macos as m
        else:
            from platform_support import linux as m
        _IMPL = m
    return _IMPL


def platform_key() -> str:
    """更新清单里的平台键：windows-x64 / macos-arm64 / macos-x64 / linux-x64 …"""
    return _impl().platform_key()


def register_url_protocol() -> str:
    return _impl().register_url_protocol()


def install_url_handler(callback) -> bool:
    """macOS 专用：接收运行中的 aikaui:// 链接。其它平台返回 False。

    必须在主线程调用。"""
    m = _impl()
    fn = getattr(m, "install_url_handler", None)
    if fn is None:
        return False
    return bool(fn(callback))


def create_shortcut() -> str:
    return _impl().create_shortcut()


def launch_claude(directory: str = "") -> str:
    return _impl().launch_claude(directory)


def find_claude() -> str | None:
    m = _impl()
    fn = getattr(m, "find_claude_path", None)
    return fn() if fn is not None else None


def desktop_dir():
    return _impl().desktop_dir()


def path_dirs():
    return _impl().path_dirs()


def shell_command(cmd: str):
    return _impl().shell_command(cmd)


def taskbar_reserve_px() -> int:
    return int(_impl().taskbar_reserve_px())


def notify(title: str, message: str) -> None:
    _impl().notify(title, message)


# ================= VSCode =================

def find_vscode() -> dict | None:
    """找 VSCode，返回 {"exe", "cli_argv", "launch_argv", "source"} 或 None。

    只读、不起进程。后端没实现就返回 None（当"没装"处理，别让工具崩）。"""
    m = _impl()
    fn = getattr(m, "find_vscode", None)
    return fn() if fn is not None else None


# ================= winget（Windows 的包管理器） =================

def find_winget() -> str | None:
    """找 winget，找不到返回 None。其它平台没有这个东西，一律 None。"""
    m = _impl()
    fn = getattr(m, "find_winget", None)
    return fn() if fn is not None else None


def ensure_winget(on_progress=None, on_note=None) -> dict:
    """确保有 winget：没有就下载微软官方的 App Installer 装上（Windows 专用）。

    返回 {"ok", "exe", "mode", "note"}；mode = existing / appx / portable。
    其它平台直接返回 ok=False —— 调用方要判平台，别把它当成通用能力。"""
    m = _impl()
    fn = getattr(m, "ensure_winget", None)
    if fn is None:
        return {"ok": False, "exe": "", "mode": "",
                "note": "这个平台没有 winget"}
    return fn(on_progress, on_note)


def install_vscode(on_progress=None, on_note=None) -> str:
    m = _impl()
    fn = getattr(m, "install_vscode", None)
    if fn is None:
        from platform_support.common import download_page_fallback
        return download_page_fallback("这个平台没有自动安装 VSCode 的实现")
    return fn(on_progress, on_note)


def open_vscode(directory: str = "", new_window: bool = False,
                trust: bool = False) -> str:
    """打开 VSCode 并带上工作目录。

    new_window=True 用于"刚装完插件"：已开的窗口是装插件之前加载的，里面没有它。
    trust=True 带上 `--disable-workspace-trust`：我们替用户新开的目录一定是
    "未信任"的，不关掉这道门禁，扩展在受限模式里连终端都开不了（2026-10-31）。"""
    m = _impl()
    fn = getattr(m, "open_vscode", None)
    if fn is None:
        return "打开 VSCode 失败：这个平台没有打开 VSCode 的实现"
    return fn(directory, new_window, trust=trust)


def open_uri(uri: str) -> str:
    """把 vscode://xxx 这类自定义协议链接交给系统处理。成功返回空串，失败返回原因。

    vscode://anthropic.claude-code/open 是 Claude 插件自己注册的入口，
    打开 VSCode 之后靠它把 Claude 面板亮出来。"""
    m = _impl()
    fn = getattr(m, "open_uri", None)
    if fn is None:
        return "这个平台没有打开链接的实现"
    return fn(uri)
