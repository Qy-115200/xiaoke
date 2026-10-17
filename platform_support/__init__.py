# -*- coding: utf-8 -*-
"""平台适配层。

调用方只 import 这个包，不直接 import windows/macos/linux 子模块——
具体实现按 sys.platform 延迟加载，这样 Windows 的 winreg 不会在 macOS 上被导入。

约定：每个平台的实现都必须提供下面这些函数，返回的文字尽量保持一致
（agent_core 靠 launch_claude() 返回值以「已打开新终端」开头判断安装完成）。
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
