# -*- coding: utf-8 -*-
"""RechargeMixin：充值弹窗。

两页：第 1 页选金额 + 支付渠道，第 2 页显示付款二维码。
二维码是服务器给的现成图片（create-order 返回的 img 字段），下载下来直接画进窗口。

★ 支付渠道**不硬编码**：开窗时问一次 /api/pay-methods，服务器说开了哪些就画哪些，
  ready=false 的按钮置灰并把 reason 显示出来。将来开关渠道只改服务器，客户端不用发版。

付款是**服务端异步回调**自动加额度的，客户端不轮询：用户点「我已完成付款」→
查一次余额，比付款前多了就说明到账了。

★ 这里有两个"刷新余额"，别搞混：
    refresh_balance()  （agent_core 的）查完丢进 msg_queue，给顶栏显示用
    fetch_balance()    （server.api 的别名）查完返回 dict，用来判断到没到账
"""

import json
import threading
import tkinter as tk
import webbrowser

import customtkinter as ctk

from agent_core import refresh_balance
from bus import msg_queue
from config import BG, BLUE, BORDER, DIM, GREEN, ORANGE, RED, SERVER_URL, TEXT
from helpers import _fmt_yuan, _to_float
from images import _load_qr_image
from notify import _notify
from server.api import CHANNEL_NAMES, channel_of, create_recharge
from server.api import query_pay_methods
from server.api import refresh_balance as fetch_balance
from server.identity import load_identity
from ui.fonts import S_BODY, S_BTN, S_DIALOG, S_HEAD, S_SMALL, font


class RechargeMixin:
    # ---------- 充值 ----------
    # 两页：第 1 页选金额 + 支付渠道，第 2 页显示付款二维码。
    # 二维码是服务器给的现成图片（img 字段），下载下来直接画进窗口。
    # 付款是服务端异步回调自动加额度的，客户端不轮询，
    # 用户点「我已完成付款」→ 查一次余额，比之前多了就说明到账。

    def show_recharge_dialog(self, title="余额不足", body=None, force=False):
        """弹模态充值窗。force=True 时无视"已经弹过"的限制（用户主动点/402 错误）。"""
        # 已经开着 → 提到前面来
        if self._dlg is not None and self._dlg.winfo_exists():
            self._dlg.lift()
            self._dlg.focus_force()
            return
        if self._recharge_shown and not force:
            return                               # 自动弹的，一个会话只弹一次，别烦人
        self._recharge_shown = True

        self._dlg_title_text = title
        self._dlg_body_text = body or "要继续使用 Claude Code，请先充值："
        self._qr_image = None                    # 当前二维码的 CTkImage（防被 GC 回收）
        self._qr_box = None                      # 放二维码的 Label
        self._pay_info = None                    # 当前订单信息
        self._balance_before = None              # 付款前余额，用来判断有没有到账
        self._channels = None                    # None = 还在问；[] = 问到了但没有可用的
        self._channel = None                     # 当前选中的渠道（alipay / wechat）
        self._page = "amount"                    # 当前停在哪一页，异步结果回来时要判断

        self._open_dialog()
        self._build_amount_page()
        # 问支付渠道是网络请求，不能卡在主线程上——先按"还没问到"画出来，回来了再重画
        threading.Thread(target=self._fetch_methods_worker, daemon=True).start()

    # ---------- 支付渠道（服务器说了算） ----------

    @staticmethod
    def _fetch_methods_worker():
        """后台线程：问服务器开了哪些支付渠道"""
        try:
            data = json.loads(query_pay_methods())
        except Exception as e:
            data = {"methods": [], "error": str(e)}
        msg_queue.put(("recharge_methods", data))

    def show_recharge_methods(self, data):
        """主线程：渠道列表回来了 → 记住它，用户还停在选金额页就重画一次"""
        methods = (data or {}).get("methods") or []
        self._channels = methods
        ready = [m["channel"] for m in methods if m.get("ready")]
        if self._channel not in ready:           # 选中的没了 / 还没选过 → 挑第一个能用的
            self._channel = ready[0] if ready else None
        if (self._dlg is not None and self._dlg.winfo_exists()
                and self._page == "amount"):
            self._build_amount_page()

    def _pick_channel(self, ch):
        """点了某个支付渠道按钮 → 选中它，重画按钮的选中态"""
        self._channel = ch
        self._build_amount_page()

    # ---------- 第 1 页：选金额 ----------

    def _open_dialog(self):
        """建出空的充值窗口（只建一次，两页共用）"""
        dlg = ctk.CTkToplevel(self.root)
        self._dlg = dlg
        dlg.title("充值")
        dlg.geometry("460x620")
        dlg.minsize(460, 560)
        dlg.configure(fg_color=BG)
        dlg.transient(self.root)                 # 跟随主窗
        dlg.after(150, lambda: self._safe_grab(dlg))   # 直接 grab_set 有时会失效

        # 页面容器：切页就是清掉这里面的东西重画
        self._dlg_body = ctk.CTkFrame(dlg, fg_color="transparent")
        self._dlg_body.pack(fill=tk.BOTH, expand=True)

    def _clear_dialog_body(self):
        """清空当前页的所有控件（切页用）"""
        for w in self._dlg_body.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        # 二维码控件被销毁了，引用也要跟着清掉——否则切回选金额页后，
        # _qr_box 还指着一个已经销毁的控件，之后再用就会报错
        self._qr_box = None
        self._qr_image = None

    def _build_amount_page(self):
        """第 1 页：金额 + 支付渠道 + 账户码"""
        box = self._dlg_body
        # 重画（比如渠道列表刚回来）不能把用户已经填的金额弄丢
        prev_amount = ""
        try:
            prev_amount = self._amount_entry.get()
        except Exception:
            pass
        self._clear_dialog_body()
        self._page = "amount"

        ident = load_identity() or {}
        code = ident.get("account_code") or "—"

        ctk.CTkLabel(
            box, text=f"⚠️ {self._dlg_title_text}",
            font=font(S_DIALOG, "bold"),
            text_color=ORANGE,
        ).pack(pady=(22, 6))

        ctk.CTkLabel(
            box, text=self._dlg_body_text,
            font=font(S_BODY),
            text_color=TEXT, wraplength=400, justify="center",
        ).pack(pady=(0, 14))

        # --- 三个常用金额 ---
        btn_row = ctk.CTkFrame(box, fg_color="transparent")
        btn_row.pack(pady=(0, 6))
        for amount in (20, 50, 100):
            ctk.CTkButton(
                btn_row, text=f"充 ¥{amount}", width=112, height=42,
                font=font(S_BODY, "bold"),
                command=lambda a=amount: self._start_recharge(a),
            ).pack(side=tk.LEFT, padx=4)

        # --- 自定义金额 ---
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(pady=(6, 4))
        ctk.CTkLabel(
            row, text="其他金额：", font=font(S_SMALL),
            text_color=DIM,
        ).pack(side=tk.LEFT)
        self._amount_entry = ctk.CTkEntry(
            row, width=84, height=34, placeholder_text="元",
            font=font(S_BODY),
        )
        self._amount_entry.pack(side=tk.LEFT)
        if prev_amount:
            self._amount_entry.insert(0, prev_amount)
        self._amount_entry.bind("<Return>", lambda e: self._start_recharge(None))
        ctk.CTkButton(
            row, text="去支付", width=76, height=34,
            font=font(S_SMALL),
            command=lambda: self._start_recharge(None),
        ).pack(side=tk.LEFT, padx=(6, 0))

        # --- 支付渠道：两个按钮并列（渠道由服务器给，不硬编码） ---
        pay_row = ctk.CTkFrame(box, fg_color="transparent")
        pay_row.pack(pady=(12, 2))
        self._build_channel_buttons(pay_row)

        # --- 状态文字（创建订单中 / 报错）---
        self._dlg_status = ctk.CTkLabel(
            box, text="", font=font(S_SMALL),
            text_color=DIM, wraplength=400, justify="center",
        )
        self._dlg_status.pack(pady=(10, 6))

        # --- 账户码 ---
        ctk.CTkLabel(
            box, text="—" * 34, font=font(S_SMALL), text_color=BORDER,
        ).pack(fill=tk.X, padx=30)
        ctk.CTkLabel(
            box, text=f"账户码：{code}",
            font=font(S_HEAD, "bold", mono=True),
            text_color=TEXT,
        ).pack(pady=(10, 2))
        ctk.CTkLabel(
            box, text="（请截图保存，用于换设备找回）",
            font=font(S_SMALL), text_color=DIM,
        ).pack()

        # --- 灰色小字：技术用户入口 ---
        link = ctk.CTkLabel(
            box, text="已经是技术用户？了解其他使用方式 →",
            font=font(S_SMALL),
            text_color=DIM, cursor="hand2",
        )
        link.pack(pady=(12, 4))
        link.bind("<Button-1>", lambda e: webbrowser.open(SERVER_URL))

        ctk.CTkButton(
            box, text="关闭", width=88, height=34,
            font=font(S_BTN),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self._close_dialog,
        ).pack(pady=(4, 14))

    def _build_channel_buttons(self, parent):
        """把服务器给的渠道画成一排按钮。选中的高亮，不可用的置灰+写原因。

        ★ 为什么是按钮不是单选圈：现在的渠道是"支付宝 / 微信"两个并列，
          而且渠道数将来可能变——按钮排一行比单选圈更像"点哪个用哪个"。"""
        ctk.CTkLabel(
            parent, text="付款方式：", font=font(S_SMALL), text_color=DIM,
        ).pack(side=tk.LEFT)

        methods = self._channels
        if methods is None:                      # 还在问服务器
            ctk.CTkLabel(
                parent, text="正在获取…", font=font(S_SMALL), text_color=DIM,
            ).pack(side=tk.LEFT)
            return
        if not methods:                          # 问到了，一个能用的都没有
            ctk.CTkLabel(
                parent, text="暂时没有可用的支付方式", font=font(S_SMALL), text_color=ORANGE,
            ).pack(side=tk.LEFT)
            return

        for m in methods:
            ch = m["channel"]
            ready = bool(m.get("ready"))
            selected = (self._channel == ch)
            ctk.CTkButton(
                parent, text=m.get("name") or CHANNEL_NAMES.get(ch, ch),
                width=104, height=36,
                font=font(S_BODY, "bold" if selected else "normal"),
                fg_color=BLUE if selected else "#3a3a3a",
                hover_color="#4a7ba7" if selected else "#4a4a4a",
                text_color=TEXT if ready else DIM,
                text_color_disabled=DIM,
                state=tk.NORMAL if ready else tk.DISABLED,
                command=(lambda c=ch: self._pick_channel(c)) if ready else None,
            ).pack(side=tk.LEFT, padx=(0, 8))

        # 置灰的原因写在下面一行，不跟按钮挤在一起
        reasons = ["%s：%s" % (m.get("name") or m["channel"], m["reason"])
                   for m in methods if not m.get("ready") and m.get("reason")]
        if reasons:
            ctk.CTkLabel(
                parent, text="（" + "；".join(reasons) + "）",
                font=font(S_SMALL), text_color=DIM, wraplength=150, justify="left",
            ).pack(side=tk.LEFT)

    # ---------- 第 2 页：扫码付款 ----------

    def _build_pay_page(self, info):
        """第 2 页：二维码 + 订单号 + 「我已完成付款」"""
        box = self._dlg_body
        self._clear_dialog_body()
        self._page = "pay"

        money = info.get("money") or f"{info.get('amount') or ''}.00"
        # 渠道名以服务器回的 channel 为准；老订单只有 pay_type，那就退回按它认
        ch = channel_of(info.get("channel"), default=None) or channel_of(info.get("pay_type"))
        pay_name = CHANNEL_NAMES.get(ch, "支付宝")

        ctk.CTkLabel(
            box, text=f"请用{pay_name}扫码支付 ¥{money}",
            font=font(S_DIALOG, "bold"),
            text_color=GREEN,
        ).pack(pady=(18, 10))

        # 自动换过渠道的话得说一声——用户点的是微信，出来的却是支付宝，不说就是骗人
        if info.get("switched_from"):
            ctk.CTkLabel(
                box, text=info.get("note") or "",
                font=font(S_SMALL), text_color=ORANGE, wraplength=400, justify="center",
            ).pack(pady=(0, 6))

        # --- 二维码：外面套个白底框，深色主题下也能扫得动 ---
        qr_wrap = ctk.CTkFrame(box, fg_color="#ffffff", corner_radius=8)
        qr_wrap.pack(pady=(0, 8), padx=40, fill=tk.X)
        self._qr_box = ctk.CTkLabel(
            qr_wrap, text="二维码加载中…", width=240, height=240,
            font=font(S_SMALL), text_color="#666666",
        )
        self._qr_box.pack(padx=12, pady=12)

        self._qr_image = info.get("_image")
        if self._qr_image is not None:
            self._qr_box.configure(image=self._qr_image, text="")
        elif info.get("pay_url"):
            # 没图但有支付页链接 → 还有路可走，告诉用户走哪条
            self._qr_box.configure(text="二维码没加载出来\n点下面「打开支付页」继续付款")
        else:
            # 微信直连这条常常没有 pay_url（现在就是这么返回的），图没拿到就真没别的路了，
            # 那就如实说，别让用户对着空框干等。下面会给一个"重新加载"的按钮。
            self._qr_box.configure(text="二维码图片没取到")

        # 图没取到多半是拿图那一下网络抖了（订单本身是好的），所以给一条**重试**的路。
        # 没有这个按钮，用户只能关掉重来——那会白白多开一笔订单，
        # 而且这单的二维码本来好好的，只是没下下来。
        if self._qr_image is None and info.get("img"):
            ctk.CTkButton(
                box, text="🔄 重新加载二维码", width=150, height=30,
                font=font(S_SMALL),
                fg_color="#3a3a3a", hover_color="#4a4a4a",
                command=self._reload_qr_image,
            ).pack(pady=(6, 0))

        ctk.CTkLabel(
            box, text=f"订单号：{info.get('order_no') or '—'}",
            font=font(S_SMALL, mono=True), text_color=DIM,
        ).pack(pady=(2, 0))

        # --- 状态 ---
        self._dlg_status = ctk.CTkLabel(
            box, text="付完款点下面的按钮刷新余额 · 到账可能需要几秒",
            font=font(S_SMALL),
            text_color=DIM, wraplength=400, justify="center",
        )
        self._dlg_status.pack(pady=(8, 6))

        # --- 按钮组 ---
        ctk.CTkButton(
            box, text="✅ 我已完成付款", height=44,
            font=font(S_BTN, "bold"),
            fg_color="#2f6f4f", hover_color="#3a8560",
            command=self._on_paid_clicked,
        ).pack(fill=tk.X, padx=40, pady=(4, 6))

        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(pady=(0, 10))
        # 微信直连那条（现在的一等公民）没有 pay_url，这个按钮点了只会弹一句
        # "这单没有支付链接"。与其摆个永远点不动的按钮，不如不画。
        if info.get("pay_url"):
            ctk.CTkButton(
                row, text="打开支付页", width=116, height=32,
                font=font(S_SMALL),
                fg_color="#3a3a3a", hover_color="#4a4a4a",
                command=self._open_pay_url,
            ).pack(side=tk.LEFT, padx=4)
        ctk.CTkButton(
            row, text="重新选择", width=116, height=32,
            font=font(S_SMALL),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self._build_amount_page,
        ).pack(side=tk.LEFT, padx=4)
        ctk.CTkButton(
            row, text="关闭", width=88, height=32,
            font=font(S_SMALL),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self._close_dialog,
        ).pack(side=tk.LEFT, padx=4)

    def _open_pay_url(self):
        """在浏览器里打开支付页（扫码不方便时的备用路径）"""
        url = (self._pay_info or {}).get("pay_url")
        if url:
            webbrowser.open(url)
        else:
            self._set_dlg_status("这单没有支付链接，请直接用二维码付款", RED)

    def _reload_qr_image(self):
        """重新下一遍二维码图。订单不用重开——重开就等于多下一笔单。"""
        info = self._pay_info or {}
        url = info.get("img")
        if not url:
            self._set_dlg_status("这单没有二维码图片地址，重开充值窗试试", ORANGE)
            return
        self._set_dlg_status("正在重新加载二维码…", DIM)
        threading.Thread(target=self._reload_qr_worker, args=(url,), daemon=True).start()

    @staticmethod
    def _reload_qr_worker(url):
        try:
            img = _load_qr_image(url)
        except Exception:
            img = None
        msg_queue.put(("recharge_qr", {"image": img}))

    def show_qr_image(self, payload):
        """主线程：重试的结果回来了"""
        img = (payload or {}).get("image")
        if img is None:
            self._set_dlg_status("还是没取到。网络恢复后再点一次「重新加载二维码」", ORANGE)
            return
        self._qr_image = img
        if self._qr_box is not None:
            try:
                self._qr_box.configure(image=img, text="")
            except Exception:
                return
        self._set_dlg_status("二维码加载好了，扫一下就行", GREEN)

    def _set_dlg_status(self, text, color=DIM):
        if self._dlg_status is not None:
            try:
                self._dlg_status.configure(text=text, text_color=color)
            except Exception:
                pass

    # ---------- 创建订单 ----------

    @staticmethod
    def _safe_grab(dlg):
        """给弹窗加模态，失败也不影响（有些窗口管理器不让抢焦点）"""
        try:
            dlg.grab_set()
        except Exception:
            pass

    def _close_dialog(self):
        dlg = self._dlg
        self._dlg = None
        self._dlg_status = None
        self._qr_box = None
        if dlg is not None:
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass

    def _start_recharge(self, amount):
        """点了某个金额（或"去支付"）→ 后台创单。

        amount=None 表示金额来自输入框。"""
        ch = self._channel
        if ch is None:
            if self._channels is None:
                ch = "alipay"          # 渠道还没问到，别卡着用户：按老默认走，创单时服务器会兜
            else:
                # 问到了，但一个 ready 的都没有（服务器正在维护某条通道）
                self._set_dlg_status("现在没有可用的支付方式，稍后再试", ORANGE)
                return

        if amount is None:
            raw = ""
            try:
                raw = self._amount_entry.get().strip()
            except Exception:
                pass
            if not raw:
                self._set_dlg_status("先填一个充值金额，比如 30", ORANGE)
                return
            try:
                amount = int(float(raw))
            except ValueError:
                self._set_dlg_status(f"「{raw}」不是有效金额，填个数字吧", RED)
                return

        pay_name = CHANNEL_NAMES.get(ch, ch)
        self._set_dlg_status(f"正在创建 ¥{amount} 订单（{pay_name}）…", DIM)
        # 顺手记一下当前余额，付款后拿它对比，判断有没有真的到账
        self._balance_before = self._last_yuan
        threading.Thread(
            target=self._recharge_worker, args=(amount, ch), daemon=True
        ).start()

    @staticmethod
    def _recharge_worker(amount, channel):
        """后台线程：创单 + 下载二维码图片，结果通过队列回主线程

        ★ 网络请求一律不能放在主线程，否则界面会卡住不动。"""
        try:
            res = json.loads(create_recharge(amount, channel))
        except Exception as e:
            res = {"error": f"创建订单失败：{e}"}

        # 顺手把服务器给的二维码图片下下来（这是张现成的图片，不用自己生成）
        if not res.get("error") and res.get("img"):
            res["_image"] = _load_qr_image(res["img"])
        msg_queue.put(("recharge_result", res))

    def show_recharge_result(self, res):
        """主线程：创单结果 → 成功就切到扫码页"""
        if res.get("error"):
            err = res["error"]
            self.write(f"\n[系统] 充值失败：{err}")
            # 创建订单时可能还停在选金额页（状态标签是那一个）
            self._set_dlg_status(f"充值失败：{err[:120]}", RED)
            # 如果已经在扫码页，把错误也写进日志区，避免用户看不到
            return

        self._pay_info = res
        order_no = res.get("order_no") or "—"
        money = res.get("money") or res.get("amount")
        ch = channel_of(res.get("channel"), default=None) or channel_of(res.get("pay_type"))
        pay_name = CHANNEL_NAMES.get(ch, "")
        self.write(f"\n[系统] 充值订单已创建：{order_no}（¥{money}{'，' + pay_name if pay_name else ''}）")
        if res.get("switched_from"):
            # 用户点了微信、出来的却是支付宝 —— 日志里也得留一句，不能只在弹窗上说
            self.write(f"[系统] {res.get('note')}")
        self.write("[系统] 请用手机扫码付款，付完点「我已完成付款」")

        # 窗口还开着才切页（用户可能已经关了）
        if self._dlg is not None and self._dlg.winfo_exists():
            self._build_pay_page(res)

    # ---------- 付款后刷新余额 ----------

    def _on_paid_clicked(self):
        """点「我已完成付款」→ 后台查一次余额"""
        self._set_dlg_status("正在查询余额…", DIM)
        before = self._balance_before
        threading.Thread(target=self._check_paid_worker, args=(before,), daemon=True).start()

    @staticmethod
    def _check_paid_worker(before):
        """后台线程：查余额（服务端是异步回调加额度，所以只需查一次）

        ★ 故意不写"轮询直到到账"——支付到账时间不确定，
          轮询会一直占着连接，而且用户可能压根没付。让用户自己再点一次更实在。"""
        try:
            bal = fetch_balance()
        except Exception as e:
            bal = {"error": f"余额查询失败：{e}"}
        msg_queue.put(("recharge_paid", {"balance": bal, "before": before}))

    def show_paid_result(self, payload):
        """主线程：对比付款前后余额，告诉用户到没到账"""
        bal = (payload or {}).get("balance") or {}
        before = (payload or {}).get("before")

        if bal.get("error"):
            self._set_dlg_status(f"查询失败：{bal['error'][:100]}", RED)
            self.write(f"\n[系统] 余额查询失败：{bal['error']}")
            return

        yuan = _to_float(bal.get("yuan"))

        # 先刷新顶栏（顺手把最新的余额/用量拉一次）
        refresh_balance()

        if yuan is None:
            self._set_dlg_status("查到余额了，但数字读不出来，稍后再点一次", ORANGE)
            return

        if before is not None and yuan > before + 1e-9:
            added = yuan - before
            self._set_dlg_status(f"到账成功！本次 +{_fmt_yuan(added)}，当前余额 {_fmt_yuan(yuan)}", GREEN)
            self.write(f"\n[系统] 💰 充值到账：+{_fmt_yuan(added)}，当前余额 {_fmt_yuan(yuan)}")
            self._balance_before = yuan          # 更新基准，避免连点两次重复算
            _notify("充值到账", f"本次充值 {_fmt_yuan(added)}，当前余额 {_fmt_yuan(yuan)}")
        elif before is None:
            # 创单时没拿到基准余额（比如程序刚起来），只能说"当前多少"
            self._set_dlg_status(f"当前余额 {_fmt_yuan(yuan)}（没有付款前的数字可比对）", DIM)
            self.write(f"\n[系统] 当前余额 {_fmt_yuan(yuan)}")
            self._balance_before = yuan
        else:
            self._set_dlg_status(
                f"余额还没变（{_fmt_yuan(yuan)}）· 支付到账可能要几秒，稍等一下再点一次", ORANGE
            )
            self.write(f"\n[系统] 余额仍是 {_fmt_yuan(yuan)}，如果已经付款请稍等几秒再点一次")
