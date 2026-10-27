# -*- coding: utf-8 -*-
"""GateMixin：检测到别的中转时，拦住用户问一句。

这个客户端只配合 aikaui 中转工作（见 server/relay_gate.py 里的原因）。这里只管
**把话说清楚、把选择摆出来**：

  换成 aikaui 中转（继续）  → 备份原来的文件，写我们的配置，返回 True
  退出                      → 返回 False，main.py 直接关窗口

★ 切换动作放后台线程：首次运行要现场注个账号（PoW 可能几秒），冻住界面不行。
★ 切换失败**不关窗**：把失败原因留在窗里，让用户重试或者退出——
  失败原因里只有文件名和 npm/网络的错误，没有任何密钥。
"""

import threading
import tkinter as tk

import customtkinter as ctk

from bus import msg_queue
from config import BG, BLUE, BORDER, DIM, GREEN, PANEL, RED, TEXT
from ui.fonts import S_BODY, S_BTN, S_HEAD, S_SMALL, font


class GateMixin:
    def ask_relay_conflict(self, gate, switch_fn):
        """弹模态窗。返回 (是否已换成我们的, 切换成功后要写进对话区的那句话)。"""
        self._gate_result = None
        self._gate_message = ""
        self._gate_busy = False          # 正在切换时不许关窗，免得写了一半

        dlg = ctk.CTkToplevel(self.root)
        self._gate_dlg = dlg
        dlg.title("需要换成 aikaui 中转")
        dlg.geometry("600x430")
        dlg.resizable(False, False)
        dlg.configure(fg_color=BG)
        dlg.transient(self.root)
        dlg.protocol("WM_DELETE_WINDOW", self._gate_on_quit)
        dlg.after(150, self._safe_grab_gate)

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(22, 18))

        ctk.CTkLabel(
            body, text="检测到其他中转", anchor="w",
            font=font(S_HEAD, "bold"), text_color=TEXT,
        ).pack(fill=tk.X)

        ctk.CTkLabel(
            body, text="这台电脑上的 Claude Code 现在指向别处：", anchor="w",
            font=font(S_BODY), text_color=DIM,
        ).pack(fill=tk.X, pady=(10, 6))

        # 只列文件名和 host —— 地址里可能带 token，不往界面上放
        detail = ctk.CTkFrame(body, fg_color=PANEL, corner_radius=8,
                              border_width=1, border_color=BORDER)
        detail.pack(fill=tk.X)
        ctk.CTkLabel(
            detail, text="\n".join(gate.lines()), anchor="w", justify="left",
            font=font(S_SMALL), text_color=TEXT, wraplength=520,
        ).pack(fill=tk.X, padx=14, pady=12)

        ctk.CTkLabel(
            body,
            text=("本客户端只配合 aikaui 中转使用，换成我们的才能继续。\n"
                  "原来的配置会先备份成 *.aikaui-bak，想换回去拷一下就行。"),
            anchor="w", justify="left", font=font(S_BODY), text_color=DIM,
            wraplength=520,
        ).pack(fill=tk.X, pady=(12, 0))

        self._gate_status = ctk.CTkLabel(
            body, text="", anchor="w", justify="left",
            font=font(S_SMALL), text_color=DIM, wraplength=520,
        )
        self._gate_status.pack(fill=tk.X, pady=(10, 0))

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(side=tk.BOTTOM, fill=tk.X, pady=(14, 0))

        self._gate_switch = ctk.CTkButton(
            row, text="换成 aikaui 中转（继续）", height=40, corner_radius=8,
            font=font(S_BTN, "bold"), fg_color=BLUE, hover_color="#4a9fe0",
            command=lambda: self._gate_start_switch(switch_fn),
        )
        self._gate_switch.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._gate_quit = ctk.CTkButton(
            row, text="退出", width=110, height=40, corner_radius=8,
            font=font(S_BTN), fg_color="transparent", hover_color=PANEL,
            border_width=1, border_color=BORDER, text_color=DIM,
            command=self._gate_on_quit,
        )
        self._gate_quit.pack(side=tk.LEFT, padx=(10, 0))

        # 窗口是模态的：在它关掉之前，main.py 不会往下走（也不会起 Agent）
        self.root.wait_window(dlg)
        return (self._gate_result is True, self._gate_message)

    # ---------- 切换 ----------

    def _gate_start_switch(self, switch_fn):
        self._gate_busy = True
        self._gate_switch.configure(state=tk.DISABLED, text="正在切换…")
        self._gate_quit.configure(state=tk.DISABLED)
        self._gate_status.configure(text="先把原来的配置备份好，再写我们的……",
                                    text_color=DIM)
        threading.Thread(target=self._gate_worker, args=(switch_fn,),
                         daemon=True).start()

    @staticmethod
    def _gate_worker(switch_fn):
        try:
            ok, message = switch_fn()
        except Exception as e:                  # 走到这儿说明连异常都没兜住
            ok, message = False, f"切换时出错：{e}"
        msg_queue.put(("relay_gate", {"ok": bool(ok), "message": str(message or "")}))

    def on_relay_gate_result(self, data):
        """主线程：切换结果回来了。成功就关窗，失败就把原因留在窗里。"""
        dlg = getattr(self, "_gate_dlg", None)
        if dlg is None or not dlg.winfo_exists():
            return
        self._gate_busy = False
        if data.get("ok"):
            self._gate_result = True
            self._gate_message = data.get("message") or "已换成 aikaui 中转。"
            self._gate_status.configure(text=self._gate_message, text_color=GREEN)
            dlg.after(400, dlg.destroy)         # 让用户看清"备份到哪了"再关
            return
        self._gate_status.configure(
            text=(data.get("message") or "没换成。") +
                 "\n\n可以再试一次；一直不行就退出，先把那份配置手动改回来。",
            text_color=RED)
        self._gate_switch.configure(state=tk.NORMAL, text="换成 aikaui 中转（继续）")
        self._gate_quit.configure(state=tk.NORMAL)

    def _gate_on_quit(self):
        if getattr(self, "_gate_busy", False):
            return                       # 正在写盘/备份，等它回来再决定
        self._gate_result = False
        dlg = getattr(self, "_gate_dlg", None)
        if dlg is not None and dlg.winfo_exists():
            dlg.destroy()

    def _safe_grab_gate(self):
        """直接 grab_set 有时会失效（窗口还没映射完），挪到 after 里再做一次。"""
        dlg = getattr(self, "_gate_dlg", None)
        if dlg is None or not dlg.winfo_exists():
            return
        try:
            dlg.grab_set()
            dlg.lift()
            dlg.focus_force()
        except Exception:
            pass
