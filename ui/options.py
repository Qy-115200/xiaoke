# -*- coding: utf-8 -*-
"""OptionsMixin：ask_user 的选项面板。

交互参照 Claude Code：用 CTkButton 而不是纯文本，**一步到位，没有再点一次"确定"这一说**：
  · 鼠标点一下选项 → 就是选了它，直接提交
  · ↑↓ 在选项间移动，回车提交
  · Esc 直接跳到"自己打字"
  · 最后一项永远是"✏️ 输入点别的"（点到它只是把光标送进输入框，不算回答）

键盘事件绑在输入框上（widget 级）而不是 root，这样返回 "break" 才能真正
拦掉输入框自己的光标移动行为。
"""

import re
import tkinter as tk

import customtkinter as ctk

from bus import input_event, input_holder
from config import (
    DIM,
    FREE_TEXT_LABEL,
    OPTION_BG,
    OPTION_HOVER,
    OPTION_SEL,
    OPTION_SEL_HOVER,
    OPTION_SEL_TEXT,
    PANEL,
    TEXT,
)
from helpers import _norm_options
from ui.fonts import S_BODY, S_SMALL, font


class OptionsMixin:
    # ---------- 选项列表（ask_user 的交互区）----------
    # 用 CTkButton 而不是文本，这样鼠标能点、能悬停高亮；点一下就等于选它，
    # 不再有"先选后确定"两步。键盘 ↑↓ 移动选中项，回车提交。
    # 最后一个永远是"输入点别的"。

    def _build_options_panel(self):
        """建好选项面板（默认不显示，ask_user 时才 pack 出来）

        ★ 父容器是 chat_view（对话页），不是 root —— 因为切到「用量信息」页时
          选项面板得跟着对话页一起藏起来，不能浮在用量页上。"""
        self.options_frame = ctk.CTkFrame(self.chat_view, fg_color=PANEL, corner_radius=8)

        head = ctk.CTkFrame(self.options_frame, fg_color="transparent")
        head.pack(fill=tk.X, padx=10, pady=(8, 2))

        ctk.CTkLabel(
            head, text="点一下就选好了，也可以直接在下面打字",
            font=font(S_SMALL), text_color=DIM, anchor="w",
        ).pack(side=tk.LEFT)

        self.option_rows = []        # [{"btn": ..., "text": ..., "free": bool}]
        self.option_index = 0        # 当前选中的下标

    def show_options(self, options):
        """显示可点击的选项（主线程调用）"""
        self.clear_options()                     # 先清掉上一轮的

        # 窗口缩在托盘里的话先弹出来——Agent 正卡着等回答，看不见就没法继续
        try:
            if str(self.root.state()) == "withdrawn":
                self.show_window()
        except Exception:
            pass

        # 选项面板长在"对话"页里，所以要是在用量页就切回去，
        # 否则 Agent 在等回答、用户却看不见问题
        try:
            self.show_view("chat")
        except Exception:
            pass

        options = _norm_options(options)
        for i, txt in enumerate(options):
            self._add_option_row(i + 1, txt, i)

        # ★ 最后一个永远是"输入点别的"
        self._add_option_row(len(options) + 1, FREE_TEXT_LABEL, len(options), free=True)

        self.options_frame.pack(fill=tk.X, padx=12, pady=(0, 6), before=self.entry_frame)
        self._select_option(0)                   # 默认选中第一个
        # ★ 面板一冒出来日志框就被压矮，问题可能掉到可视区下面——等布局稳定
        #   之后再滚一次底，保证用户一眼看到 Agent 问的是什么。
        self.scroll_log_to_end()
        self.entry.focus_set()                   # 焦点给输入框，键盘直接可用

    def _add_option_row(self, number, text, index, free=False):
        """加一行选项。number 是显示用的序号，text 是选项原文（回答时用它）"""
        btn = ctk.CTkButton(
            self.options_frame, text=f"{number}    {text}", anchor="w", height=40,
            corner_radius=6,
            font=font(S_BODY),
            fg_color=OPTION_BG, hover_color=OPTION_HOVER, text_color=TEXT,
            # 鼠标点一下 = 选了它并立刻提交（不用再点"确定"）
            command=lambda k=index: self._click_option(k),
        )
        btn.pack(fill=tk.X, padx=10, pady=3)
        self.option_rows.append({"btn": btn, "text": text, "free": free})

    def _click_option(self, index):
        """鼠标单击某个选项：直接当答案提交，点完这轮就结束了。

        ★ 只有"输入点别的"例外 —— 那一项不是答案，点到它只是把光标送进输入框，
          让用户自己打字，选项面板留着不关。"""
        if not self.option_rows or not (0 <= index < len(self.option_rows)):
            return
        self._select_option(index)               # 先高亮一下，点下去有个反馈
        if self.option_rows[index]["free"]:
            self._pick_free_text()
            return
        self._submit_option(index)

    def _select_option(self, index):
        """把选中高亮移到第 index 项（越界会循环）"""
        if not self.option_rows:
            return
        n = len(self.option_rows)
        index = index % n                        # 上下键到头了就绕回去
        self.option_index = index

        for k, row in enumerate(self.option_rows):
            selected = (k == index)
            row["btn"].configure(
                fg_color=OPTION_SEL if selected else OPTION_BG,
                hover_color=OPTION_SEL_HOVER if selected else OPTION_HOVER,
                text_color=OPTION_SEL_TEXT if selected else TEXT,
            )

    def confirm_option(self):
        """回车：提交当前高亮的那一项"""
        if not self.option_rows:
            return "break"
        index = self.option_index
        if self.option_rows[index]["free"]:      # 高亮停在"输入点别的" → 让用户打字
            self._pick_free_text()
            return "break"
        self._submit_option(index)
        return "break"

    def _submit_option(self, index):
        """把第 index 项当答案交给 Agent。鼠标点击和回车都走这里，保证两条路一致。"""
        if not self.option_rows or not (0 <= index < len(self.option_rows)):
            return
        answer = self.option_rows[index]["text"]
        self.write(f"\n[你] {answer}")
        self._submit_answer(answer)

    def _pick_free_text(self):
        """选到"输入点别的"：把光标放进输入框，选项留着（还能改主意点别的）"""
        self.entry.focus_set()
        self.write("[系统] 好，直接打字告诉我")

    def _submit_answer(self, text):
        """把回答交给 Agent 线程（主线程调用）"""
        self.clear_options()
        self.waiting_for_answer = False          # 防止重复触发
        input_holder["value"] = text
        input_event.set()                        # 唤醒 Agent

    def clear_options(self):
        """收起选项面板"""
        for row in getattr(self, "option_rows", []):
            try:
                row["btn"].destroy()
            except Exception:
                pass
        self.option_rows = []
        self.option_index = 0
        try:
            self.options_frame.pack_forget()
        except Exception:
            pass

    def _resolve_index(self, text):
        """用户直接打 "2" 这种序号 → 还原成对应的选项文字"""
        if not self.option_rows:
            return text
        m = re.fullmatch(r"\s*(\d{1,2})\s*[\.\、\．\)\）]?\s*", text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(self.option_rows):
                row = self.option_rows[n - 1]
                if not row["free"]:
                    return row["text"]
        return text

    # ---------- 键盘事件 ----------

    def _on_key_up(self, event=None):
        if self.option_rows:
            self._select_option(self.option_index - 1)
            return "break"                       # break：别让光标在输入框里乱跑
        return None

    def _on_key_down(self, event=None):
        if self.option_rows:
            self._select_option(self.option_index + 1)
            return "break"
        return None

    def _on_key_escape(self, event=None):
        if self.option_rows:                     # Esc = 直接跳到"自己打字"
            self._select_option(len(self.option_rows) - 1)
            self._pick_free_text()
            return "break"
        return None

    def _on_return(self, event=None):
        """回车：输入框空着就是"确定选中的选项"，有内容就是发送内容"""
        if self.option_rows and not self.entry.get().strip():
            return self.confirm_option()
        self.on_send()
        return "break"
