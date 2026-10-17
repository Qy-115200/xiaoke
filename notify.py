# -*- coding: utf-8 -*-
"""桌面通知（余额不足告警用）。

真正的弹窗实现按平台在 platform_support 里：
  Windows  win10toast → plyer → PowerShell 的 WinRT toast
  macOS    osascript -e 'display notification …'
  Linux    notify-send（没装就静默）

这里只做两件事：把调用丢进后台线程（有的实现会阻塞好几秒），以及吞掉所有异常——
通知弹不出来绝不能影响主程序。
"""

import threading

import platform_support


def _notify(title: str, message: str) -> None:
    """弹桌面通知（余额不足时用）。非阻塞，失败静默。"""
    def _run():
        try:
            platform_support.notify(title, message)
        except Exception:
            pass                    # 静默放弃

    threading.Thread(target=_run, daemon=True).start()
