# -*- coding: utf-8 -*-
"""字体：一处定义，全界面统一。

为什么要单独一个模块：

1. **默认字体看着"廉价"**。CustomTkinter 默认走系统兜底字体，中英混排时标点忽宽忽窄；
   而 `微软雅黑`（Microsoft YaHei）是给正文排版设计的，小字号下偏松、偏糊。
   这里优先用 **Microsoft YaHei UI** —— 同一套字形的界面版，字距更紧、更端正，
   是 Windows 自己画设置面板用的那一款。

2. **发到别人机器上不能变形**。字体是运行时问 Tk 要的（`tkfont.families()`），
   候选表按优先级排，命中第一个就用。老 Win10 没有 YaHei UI 就退到普通雅黑，
   再没有就退到系统 UI 字体 —— 绝不会突然变成宋体。

3. **字号集中**。下面那一组 S_* 常量是全界面的字号表，改一处全界面跟着变，
   不要再在别的文件里写 `size=11` 这种魔法数字。

数字和代码用等宽（Cascadia 是 Win11 自带的现代等宽，比 Consolas 干净），
这样 token 数、订单号、日志列与列之间是对齐的 —— 这是"正规"最省事的一半。

**画布上的字要特别注意**：CTkFont 的 size 是**像素**（CTk 内部传负数给 Tk，还会按屏幕
DPI 再放大一遍），而 tk.Canvas 的 create_text 收到的是**磅**，1 磅只有 1.33 像素，
压根不跟 CTk 一起放大。同一个字号写两处，图上的字就会比旁边的控件小一圈。所以画布
一律走 spec()：它把字号换算成 CTkFont 同一口径的像素，并且配一个 canvas_scale() 给
画布上的长度用（详见那两个函数的注释）。
"""

import tkinter.font as tkfont

import customtkinter as ctk

# ---------- 字体候选（按优先级）----------

_UI_CANDIDATES = (
    "Microsoft YaHei UI",     # 微软雅黑界面版：首选
    "微软雅黑",
    "Microsoft YaHei",
    "Segoe UI",               # 实在是没中文雅黑时的兜底
    "Noto Sans SC",
)

_MONO_CANDIDATES = (
    "Cascadia Mono",          # Win11 自带，无连字，最适合读数字
    "Cascadia Code",
    "Consolas",               # Win7 起都有，稳
    "JetBrains Mono",
    "Courier New",
)

# ---------- 字号表 ----------

S_LOGO = 17        # 侧栏品牌名
S_TITLE = 21       # 页面标题
S_DIALOG = 18      # 弹窗标题（比页面标题小一档）
S_HERO = 38        # 余额那个大数字
S_HEAD = 15        # 分区小标题（"各模型 token 用量"）
S_BODY = 14        # 正文 / 输入框 / 选项
S_NAV = 14         # 左侧导航
S_BTN = 14         # 一般按钮
S_MONO = 13        # 日志区（等宽）
S_SMALL = 12       # 说明文字、表头、小按钮
S_TINY = 11        # 角标、注解
S_AXIS = 10        # 图表坐标轴刻度
S_CANVAS = 11      # 图表柱顶数值 / 提示框正文

# ---------- 运行时解析 ----------

_ui_family = None
_mono_family = None
_cache = {}


def _resolve(candidates):
    """挑第一个本机真有的字体；问不到（还没有 root）就用候选表头一个"""
    try:
        available = set(tkfont.families())
    except Exception:
        return candidates[0]
    for name in candidates:
        if name in available:
            return name
    return candidates[0]


def family(mono=False):
    """界面字体名 / 等宽字体名（只解析一次，之后走缓存）"""
    global _ui_family, _mono_family
    if mono:
        if _mono_family is None:
            _mono_family = _resolve(_MONO_CANDIDATES)
        return _mono_family
    if _ui_family is None:
        _ui_family = _resolve(_UI_CANDIDATES)
    return _ui_family


def font(size=S_BODY, weight="normal", mono=False):
    """给组件用（CTkLabel / CTkButton / CTkEntry …）。

    同样的 (size, weight, mono) 只建一次 CTkFont —— 字体对象多了会占 Tk 资源，
    而且复用一个对象也让整个界面看起来更一致。"""
    key = (size, weight, mono)
    got = _cache.get(key)
    if got is None:
        got = ctk.CTkFont(family=family(mono), size=size, weight=weight)
        _cache[key] = got
    return got


def spec(size=S_CANVAS, weight="normal", mono=False):
    """给 tk.Canvas 的 create_text 用。

    画布不吃 CTkFont 对象，只认 (family, size) 或 (family, size, weight) 这种元组。
    ★ size 前面那个负号是 Tk 的规矩：负数 = 像素，正数 = 磅。这里给像素，是为了
      跟 CTkFont 同一个口径 —— CTkFont(size=12) 在 150% 缩放下实际就是 18 像素高。"""
    px = -csize(size)
    if weight == "normal":
        return (family(mono), px)
    return (family(mono), px, weight)


_canvas_scale = None


def canvas_scale():
    """画布上的长度要乘的倍数（这台机器上是 1.5，也就是 150% 缩放）。

    ★ 这是整套字体里唯一一处"不显然"的地方：CTk 会按屏幕 DPI 把控件尺寸和字体一起
      放大，但 tk.Canvas 里画的东西不归它管 —— 画布坐标系就是物理像素。不手动乘这
      一下，图表上的字、留白、柱子粗细全都比旁边的控件小一圈，看着就是两张皮。"""
    global _canvas_scale
    if _canvas_scale is None:
        _canvas_scale = 1.0
        try:
            import tkinter
            root = tkinter._default_root
            if root is not None:
                _canvas_scale = float(ctk.ScalingTracker.get_widget_scaling(root))
        except Exception:
            pass                       # 拿不到就当 1.0，宁可不缩放也别崩
    return _canvas_scale


def csize(size=S_CANVAS):
    """画布字号（像素）。给 helpers._text_width / _fit_text 算文字宽度用，
    必须和 spec() 传下去的字号是同一个数，否则提示框会量错。"""
    return max(1, int(round(size * canvas_scale())))
