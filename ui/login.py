# -*- coding: utf-8 -*-
"""LoginMixin：换设备登录弹窗。

用户在「用量信息」页点「🔑 换设备登录」，输入**账户码 + 账户密钥**，把这台电脑
切到那个账号上（对应服务端的 POST /api/bind-account）。

★ 为什么要两样：账户码会露给旁人（截图、口述、页面上露前 4 位），单靠它就把
  整台机器的余额领走太薄。密钥只在下发时出现一次，服务端只存 sha256。
  2026-10-24 之前注册的老账号没有密钥，服务端对它们仍只认码——所以这里
  **不**把密钥设成必填，空着照样发，由服务端说了算。

★ 三条纪律，都写在这儿了，改的时候别丢：

  1. **输入原样发出去。** 服务器会归一化大小写、横线、空格、全角（中文输入法
     打的），认 O↔0 / I↔1，甚至能把码从一整句话里挑出来。在这里先做正则校验
     或者"顺手帮用户清理一下"，只会把用户打对了的码挡在门外。所以下面读出来
     直接给 server.api.login_with_account_code，一个字符都不动。

  2. **限流和"码错了"是两回事。** 服务器限流（429）时会说 retry_after=N 秒。
     把它说成"账户码不对"，用户就会开始改码、反复重试，越试越登不上。
     所以这里限流要显示成"等 N 秒"，并且按钮也真的锁 N 秒。

  3. **密钥不落任何文字。** 不回显、不写进对话日志、不拼进异常信息。

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
from helpers import _mask_secret
from server.identity import load_identity
from ui.fonts import S_BODY, S_BTN, S_DIALOG, S_MONO, S_SMALL, font


class LoginMixin:
    # ---------- 弹窗 ----------

    def show_login_dialog(self, prefill="", notice="", prefill_key=""):
        """打开「换设备登录」弹窗（已经开着就提到前面来）。

        三个可选参数是给**自动找回**用的（本机凭证丢了、但注册表/配置目录里还留着
        上次的凭证，见 server/vault.py）：
          prefill      预填账户码 —— 用户只要点一下「登录」；
          prefill_key  预填账户密钥（老版本保险箱里可能只有码，那就留空）；
          notice       替换掉默认的说明文字 —— 告诉用户"为什么这个框自己弹出来了"。
        ★ 即使能自动恢复也**不替用户点这一下**：共用电脑上前一个人删了凭证，
          后一个人开机就被恢复进别人的账号，等于把余额送给陌生人。
        """
        if getattr(self, "_login_dlg", None) is not None:
            try:
                if self._login_dlg.winfo_exists():
                    self._login_dlg.lift()
                    self._login_dlg.focus_force()
                    if getattr(self, "_login_entry", None) is not None:
                        try:
                            if prefill and not self._login_entry.get():
                                self._login_entry.insert(0, prefill)
                            if (prefill_key
                                    and getattr(self, "_login_key_entry", None) is not None
                                    and not self._login_key_entry.get()):
                                self._login_key_entry.insert(0, prefill_key)
                        except Exception:
                            pass
                    return
            except Exception:
                pass

        dlg = ctk.CTkToplevel(self.root)
        self._login_dlg = dlg
        self._login_btn = None            # 登录按钮（限流时要锁它）
        self._login_status = None         # 状态那行字
        self._login_left = 0              # 限流倒计时还剩几秒
        self._login_recovery = bool(prefill)   # 这次弹窗是"自动找回"触发的

        dlg.title("换设备登录")
        # 现在有两个输入框；自动找回时的说明又比平时长，窗口再给高一点
        dlg.geometry("470x520" if notice else "450x470")
        dlg.configure(fg_color=BG)
        dlg.transient(self.root)          # 跟随主窗
        dlg.after(150, self._safe_grab_login)

        ctk.CTkLabel(
            dlg, text="🔑 换设备登录",
            font=font(S_DIALOG, "bold"), text_color=TEXT,
        ).pack(pady=(20, 6))

        ctk.CTkLabel(
            dlg,
            text=(notice or
                  "在另一台电脑上继续用同一个账号：\n"
                  "把之前存下来的账户码和账户密钥填进来\n"
                  "（本机在左边「🔐 账户信息」页，点「👁 显示」后复制）。"),
            font=font(S_SMALL), text_color=DIM, justify="center", wraplength=410,
        ).pack(pady=(0, 10))

        ctk.CTkLabel(dlg, text="账户码", font=font(S_SMALL), text_color=DIM).pack(anchor="w", padx=52)
        entry = ctk.CTkEntry(
            dlg, width=360, height=38, justify="center",
            font=font(S_MONO, mono=True),
            placeholder_text="CC-XXXXXX-XXXXXX",
        )
        entry.pack(pady=(2, 8))
        self._login_entry = entry
        if prefill:
            # ★ 预填的是本机保险箱里的凭证，不给用户看第二遍：直接填进框里，
            #   他点「登录」就恢复；不满意也能自己改。
            entry.insert(0, str(prefill))
        entry.bind("<Return>", lambda e: self._submit_login())

        ctk.CTkLabel(dlg, text="账户密钥", font=font(S_SMALL), text_color=DIM).pack(anchor="w", padx=52)
        key_entry = ctk.CTkEntry(
            dlg, width=360, height=38, justify="center", show="●",
            font=font(S_MONO, mono=True),
            placeholder_text="64 位字母数字",
        )
        key_entry.pack(pady=(2, 4))
        self._login_key_entry = key_entry
        if prefill_key:
            key_entry.insert(0, str(prefill_key))
        key_entry.bind("<Return>", lambda e: self._submit_login())

        ctk.CTkLabel(
            dlg,
            text="密钥在「🔐 账户信息」页点「👁 显示」后整串复制。\n"
                 "2026-10-24 之前注册的老账号没有密钥，留空即可。",
            font=font(S_SMALL), text_color=DIM, justify="center", wraplength=410,
        ).pack(pady=(0, 6))

        # 状态行：成功绿灯，限流黄灯，码不对红灯
        self._login_status = ctk.CTkLabel(
            dlg, text="", font=font(S_SMALL),
            text_color=DIM, wraplength=400, justify="center",
        )
        self._login_status.pack(pady=(2, 8))

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
        was_recovery = getattr(self, "_login_recovery", False)
        had_identity = bool(load_identity())
        self._login_dlg = None
        self._login_btn = None
        self._login_status = None
        self._login_left = 0
        self._login_recovery = False
        self._login_entry = None
        self._login_key_entry = None
        if dlg is not None:
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass
        # ★ 自动找回那次，用户点了「取消」= 这不是他的账号（或者他想重来）。
        #   这时候程序是"没有账号"的状态：得说清楚怎么接着往下走，
        #   否则界面看着正常、其实一句话都答不了。
        if was_recovery and not had_identity:
            self.write("[系统] 没有恢复账号。这台电脑现在没有可用凭证。")
            self.write("[系统] 想恢复原来的账号：点「用量信息」→「🔑 换设备登录」，"
                       "输入之前存下来的账户码和账户密钥。")

    # ---------- 提交 ----------

    def _submit_login(self):
        """点了「登录」：把两个输入框里的东西**原样**交给后台线程去绑。"""
        if self._login_left > 0:
            return                                    # 限流倒计时里别放行
        try:
            raw = self._login_entry.get()
        except Exception:
            return
        try:
            raw_key = self._login_key_entry.get() if self._login_key_entry is not None else ""
        except Exception:
            raw_key = ""
        if not str(raw).strip():
            self._set_login_status("先填账户码，比如 CC-XXXXXX-XXXXXX", ORANGE)
            return
        # ★ 密钥**不在这里硬拦**：2026-10-24 之前注册的老账号根本没有密钥，
        #   客户端拦下来就等于把那批用户永久锁在门外。空着照样发，由服务端说了算
        #   （新账号会回 account_key_required，界面再把光标放到密钥框上）。
        self._set_login_status("正在登录…", DIM)
        if self._login_btn is not None:
            try:
                self._login_btn.configure(state=tk.DISABLED, text="登录中…")
            except Exception:
                pass
        threading.Thread(target=self._login_worker, args=(raw, raw_key), daemon=True).start()

    @staticmethod
    def _login_worker(raw, raw_key):
        """后台线程：绑账号。

        ★ raw 直接进 login_with_account_code，不 strip、不大写、不补横线——
          归一化是服务器的事（见本文件开头第 1 条）。密钥只 trim。
        ★ 密钥**绝不进日志**：log / 崩溃上报 / 对话历史里都不该出现它。"""
        import json

        from server.api import login_with_account_code
        try:
            res = json.loads(login_with_account_code(raw, raw_key))
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
            self._login_recovery = False       # 恢复成功了，退出时别再提示"没有账号"
            synced = bool(res.get("commands_synced"))
            self._set_login_status(
                "登录成功，这台电脑已经切到这个账号了。" if synced else
                "登录成功，但命令行配置没改成功，终端里可能还是旧账号。",
                GREEN if synced else ORANGE)
            # ★ 账户码不整串写进对话：这段会进模型上下文和历史记录。
            #   打码不影响任何功能，用户要完整值去「🔐 账户信息」页复制。
            self.write("[系统] 已登录：%s" % _mask_secret(res.get("account_code") or "", 4))
            if synced:
                self.write("[系统] 命令行配置已同步（~/.claude.json 和 "
                           "~/.claude/settings.json），重开一次 Claude Code 就用新账号。")
            else:
                self.write("[系统] 命令行配置没改成功：%s" % (res.get("commands_note") or ""))
                self.write("[系统] 要让 Claude Code 也用这个账号，"
                           "重新点一次「一键安装 Claude Code」。")
            # 账号变了 → 余额/用量要按新账号重查（后台线程，别卡界面）
            threading.Thread(target=refresh_balance, daemon=True).start()
            # 「账户信息」页上的两串凭据也跟着换了
            self._refresh_account_panel()
            return

        if res.get("rate_limited"):
            wait = int(res.get("retry_after") or 60)
            self._set_login_status(res.get("error") or "试得太频繁了，等一会儿再试", ORANGE)
            self._lock_login_button(wait)
            return

        # ★ 缺密钥 / 密钥错都只是"这个框还差点东西"，不是"账号有问题"：
        #   说清楚，把光标放到该改的那个框里，别让用户以为是账号码抄错了。
        if res.get("need_account_key") or res.get("bad_account_key"):
            color = ORANGE if res.get("need_account_key") else RED
            self._set_login_status(res.get("error") or "还需要账户密钥", color)
            if self._login_key_entry is not None:
                try:
                    self._login_key_entry.focus_set()
                except Exception:
                    pass
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
