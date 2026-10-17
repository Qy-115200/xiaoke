# -*- coding: utf-8 -*-
"""可选依赖探测。

pystray（托盘）和 Pillow（画图标 / 显示二维码）都是可选依赖：缺了程序照样跑，
只是对应功能降级。探测结果放在模块级常量里，别的地方直接 import 判断。

导入失败时那些名字会被置成 None，这样 `from deps import Image` 永远能成功，
调用方只需要判 HAVE_PIL 就行。
"""

# 托盘是可选依赖：缺了也要能跑，只是没有托盘功能
try:
    import pystray
    HAVE_TRAY = True
except Exception:                # 没装 pystray
    pystray = None
    HAVE_TRAY = False
# Pillow 是二维码显示的必要依赖（要把服务器给的二维码图片画进窗口）
try:
    from PIL import Image, ImageDraw
    HAVE_PIL = True
except Exception:                # 没装 Pillow
    Image = ImageDraw = None
    HAVE_PIL = False

HAVE_TRAY = HAVE_TRAY and HAVE_PIL     # 托盘图标也要靠 Pillow 现画
