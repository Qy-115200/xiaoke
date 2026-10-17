# -*- coding: utf-8 -*-
"""系统托盘。

pystray 的 run() 是阻塞的，所以放在独立线程。

线程约束：菜单回调运行在托盘线程，**绝对不能**直接碰 tkinter 组件，
统一用 gui.root.after(0, fn) 把动作丢回主线程执行（见 _call）。
"""

import threading

import pystray

from deps import HAVE_TRAY
from images import _icon_image


# ================= 系统托盘 =================

class Tray:
    """托盘图标。pystray 的 run() 是阻塞的，所以放在独立线程。

    线程约束：菜单回调运行在托盘线程，绝对不能直接碰 tkinter 组件，
    统一用 gui.root.after(0, fn) 把动作丢回主线程执行。"""

    def __init__(self, gui):
        self.gui = gui
        self.icon = None
        self.ok = False                # 托盘是否真的起来了
        self.alert = False             # 当前是不是"告警红"状态

    def start(self):
        if not HAVE_TRAY:
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            menu = pystray.Menu(
                # default=True → 双击托盘图标触发这一项
                pystray.MenuItem("打开主窗口", self._on_show, default=True),
                pystray.MenuItem("用量信息", self._on_usage),
                pystray.MenuItem("退出", self._on_quit),
            )
            self.icon = pystray.Icon(
                "claude_code_assistant", _icon_image(False), "Claude Code 助手", menu,
            )
            self.ok = True
            self.icon.run()             # ★ 阻塞在这里，直到 icon.stop()
        except Exception:
            self.ok = False             # 托盘起不来就当没有，主程序照常跑

    def _call(self, fn):
        """把动作调度回 tkinter 主线程"""
        try:
            self.gui.root.after(0, fn)
        except Exception:
            pass

    def _on_show(self, *args):
        self._call(self.gui.show_window)

    def _on_usage(self, *args):
        """打开主窗口并切到「用量信息」页 —— 充值入口就在那一页里，不单独放托盘菜单项"""
        self._call(lambda: (self.gui.show_window(), self.gui.show_view("usage")))

    def _on_quit(self, *args):
        try:
            self.icon.stop()            # 先让 run() 退出（stop 可以从别的线程调）
        except Exception:
            pass
        self._call(self.gui.quit_app)

    def set_alert(self, on):
        """余额偏低 → 图标变红"""
        if self.icon is None or on == self.alert:
            return
        self.alert = on
        try:
            self.icon.icon = _icon_image(on)
        except Exception:
            pass

    def stop(self):
        """停掉托盘（main 退出时调）"""
        self.ok = False
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
