# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（三平台共用，文件名用 ASCII 是为了 CI 和命令行都能直接引用）。

  Windows  python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCode安装助手.exe（单文件）

  macOS    python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCodeAssistant.app（未签名；Info.plist 里声明了 aikaui://）

  Linux    python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCodeAssistant（单文件；发行时再和 .desktop + png 一起打 tar.gz）

版本号只从 config.APP_VERSION 来，别在这里写死。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, SPECPATH)                      # noqa: F821  (PyInstaller 提供的变量)
from config import APP_ID, APP_NAME, APP_VERSION  # noqa: E402

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
NAME = "ClaudeCode安装助手" if IS_WIN else "ClaudeCodeAssistant"

# ================= 资源 =================
# 窗口图标：Windows 用 .ico，macOS/Linux 用 .png（images.ensure_icon_file / ensure_icon_png
# 会先找 sys._MEIPASS 下的这一份）。
datas = []
if IS_WIN:
    datas.append(("agent.ico", "."))
if Path(SPECPATH, "agent.png").exists():           # noqa: F821
    datas.append(("agent.png", "."))

# ================= 第三方包的隐式资源 =================
binaries = []
hiddenimports = []
for pkg in ("openai", "pydantic", "pydantic_core", "httpx2", "httpcore2", "anyio",
            "jiter", "sniffio", "certifi", "urllib3", "charset_normalizer", "idna",
            "typing_extensions"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue                                    # 某个包换了名字/没装，跳过
    datas += d
    binaries += b
    hiddenimports += h

# win32com 只存在于 Windows；在 mac/linux 上 collect_all 会直接失败
if IS_WIN:
    try:
        d, b, h = collect_all("win32com")
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# PyInstaller 不会自动跟着 platform_support 里的延迟 import 走，显式点一下
hiddenimports += ["platform_support.windows", "platform_support.macos", "platform_support.linux"]

# ================= 版本信息（Windows 文件属性）=================
version_file = None
if IS_WIN:
    candidate = Path(SPECPATH, "build", "version_info.txt")   # noqa: F821
    if candidate.exists():
        version_file = str(candidate)

# ================= 打包 =================
a = Analysis(
    ["main.py"],
    pathex=[SPECPATH],                              # noqa: F821
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    icon="agent.ico" if IS_WIN else "agent.icns",
    version=version_file,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=IS_WIN,                                     # UPX 在 mac/linux 上容易出问题，只在 Windows 开
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,                            # macOS 的 URL 我们用 pyobjc 收 Apple Event
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if IS_MAC:
    app = BUNDLE(
        exe,
        name=NAME + ".app",
        icon="agent.icns",
        bundle_identifier=APP_ID,
        info_plist={
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSRequiresAquaSystemAppearance": False,
            # ★ aikaui:// 协议：支付页付完款点「返回助手」时，LaunchServices 靠这里找到 .app
            "CFBundleURLTypes": [
                {
                    "CFBundleURLName": APP_ID,
                    "CFBundleURLSchemes": ["aikaui"],
                }
            ],
        },
    )
