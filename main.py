# -*- coding: utf-8 -*-
"""小克 —— 程序入口。

这里只做四件事：建窗口、起后台线程、打欢迎信息、进事件循环。
具体实现都在各自的模块里：
  config / deps / helpers / images / notify / bus / llm  基础件
  platform_support/  三个平台的差异（协议、快捷方式、终端、通知）
  server/   设备身份、HTTP 接口、工具函数与说明书
  ui/       窗口、选项面板、充值弹窗
  agent_core.py  Agent 主循环

★ 三个平台都不做托盘：关窗口 = 退出程序。
"""

import sys
import threading

import customtkinter as ctk

import platform_support
import updater
from agent_core import agent_worker, balance_worker, refresh_balance
from config import APP_NAME
from bus import msg_queue, poll_queue
from server import relay_gate
from server.tools_fixed import parse_protocol_arg, parse_protocol_url, register_protocol
from server.ui_bridge import set_ui
from ui.gui import GUI


def _repair_config_worker():
    """开机对一次账：两份 Claude Code 配置跟 identity.json 不一致就静默补齐。

    ★ 只在**真的改了东西**时说一句，而且只进对话区（不弹窗、不打扰）：
      没账号、没有 diff、配置指向别人家的中转 —— 一个字都不说。
    ★ 任何异常都吞掉：自愈是顺手的事，不能把启动拖垮。"""
    try:
        from server.claude_config import check_and_repair
        result = check_and_repair()
        if result.repaired:
            msg_queue.put(("log", f"[系统] {result.summary}"))
    except Exception:
        pass


def main():
    """程序入口"""
    # ---------- 自动更新：把上次下载好的新版本换上去 ----------
    # 必须在创建 Tk 窗口**之前**、而且必须在最前面：替换成功后 _relaunch 会
    # 用原参数重新拉起自己并 sys.exit(0)；失败就继续用旧版本跑，绝不阻塞启动。
    try:
        updater.apply_pending_update(sys.argv)
        updater.cleanup_stale_files()
    except Exception:
        pass

    ctk.set_appearance_mode("dark")      # ★ 深色主题
    ctk.set_default_color_theme("blue")

    # ---------- 解析启动参数 ----------
    proto = parse_protocol_arg()         # aikaui://recharge?user_id=7
    auto_recharge = ("--recharge" in sys.argv) or (proto.get("action") == "recharge")

    # ---------- 注册 aikaui:// 协议（尽力而为，失败不影响启动）----------
    # 支付页面付完款会打开 aikaui://xxx 链接，靠这个协议把程序唤起来；
    # 余额用完时服务器在 402 报错里给的「点这里充值」链接也走它。
    # macOS 不用运行时注册（协议写在 Info.plist），但需要装一个 Apple Event 处理器，
    # 这样程序已经开着时点链接也能收得到。
    threading.Thread(target=register_protocol, daemon=True).start()

    root = ctk.CTk()                     # 创建主窗口
    gui = GUI(root)                      # 创建 GUI 对象
    set_ui(gui)                          # ★ 把 gui 注入给工具层（server/ui_bridge.py）

    # ★ 队列轮询从这里就开始，而不是等 mainloop 之前那一次：下面「别人的中转」
    #   那个模态窗是 wait_window（嵌套事件循环），它自己也得收后台线程的回话。
    #   只在这里排一次 —— 两处都排会变成两条轮询链，越跑越多。
    root.after(100, lambda: poll_queue(gui))

    # ---------- macOS：运行中收到 aikaui:// 链接 ----------
    # 回调一定在主线程（Apple Event 只在主线程分发），所以可以直接操作界面。
    def _on_url(url):
        proto = parse_protocol_url(url)
        if proto.get("action") == "recharge":
            gui.show_view("usage")
            gui.show_recharge_dialog(
                title="充值", body="请选择充值金额，付款后余额会自动到账。", force=True,
            )

    platform_support.install_url_handler(_on_url)   # 其它平台返回 False，什么都不做

    # 上一次启动完成了自动更新的话，在这里说一声（只提示一次）
    try:
        notice = updater.consume_applied_notice()
        if notice:
            gui.write(f"[系统] {notice}")
    except Exception:
        pass

    # ---------- 只办充值的启动方式 ----------
    # 用户在终端里点服务器给的 aikaui://recharge 链接 → 这里被唤起。
    # 不起 Agent 线程，关掉窗口就退出。
    #
    # 展示顺序是刻意的：**先用量页、再充值窗**。
    # 余额不足的人第一反应是"钱花在哪了"，所以主窗口直接切到用量页，
    # 后台拉一次余额 + 用量；数据一回来（最多等 3 秒）再自动弹充值窗。
    # 充值窗会停在主窗右边，不把刚看到的图表整个盖住。
    # ★ 别在这里起 agent_worker —— 用户装完就想让助手消失，
    #   不能再留一个常驻进程。
    if auto_recharge:
        gui._recharge_only = True
        gui.show_view("usage")
        threading.Thread(target=refresh_balance, daemon=True).start()

        def _open_recharge_when_ready(attempt=0):
            """等第一次余额/用量回来再弹充值窗；网络差也不卡住流程。"""
            ready = bool(gui._usage_payload.get("balance"))
            if ready or attempt >= 30:      # 3 秒兜底
                gui.show_recharge_dialog(
                    title="充值", body="请选择充值金额，付款后余额会自动到账。", force=True,
                )
            else:
                root.after(100, lambda: _open_recharge_when_ready(attempt + 1))

        root.after(300, _open_recharge_when_ready)   # 先让窗口画出来
        root.mainloop()
        return

    # ---------- 别人的中转：拒绝继续 ----------
    # 这个客户端只配合 aikaui 中转工作。终端要是还指着别处，我们既收不到那台
    # 机器的用量，也不该继续发我们的对话额度 —— 所以拦在这里，直到换成我们的。
    # ★ 只办充值的启动方式上面已经 return 了：那条路只付钱、不碰终端配置。
    # ★ 读盘出错就当判断不了，不拦（不能因为一个坏文件让人开不了程序）。
    try:
        from server.identity import load_identity
        gate = relay_gate.check(load_identity())
    except Exception:
        gate = None
    if gate is not None and gate.blocked:
        switched, message = gui.ask_relay_conflict(gate, relay_gate.switch_to_ours)
        if not switched:
            gui.write("[系统] 已退出：这台电脑上的 Claude Code 还指向别处，"
                      "本客户端只配合 aikaui 中转使用。")
            gui.write("       想继续用，重新打开后选「换成 aikaui 中转」，"
                      "原来的配置会先备份成 *.aikaui-bak。")
            root.destroy()
            return
        gui.write(f"[系统] {message}")

    # ---------- 欢迎信息 ----------
    gui.write("=" * 55)
    gui.write(f"  {APP_NAME} · Claude Code 一键配置")
    gui.write("=" * 55)
    gui.write("")
    gui.write(f"我是{APP_NAME}，随时可以问我任何问题。")
    gui.write("")
    gui.write("• 想装 Claude Code？点下面蓝色按钮，或直接说\"帮我装 Claude Code\"")
    gui.write("• 左边有三个页面：💬 对话（在这儿聊天）、📊 用量信息（余额 / token 用量 / 充值）、"
              "🔐 账户信息（账户码 / 账户密钥，记得截图存好）")
    gui.write("• 关掉窗口就是退出程序")

    # ---------- 启动后台线程 ----------
    threading.Thread(target=agent_worker, daemon=True).start()     # Agent 主循环
    threading.Thread(target=balance_worker, daemon=True).start()   # 余额定时刷新

    # ---------- 配置自愈（静默，见 server/claude_config.check_and_repair）----------
    # 重装 Claude Code / 上次写盘中断 / 服务端换了 token，都会让配置文件跟我们
    # 记的账号对不上。这条线程只负责"对账 + 补齐"，改了什么会在对话区留一行。
    # ★ 只办充值的启动方式不跑（那条路上面已经 return）。
    threading.Thread(target=_repair_config_worker, daemon=True).start()

    # ---------- 后台检查更新（静默下载，下次启动生效）----------
    # 只办充值的启动方式不查——那种场景用户只想赶紧付钱，别抢网络。
    updater.start_background_check(
        on_ready=lambda text: msg_queue.put(("log", f"[系统] {text}")),
    )

    root.mainloop()                      # ★ 进入 GUI 事件循环（阻塞在这里）


if __name__ == "__main__":
    main()
