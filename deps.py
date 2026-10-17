# -*- coding: utf-8 -*-
"""可选依赖探测。

Pillow（画窗口图标 / 显示二维码）是可选依赖：缺了程序照样跑，只是对应功能降级。
探测结果放在模块级常量里，别的地方直接 import 判断。

导入失败时那些名字会被置成 None，这样 `from deps import Image` 永远能成功，
调用方只需要判 HAVE_PIL 就行。

（托盘已经彻底删除，pystray 不再是依赖。）
"""

# Pillow 是二维码显示 / 现场生成图标的必要依赖
try:
    from PIL import Image, ImageDraw
    HAVE_PIL = True
except Exception:                # 没装 Pillow
    Image = ImageDraw = None
    HAVE_PIL = False
