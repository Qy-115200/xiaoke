# -*- coding: utf-8 -*-
"""线程之间传话用的几个容器。**不带任何界面依赖。**

这个模块存在的唯一原因：`bus.py` 为了 `poll_queue()` 要 `import tkinter`，
而 `agent_core` 只用到这里的四个对象。服务器版要复用 `agent_core`
（`server.spec` 把 tkinter 整包排掉了），所以把"数据"和"画界面"拆开：

  agent_bus.py  队列和事件，纯标准库（queue / threading）
  bus.py        重新导出这四个对象 + poll_queue()（桌面版主线程的搬运工）

★ 两边必须是**同一批对象**：`bus.py` 里是 `from agent_bus import …`，
  不是自己再造一份。否则 Agent 往一个队列里放，界面从另一个队列里取，
  表现为"什么都不显示"，而且不报错。
"""

import queue
import threading

# Agent / 余额刷新线程 → 主线程：日志、忙碌状态、弹窗、进度
msg_queue = queue.Queue()
# 主线程 → Agent 线程：用户输入（None 表示退出）
user_input_queue = queue.Queue()
# ask_user 的"闹钟"：Agent 线程发问后在这里等
input_event = threading.Event()
# 放 ask_user 的回答（配合 input_event 用）
input_holder = {"value": None}

# 「中断」：用户点了界面上的按钮（桌面版）或连按两次 Ctrl-C（服务器版）。
# ★ 用它而不是直接杀线程：Python 没有安全的"杀线程"；这里只是立一块牌子，
#   长活儿（下载 / 装包）在每个块之间看一眼，Agent 循环在每步开头看一眼。
#   牌子立起来之后要 clear()，否则下一次任务一开工就"已中断"。
cancel_event = threading.Event()


def request_cancel() -> None:
    """立牌子：请求中断当前这一轮。"""
    cancel_event.set()


def clear_cancel() -> None:
    """开工前把牌子摘掉（每个新任务开始时调）。"""
    cancel_event.clear()


def is_cancelled() -> bool:
    return cancel_event.is_set()


__all__ = ["msg_queue", "user_input_queue", "input_event", "input_holder",
           "cancel_event", "request_cancel", "clear_cancel", "is_cancelled"]
