# -*- coding: utf-8 -*-
"""WindowMixin：主窗口本身。

左边一条导航栏（对话 / 用量信息），右边是内容区。**所有按钮都在左边**：

  💬 对话      聊天记录 + 输入框 + 「一键安装 Claude Code」
  📊 用量信息  余额、各模型 token 用量、充值入口（见 ui/usage.py）

主窗口**不再显示余额、也不放充值按钮** —— 钱相关的信息全在"用量信息"页里，
只有余额真的见底时，底部会冒出一条"是否立即充值"的询问条（offer_recharge），
用户点了才会弹充值窗。

对外（Agent / 托盘）暴露的方法还是那几个：
ui_log / ui_ask / ui_progress_start / ui_progress_stop / update_balance。

线程约束：所有组件操作必须在主线程，其它线程通过 msg_queue 发消息，
poll_queue 每 100ms 取一次并在这里更新界面。
"""

import re
import tkinter as tk

import customtkinter as ctk

from bus import input_event, input_holder, msg_queue, user_input_queue
from config import (
    BG,
    DIM,
    OPTION_HOVER,
    OPTION_SEL,
    OPTION_SEL_HOVER,
    OPTION_SEL_TEXT,
    PANEL,
    RAIL_W,
    SURFACE,
    TEXT,
    WARN_BG,
)
from deps import HAVE_TRAY
from helpers import _fmt_yuan, _to_float
from images import ensure_icon_file
from llm import reset_client
from notify import _notify
from ui.fonts import (
    S_BODY,
    S_BTN,
    S_LOGO,
    S_MONO,
    S_NAV,
    S_SMALL,
    S_TINY,
    font,
)

# 安装流程完整走完后，助手留多少秒让用户看收尾说明，然后自动退出。
# 改这里就行——别在 schedule_auto_quit 里写死数字。
AUTO_QUIT_SECONDS = 10


class WindowMixin:
    def __init__(self, root):
        self.root = root                        # tkinter 主窗口
        self.waiting_for_answer = False          # 是否正处于 ask_user 等待状态
        self.tray = None                         # 托盘对象（main() 里注入）
        self._dlg = None                         # 充值弹窗
        self._dlg_status = None                  # 充值弹窗里的状态文字
        self._recharge_shown = False             # 充值引导是否已经弹过（避免反复弹）
        self._low_notified = False               # 余额偏低是否已通知过
        self._prompt_shown = False               # 底部"是否充值"询问条是否已经问过
        self._last_yuan = None                   # 最近一次查到的余额（充值前后对比用）
        self._usage_payload = {}                 # 最近一次余额/用量数据（用量页用）
        self._pay_info = None                    # 当前充值订单信息
        self._qr_image = None                    # 二维码图片对象（得留着，不然会被回收）
        self._qr_box = None                      # 显示二维码的控件
        self._quit_on_close = False              # 充值弹窗关了就直接退出（aikaui://recharge 用）
        self._recharge_only = False              # 这次启动只办充值，不起托盘/Agent
        self._auto_quit_id = None                # 安装完成后"几秒后自动退出"的 after id

        root.title("Claude Code 助手")
        root.geometry("960x780")
        root.minsize(860, 660)
        root.configure(fg_color=BG)

        # 点右上角 X → 隐藏到托盘，不退出
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        ico = ensure_icon_file()
        if ico:
            try:
                root.iconbitmap(ico)
            except Exception:
                pass                            # 图标设不上不影响运行

        self._build_rail()

        # ---------- 内容区：两个页面叠在同一格里，切换用 grid_remove ----------
        self.content = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.content.grid_rowconfigure(0, weight=1)     # 页面占满
        self.content.grid_columnconfigure(0, weight=1)
        # 第 1 行留给底部的询问条：没有 weight，所以它只占自己那么高

        self.chat_view = ctk.CTkFrame(self.content, fg_color=BG, corner_radius=0)
        self.chat_view.grid(row=0, column=0, sticky="nsew")
        self.usage_view = self._build_usage_view(self.content)   # ui/usage.py
        self.usage_view.grid(row=0, column=0, sticky="nsew")

        self._build_chat_view()
        self._build_prompt_bar()
        self._build_options_panel()

        self.show_view("chat")

    # ---------- 左侧导航栏 ----------

    def _build_rail(self):
        """左侧一条窄导航栏 —— 界面上所有按钮都集中在这一边"""
        rail = ctk.CTkFrame(self.root, fg_color=PANEL, corner_radius=0, width=RAIL_W)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        rail.pack_propagate(False)               # 固定宽度，不让子控件把它撑开

        ctk.CTkLabel(
            rail, text="Claude Code", anchor="w",
            font=font(S_LOGO, "bold"), text_color=TEXT,
        ).pack(fill=tk.X, padx=16, pady=(18, 0))
        ctk.CTkLabel(
            rail, text="助手", anchor="w",
            font=font(S_SMALL), text_color=DIM,
        ).pack(fill=tk.X, padx=16, pady=(0, 18))

        self.nav_buttons = {}
        for key, label in (("chat", "💬  对话"), ("usage", "📊  用量信息")):
            btn = ctk.CTkButton(
                rail, text=label, anchor="w", height=42, corner_radius=6,
                font=font(S_NAV),
                fg_color="transparent", hover_color=OPTION_HOVER, text_color=TEXT,
                command=lambda k=key: self.show_view(k),
            )
            btn.pack(fill=tk.X, padx=10, pady=3)
            self.nav_buttons[key] = btn

        ctk.CTkLabel(
            rail, text="余额和用量在\n「用量信息」里", anchor="w", justify="left",
            font=font(S_TINY), text_color=DIM,
        ).pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=16)

    def show_view(self, key="chat"):
        """切换内容区：chat（对话）/ usage（用量信息）"""
        if key not in ("chat", "usage"):
            key = "chat"
        for k, view in (("chat", self.chat_view), ("usage", self.usage_view)):
            if k == key:
                view.grid(row=0, column=0, sticky="nsew")
            else:
                view.grid_remove()               # 记住位置，下次 grid() 就回来

        for k, btn in self.nav_buttons.items():
            active = (k == key)
            btn.configure(
                fg_color=OPTION_SEL if active else "transparent",
                hover_color=OPTION_SEL_HOVER if active else OPTION_HOVER,
                text_color=OPTION_SEL_TEXT if active else TEXT,
            )

        if key == "usage":
            self.usage_view_update(self._usage_payload)
        else:
            self.entry.focus_set()

    # ---------- 对话页 ----------

    def _build_chat_view(self):
        """聊天区：日志框 + 进度条 + 输入行 + 底部大按钮"""
        view = self.chat_view

        # CTkTextbox 自带滚动条 + 深色主题；insert/see/configure 的用法跟 tk 的 Text 一样
        self.log = ctk.CTkTextbox(
            view, wrap="word",
            font=font(S_MONO, mono=True),
            fg_color=SURFACE, text_color=TEXT, border_width=0,
            state=tk.DISABLED,
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=14, pady=(14, 8))

        # ---------- 进度条（默认隐藏，bash 跑长命令时才显示）----------
        self.progress_frame = ctk.CTkFrame(view, fg_color=PANEL, corner_radius=8)
        self.progress_label = ctk.CTkLabel(
            self.progress_frame, text="", anchor="w",
            font=font(S_SMALL), text_color=OPTION_SEL_TEXT,
        )
        self.progress_label.pack(fill=tk.X, padx=12, pady=(10, 4))
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, padx=12, pady=(0, 10))

        # ---------- 输入行：输入框 + 发送（充值按钮不在这里，它挪到用量页了）----------
        self.entry_frame = ctk.CTkFrame(view, fg_color="transparent")
        self.entry_frame.pack(fill=tk.X, padx=14, pady=(0, 10))

        self.entry = ctk.CTkEntry(
            self.entry_frame, height=38, font=font(S_BODY),
            placeholder_text="说点什么…（按回车发送）",
        )
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 键盘绑定：↑↓ 选选项，回车提交/发送，Esc 跳到"自己打字"
        # 绑在 entry 上（widget 级）而不是 root，这样 return "break" 能真正拦掉
        # 输入框自己的光标移动行为
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Up>", self._on_key_up)
        self.entry.bind("<Down>", self._on_key_down)
        self.entry.bind("<Escape>", self._on_key_escape)

        ctk.CTkButton(
            self.entry_frame, text="发送", width=78, height=38,
            font=font(S_BTN),
            command=self.on_send,
        ).pack(side=tk.LEFT, padx=(10, 0))

        # ---------- 底部大按钮：一键安装 ----------
        self.start_btn = ctk.CTkButton(
            view, text="一键安装 Claude Code", height=50,
            font=font(S_BODY + 2, "bold"),
            command=self.on_start,
        )
        self.start_btn.pack(fill=tk.X, padx=14, pady=(0, 14))

    # ---------- 底部询问条：余额见底时问一句，点了才弹充值窗 ----------

    def _build_prompt_bar(self):
        """建出询问条（默认藏着）。

        ★ 这里刻意**不**做"余额用完就自动弹充值窗" —— 那样太像推销，
          用户明确说过会反感。改成只问一句，点了「立即充值」才弹。"""
        bar = ctk.CTkFrame(self.content, fg_color=WARN_BG, corner_radius=8)
        self.prompt_bar = bar

        self.prompt_label = ctk.CTkLabel(
            bar, text="", anchor="w",
            font=font(S_BODY), text_color=TEXT,
        )
        self.prompt_label.pack(side=tk.LEFT, padx=16, pady=12)

        ctk.CTkButton(
            bar, text="暂不", width=72, height=32,
            font=font(S_SMALL),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.dismiss_recharge_prompt,
        ).pack(side=tk.RIGHT, padx=(0, 14), pady=10)

        ctk.CTkButton(
            bar, text="立即充值", width=96, height=32,
            font=font(S_SMALL, "bold"),
            fg_color="#2f6f4f", hover_color="#3a8560",
            command=self._on_prompt_recharge,
        ).pack(side=tk.RIGHT, padx=(0, 8), pady=10)

    def offer_recharge(self, yuan=None, reason="", force=False):
        """余额见底时**只问一句**，不自己弹充值窗。

        reason 非空表示是服务器直接说"余额不足"（代理层把上游额度错误归一成 402），
        那种情况 force=True，
        不然"一个会话只问一次"的限制会让用户看不到任何提示。"""
        if self._prompt_shown and not force:
            return
        self._prompt_shown = True

        if yuan is None:
            yuan = self._last_yuan
        if reason:
            text = f"⚠️ {reason}，是否立即充值？"
        elif yuan is not None:
            text = f"⚠️ 余额只剩 {_fmt_yuan(yuan)}，是否立即充值？"
        else:
            text = "⚠️ 余额不足，是否立即充值？"
        self.prompt_label.configure(text=text)
        self.prompt_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

    def hide_recharge_prompt(self):
        """收起询问条（余额恢复了，或者用户点了"暂不"）"""
        try:
            self.prompt_bar.grid_remove()
        except Exception:
            pass

    def dismiss_recharge_prompt(self):
        """点「暂不」：收起询问条。余额没恢复之前不再问，免得烦人。"""
        self.hide_recharge_prompt()
        self.write("[系统] 好的，需要充值随时点左边「用量信息」→「充值」")

    def _on_prompt_recharge(self):
        """点「立即充值」：这才弹充值窗"""
        self.hide_recharge_prompt()
        self.show_recharge_dialog(force=True)

    # ---------- 日志 ----------

    def scroll_log_to_end(self):
        """把对话区滚到最底。

        ★ 不能 insert 完立刻 see(END) 就算完：紧接着的布局变化会把滚好的位置
          顶掉——典型的就是 ask_user：先写问题、再冒出选项面板，面板把日志框
          压矮，问题就掉到可视区下面去了。所以放到 after_idle 里再滚一次，
          并补一个稍晚的兜底（窗口刚从托盘恢复时，控件还没真正映射）。"""
        def _see():
            try:
                self.log.see(tk.END)
            except Exception:
                pass
        try:
            self.log.after_idle(_see)
            self.log.after(80, _see)            # 布局稳定后再补一次
        except Exception:
            _see()

    def write(self, text):
        """往日志区追加一行文字（只能在主线程调）"""
        self.log.configure(state=tk.NORMAL)     # 解锁才能写
        self.log.insert(tk.END, text + "\n")
        self.scroll_log_to_end()                # 滚到最底（见上面的说明）
        self.log.configure(state=tk.DISABLED)   # 再锁上，防止用户手改

    # ---------- 用户操作 ----------

    def on_start(self):
        """用户点"一键安装"按钮"""
        self.write("\n[你] 帮我安装 Claude Code")
        user_input_queue.put("帮我安装并配置 Claude Code")

    def on_send(self, event=None):
        """用户按回车或点"发送"按钮"""
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, tk.END)
        self.write(f"\n[你] {text}")

        if self.waiting_for_answer:             # 正在等 ask_user 的回答
            # ★ 直接打 "2" 也算选了第 2 项（老提示里就是这么写的）
            self._submit_answer(self._resolve_index(text))
        else:                                   # 新任务
            user_input_queue.put(text)

    # ---------- 供 Agent 调用 ----------

    def on_token_acquired(self):
        """server.identity.register_device 领到 token 后回调：丢掉旧 client，
        下次 get_client() 会带上新的 device_id 重建"""
        reset_client()

    def ui_log(self, text):
        """Agent 调用：往日志区发消息"""
        msg_queue.put(("log", text))

    def ui_ask(self, question, options):
        """Agent 调用：向用户提问，阻塞直到用户回答

        有选项时不再往日志里打编号，而是交给主线程渲染成可点的选项列表
        （鼠标点一下就直接提交 / ↑↓ 选 + 回车提交，最后一项永远是"输入点别的"）。
        """
        options = list(options or [])

        # ★ 先把"我在等回答"的状态准备好，再让主线程去画选项。
        # 否则用户手快点了选项，状态还没就绪，回答会丢。
        self.waiting_for_answer = True           # 告诉 on_send：接下来的输入是"回答"
        input_event.clear()
        input_holder["value"] = None

        msg_queue.put(("log", f"\n[Agent] {question}"))

        if options:
            msg_queue.put(("ask", {"options": options}))
        else:
            # 没给 options：看问题文本里是不是已经自己写了编号
            has_options_in_question = bool(
                re.search(r"^\s*1\s*[\.\、\．\)\）]\s*\S", question, re.MULTILINE)
            )
            if has_options_in_question:
                msg_queue.put(("log", "（在下面输入序号或直接回答）"))

        input_event.wait()                       # ★ 阻塞，直到主线程里 set()
        self.waiting_for_answer = False

        return f"用户回答：{input_holder['value']}"

    def ui_progress_start(self, text="处理中..."):
        """Agent 调用：显示进度条"""
        msg_queue.put(("progress_start", text))

    def ui_progress_stop(self):
        """Agent 调用：隐藏进度条"""
        msg_queue.put(("progress_stop", None))

    # ---------- 余额显示 ----------

    def update_balance(self, payload):
        """余额 / 用量回来了（主线程调用）。

        数据和显示是分开的：**数字全给"用量信息"页**（usage_view_update），
        主窗口只在余额见底时冒一条询问条。低额提醒按三档来：
          ¥2 以下 → 托盘图标变橙、系统通知一次
          ¥1 以下 → 底部问一句"是否立即充值"（不自己弹充值窗）
        """
        payload = payload or {}
        self._usage_payload = payload

        bal = payload.get("balance") or {}

        # ★ 只办充值的启动方式（点终端里服务器给的 aikaui://recharge 链接进来）：
        #   要把数据画到用量页——用户先看到"钱花在哪了"，充值窗随后才弹。
        #   但别在这时候弹托盘告警 / 系统通知 / 底部询问条：
        #   用户已经在充值流程里了，再弹一遍就是噪音。
        if self._recharge_only:
            if not bal.get("rate_limited"):
                self._last_yuan = _to_float(bal.get("yuan"))
            self.usage_view_update(payload)
            return

        # ★ 余额那块被服务器限流（429）时，**什么都别动**：
        #   这不是"余额变成 0 了"，也不是"服务挂了"。要是照旧往下走，
        #   _last_yuan 会被清成 None（付款到没到账就判断不了了），
        #   低额提醒也会被当成"余额恢复了"重置，反而更容易反复打扰用户。
        #   用量页自己会跳过被限流的那块，把上一次的真实数字继续显示着。
        if bal.get("rate_limited"):
            self.usage_view_update(payload)
            return

        yuan = _to_float(bal.get("yuan"))
        self._last_yuan = yuan        # 记下来：付款后拿它跟新余额对比，判断有没有到账

        self.usage_view_update(payload)          # ★ 用量信息页自己去画

        low = yuan is not None and yuan < 2

        # --- 托盘图标变色 ---
        if self.tray:
            self.tray.set_alert(low)

        # --- 跌破 ¥2 弹一次系统通知 ---
        if low and not self._low_notified:
            self._low_notified = True
            _notify("Claude Code 助手", f"余额只剩 ¥{yuan:.2f}，需要的话点「用量信息」页充值")
        elif not low:
            self._low_notified = False           # 充过值了，下次再跌破重新提醒
            self._prompt_shown = False
            self.hide_recharge_prompt()          # 余额回来了，询问条收掉

        # --- 见底：只问一句，用户点了才弹充值窗 ---
        if yuan is not None and yuan < 1:
            self.offer_recharge(yuan)

    # ---------- 窗口显隐 / 退出 ----------

    def show_window(self):
        """从托盘恢复窗口（必须是主线程，托盘线程用 root.after 调度）"""
        try:
            self.root.deiconify()                # 取消 withdraw 的隐藏
            self.root.lift()                     # 提到最前
            self.root.focus_force()              # 抢焦点
        except Exception:
            pass

    def hide_to_tray(self):
        """点 X：有托盘就隐藏到托盘，没托盘就直接退出（否则窗口没了程序还在跑，用户找不到）"""
        if HAVE_TRAY and self.tray is not None and self.tray.ok:
            self.root.withdraw()
            self.write("[系统] 已最小化到托盘，双击托盘图标可重新打开")
        else:
            self.quit_app()

    def schedule_auto_quit(self, seconds=None):
        """安装流程走完 → 留一点时间给用户看收尾说明，然后助手自己退出。

        为什么不是立刻退：最后那条 Agent 消息里写了工作目录和 VSCode 的用法，
        秒退用户根本来不及看。为什么只在这个时机退：用户只是让助手"再打开一次
        Claude Code"时也调 launch_claude，那种情况不该把助手关掉——
        所以触发条件是**这一轮完整走过安装流程**，见 agent_core.run_one_task。"""
        if getattr(self, "_recharge_only", False):
            return                               # 只办充值的启动方式没有安装流程
        if self._auto_quit_id is not None:
            return                               # 已经排过了，别重复排
        if seconds is None:
            seconds = AUTO_QUIT_SECONDS
        self.write(f"[系统] 安装完成，{seconds} 秒后自动退出。"
                   " Claude Code 已经在终端里打开了，以后从桌面图标启动就行。")
        self._auto_quit_id = self.root.after(int(seconds * 1000), self.quit_app)

    def quit_app(self):
        """真的退出：先停托盘，再关窗口"""
        if self._auto_quit_id is not None:
            try:
                self.root.after_cancel(self._auto_quit_id)
            except Exception:
                pass
            self._auto_quit_id = None
        if self.tray is not None:
            self.tray.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
