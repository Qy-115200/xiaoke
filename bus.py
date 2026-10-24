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

msg_queue = queue.Queue()           # Agent / 余额刷新线程 → GUI 主线程
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
            elif kind == "install_done":
                # 安装流程完整走完（… → launch_claude 成功）→ 给用户几秒看完
                # 收尾说明，然后助手自己退出，别一直占着托盘。
                gui.schedule_auto_quit()
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
            elif kind == "recovery":
                # 本地凭证没了、但 server/vault.py 里还留着上次的凭证 →
                # 打开登录弹窗并预填好，**由用户点一下确认**才恢复。
                # （共享电脑上不能替人做主：前一个人删了凭证，后一个人一开机
                #   就被"恢复"进别人的账号，等于把余额送给陌生人。）
                # ★ 登录要「账户码 + 账户密钥」两样，保险箱里两样都存了，
                #   所以一起预填；老版本只存了码时 account_key 为空，
                #   用户手填或者（老账号）留空。
                gui.show_login_dialog(
                    prefill=data.get("account_code") or "",
                    prefill_key=data.get("account_key") or "",
                    notice=("检测到这台电脑的本地凭证不见了，但还留着上次的账户码和密钥。\n"
                            "确认是您自己的账号就点「登录」，账号和余额会一起回来；\n"
                            "不是您的（比如这是共用电脑）就关掉这个窗口。"),
                )
            elif kind == "recovered":
                # 注册时服务器按硬件指纹认出了旧账号，把余额还回来了
                gui.show_recovered_notice()
            elif kind == "ask":
                # ask_user 带了选项 → 渲染成可点 / 可键盘选的列表
                gui.show_options(data.get("options") or [])
            elif kind == "progress_start":
                gui.progress_label.configure(text=data)
                gui.progress_bar.configure(mode="indeterminate")
                gui.progress_cancel.pack_forget()      # 跑马灯不需要取消
                gui.progress_frame.pack(fill=tk.X, padx=12, pady=(0, 6),
                                        before=gui.entry_frame)   # 插到输入行之前
                gui.progress_bar.start()
            elif kind == "pow_progress":
                # 注册的人机校验：除了文字还要一个**确定**的进度条 + 「取消」按钮
                text, pct = data
                gui.progress_label.configure(text=text)
                gui.progress_bar.stop()
                gui.progress_bar.configure(mode="determinate")
                gui.progress_bar.set(pct if pct is not None else 0)
                if not gui.progress_cancel.winfo_ismapped():
                    gui.progress_cancel.configure(text="取消", state=tk.NORMAL)
                    gui.progress_cancel.pack(side=tk.RIGHT, padx=(8, 0))
                gui.progress_frame.pack(fill=tk.X, padx=12, pady=(0, 6),
                                        before=gui.entry_frame)
            elif kind == "progress_stop":
                gui.progress_bar.stop()
                gui.progress_cancel.pack_forget()
                gui.progress_frame.pack_forget()
    except queue.Empty:
        pass                             # 队列空，正常退出

    gui.root.after(100, lambda: poll_queue(gui))   # ★ 100ms 后再次调用自己
