# -*- coding: utf-8 -*-
"""线程间通信。

三个线程（GUI 主线程 / Agent 线程 / 余额刷新线程）之间靠队列传话，不直接互相调用：

  msg_queue        工作线程 → GUI 主线程：日志、余额、忙碌状态、弹窗、进度条
  user_input_queue GUI 主线程 → Agent 线程：用户输入（None 表示退出）
  input_event      给 ask_user 用的"闹钟"：Agent 线程发问后在这里等
  input_holder     放 ask_user 的回答（配合 input_event 用）

poll_queue() 在 GUI 主线程里每 100ms 跑一次，把 msg_queue 里的消息翻译成界面操作。
队列里的消息是 (kind, data) 二元组。
"""

import queue
import threading
import tkinter as tk

# ================= 通信队列 =================
# 多线程环境下的"信箱"：一个线程往里面放东西，另一个线程取

msg_queue = queue.Queue()           # Agent / 托盘 / 刷新线程 → GUI 主线程
user_input_queue = queue.Queue()    # GUI 主线程 → Agent 线程：传用户输入
input_event = threading.Event()     # 用于 ask_user：GUI 唤醒 Agent 的"闹钟"
input_holder = {"value": None}      # 存 ask_user 的回答


def poll_queue(gui):
    """GUI 主线程定时轮询消息队列（每 100ms 一次）"""
    try:
        while True:                      # 一直取，直到队列空
            kind, data = msg_queue.get_nowait()
            if kind == "log":
                gui.write(data)
            elif kind == "balance":
                gui.update_balance(data)
            elif kind == "busy":
                if data:                 # 忙碌
                    gui.start_btn.configure(state=tk.DISABLED, text="处理中...")
                else:                    # 空闲
                    gui.start_btn.configure(state=tk.NORMAL, text="一键安装 Claude Code")
            elif kind == "recharge":
                # 服务器说余额不足（代理层把上游的额度 403 归一成 402）
                # → **不**直接弹充值窗，只在底部问一句
                # "是否立即充值"，用户点了才弹（见 ui/window.py 的 offer_recharge）
                gui.offer_recharge(reason=data.get("reason") or "余额不足", force=True)
            elif kind == "recharge_result":
                gui.show_recharge_result(data)
            elif kind == "recharge_methods":
                # 服务器给了当前开了哪些支付渠道 → 充值窗照着画按钮
                gui.show_recharge_methods(data)
            elif kind == "recharge_qr":
                # 用户点了「重新加载二维码」，图重新下回来了
                gui.show_qr_image(data)
            elif kind == "recharge_paid":
                # 用户点了「我已完成付款」→ 后端查完余额回来了
                gui.show_paid_result(data)
            elif kind == "login_result":
                # 用户点了「换设备登录」→ 绑定结果回来了
                gui.show_login_result(data)
            elif kind == "ask":
                # ask_user 带了选项 → 渲染成可点 / 可键盘选的列表
                gui.show_options(data.get("options") or [])
            elif kind == "progress_start":
                gui.progress_label.configure(text=data)
                gui.progress_frame.pack(fill=tk.X, padx=12, pady=(0, 6),
                                        before=gui.entry_frame)   # 插到输入行之前
                gui.progress_bar.start()
            elif kind == "progress_stop":
                gui.progress_bar.stop()
                gui.progress_frame.pack_forget()
    except queue.Empty:
        pass                             # 队列空，正常退出

    gui.root.after(100, lambda: poll_queue(gui))   # ★ 100ms 后再次调用自己
