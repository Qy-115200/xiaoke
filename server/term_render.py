# -*- coding: utf-8 -*-
"""终端里那一小块"活区"怎么排版。

**只算字符、不写屏幕**：所有函数返回字符串列表，由 `server/term_chat.py` 负责
把它们画出去。这样排版规则可以在 Windows 上离线测。

★ 唯一的硬不变量：**每条活区行的显示宽度不超过终端列数**。活区靠"上移 N 行 +
从光标清到屏幕底部"来擦除，只要有一行悄悄折行，N 就少算一行，屏幕上会留下
擦不掉的字。所有裁剪都走 `clip()`，中文按两格算。

配色只用反显（`\\x1b[7m`），不指定具体颜色 —— 黑色终端、浅色终端、
`NO_COLOR` 环境都不会出问题；认不出能力时退化成行首一个 `>`。
"""

import unicodedata

from helpers import norm_options, resolve_option  # noqa: F401  （转出，调用方按名字取）

# 最后一个"自己打字"选项。不用 config.FREE_TEXT_LABEL：那里面有个 ✏️，
# 它的显示宽度在不同终端里算 1 还是 2 不一致，会破坏上面那条硬不变量。
FREE_TEXT = "输入点别的（自己打字）"

HINT = "上下键选择 · 回车确认 · Esc 自己打字"
PROMPT = "你> "
DIM = "\x1b[2m"
RESET = "\x1b[0m"
REVERSE = "\x1b[7m"


# ================= 宽度 / 裁剪 =================

def char_width(ch: str) -> int:
    """一个字符占几列。组合符号 0 格，东亚宽/全角 2 格，其余 1 格。"""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def width(text) -> int:
    return sum(char_width(ch) for ch in str(text))


def clip(text, cols: int) -> str:
    """裁到不超过 cols 列；被裁掉时末尾留一个 …（它算 1 格）。"""
    text = str(text)
    cols = max(1, int(cols))
    if width(text) <= cols:
        return text
    out = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > cols - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


# ================= 输入行 =================

def input_line(buffer, cursor, cols, prefix=PROMPT, color=True) -> str:
    """`你> 我|们` 这样一行，光标用 | 表示，超宽时从左边滚掉。"""
    buffer = str(buffer or "")
    cursor = max(0, min(int(cursor), len(buffer)))
    avail = max(1, int(cols) - width(prefix))
    body = buffer[:cursor] + "|" + buffer[cursor:]
    # 裁掉左边直到放得下（光标始终在可见范围内）
    while body and width(body) > avail:
        body = body[1:]
    body = clip(body, avail)
    line = prefix + body
    if not color:
        line = line.replace(REVERSE, "").replace(RESET, "")
    return clip_prefix(line, prefix, avail)


def clip_prefix(line, prefix, avail) -> str:
    """前缀是纯文本，不做裁剪；只要保证整行不超宽。"""
    return clip(line, width(prefix) + avail)


# ================= 选项块 =================

def _window(total: int, selected: int, max_rows: int):
    if total <= max_rows:
        return 0, total
    start = max(0, int(selected) - max_rows // 2)
    start = min(start, total - max_rows)
    return start, start + max_rows


def _option_row(text, selected, cols, color=True) -> str:
    body = clip(f"{text}", max(1, cols - 2))
    pad = max(0, cols - 2 - width(body))
    if not color:
        return ("> " if selected else "  ") + body
    if selected:
        return " " + REVERSE + body + " " * pad + RESET
    return "  " + body


def ask_block(options, selected, buffer, cursor, cols, rows, color=True):
    """ASKING 状态下的整个活区：提示 + 选项（可能分页）+ 输入行。

    返回的每一行都已经裁到 cols 以内。"""
    options = norm_options(options)
    total = len(options) + 1                 # 最后一行永远是"自己打字"
    selected = max(0, min(int(selected), total - 1))
    rows = max(6, int(rows))
    max_rows = max(1, rows - 4)              # 提示 + 输入行 + 上下午各一条省略提示

    lines = [clip(HINT, cols)]
    start, end = _window(total, selected, max_rows)
    if start > 0:
        lines.append(clip(f"  … 上面还有 {start} 项", cols))
    for i in range(start, end):
        label = f"{i + 1}. " + (options[i] if i < len(options) else FREE_TEXT)
        lines.append(_option_row(label, i == selected, cols, color))
    if end < total:
        lines.append(clip(f"  … 下面还有 {total - end} 项", cols))
    lines.append(input_line(buffer, cursor, cols, color=color))
    return lines


def option_labels(options) -> list:
    """界面上从 1 开始编号的那一批（不含"自己打字"）。"""
    return norm_options(options)


# ================= 其他状态的一行 =================

def idle_line(buffer, cursor, cols, color=True) -> str:
    return input_line(buffer, cursor, cols, color=color)


def busy_line(text, elapsed=0, cols=80) -> str:
    """忙碌时那一行。只用 ASCII 和中文，不用 emoji（宽度不可靠）。"""
    secs = int(max(0, elapsed or 0))
    label = f"  · {text} {secs}s" if secs else f"  · {text}"
    return clip(label, cols)


def dim_line(text, cols=80) -> str:
    return DIM + clip(text, cols) + RESET


# ================= 擦 / 移动 =================

def erase_live(rows: int) -> str:
    """回到活区第一行的行首，并把下面全清掉。

    行数为 0 时什么都不做；**绝不上移超过活区自己的行数**。"""
    rows = int(rows)
    if rows <= 0:
        return ""
    up = f"\x1b[{rows - 1}A" if rows > 1 else ""
    return up + "\r\x1b[J"


def reserve(extra: int) -> str:
    """在光标下面腾出 extra 行（必要时把历史顶上去），再把光标送回原处。"""
    extra = int(extra)
    if extra <= 0:
        return ""
    return "\r\n" * extra + (f"\x1b[{extra}A" if extra else "")


def draw_lines(lines) -> str:
    """把若干行拼成一次写屏的内容。**末尾不带换行** —— 带了就会顶屏滚动。"""
    return "\r\n".join("\r\x1b[2K" + line for line in lines)


__all__ = [
    "FREE_TEXT", "HINT", "PROMPT",
    "char_width", "width", "clip",
    "input_line", "ask_block", "option_labels",
    "idle_line", "busy_line", "dim_line",
    "erase_live", "reserve", "draw_lines",
    "norm_options", "resolve_option",
]
