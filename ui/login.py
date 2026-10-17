# -*- coding: utf-8 -*-
"""LoginMixin：换设备登录弹窗。

用户在「用量信息」页点「🔑 换设备登录」，输入自己的账户码，把这台电脑切到
那个账号上（对应服务端的 POST /api/bind-account）。

★ 两条纪律，都写在这儿了，改的时候别丢：

  1. **输入原样发出去。** 服务器会归一化大小写、横线、空格、全角（中文输入法
     打的），认 O↔0 / I↔1，甚至能把码从一整句话里挑出来。在这里先做正则校验
     或者"顺手帮用户清理一下"，只会把用户打对了的码挡在门外。所以下面读出来
     直接给 server.api.login_with_account_code，一个字符都不动。

  2. **限流和"码错了"是两回事。** 服务器限流（429）时会说 retry_after=N 秒。
     把它说成"账户码不对"，用户就会开始改码、反复重试，越试越登不上。
     所以这里限流要显示成"等 N 秒"，并且按钮也真的锁 N 秒。

登录成功后，后台会**同时**把新账号的 token 写进 Claude Code 的两个配置文件
（~/.claude.json 和 ~/.claude/settings.json），不用再走一次「一键安装」。
只改 identity.json 不够——settings.json 的 env 优先级更高，漏了它终端里
Claude Code 会继续烧上一个账号（2026-10-17 修的就是这个 bug）。
写入失败时如实说，并让用户重走一键安装。
"""

import threading
import tkinter as tk

import customtkinter as ctk

from agent_core import refresh_balance
from bus import msg_queue
from config import BG, DIM, GREEN, ORANGE, RED, TEXT
from ui.fonts import S_BODY, S_BTN, S_DIALOG, S_MONO, S_SMALL, font


class LoginMixin:
    # ---------- 弹窗 ----------

    def show_login_dialog(self):
        """打开「换设备登录」弹窗（已经开着就提到前面来）"""
        if getattr(self, "_login_dlg", None) is not None:
            try:
                if self._login_dlg.winfo_exists():
                    self._login_dlg.lift()
                    self._login_dlg.focus_force()
                    return
            except Exception:
                pass

        dlg = ctk.CTkToplevel(self.root)
        self._login_dlg = dlg
        self._login_btn = None            # 登录按钮（限流时要锁它）
        self._login_status = None         # 状态那行字
        self._login_left = 0              # 限流倒计时还剩几秒

        dlg.title("换设备登录")
        dlg.geometry("440x330")
        dlg.configure(fg_color=BG)
        dlg.transient(self.root)          # 跟随主窗
        dlg.after(150, self._safe_grab_login)

        ctk.CTkLabel(
            dlg, text="🔑 换设备登录",
            font=font(S_DIALOG, "bold"), text_color=TEXT,
        ).pack(pady=(22, 8))

        ctk.CTkLabel(
            dlg,
            text=("在另一台电脑上继续用同一个账号：\n"
                  "把这台机器上「用量信息」页显示的账户码填进来。"),
            font=font(S_SMALL), text_color=DIM, justify="center", wraplength=380,
        ).pack(pady=(0, 12))

        entry = ctk.CTkEntry(
            dlg, width=340, height=40, justify="center",
            font=font(S_MONO, mono=True),
            placeholder_text="CC-XXXXXX-XXXXXX",
        )
        entry.pack(pady=(0, 6))
        self._login_entry = entry
        entry.bind("<Return>", lambda e: self._submit_login())

        # 状态行：成功绿灯，限流黄灯，码不对红灯
        self._login_status = ctk.CTkLabel(
            dlg, text="", font=font(S_SMALL),
            text_color=DIM, wraplength=380, justify="center",
        )
        self._login_status.pack(pady=(2, 10))

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack()
        self._login_btn = ctk.CTkButton(
            row, text="登录", width=150, height=38,
            font=font(S_BTN, "bold"),
            command=self._submit_login,
        )
        self._login_btn.pack(side=tk.LEFT, padx=6)
        ctk.CTkButton(
            row, text="取消", width=110, height=38,
            font=font(S_BTN),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self._close_login_dialog,
        ).pack(side=tk.LEFT, padx=6)

        entry.focus_set()

    def _safe_grab_login(self):
        """给弹窗加模态；有些窗口管理器不让抢焦点，失败就算了"""
        try:
            if self._login_dlg is not None:
                self._login_dlg.grab_set()
        except Exception:
            pass

    def _close_login_dialog(self):
        dlg = getattr(self, "_login_dlg", None)
        self._login_dlg = None
        self._login_btn = None
        self._login_status = None
        self._login_left = 0
        if dlg is not None:
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass

    # ---------- 提交 ----------

    def _submit_login(self):
        """点了「登录」：把输入框里的东西**原样**交给后台线程去绑。"""
        if self._login_left > 0:
            return                                    # 限流倒计时里别放行
        try:
            raw = self._login_entry.get()
        except Exception:
            return
        if not str(raw).strip():
            self._set_login_status("先填账户码，比如 CC-XXXXXX-XXXXXX", ORANGE)
            return
        self._set_login_status("正在登录…", DIM)
        if self._login_btn is not None:
            try:
                self._login_btn.configure(state=tk.DISABLED, text="登录中…")
            except Exception:
                pass
        threading.Thread(target=self._login_worker, args=(raw,), daemon=True).start()

    @staticmethod
    def _login_worker(raw):
        """后台线程：绑账号。

        ★ raw 直接进 login_with_account_code，不 strip、不大写、不补横线——
          归一化是服务器的事（见本文件开头第 1 条）。"""
        import json

        from server.api import login_with_account_code
        try:
            res = json.loads(login_with_account_code(raw))
        except Exception as e:
            res = {"error": f"登录失败：{e}"}
        msg_queue.put(("login_result", res))

    def show_login_result(self, res):
        """主线程：绑定结果回来了"""
        res = res or {}
        if self._login_btn is not None:
            try:
                self._login_btn.configure(state=tk.NORMAL, text="登录")
            except Exception:
                pass

        if res.get("ok"):
            synced = bool(res.get("commands_synced"))
            self._set_login_status(
                "登录成功，这台电脑已经切到这个账号了。" if synced else
                "登录成功，但命令行配置没改成功，终端里可能还是旧账号。",
                GREEN if synced else ORANGE)
            self.write("[系统] 已登录：%s" % (res.get("account_code") or ""))
            if synced:
                self.write("[系统] 命令行配置已同步（~/.claude.json 和 "
                           "~/.claude/settings.json），重开一次 Claude Code 就用新账号。")
            else:
                self.write("[系统] 命令行配置没改成功：%s" % (res.get("commands_note") or ""))
                self.write("[系统] 要让 Claude Code 也用这个账号，"
                           "重新点一次「一键安装 Claude Code」。")
            # 账号变了 → 余额/用量要按新账号重查（后台线程，别卡界面）
            threading.Thread(target=refresh_balance, daemon=True).start()
            return

        if res.get("rate_limited"):
            wait = int(res.get("retry_after") or 60)
            self._set_login_status(res.get("error") or "试得太频繁了，等一会儿再试", ORANGE)
            self._lock_login_button(wait)
            return

        self._set_login_status(res.get("error") or "登录失败，稍后再试", RED)

    def _lock_login_button(self, seconds):
        """限流期间把「登录」按钮真的锁住，倒数完再放开。

        ★ 只是嘴上说"等一会儿"不够：用户会一直点，一点就又算一次，反而把
          限流窗口越推越长。"""
        self._login_left = max(1, int(seconds))

        def tick():
            if self._login_btn is None:
                return                                  # 弹窗关掉了
            if self._login_left <= 0:
                try:
                    self._login_btn.configure(state=tk.NORMAL, text="登录")
                except Exception:
                    pass
                return
            try:
                self._login_btn.configure(
                    state=tk.DISABLED, text="%d 秒后可重试" % self._login_left)
            except Exception:
                return
            self._login_left -= 1
            self.root.after(1000, tick)      # 下一秒接着倒数

        tick()

    def _set_login_status(self, text, color=DIM):
        if self._login_status is not None:
            try:
                self._login_status.configure(text=text, text_color=color)
            except Exception:
                pass
