# -*- coding: utf-8 -*-
"""纯函数小工具：数字/金额格式化、凭证打码、选项清洗，以及用量图表的取数与刻度。

这里不 import 任何本地模块，界面层和图表都从这里拿格式化逻辑。
"""

import math
import re

def _fmt_tokens(n) -> str:
    """把 token 数变成人看的样子：12345 → 12.3K"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_tokens_short(n) -> str:
    """跟 _fmt_tokens 一样，但整数不带小数点：800000 → 800K（刻度、柱顶标数值用）。

    轴上的刻度要尽量短，"800.0K" 读起来比 "800K" 啰嗦，还容易跟 "800M" 看混。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    for scale, suffix in ((1_000_000, "M"), (1_000, "K")):
        if abs(n) >= scale:
            v = n / scale
            return f"{v:.0f}{suffix}" if abs(v - round(v)) < 0.05 else f"{v:.1f}{suffix}"
    return str(n)


def _fmt_yuan(v) -> str:
    """把金额格式化成 ¥x.xx；拿不到就返回 —"""
    try:
        return f"¥{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _to_float(v):
    """字符串/数字 → float；失败返回 None"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _norm_by_model(block):
    """把一个用量数据块（today / month）里的"按模型拆分"取出来。

    服务器理想返回（这是我们希望后端补的字段）：
        {"tokens": 756344, "yuan": "0.04",
         "by_model": {"deepseek-flash": {"tokens": 742000, "yuan": "0.03"},
                      "deepseek-v4-pro": {"tokens": 14344, "yuan": "0.01"}}}
    为了不跟后端绑死，下面这些写法都能吃：
        "by_model" / "models" / "by_model_tokens"          三个键名都认
        {模型: token数}  /  {模型: {"tokens":…, "yuan":…}}  字典两种值
        [{"model": …, "tokens": …}, …]  /  [{"name": …, "tokens": …}]
    返回按 token 从多到少排好的 [{"model":…, "tokens": int, "yuan": float|None}]；
    一个模型数据都没有时退回总量的单根柱子（模型名"全部模型"），
    这样后端还没上线这个字段时，图表也有东西可画。
    """
    raw = None
    for key in ("by_model", "models", "by_model_tokens"):
        if block.get(key):
            raw = block[key]
            break

    rows = []
    if isinstance(raw, dict):
        for model, v in raw.items():
            if isinstance(v, dict):
                tok, yuan = v.get("tokens"), v.get("yuan")
            else:
                tok, yuan = v, None
            t = _to_float(tok)
            if t is None or t < 0:
                continue
            rows.append({"model": str(model), "tokens": int(t), "yuan": _to_float(yuan)})
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("model") or item.get("name") or item.get("model_name") or "未知"
            t = _to_float(item.get("tokens"))
            if t is None or t < 0:
                continue
            rows.append({"model": str(name), "tokens": int(t), "yuan": _to_float(item.get("yuan"))})

    rows.sort(key=lambda r: -r["tokens"])

    if not rows:                             # 后端还没给按模型的明细 → 退回总量
        total = _to_float(block.get("tokens"))
        if total and total >= 0:
            rows = [{"model": "全部模型", "tokens": int(total),
                     "yuan": _to_float(block.get("yuan"))}]
    return rows


def _norm_series(block):
    """取出一个用量块里的**时间序列**（图表按小时 / 按天画）。

    服务器现在返回：
        {"series": [{"label": "00:00", "tokens": 123, "yuan": "0.01"}, …]}
    也兼容 by_hour / by_day 两个候选键名，以及 {hour:…} / {day:…} 的写法。

    返回按服务器给的顺序排列的
        [{"label": str, "tokens": int, "yuan": float|None}, …]
    没有这个字段时返回 []，界面会自动退回按模型画柱状图（老接口不白屏）。
    """
    raw = None
    for key in ("series", "by_hour", "by_day"):
        if block.get(key):
            raw = block[key]
            break

    rows = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("t") or item.get("time")
            if label is None:
                if item.get("hour") is not None:
                    label = f"{int(item['hour']):02d}:00"
                elif item.get("day") is not None:
                    label = str(item.get("day"))
            t = _to_float(item.get("tokens"))
            if label is None or t is None or t < 0:
                continue
            rows.append({
                "label": str(label),
                "tokens": int(t),
                "yuan": _to_float(item.get("yuan")),
            })
    return rows


def _nice_ticks(vmax, target=4):
    """给图表算"好看的刻度"：返回 (步长, 顶端值)。

    步长只取 1/2/2.5/5/10 × 10^k，所以刻度上永远是 0 / 500K / 1.0M 这种整数。
    """
    vmax = _to_float(vmax) or 0
    if vmax <= 0:
        return 1, 1
    raw = vmax / max(1, target)
    mag = 10 ** math.floor(math.log10(raw))
    step = 10 * mag
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            step = m * mag
            break
    top = math.ceil(vmax / step) * step
    return step, top


def _fit_text(text, px, size=14):
    """把文字截到 px 像素以内（按像素算，不是按字符数）。

    图表轴标签要用它：槽有多宽就显示多宽的字。按字符数砍在 "deepseek-v4-pro"
    和 "全部模型" 之间差得太多，中英混排时会一个溢出、一个浪费。
    """
    s = str(text)
    if _text_width(s, size) <= px:
        return s
    out = ""
    for ch in s:
        if _text_width(out + ch + "…", size) > px:
            break
        out += ch
    return (out + "…") if out else s[:1]


def _text_width(text, size=14):
    """粗估一段文字的宽度（像素）。

    ★ size 是**像素**高度，跟 CTkFont 一个口径（不是磅）—— ui/fonts.csize() 给的
      就是这个数。中文方块字按满宽算，西文按约 0.62 字宽算（微软雅黑量下来是 0.51、
      等宽是 0.60，取 0.62 是宁可估大：估大了只是框宽一点，估小了字会溢出框外）。
    """
    w = 0
    for ch in str(text):
        w += size if ord(ch) > 0x2E80 else size * 0.62
    return int(w) + 4


def _norm_options(options):
    """把模型给的 options 洗成一批干净的字符串。

    模型有时会写成 [{"label": "保留"}, ...]，或者自己在文字前面加编号
    （"1. 保留原来的"）。界面本来就会自动加序号，所以这里统一：
      · 字典 → 取 label / text / title
      · 去掉开头模型自己加的 "1." "2、" "A)" 之类编号
      · 去掉空串和多余空白
    """
    out = []
    for o in (options or []):
        if isinstance(o, dict):
            o = o.get("label") or o.get("text") or o.get("title") or o.get("value") or str(o)
        s = re.sub(r"\s+", " ", str(o)).strip()
        if not s:
            continue
        # 数字编号：1. / 1、/ 1) / 1） / (1)  —— 后面的 (?!\d) 是为了别把 "1.5 倍" 砍坏
        s = re.sub(r"^\s*[\(\（]?\s*\d{1,2}\s*[\.\、\．\)\）]\s*(?!\d)", "", s)
        # 字母编号：A. / B、/ C)
        s = re.sub(r"^\s*[A-Da-d]\s*[\.\、\．\)\）]\s+", "", s)
        s = re.sub(r"^\s*[\-\*•]\s*", "", s).strip()   # 开头的 - * •
        if s:
            out.append(s)
    return out


def _mask_secret(value, keep=4, mask_len=24) -> str:
    """把凭证打码给人看：保留前 keep 位，其余换成 *。

    账户码（CC-XXXXXX-XXXXXX）留前 4 位，用户认得出"是不是这一个"，又不是整串
    亮在屏幕上；账户密钥长度固定，用 keep=0 就整串打成一排 *。

    ★ 这只是"别被路过的人一眼看走"，**不是加密**：明文就躺在本地凭证里，
      点一下眼睛按钮就出来了。所以界面上必须配那句红字警告 —— 打码不能替代提醒。
    """
    s = "" if value is None else str(value).strip()
    if not s:
        return ""
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        keep = 0
    keep = max(0, min(keep, len(s)))
    if keep >= len(s):
        return "*" * len(s)
    hidden = max(8, min(len(s) - keep, mask_len))
    return s[:keep] + "*" * hidden
