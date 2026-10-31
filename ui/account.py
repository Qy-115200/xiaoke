# -*- coding: utf-8 -*-
"""AccountMixin：账户信息页 —— 账户码 / 账户密钥的打码展示、复制、找回提示。

为什么单独一页：这两串东西是**找回账号和余额的唯一凭据**，得给它们足够的位置
写清楚"这是什么、丢了会怎样、该存在哪"。塞进用量页的余额卡下面会把两张图挤扁，
而且那样一来红字警告只能缩成一行小字，没人会看。

三条纪律，改的时候别丢：

  1. **默认打码。** 打开页面看到的是 `CC-1************` / `************************`。
     点眼睛才明文 —— 而且明文状态不做任何持久化，切走再回来又变回打码。
  2. **凭证不进日志、不进对话。** 复制按钮只写系统剪贴板；日志里只说"已复制"。
     明文显示时也不往日志里打值（日志是用户会截图的地方）。
  3. **打码不是加密。** 明文就在本地凭证里，能碰到这台电脑的人点一下就能看到。
     所以旁边那句红字警告是**必需品**，不是装饰。

`ui_recovered()` 是给 Agent 线程调的回调（服务器说"认出这台机器的旧账号了"），
它只往 bus.msg_queue 里丢消息，真正的弹窗由主线程的 show_recovered_notice() 弹。
"""

import tkinter as tk

import customtkinter as ctk

from bus import msg_queue
from config import BG, DIM, GREEN, RED, SURFACE, TEXT
from helpers import _mask_secret
from server.identity import load_identity
from server.vault import backend as vault_backend
from ui.fonts import S_BODY, S_BTN, S_DIALOG, S_SMALL, S_TINY, S_TITLE, font

# 任务书里写死的原话。别改字 —— 这句话是给用户"这串东西丢了会怎样"的唯一说明。
WARNING_TEXT = ("⚠️ 重要：这是您找回账户和余额的唯一凭证，请截图保存在安全的地方。"
                "切勿发给他人，否则余额将被盗用。")

# 两行凭证：键 = identity.json 里的字段名
_ROWS = (
    ("account_code", "账户码", "换电脑 / 重装后在「换设备登录」里填这一串", 4),
    ("account_key", "账户密钥", "登录要连它一起填；注册时只下发一次，丢了就只能靠本机这份", 0),
)


class AccountMixin:
    # ---------- 建页 ----------

    def _build_account_view(self, parent):
        """把「账户信息」页建出来（默认不显示，由 show_view 切换）"""
        view = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        self.account_view = view
        self._acct_values = {}          # 原始值；只在内存里，绝不进日志
        self._acct_revealed = {}        # 哪几行现在是明文
        self._acct_value_labels = {}
        self._acct_eye_buttons = {}
        self._acct_copy_buttons = {}
        self._recovered_dlg = None

        head = ctk.CTkFrame(view, fg_color="transparent")
        head.pack(fill=tk.X, padx=18, pady=(16, 10))
        ctk.CTkLabel(
            head, text="账户信息", font=font(S_TITLE, "bold"), text_color=TEXT,
        ).pack(side=tk.LEFT)
        ctk.CTkLabel(
            head, text="默认打码 · 点「显示」才看得到明文",
            font=font(S_SMALL), text_color=DIM,
        ).pack(side=tk.LEFT, padx=(12, 0))

        # ---------- 凭证卡：两行，每行「名称 · 值 · 眼睛 · 复制」 ----------
        card = ctk.CTkFrame(view, fg_color=SURFACE, corner_radius=10)
        card.pack(fill=tk.X, padx=18, pady=(0, 10))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill=tk.X, padx=18, pady=16)
        inner.grid_columnconfigure(1, weight=1)     # 值那一列吃掉多余宽度

        for row, (key, title, hint, keep) in enumerate(_ROWS):
            top = row * 2
            ctk.CTkLabel(
                inner, text=title, font=font(S_BODY, "bold"),
                text_color=TEXT, anchor="w", width=84,
            ).grid(row=top, column=0, sticky="w", pady=(0 if row == 0 else 14, 2))

            value = ctk.CTkLabel(
                inner, text="—", font=font(S_BODY, mono=True),
                text_color=TEXT, anchor="w", justify="left", wraplength=540,
            )
            value.grid(row=top, column=1, columnspan=3, sticky="w",
                       pady=(0 if row == 0 else 14, 2))
            self._acct_value_labels[key] = value

            # 眼睛按钮 / 复制按钮放在第二行，跟说明文字同一行，不挤占值的宽度
            btns = ctk.CTkFrame(inner, fg_color="transparent")
            btns.grid(row=top + 1, column=0, columnspan=4, sticky="w")
            ctk.CTkLabel(
                btns, text=hint, font=font(S_TINY), text_color=DIM,
            ).pack(side=tk.LEFT, padx=(0, 12))
            eye = ctk.CTkButton(
                btns, text="👁 显示", width=78, height=24, corner_radius=4,
                font=font(S_TINY), fg_color="#3a3a3a", hover_color="#4a4a4a",
                command=lambda k=key: self.on_account_toggle(k),
            )
            eye.pack(side=tk.LEFT)
            self._acct_eye_buttons[key] = eye
            cp = ctk.CTkButton(
                btns, text="复制", width=58, height=24, corner_radius=4,
                font=font(S_TINY), fg_color="#3a3a3a", hover_color="#4a4a4a",
                command=lambda k=key: self.on_account_copy(k),
            )
            cp.pack(side=tk.LEFT, padx=(8, 0))
            self._acct_copy_buttons[key] = cp

        # ---------- 红字警告：打码不能替代提醒 ----------
        ctk.CTkLabel(
            view, text=WARNING_TEXT, font=font(S_SMALL, "bold"),
            text_color=RED, anchor="w", justify="left", wraplength=700,
        ).pack(fill=tk.X, padx=22, pady=(2, 12))

        # ---------- 怎么找回 / 本机还留了一份什么 ----------
        try:
            where = vault_backend()
        except Exception:
            where = ""
        lines = [
            "• 换电脑、重装系统：点「用量信息」页右上角的「🔑 换设备登录」，"
            "账户码和账户密钥两串一起填。",
            "• 账户码 / 账户密钥只在本机显示，服务器只在注册那一次下发密钥。",
        ]
        if where:
            lines.append(f"• 本机另留了一份加密备份（{where}）：本地凭证被删掉时，"
                         "重新打开程序会问您要不要恢复，不会默默开一个新号。")
        ctk.CTkLabel(
            view, text="\n".join(lines), font=font(S_TINY),
            text_color=DIM, anchor="w", justify="left", wraplength=700,
        ).pack(fill=tk.X, padx=22)

        self._refresh_account_panel()
        return view

    # ---------- 数据 → 界面 ----------

    def _refresh_account_panel(self):
        """从本地凭证里重读两串值，按"打码 / 明文"重画。"""
        ident = load_identity() or {}
        for key, _title, _hint, _keep in _ROWS:
            self._acct_values[key] = str(ident.get(key) or "").strip()
        for key in self._acct_values:
            self._paint_account_row(key)

    def _paint_account_row(self, key):
        """把某一行按当前状态画出来（值 / 打码 / 没有）"""
        value = self._acct_values.get(key) or ""
        shown = bool(self._acct_revealed.get(key))

        lbl = self._acct_value_labels.get(key)
        if lbl is not None:
            if not value:
                lbl.configure(text="—", text_color=DIM)
            elif shown:
                lbl.configure(text=value, text_color=TEXT)
            else:
                keep = 4 if key == "account_code" else 0
                lbl.configure(text=_mask_secret(value, keep=keep), text_color=TEXT)

        eye = self._acct_eye_buttons.get(key)
        if eye is not None:
            try:
                eye.configure(
                    text=("🙈 隐藏" if shown else "👁 显示"),
                    state=tk.NORMAL if value else tk.DISABLED,
                )
            except Exception:
                pass
        cp = self._acct_copy_buttons.get(key)
        if cp is not None:
            try:
                cp.configure(state=tk.NORMAL if value else tk.DISABLED)
            except Exception:
                pass

    # ---------- 按钮 ----------

    def on_account_toggle(self, key):
        """点了眼睛：在打码 / 明文之间切换（明文状态不持久化）"""
        if not self._acct_values.get(key):
            return
        self._acct_revealed[key] = not self._acct_revealed.get(key, False)
        self._paint_account_row(key)

    def on_account_copy(self, key):
        """只写系统剪贴板，不写日志、不进对话 —— 日志是用户会截图的地方。"""
        value = self._acct_values.get(key) or ""
        if not value:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.root.update_idletasks()        # Tk 剪贴板要过一轮事件循环才真正落地
        except Exception:
            return
        btn = self._acct_copy_buttons.get(key)
        if btn is None:
            return
        try:
            btn.configure(text="已复制")
            # 换成"已复制"给个反馈，过一会儿自己变回来（别让它一直挂着）
            self.root.after(1400, lambda b=btn: self._reset_copy_button(b))
        except Exception:
            pass

    @staticmethod
    def _reset_copy_button(btn):
        try:
            btn.configure(text="复制")
        except Exception:
            pass

    # ---------- 服务器说"认出旧账号了" ----------

    def ui_recovered(self):
        """server.identity 在 Agent 线程里调：服务器按指纹认出了旧账号。

        ★ 这里**不能**碰控件，只往队列里丢一条消息，弹窗在主线程做。"""
        msg_queue.put(("recovered", {}))

    def show_recovered_notice(self):
        """主线程：弹一次"账号已恢复"的提示，并把账户信息页刷新一下。"""
        old = self._recovered_dlg
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    return
            except Exception:
                pass

        self._refresh_account_panel()

        dlg = ctk.CTkToplevel(self.root)
        self._recovered_dlg = dlg
        dlg.title("账号已自动恢复")
        dlg.geometry("460x240")
        dlg.configure(fg_color=BG)
        dlg.transient(self.root)

        def close():
            self._recovered_dlg = None
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass

        dlg.protocol("WM_DELETE_WINDOW", close)

        ctk.CTkLabel(
            dlg, text="✅ 账号已自动恢复",
            font=font(S_DIALOG, "bold"), text_color=GREEN,
        ).pack(pady=(26, 10))
        ctk.CTkLabel(
            dlg, text="检测到这台机器之前已经注册过，已直接登回原来的账户。",
            font=font(S_SMALL), text_color=TEXT, wraplength=400, justify="center",
        ).pack(padx=28)
        ctk.CTkButton(
            dlg, text="知道了", width=140, height=36,
            font=font(S_BTN, "bold"), command=close,
        ).pack(pady=24)

        # 有些窗口管理器不让抢焦点，失败就算了（跟登录弹窗一样）
        dlg.after(150, lambda: self._safe_grab_recovered(dlg))

    def _safe_grab_recovered(self, dlg):
        try:
            dlg.grab_set()
        except Exception:
            pass
