# -*- coding: utf-8 -*-
"""UsageMixin：用量信息页 —— 余额、各模型 token 用量图表、充值入口。

这个页面是程序里**唯一**跟钱有关的地方：主窗口不显示余额、也不放充值按钮，
只有余额真的见底时，窗口底部会问一句"是否立即充值"（见 window.py 的
offer_recharge），用户点了才会弹充值窗。

图表是直接画在 tk.Canvas 上的柱状图，几个刻意的决定：

- **单色**。柱子表达的是"每个时间桶 / 每个模型用了多少 token"，刻度就写在
  轴线下面，颜色不承担区分职责 —— 所以只用一种蓝（config.SERIES），不需要图例。
- **图表按时间分布**：今日按小时（00:00…当前小时）、本月按天（10-01…今天）。
  服务端没给 series 时退回按模型画，老接口也不白屏。
- **柱顶直接标数值**，读图不用去对 Y 轴；鼠标悬停还会出一个提示框。
- **下面配一张按模型的明细表**：所有数字不靠悬停也拿得到（截图、读屏都不吃亏）。
- 时间桶多时只每隔几个画一个刻度；明细表最多 8 行，多出来的折进「其他」。

配色按 dataviz 规范选过：蓝 #3987e5 在卡片底色 #1f1f1f 上通过色带、彩度、
对比度三项校验（≥3:1）；悬停用同色阶提亮一档 #5598e7。
"""

import threading
import time
import tkinter as tk

import customtkinter as ctk

from agent_core import refresh_balance
from config import (
    APP_VERSION,
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
    _mask_secret,
    _nice_ticks,
    _norm_by_model,
    _norm_series,
    _text_width,
    _to_float,
)
from server.identity import load_identity
from ui.fonts import (
    S_AXIS,
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
        self._rows = []                # 当前期间的模型明细（挑模型 / 算合计用）
        self._charts = []              # 两张图：flash / pro，各一份画布和状态
        self._usage_stamp = ""         # 用量数据的到达时间（HH:MM），图表右上角用
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

        # ---------- 版本号：贴在页面最底下 ----------
        # ★ 必须在这张会 expand 的图表卡片之前 pack(side=BOTTOM)，
        #   否则它会被挤到图表下面、窗口一矮就看不见了。
        #   报问题时让用户说这个版本号，比问"你什么时候下的"靠谱。
        ctk.CTkLabel(
            view, text=f"v{APP_VERSION} · Claude Code 助手",
            font=font(S_TINY), text_color=DIM,
        ).pack(side=tk.BOTTOM, pady=(0, 8))

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

        # 账户码这一行：**打码**的码 + 一个复制按钮。完整凭证在「🔐 账户信息」页
        # （打码 + 眼睛 + 红字警告）；这里只留前 4 位让人认得出是哪个账号，
        # 不显示明文 —— 余额页是用户最容易截给别人看的地方。
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
            ctrl, text="各模型 token 用量（按时间）",
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

        # ---------- 两张图：flash / pro 各一张时间分布图 ----------
        # 上下各占一半；每张卡片标题就是模型名，右边写本期间的合计。
        for idx in range(2):
            self._charts.append(self._build_chart_card(view, idx))

        self._paint_range_buttons()
        return view

    def _build_chart_card(self, parent, idx):
        """建一张图表卡：模型名 + 合计 + 画布（上下两张共用这个函数）"""
        card = ctk.CTkFrame(parent, fg_color=SURFACE, corner_radius=10)
        card.pack(fill=tk.BOTH, expand=True, padx=18,
                  pady=(0, 8 if idx == 0 else 16))

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill=tk.X, padx=14, pady=(8, 0))
        title = ctk.CTkLabel(
            head, text="—", font=font(S_HEAD, "bold"), text_color=TEXT,
        )
        title.pack(side=tk.LEFT)
        total = ctk.CTkLabel(
            head, text="", font=font(S_SMALL, mono=True), text_color=DIM,
        )
        total.pack(side=tk.RIGHT)

        canvas = tk.Canvas(card, bg=SURFACE, highlightthickness=0, bd=0)
        canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))

        chart = {
            "canvas": canvas, "title": title, "total": total,
            "model": "", "series": [], "series_is_time": False,
            "bars": [], "hover": -1,
        }
        canvas.bind("<Configure>", lambda e, c=chart: self._draw_chart(c))
        canvas.bind("<Motion>", lambda e, c=chart: self._on_chart_motion(c, e))
        canvas.bind("<Leave>", lambda e, c=chart: self._set_hover(c, -1))
        return chart

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
            self._usage_stamp = time.strftime("%H:%M")   # 记的是"数据到达时间"
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
        # ★ 这里只露前 4 位：完整凭证在「🔐 账户信息」页（打码 + 眼睛 + 红字警告）。
        #   复制按钮照旧可用 —— 不显示明文也能复制，用户不用把码抄下来。
        shown = _mask_secret(code, keep=4) if code != "—" else "—"
        if bal.get("error"):
            note = f"账户码 {shown} · 余额没查到（{str(bal['error'])[:30]}）"
            if self.copy_code_btn is not None:
                self.copy_code_btn.configure(state=tk.DISABLED)
        else:
            note = f"账户码 {shown} · 完整内容在「账户信息」页"
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
        """按当前期间重取两个模型的序列 → 重画两张图"""
        self._paint_range_buttons()

        block = (self._usage_data.get("usage") or {}).get(self._range) or {}
        self._rows = _norm_by_model(block)

        flash, pro = self._pick_models(block)
        for chart, model in zip(self._charts, (flash, pro)):
            self._fill_chart(chart, model, block)

        note = f"更新于 {self._usage_stamp}" if self._usage_stamp else ""
        # 老服务端只给总量序列、没给按模型的序列 → 两张图会退化成各一根柱子，
        # 明说一句，别让人以为时间轴坏了。
        if note and not any(c["series_is_time"] for c in self._charts):
            note += " · 服务器暂未按模型拆分时间"
        self.chart_note.configure(text=note)

    def _pick_models(self, block):
        """挑出 flash / pro 两个模型名。

        优先用本次数据里真实出现的模型名；某个模型这个期间没用量时，
        用身份文件里的 fast_model / model 补上，保证两张图始终都在。"""
        ident = load_identity() or {}
        series_map = block.get("series_by_model") or {}
        names = list(dict.fromkeys(
            list(series_map.keys())
            + [r["model"] for r in self._rows]
            + [ident.get("fast_model"), ident.get("model")]
        ))
        names = [n for n in names if n]

        def pick(word, fallback):
            for n in names:
                if word in n.lower():
                    return n
            return fallback

        flash = pick("flash", ident.get("fast_model") or "deepseek-flash")
        pro = pick("pro", ident.get("model") or "deepseek-v4-pro")
        if pro == flash:                       # 只有一个模型名时别让两张图重名
            pro = next((n for n in names if n != flash), pro + "（pro）")
        return flash, pro

    def _fill_chart(self, chart, model, block):
        """把一个模型在当前期间的数据塞进它的图表卡"""
        raw = (block.get("series_by_model") or {}).get(model)
        series = _norm_series({"series": raw}) if raw else []

        # 老服务端没给按模型的时间序列 → 退回该模型的一根总量柱子，不白屏
        if not series:
            bm = (block.get("by_model") or {}).get(model) or {}
            if bm:
                series = [{
                    "label": model,
                    "tokens": int(_to_float(bm.get("tokens")) or 0),
                    "yuan": _to_float(bm.get("yuan")),
                }]

        chart["model"] = model
        chart["series"] = series
        chart["series_is_time"] = bool(raw and series)
        chart["title"].configure(text=model)

        if series:
            tokens = sum(r["tokens"] for r in series)
            yuan = sum(r["yuan"] for r in series if r.get("yuan") is not None)
            chart["total"].configure(text=f"{_fmt_tokens(tokens)} token · {_fmt_yuan(yuan)}")
        else:
            chart["total"].configure(text="暂无数据")
        self._draw_chart(chart)

    def _paint_range_buttons(self):
        for key, btn in self._range_buttons.items():
            active = (key == self._range)
            btn.configure(
                fg_color=BLUE if active else "#2f2f2f",
                hover_color=BLUE if active else "#3a3a3a",
                text_color="#ffffff" if active else DIM,
            )

    # ---------- 图表 ----------

    def _draw_chart(self, chart):
        """重画一张图表（数据变了、窗口大小变了都走这里）

        ★ 画布上的长度一律按"逻辑像素"写，画之前统一乘 k = canvas_scale()。
          画布是 tk.Canvas，CTk 不会替它缩放；屏幕 150% 时控件都放大了 1.5 倍，
          不乘这一下，图上的字和线会明显比旁边小一圈。字号那部分由 fonts.spec()
          自己乘（负数=像素），这里只管留白、粗细、间距这些长度。"""
        c = chart["canvas"]
        try:
            c.delete("all")
        except Exception:
            return
        chart["bars"] = []
        chart["hover"] = -1

        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return                        # 还没排版好，Configure 会再叫一次

        k = canvas_scale()
        series = chart["series"]
        if not series:
            c.create_text(
                w / 2, h / 2, text="暂时没有用量数据", fill=DIM,
                font=spec(S_CANVAS),
            )
            return

        # 四边留白：左边给 Y 轴刻度，下面留一行放时间刻度 / 模型名
        pad_l, pad_r = int(66 * k), int(20 * k)
        pad_t, pad_b = int(30 * k), int(48 * k)
        label_lift = int(_LABEL_LIFT * k)
        plot_w = max(20, w - pad_l - pad_r)
        plot_h = max(20, h - pad_t - pad_b)
        base = pad_t + plot_h

        vmax = max(r["tokens"] for r in series)
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
        band = min(plot_w / len(series), 120.0 * k)
        group_w = band * len(series)
        x0 = pad_l + (plot_w - group_w) / 2
        bw = max(6 * k, min(24 * k, band * 0.46))
        n_series = len(series)
        for idx, row in enumerate(series):
            cx = x0 + band * (idx + 0.5)
            x1, x2 = cx - bw / 2, cx + bw / 2
            y1 = base - plot_h * (row["tokens"] / top if top else 0)

            # 时间轴：桶多（24 小时 / 31 天）时只画一部分刻度，别挤成一团；
            # 退回按模型时仍然每根都标（模型数量已经折到 ≤8）。
            # ★ 从**最后一根**往回数，保证当前小时 / 今天一定有标签。
            draw_label = True
            label_step = 1
            label_slot = band - 12 * k
            if chart["series_is_time"]:
                label_step = max(1, (n_series + 7) // 8)     # 最多约 8 个刻度
                draw_label = ((n_series - 1 - idx) % label_step == 0)
                # 标签是隔几个槽才画一个的，可以借用中间空着的槽宽，
                # "10-01" 这种 5 个字符就不会被截成 "10…"。
                label_slot = band * label_step - 12 * k

            self._draw_bar(c, x1, y1, x2, base, f"bar{idx}", int(_ROUND * k), k)
            # 桶多的时候柱顶数值也只标在刻度那几根上，否则 24/31 个数字会叠在一起；
            # 其余桶的准确值靠悬停提示，卡片右上角也有期间总量。
            if (not chart["series_is_time"]) or n_series <= 12 or draw_label:
                c.create_text(
                    cx, max(y1 - label_lift, 12 * k), text=_fmt_tokens_short(row["tokens"]),
                    fill=TEXT, font=spec(S_CANVAS, mono=True), tags=f"cap{idx}",
                )
            if draw_label:
                # 标签按"槽有多宽"截断，不是按字符数 —— 中英混排长度差太多
                c.create_text(
                    cx, base + 12 * k,
                    text=_fit_text(row["label"], label_slot, csize(S_AXIS)), anchor="n",
                    fill=DIM, font=spec(S_AXIS),
                )
            chart["bars"].append({
                "i": idx, "cx": cx, "y1": y1, "row": row,
                # 命中区按整个"槽"算，不是按 24px 的柱子算 —— 鼠标不用瞄得很准
                "hit1": x0 + band * idx, "hit2": x0 + band * (idx + 1),
            })

    def _draw_bar(self, canvas, x1, y1, x2, y2, tag, radius, k=1.0):
        """一根柱子：柱顶 4px 圆角，贴着基线那头是直角。

        ★ 柱子太矮（不到 10px）时直接画平头方块 —— 否则那点高度全被圆角吃掉，
        看起来像颗药丸，反而把 900 token 这种小数值画夸张了。"""
        c = canvas
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

    def _on_chart_motion(self, chart, event):
        hit = -1
        for b in chart["bars"]:
            if b["hit1"] <= event.x <= b["hit2"]:
                hit = b["i"]
                break
        self._set_hover(chart, hit)

    def _set_hover(self, chart, idx):
        """悬停：那根柱子提亮一档 + 出提示框（值在前，名字在后）"""
        if idx == chart["hover"]:
            return
        chart["hover"] = idx
        try:
            for b in chart["bars"]:
                chart["canvas"].itemconfig(
                    f"bar{b['i']}", fill=SERIES_HOVER if b["i"] == idx else SERIES
                )
            chart["canvas"].delete("tip")
        except Exception:
            return
        if 0 <= idx < len(chart["bars"]):
            self._draw_tip(chart, chart["bars"][idx])

    def _draw_tip(self, chart, bar):
        row = bar["row"]
        value = f"{row['tokens']:,} token"
        money = _fmt_yuan(row["yuan"]) if row.get("yuan") is not None else None
        label = row["label"] + (f" · {money}" if money else "")

        # 框的尺寸全部从字号和缩放推出来 —— 换字号、换屏幕 DPI，提示框自己跟着长，
        # 不会把文字切掉（_text_width / csize 是同一套像素口径）
        k = canvas_scale()
        pad = int(10 * k)
        row1 = pad + int(6 * k)                             # 第一行基线（相对框顶）
        row2 = row1 + csize(S_AXIS) + int(8 * k)            # 第二行基线
        box_h = row2 + pad + int(6 * k)
        box_w = max(_text_width(value, csize(S_CANVAS)),
                    _text_width(label, csize(S_AXIS))) + pad * 2 + int(20 * k)
        w, h = chart["canvas"].winfo_width(), chart["canvas"].winfo_height()
        edge = int(6 * k)

        x = min(max(bar["cx"] - box_w / 2, edge), max(edge, w - box_w - edge))
        y = bar["y1"] - box_h - int(_CAP_GAP * k)           # 让开柱顶那行数值
        if y < edge:                                        # 柱子太高 → 提示框改放柱顶下方
            y = bar["y1"] + int(_CAP_GAP * k) - int(6 * k)
        y = min(y, max(edge, h - box_h - edge))

        c = chart["canvas"]
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

        ★ 这一页显示的是**打码**的码（完整内容在「🔐 账户信息」页），但复制照旧给全码：
          手抄最容易错一位（O 和 0、I 和 1 本来就容易看混），不显示明文也能一键复制。
          日志里只说"已复制"，不打值 —— 日志是用户会截图的地方。"""
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
        self.write("[系统] 账户码已复制。完整凭证和找回说明在左边「🔐 账户信息」页。")

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
