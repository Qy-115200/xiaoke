# -*- coding: utf-8 -*-
"""图标与二维码图片。

托盘的圆角方块图标是运行时用 Pillow 现画的（省得依赖美术资源）；
付款二维码则是把服务器给的图片下载下来转成 CTkImage。
两个函数都做成"失败返回 None"而不是抛异常——图标和二维码都不是必需品。
"""

import sys
from pathlib import Path

import customtkinter as ctk

from config import BLUE, RED
from deps import HAVE_TRAY, HAVE_PIL, Image, ImageDraw

def _icon_image(alert: bool = False, size: int = 64):
    """画一个托盘/窗口图标。
    alert=True 画红色（余额不足告警），否则画主题蓝。
    不用外部图片文件，运行时用 Pillow 现画，省得依赖美术资源。"""
    if not HAVE_TRAY:
        return None
    color = RED if alert else BLUE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))     # 透明底
    d = ImageDraw.Draw(img)
    # 圆角方块
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill=color)
    # 里面画一个 ">_" 终端符号
    d.line([(18, 22), (30, 32), (18, 42)], fill="white", width=5, joint="curve")
    d.line([(34, 42), (46, 42)], fill="white", width=5)
    return img

def ensure_icon_file():
    """给窗口图标（iconbitmap）找一个 .ico 文件，返回路径或 None。
    优先用打包进来的 agent.ico；没有就现生成一个存到用户目录。"""
    # 1) PyInstaller 解包目录 / 源码目录
    for base in (getattr(sys, "_MEIPASS", None), Path(__file__).parent):
        if not base:
            continue
        p = Path(base) / "agent.ico"
        if p.exists():
            return str(p)

    # 2) 现场生成
    data_dir = Path.home() / ".claude-agent"
    target = data_dir / "agent.ico"
    if target.exists():
        return str(target)
    img = _icon_image(False)
    if img is None:
        return None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Pillow 存 .ico 要给尺寸列表
        img.save(target, sizes=[(64, 64), (32, 32), (16, 16)])
        return str(target)
    except Exception:
        return None                 # 存不了就算了，图标不是必需品


def _load_qr_image(url: str, size: int = 240):
    """把服务器给的二维码图片下载下来，做成界面能直接放的 CTkImage。

    ★ 为什么下载图片而不是自己生成二维码：
    服务器 create-order 返回的 img 就是支付平台给的现成二维码图，
    直接用它最稳——自己按 pay_url 重新生成，万一编码方式跟平台不一致就扫不出来。

    失败返回 None（界面会退化成"点打开支付页"的兜底提示），绝不抛异常。"""
    if not HAVE_PIL:
        return None
    try:
        import io
        import requests
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None

    try:
        # 统一缩放到 size×size，二维码必须整数倍缩放才不糊，
        # 所以用 LANCZOS 而不是 NEAREST，扫不动的情况最少
        img = img.resize((size, size), Image.LANCZOS)
        return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
    except Exception:
        return None
