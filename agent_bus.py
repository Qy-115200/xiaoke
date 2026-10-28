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

__all__ = ["msg_queue", "user_input_queue", "input_event", "input_holder"]
