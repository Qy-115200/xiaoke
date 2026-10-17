# -*- coding: utf-8 -*-
"""Claude Code 安装助手 —— 程序入口。

这里只做四件事：建窗口、起后台线程、打欢迎信息、进事件循环。
具体实现都在各自的模块里：
  config / deps / helpers / images / notify / bus / llm  基础件
  server/   设备身份、HTTP 接口、工具函数与说明书
  ui/       窗口、选项面板、充值弹窗、托盘
  agent_core.py  Agent 主循环
"""

import sys
import threading

import customtkinter as ctk

from agent_core import agent_worker, balance_worker
from bus import poll_queue
from server.tools_fixed import parse_protocol_arg, register_protocol
from server.ui_bridge import set_ui
from ui.gui import GUI
from ui.tray import Tray


def main():
    """程序入口"""
    ctk.set_appearance_mode("dark")      # ★ 深色主题
    ctk.set_default_color_theme("blue")

    # ---------- 解析启动参数 ----------
    proto = parse_protocol_arg()         # aikaui://recharge?user_id=7
    auto_recharge = ("--recharge" in sys.argv) or (proto.get("action") == "recharge")

    # ---------- 注册 aikaui:// 协议（尽力而为，失败不影响启动）----------
    # 支付页面付完款会打开 aikaui://xxx 链接，靠这个协议把程序唤起来；
    # 余额用完时服务器在 402 报错里给的「点这里充值」链接也走它。
    threading.Thread(target=register_protocol, daemon=True).start()

    root = ctk.CTk()                     # 创建主窗口
    gui = GUI(root)                      # 创建 GUI 对象
    set_ui(gui)                          # ★ 把 gui 注入给工具层（server/ui_bridge.py）

    # ---------- 只办充值的启动方式 ----------
    # 用户在终端里点服务器给的 aikaui://recharge 链接 → 这里被唤起。
    # 这种时候只弹充值窗：不建托盘、不起 Agent 线程，关掉弹窗就退出。
    # ★ 别在这里起 tray / agent_worker —— 用户装完就想让助手消失，
    #   不能再留一个常驻进程。
    if auto_recharge:
        gui._recharge_only = True
        gui._quit_on_close = True
        root.withdraw()                  # 主窗口不显示，只给充值弹窗
        root.protocol("WM_DELETE_WINDOW", gui.quit_app)
        root.after(300, lambda: gui.show_recharge_dialog(
            title="充值", body="请选择充值金额，付款后余额会自动到账。", force=True,
        ))
        root.after(100, lambda: poll_queue(gui))   # 充值结果也要靠队列回到主线程
        root.mainloop()
        return

    # ---------- 启动系统托盘 ----------
    tray = Tray(gui)
    gui.tray = tray
    tray.start()

    # ---------- 欢迎信息 ----------
    gui.write("=" * 55)
    gui.write("  Claude Code 助手")
    gui.write("=" * 55)
    gui.write("")
    gui.write("我是你的 AI 管家，随时可以问我任何问题。")
    gui.write("")
    gui.write("• 想装 Claude Code？点下面蓝色按钮，或直接说\"帮我装 Claude Code\"")
    gui.write("• 左边有两个页面：💬 对话（在这儿聊天）、📊 用量信息（余额 / token 用量 / 充值）")
    gui.write("• 关掉窗口不会退出，程序会缩到右下角托盘里")

    # ---------- 启动后台线程 ----------
    threading.Thread(target=agent_worker, daemon=True).start()     # Agent 主循环
    threading.Thread(target=balance_worker, daemon=True).start()   # 余额定时刷新

    root.after(100, lambda: poll_queue(gui))   # 100ms 后启动第一次轮询
    root.mainloop()                      # ★ 进入 GUI 事件循环（阻塞在这里）


if __name__ == "__main__":
    main()
