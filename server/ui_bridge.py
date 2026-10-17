# -*- coding: utf-8 -*-
"""GUI 注入桥。

服务端这一侧（identity / api / tools_*）从来不 import GUI，也不创建 GUI。
启动时由 main.py 调 set_ui(gui) 把一个"能操作界面的对象"塞进来，
工具函数再用 get_ui() 取出来显示日志、提问、显示进度条。

★ 为什么必须用 get_ui() 而不是直接 `from server.ui_bridge import _ui`：
  从别的模块 import _ui，会在 **import 那一刻** 把当时的值（通常是 None）
  拷进那个模块的命名空间，之后 set_ui() 改的是这里的 _ui，那边永远还是 None。
  所以取的时候一定要走 get_ui()，在函数体里调用。
"""

_ui = None


def set_ui(ui):
    """main.py 启动时调用，把 GUI 对象交给工具层"""
    global _ui            # 声明要修改模块级变量 _ui
    _ui = ui


def get_ui():
    """取当前注入的 GUI 对象，没注入过就返回 None。

    ★ 调用方一律要判空，例如：
        u = get_ui()
        if u:
            u.ui_log("...")
    """
    return _ui
