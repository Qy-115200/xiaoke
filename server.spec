# -*- mode: python ; coding: utf-8 -*-
"""服务器版（无界面）的 PyInstaller 打包脚本。

    python -m PyInstaller server.spec --noconfirm --clean
    → dist/ClaudeCodeAssistant（单文件，带 console）

和 agent.spec 是**两份独立的脚本**，互不影响：

  agent.spec   入口 main.py       有界面，Windows / macOS / Linux 桌面
  server.spec  入口 server_main.py 只有终端，Linux 服务器

★ 产物名必须是 `ClaudeCodeAssistant`：updater._extract_linux 是按这个文件名
  在下载的压缩包里找可执行文件的。

★ excludes 是本文件存在的意义。界面库一条都不能漏：
  - customtkinter → 拖进 tkinter/_tkinter/Tcl/Tk，包大十几兆，而且没有 $DISPLAY
    的机器上初始化就失败；
  - openai/pydantic/httpx → Agent 那条链，服务器版一个字都用不到。
  漏一个，服务器上就会拿到一个又大又可能起不来的包。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, SPECPATH)                          # noqa: F821

NAME = "ClaudeCodeAssistant"

# ================= 第三方包的隐式资源 =================
# 身份层要 requests + 它那串依赖（证书、idna、编码表都是数据文件，PyInstaller
# 不会自动收）。这些都是纯 Python，几十 KB。
datas = []
binaries = []
hiddenimports = []
for pkg in ("requests", "certifi", "urllib3", "charset_normalizer", "idna",
            "typing_extensions"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue                                      # 没装就跳过，别让打包失败
    datas += d
    binaries += b
    hiddenimports += h

# platform_support 是按 sys.platform 延迟 import 的，PyInstaller 跟不到
hiddenimports += ["platform_support.linux"]

# 终端里画二维码（--recharge）。它在函数里被 try/except 包着延迟 import，
# 静态分析有时候会把这种当成"可选的"漏掉，点名带上。没装就跳过 ——
# 画不出来会退回打印付款链接，不影响充值本身。
try:
    import segno  # noqa: F401
except Exception:
    pass
else:
    hiddenimports += ["segno"]

# 解 Node 的 .tar.xz 要 lzma。标准库里它是可选扩展，不点名可能漏掉
hiddenimports += ["lzma", "_lzma"]

# ================= 明确排除 =================
EXCLUDES = [
    # 界面
    "tkinter", "_tkinter", "customtkinter", "PIL", "Pillow",
    "ui", "images", "deps", "helpers",
    # Agent / 模型
    "openai", "pydantic", "pydantic_core", "httpx2", "httpcore2", "anyio",
    "jiter", "sniffio", "oauthlib", "distro", "tqdm",
    "agent_core", "bus", "llm", "prompts",
    # 桌面版专属
    "updater", "notify",
    "platform_support.windows", "platform_support.macos", "win32com", "winreg",
]

# ================= 打包 =================
a = Analysis(
    ["server_main.py"],
    pathex=[SPECPATH],                                # noqa: F821
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    icon=None,                    # 终端里的程序，不需要图标
    version=None,                 # 版本信息只有 Windows 才有意义
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # UPX 在 Linux 上容易把二进制弄坏，不开
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,                 # ★ 必须 True：否则 print 和报错全被吞掉
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
