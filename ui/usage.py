# -*- coding: utf-8 -*-
"""UsageMixin：用量信息页 —— 余额、各模型 token 用量图表、充值入口。

这个页面是程序里**唯一**跟钱有关的地方：主窗口不显示余额、也不放充值按钮，
只有余额真的见底时，窗口底部会问一句"是否立即充值"（见 window.py 的
offer_recharge），用户点了才会弹充值窗。

图表是直接画在 tk.Canvas 上的柱状图，几个刻意的决定：

- **单色**。柱子表达的是"每个模型用了多少 token"，模型名就写在轴线下面，
  颜色不承担区分职责 —— 所以只用一种蓝（config.SERIES），也就不需要图例。
- **柱顶直接标数值**，读图不用去对 Y 轴；鼠标悬停还会出一个提示框。
- **下面配一张明细表**：所有数字不靠悬停也拿得到（截图、读屏都不吃亏）。
- 最多 8 根柱子，多出来的折进「其他」，不为了塞更多数据去造新颜色。

配色按 dataviz 规范选过：蓝 #3987e5 在卡片底色 #1f1f1f 上通过色带、彩度、
对比度三项校验（≥3:1）；悬停用同色阶提亮一档 #5598e7。
"""

import threading
import tkinter as tk

import customtkinter as ctk

from agent_core import refresh_balance
from config import (
    AXIS,
    BG,
    BLUE,
    DIM,
    GRID,
    ORANGE,
    RED,
    SERIES,
    SERIES_HOVER,
    SURFACE,
    TEXT,
    TIP_BG,
)
from helpers import (
    _fit_text,
    _fmt_tokens,
    _fmt_tokens_short,
    _fmt_yuan,
    _nice_ticks,
    _norm_by_model,
    _text_width,
    _to_float,
)
from server.identity import load_identity
from ui.fonts import (
    S_AXIS,
    S_BODY,
    S_CANVAS,
    S_HEAD,
    S_HERO,
    S_SMALL,
    S_TINY,
    S_TITLE,
    canvas_scale,
    csize,
    font,
    spec,
)

MAX_BARS = 8          # 图表最多画几根柱子，多出来的折进「其他」
_ROUND = 4            # 柱顶圆角半径：4px 圆角 + 基线那头直角
_LABEL_LIFT = 13      # 柱顶那行数值离柱顶的距离（字号涨了，比原来多让 1px）
_CAP_GAP = 22         # 提示框至少要抬离柱顶这么高，否则会压住柱顶那行数值


class UsageMixin:
    # ---------- 建页 ----------

    def _build_usage_view(self, parent):
        """把"用量信息"页建出来（默认不显示，由 show_view 切换）"""
        view = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        self.usage_view = view
        self._usage_data = {}          # 最近一次拿到的 {"balance":…, "usage":…}
        self._account_code = ""        # 当前账号的账户码（复制按钮用）
        self._rows = []                # 当前期间的柱子数据（已折过「其他」）
        self._bars = []                # 画布上每根柱子的位置，悬停时用来命中
        self._hover = -1               # 当前悬停的柱子下标（-1 = 没有）
        self._range = "today"          # 当前期间：today / month

        # ---------- 标题行：标题 + 充值 + 刷新 ----------
        head = ctk.CTkFrame(view, fg_color="transparent")
        head.pack(fill=tk.X, padx=18, pady=(16, 10))

        ctk.CTkLabel(
            head, text="用量信息",
            font=font(S_TITLE, "bold"),
            text_color=TEXT,
        ).pack(side=tk.LEFT)

        ctk.CTkButton(
            head, text="🔄 刷新", width=88, height=34,
            font=font(S_SMALL),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.on_usage_refresh,
        ).pack(side=tk.RIGHT)

        ctk.CTkButton(
            head, text="🔑 换设备登录", width=124, height=34,
            font=font(S_SMALL),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.show_login_dialog,
        ).pack(side=tk.RIGHT, padx=(0, 10))

        ctk.CTkButton(
            head, text="💳 充值", width=104, height=34,
            font=font(S_SMALL, "bold"),
            fg_color="#2f6f4f", hover_color="#3a8560",
            command=self.on_usage_recharge,
        ).pack(side=tk.RIGHT, padx=(0, 10))

        # ---------- 余额卡：一个大数字 + 两个消耗小格 ----------
        card = ctk.CTkFrame(view, fg_color=SURFACE, corner_radius=10)
        card.pack(fill=tk.X, padx=18, pady=(0, 10))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill=tk.X, padx=18, pady=14)

        left = ctk.CTkFrame(inner, fg_color="transparent")
        left.pack(side=tk.LEFT, anchor="n")
        ctk.CTkLabel(
            left, text="账户余额", font=font(S_SMALL),
            text_color=DIM, anchor="w",
        ).pack(anchor="w")
        self.balance_value = ctk.CTkLabel(
            left, text="—", anchor="w", text_color=TEXT,
            font=font(S_HERO, "bold"),
        )
        self.balance_value.pack(anchor="w", pady=(0, 4))

        # 账户码这一行：码 + 一个复制按钮。账户码是**换设备时唯一的凭据**，
        # 用户多半是拿手机拍照/手抄，给个复制按钮省得抄错一位就登不上。
        note_row = ctk.CTkFrame(left, fg_color="transparent")
        note_row.pack(anchor="w")
        self.balance_note = ctk.CTkLabel(
            note_row, text="", anchor="w",
            font=font(S_SMALL, mono=True), text_color=DIM,
        )
        self.balance_note.pack(side=tk.LEFT)

        self.copy_code_btn = ctk.CTkButton(
            note_row, text="复制", width=52, height=22, corner_radius=4,
            font=font(S_TINY),
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.on_copy_account_code,
        )
        self.copy_code_btn.pack(side=tk.LEFT, padx=(8, 0))

        right = ctk.CTkFrame(inner, fg_color="transparent")
        right.pack(side=tk.RIGHT, anchor="n")
        self.stat_today = self._stat_tile(right, "今日消耗")
        self.stat_month = self._stat_tile(right, "本月消耗")

        # ---------- 期间切换（今日 / 本月）----------
        ctrl = ctk.CTkFrame(view, fg_color="transparent")
        ctrl.pack(fill=tk.X, padx=18, pady=(0, 6))

        ctk.CTkLabel(
            ctrl, text="各模型 token 用量",
            font=font(S_HEAD, "bold"),
            text_color=TEXT,
        ).pack(side=tk.LEFT)

        self.chart_note = ctk.CTkLabel(
            ctrl, text="", font=font(S_SMALL),
            text_color=DIM,
        )
        self.chart_note.pack(side=tk.LEFT, padx=(12, 0))

        seg = ctk.CTkFrame(ctrl, fg_color="transparent")
        seg.pack(side=tk.RIGHT)
        self._range_buttons = {}
        for key, label in (("today", "今日"), ("month", "本月")):
            btn = ctk.CTkButton(
                seg, text=label, width=70, height=30, corner_radius=6,
                font=font(S_SMALL),
                command=lambda k=key: self.set_usage_range(k),
            )
            btn.pack(side=tk.LEFT, padx=(8, 0))
            self._range_buttons[key] = btn

        # ---------- 图表卡片（画布跟着卡片一起伸缩）----------
        chart_card = ctk.CTkFrame(view, fg_color=SURFACE, corner_radius=10)
        chart_card.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 10))

        self.chart = tk.Canvas(chart_card, bg=SURFACE, highlightthickness=0, bd=0)
        self.chart.pack(fill=tk.BOTH, expand=True, padx=4, pady=(6, 4))
        self.chart.bind("<Configure>", lambda e: self._draw_chart())
        self.chart.bind("<Motion>", self._on_chart_motion)
        self.chart.bind("<Leave>", lambda e: self._set_hover(-1))

        # ---------- 明细表 ----------
        table = ctk.CTkFrame(view, fg_color=SURFACE, corner_radius=10)
        table.pack(fill=tk.X, padx=18, pady=(0, 16))
        self.table_rows = ctk.CTkFrame(table, fg_color="transparent")
        self.table_rows.pack(fill=tk.X, padx=16, pady=12)
        self.table_rows.grid_columnconfigure(0, weight=1)     # 模型名那一列吃掉多余宽度

        self._render_table()
        self._paint_range_buttons()
        return view

    def _stat_tile(self, parent, title):
        """一个小方块：标题 + 金额 + 底下那行 token 数"""
        tile = ctk.CTkFrame(parent, fg_color="transparent")
        tile.pack(side=tk.LEFT, padx=(22, 0), anchor="n")
        ctk.CTkLabel(
            tile, text=title, anchor="e",
            font=font(S_SMALL), text_color=DIM,
        ).pack(anchor="e")
        value = ctk.CTkLabel(
            tile, text="—", anchor="e",
            font=font(S_HEAD + 2, "bold"), text_color=TEXT,
        )
        value.pack(anchor="e")
        sub = ctk.CTkLabel(
            tile, text="—", anchor="e",
            font=font(S_SMALL, mono=True), text_color=DIM,
        )
        sub.pack(anchor="e")
        return {"value": value, "sub": sub}

    # ---------- 数据回流 ----------

    def usage_view_update(self, payload):
        """余额 / 用量数据回来了（主线程调用）→ 更新这一页。

        payload 就是 bus 里 ("balance", {...}) 的那份数据，形如
        {"balance": {"yuan": "5.00", "account_code": "CC-…"}, "usage": {"today": …, "month": …}}

        ★ 两块各自可能被服务器**限流**（429，带 rate_limited 标记）。
          限流不是"余额变成 0 了"、也不是"服务挂了"：那一块**保持原样不动**，
          不弹错、不归零，用户看到的就是上一次的真实数字。"""
        payload = payload or {}
        bal = payload.get("balance") or {}
        usage = payload.get("usage") or {}

        # 限流的那一块用**上一份好数据**顶上，别拿一个空壳去把界面擦白
        merged = dict(self._usage_data or {})
        if not bal.get("rate_limited"):
            merged["balance"] = bal
        if not usage.get("rate_limited"):
            merged["usage"] = usage
        self._usage_data = merged

        if not bal.get("rate_limited"):
            self._update_balance_card(bal)
        if not usage.get("rate_limited"):
            self._update_usage_tiles(usage)

        self._render_usage()

    def _update_balance_card(self, bal):
        """余额卡：大数字 + 账户码那行"""
        yuan = _to_float(bal.get("yuan"))

        # --- 余额大数字：低额才上色，平时就是白字 ---
        if yuan is None:
            self.balance_value.configure(text="—", text_color=DIM)
        elif yuan <= 0:
            self.balance_value.configure(text=f"¥{yuan:.2f}", text_color=RED)
        elif yuan < 2:
            self.balance_value.configure(text=f"¥{yuan:.2f}", text_color=ORANGE)
        else:
            self.balance_value.configure(text=f"¥{yuan:.2f}", text_color=TEXT)

        code = bal.get("account_code") or (load_identity() or {}).get("account_code") or "—"
        self._account_code = code
        if bal.get("error"):
            note = f"账户码 {code} · 余额没查到（{str(bal['error'])[:30]}）"
            if self.copy_code_btn is not None:
                self.copy_code_btn.configure(state=tk.DISABLED)
        else:
            note = f"账户码 {code} · 换电脑时用它登录"
            if self.copy_code_btn is not None:
                self.copy_code_btn.configure(state=tk.NORMAL)
        self.balance_note.configure(text=note)

    def _update_usage_tiles(self, usage):
        """今日 / 本月消耗两个格子"""
        for key, tile in (("today", self.stat_today), ("month", self.stat_month)):
            block = usage.get(key) or {}
            tile["value"].configure(text=_fmt_yuan(block.get("yuan")))
            tokens = block.get("tokens")
            tile["sub"].configure(
                text=f"{_fmt_tokens(tokens)} token" if tokens is not None else "—"
            )

    def set_usage_range(self, key):
        """切「今日 / 本月」"""
        if key not in ("today", "month") or key == self._range:
            return
        self._range = key
        self._render_usage()

    def _render_usage(self):
        """按当前期间重取柱子数据 → 重画图表和明细表"""
        self._paint_range_buttons()

        block = (self._usage_data.get("usage") or {}).get(self._range) or {}
        rows = _norm_by_model(block)

        # 柱子太多就折「其他」：不造新颜色，也不让柱子细到看不见
        if len(rows) > MAX_BARS:
            head_rows = rows[: MAX_BARS - 1]
            rest = rows[MAX_BARS - 1:]
            head_rows.append({
                "model": "其他",
                "tokens": sum(r["tokens"] for r in rest),
                "yuan": sum(r["yuan"] or 0 for r in rest) or None,
            })
            rows = head_rows
        self._rows = rows

        # 服务器没给按模型的明细时（by_model 缺失或为空），明说一句，
        # 别让用户以为"只有一个模型"。正常情况下走不到这里。
        if rows and rows[0]["model"] == "全部模型":
            self.chart_note.configure(text="（服务器暂未按模型拆分，先看总量）")
        else:
            self.chart_note.configure(text="")

        self._render_table()
        self._draw_chart()

    def _paint_range_buttons(self):
        for key, btn in self._range_buttons.items():
            active = (key == self._range)
            btn.configure(
                fg_color=BLUE if active else "#2f2f2f",
                hover_color=BLUE if active else "#3a3a3a",
                text_color="#ffffff" if active else DIM,
            )

    # ---------- 明细表 ----------

    def _render_table(self):
        """明细表：不靠悬停也能看到每个模型的数字"""
        for child in self.table_rows.winfo_children():
            child.destroy()

        for col, title in enumerate(("模型", "Token", "花费")):
            ctk.CTkLabel(
                self.table_rows, text=title, anchor="w" if col == 0 else "e",
                font=font(S_SMALL), text_color=DIM,
            ).grid(row=0, column=col, sticky="ew" if col == 0 else "e", padx=(0, 12))

        # 发丝分隔线：一条 1px 的实心细线，比画框线轻
        ctk.CTkFrame(self.table_rows, height=1, fg_color=GRID).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(8, 8)
        )

        if not self._rows:
            ctk.CTkLabel(
                self.table_rows, text="暂时没有数据", anchor="w",
                font=font(S_SMALL), text_color=DIM,
            ).grid(row=2, column=0, columnspan=3, sticky="ew")
            return

        for i, row in enumerate(self._rows):
            r = i + 2
            ctk.CTkLabel(
                self.table_rows, text=row["model"], anchor="w",
                font=font(S_BODY), text_color=TEXT,
            ).grid(row=r, column=0, sticky="ew", pady=3)
            ctk.CTkLabel(
                self.table_rows, text=f"{row['tokens']:,}", anchor="e", width=124,
                font=font(S_BODY, mono=True), text_color=DIM,
            ).grid(row=r, column=1, sticky="e", pady=3, padx=(0, 12))
            ctk.CTkLabel(
                self.table_rows,
                text=_fmt_yuan(row["yuan"]) if row.get("yuan") is not None else "—",
                anchor="e", width=96,
                font=font(S_BODY, mono=True), text_color=DIM,
            ).grid(row=r, column=2, sticky="e", pady=3)

    # ---------- 图表 ----------

    def _draw_chart(self):
        """重画整个图表（数据变了、窗口大小变了都走这里）

        ★ 画布上的长度一律按"逻辑像素"写，画之前统一乘 k = canvas_scale()。
          画布是 tk.Canvas，CTk 不会替它缩放；屏幕 150% 时控件都放大了 1.5 倍，
          不乘这一下，图上的字和线会明显比旁边小一圈。字号那部分由 fonts.spec()
          自己乘（负数=像素），这里只管留白、粗细、间距这些长度。"""
        c = self.chart
        try:
            c.delete("all")
        except Exception:
            return
        self._bars = []
        self._hover = -1

        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return                        # 还没排版好，Configure 会再叫一次

        k = canvas_scale()
        if not self._rows:
            c.create_text(
                w / 2, h / 2, text="暂时没有用量数据", fill=DIM,
                font=spec(S_CANVAS),
            )
            return

        # 四边留白：左边给 Y 轴刻度，下面留一行放模型名
        pad_l, pad_r = int(66 * k), int(20 * k)
        pad_t, pad_b = int(30 * k), int(48 * k)
        label_lift = int(_LABEL_LIFT * k)
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        base = pad_t + plot_h

        vmax = max(r["tokens"] for r in self._rows)
        step, top = _nice_ticks(vmax, 4)

        # --- 网格线 + Y 轴刻度（圆整数，等宽字体对齐）---
        i = 0
        while step * i <= top + 1e-9 and i <= 12:
            val = step * i
            y = base - plot_h * (val / top)
            c.create_line(pad_l, y, pad_l + plot_w, y, fill=AXIS if i == 0 else GRID, width=1)
            c.create_text(
                pad_l - 10 * k, y, text=_fmt_tokens_short(int(val)), anchor="e",
                fill=DIM, font=spec(S_AXIS, mono=True),
            )
            i += 1

        # --- 柱子：最多 24px 粗，而且不铺满整个宽度 ---
        # 只有两三根柱子时，如果按"平均分配"画，它们会被拉得很开很难看。
        # 所以每个槽最多 120px，整组居中；剩下的宽度留给留白。
        band = min(plot_w / len(self._rows), 120.0 * k)
        group_w = band * len(self._rows)
        x0 = pad_l + (plot_w - group_w) / 2
        bw = max(6 * k, min(24 * k, band * 0.46))
        for idx, row in enumerate(self._rows):
            cx = x0 + band * (idx + 0.5)
            x1, x2 = cx - bw / 2, cx + bw / 2
            y1 = base - plot_h * (row["tokens"] / top if top else 0)

            self._draw_bar(x1, y1, x2, base, f"bar{idx}", int(_ROUND * k), k)
            c.create_text(
                cx, max(y1 - label_lift, 12 * k), text=_fmt_tokens_short(row["tokens"]),
                fill=TEXT, font=spec(S_CANVAS, mono=True), tags=f"cap{idx}",
            )
            # 模型名按"槽有多宽"截断，不是按字符数 —— 中英混排长度差太多
            c.create_text(
                cx, base + 12 * k,
                text=_fit_text(row["model"], band - 12 * k, csize(S_AXIS)), anchor="n",
                fill=DIM, font=spec(S_AXIS),
            )
            self._bars.append({
                "i": idx, "cx": cx, "y1": y1, "row": row,
                # 命中区按整个"槽"算，不是按 24px 的柱子算 —— 鼠标不用瞄得很准
                "hit1": x0 + band * idx, "hit2": x0 + band * (idx + 1),
            })

    def _draw_bar(self, x1, y1, x2, y2, tag, radius, k=1.0):
        """一根柱子：柱顶 4px 圆角，贴着基线那头是直角。

        ★ 柱子太矮（不到 10px）时直接画平头方块 —— 否则那点高度全被圆角吃掉，
        看起来像颗药丸，反而把 900 token 这种小数值画夸张了。"""
        c = self.chart
        h = y2 - y1
        if h < 10 * k:
            c.create_rectangle(x1, min(y1, y2 - 2 * k), x2, y2,
                               fill=SERIES, outline="", tags=tag)
            return
        c.create_rectangle(x1, y1 + radius, x2, y2, fill=SERIES, outline="", tags=tag)
        c.create_rectangle(x1, y1, x1 + radius, y1 + radius, fill=SERIES, outline="", tags=tag)
        c.create_rectangle(x2 - radius, y1, x2, y1 + radius, fill=SERIES, outline="", tags=tag)
        c.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90,
                     fill=SERIES, outline="", style=tk.PIESLICE, tags=tag)
        c.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90,
                     fill=SERIES, outline="", style=tk.PIESLICE, tags=tag)

    # ---------- 悬停提示 ----------

    def _on_chart_motion(self, event):
        hit = -1
        for b in self._bars:
            if b["hit1"] <= event.x <= b["hit2"]:
                hit = b["i"]
                break
        self._set_hover(hit)

    def _set_hover(self, idx):
        """悬停：那根柱子提亮一档 + 出提示框（值在前，名字在后）"""
        if idx == self._hover:
            return
        self._hover = idx
        try:
            for b in self._bars:
                self.chart.itemconfig(
                    f"bar{b['i']}", fill=SERIES_HOVER if b["i"] == idx else SERIES
                )
            self.chart.delete("tip")
        except Exception:
            return
        if 0 <= idx < len(self._bars):
            self._draw_tip(self._bars[idx])

    def _draw_tip(self, bar):
        row = bar["row"]
        value = f"{row['tokens']:,} token"
        money = _fmt_yuan(row["yuan"]) if row.get("yuan") is not None else None
        label = row["model"] + (f" · {money}" if money else "")

        # 框的尺寸全部从字号和缩放推出来 —— 换字号、换屏幕 DPI，提示框自己跟着长，
        # 不会把文字切掉（_text_width / csize 是同一套像素口径）
        k = canvas_scale()
        pad = int(10 * k)
        row1 = pad + int(6 * k)                             # 第一行基线（相对框顶）
        row2 = row1 + csize(S_AXIS) + int(8 * k)            # 第二行基线
        box_h = row2 + pad + int(6 * k)
        box_w = max(_text_width(value, csize(S_CANVAS)),
                    _text_width(label, csize(S_AXIS))) + pad * 2 + int(20 * k)
        w, h = self.chart.winfo_width(), self.chart.winfo_height()
        edge = int(6 * k)

        x = min(max(bar["cx"] - box_w / 2, edge), max(edge, w - box_w - edge))
        y = bar["y1"] - box_h - int(_CAP_GAP * k)           # 让开柱顶那行数值
        if y < edge:                                        # 柱子太高 → 提示框改放柱顶下方
            y = bar["y1"] + int(_CAP_GAP * k) - int(6 * k)
        y = min(y, max(edge, h - box_h - edge))

        c = self.chart
        c.create_rectangle(x, y, x + box_w, y + box_h, fill=TIP_BG, outline=AXIS, tags="tip")
        # 系列色小线段做 key（提示框里不用整块色块，太重）
        c.create_line(x + pad, y + row1, x + pad + int(10 * k), y + row1,
                      fill=SERIES_HOVER, width=2, tags="tip")
        c.create_text(x + pad + int(16 * k), y + row1, text=value, anchor="w",
                      fill=TEXT, font=spec(S_CANVAS, mono=True), tags="tip")
        c.create_text(x + pad, y + row2, text=label, anchor="w",
                      fill=DIM, font=spec(S_AXIS), tags="tip")

    # ---------- 两个按钮 ----------

    def on_usage_refresh(self):
        """刷新按钮：查一次余额 + 用量。

        ★ 网络请求必须丢到后台线程，别在主线程里阻塞（界面会卡住）。"""
        self.write("[系统] 正在刷新余额和用量…")
        threading.Thread(target=refresh_balance, daemon=True).start()

    def on_usage_recharge(self):
        """充值入口就在这一页 —— 用户自己点开的，弹窗不算打扰"""
        self.show_recharge_dialog(force=True)

    def on_copy_account_code(self):
        """把账户码复制到剪贴板。

        ★ 这是**换设备时唯一的凭据**，手抄最容易错一位（O 和 0、I 和 1 本来就
          容易看混），所以宁可直接复制。"""
        code = self._account_code or (load_identity() or {}).get("account_code") or ""
        if not code or code == "—":
            self.write("[系统] 还没拿到账户码，稍后再试")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
        except Exception:
            self.write("[系统] 复制失败，手动抄一下吧")
            return
        self.write("[系统] 账户码已复制。换电脑时在「用量信息」页点「换设备登录」，把这串填进去。")

        # 按钮上闪一下"已复制"，1.5 秒后变回去——比弹窗轻，也不挡视线
        btn = self.copy_code_btn
        if btn is None:
            return
        try:
            btn.configure(text="已复制")
        except Exception:
            return

        def restore():
            try:
                if self.copy_code_btn is not None:
                    self.copy_code_btn.configure(text="复制")
            except Exception:
                pass
        self.root.after(1500, restore)
