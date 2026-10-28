# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（三平台共用，文件名用 ASCII 是为了 CI 和命令行都能直接引用）。

  Windows  python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCode安装助手.exe（单文件）

  macOS    python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCodeAssistant.app（未签名；Info.plist 里声明了 aikaui://）

  Linux    python -m PyInstaller agent.spec --noconfirm --clean
           → dist/ClaudeCodeAssistant（单文件；发行时再和 .desktop + png 一起打 tar.gz）

版本号只从 config.APP_VERSION 来，别在这里写死。

── 体积这件事（2026-10-28 量的 Windows 单文件）────────────────────────
  改之前 35,955,401 字节（35.9MB），改完 23,632,705 字节（23.6MB），
  少了 12.3MB / 34%，功能一个没减。主要浪费在这几处：
    ① `collect_all` 默认 include_py_files=True —— 每个包的**源码**作为数据文件
       又放了一份（PYZ 里本来就有字节码）。光 openai 就重复 7.4MB 原始 / 1.6MB 压缩，
       外加 dist-info 的 RECORD。改成 collect_data_files(默认不收 .py)。
    ② `collect_all` 把所有子模块都当 hiddenimports —— `win32com.test`、
       `certifi.tests` 这类测试代码因此进包，把 unittest / pywin32_testutil
       一路拖进 PYZ。现在按名字过滤掉 test/tests。
    ③ `collect_all("win32com")` 把 pywin32 整个搬了进来（win32ui、win32gui、
       shell/mapi/axdebug/axcontrol/propsys/exchange 六个 .pyd，约 3MB）。
       这个程序只用到 `win32com.client.Dispatch("WScript.Shell")` 建快捷方式，
       所以只声明这一个 hiddenimport，剩下的交给静态分析 + hook-pywintypes。
    ④ Pillow 的可选编解码器：`_avif` 一个就 7.9MB 原始 / 4.3MB 压缩。
       这里只用 Image.new/ImageDraw（画图标）和 Image.open(PNG/JPEG)（付款二维码），
       AVIF 和 CMS 永远用不到 —— 见下面的 DROP_BINARIES。
    ⑤ win10toast 第一条分支：它 import 的 pkg_resources 在新版 setuptools 里已经
       没有了，也就是说这条分支**一直是坏的**（实际走的是 PowerShell 的 WinRT toast），
       却把 setuptools 整个拽进包里。排掉。

  没动的东西（想再瘦可以研究，但风险/收益不划算）：
    · tcl86t/tk86t + tcl 的 encoding 数据 ≈ 5.9MB —— 界面就靠它，动不了
    · libcrypto/libssl ≈ 2.4MB —— TLS，必需
    · ucrtbase.dll / msvcp140.dll —— 用户机器上可能没有，省 0.7MB 换"打不开"不值
    · openai 的资源模块（audio/images/beta/…）≈ 5.9MB 原始 —— `openai/__init__.py`
      会一次性 import 全部，排掉任何一个 `import openai` 就失败
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

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
# ★ 不用 collect_all()：它会连源码和 dist-info 一起塞进来（见文件头的体积说明）。
#   这些包真正需要的资源文件（certifi 的 cacert.pem、pydantic 的 py.typed 之类）
#   collect_data_files 一样收得到，而且它对 .py 默认就是不收。
binaries = []
hiddenimports = []


def _not_tests(name: str) -> bool:
    """包里自带的测试代码一个都不要。

    `win32com.test` / `certifi.tests` 被当 hiddenimports 收进来之后，
    会连带把 `unittest`、`pywin32_testutil` 拖进 PYZ —— 而运行时一次都不会 import。"""
    return not any(part in ("test", "tests") for part in name.split(".")[1:])


for pkg in ("openai", "pydantic", "pydantic_core", "httpx2", "httpcore2", "anyio",
            "jiter", "sniffio", "certifi", "urllib3", "charset_normalizer", "idna",
            "typing_extensions"):
    try:
        datas += collect_data_files(pkg)                       # 默认 include_py_files=False
        hiddenimports += collect_submodules(pkg, filter=_not_tests)
    except Exception:
        continue                                    # 某个包换了名字/没装，跳过

# win32com：只为了 create_shortcut() 里那一次
# `win32com.client.Dispatch("WScript.Shell")`。依赖链（实测）是
# win32com.client → CLSIDToClass/build/dynamic/gencache → pythoncom / pywintypes /
# win32api / win32event，静态分析跟得到；两个 pywin32 的 DLL 由 hook-pywintypes 收。
if IS_WIN:
    hiddenimports.append("win32com.client")

# PyInstaller 不会自动跟着 platform_support 里的延迟 import 走，显式点一下
hiddenimports += ["platform_support.windows", "platform_support.macos", "platform_support.linux"]

# ================= 不打的模块 =================
excludes = [
    # win10toast 的第一条分支一直是坏的（它 import 的 pkg_resources 已经不在
    # 新版 setuptools 里了），实际生效的是 PowerShell 的 WinRT toast。
    # 排掉它顺带把 pkg_resources → setuptools（1.9MB）一起送走。
    "win10toast", "plyer", "pkg_resources",
    # setuptools / distutils 这一整串：包里没有任何模块 import 它们（连着 PYZ 的
    # 字节码 const 查过，0 处引用），是被 PyInstaller 的 distutils 钩子带进来的，
    # 还会顺带装一个 pyi_rth_setuptools 运行时钩子。
    "setuptools", "_distutils_hack", "distutils",
    # help() 和跑测试才用得到；这是 GUI 程序，两条路都不会走到
    "pydoc", "pydoc_data", "unittest",
    # collect_all 时代被拖进来的测试包，现在两道保险都上
    "win32com.test", "certifi.tests",
    # win32com 里跟"建快捷方式"无关的那几个：makepy 是生成 gen_py 的命令行工具，
    # selecttlb / tlbrowse / combrowse 是注册表里浏览类型库的 GUI。它们把
    # pywin.*（Pythonwin 的对话框）和 win32ui.pyd（1MB）一路拖进来。
    # gencache 里对 makepy 的 import 在函数内部，我们只调 Dispatch 不走那条路。
    "win32com.client.makepy", "win32com.client.selecttlb",
    "win32com.client.tlbrowse", "win32com.client.combrowse",
    "pywin", "win32ui", "pythonwin",
]

# Pillow 的可选编解码器（.pyd 是二进制，得在 Analysis 之后过滤）。
DROP_BINARIES = ("PIL/_avif", "PIL/_imagingcms")

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
    excludes=excludes,
    noarchive=False,
    # 2 = 和 python -OO 一样：去掉 docstring。这个程序里没有任何地方读
    # `__doc__`（工具描述写在 tool_specs.py 里），openai / pydantic 的 docstring
    # 纯占地方。测试按 `python -OO -m unittest discover -s tests` 跑一遍验过。
    optimize=2,
)

# 只丢 .pyd，保留 PYZ 里的插件模块：万一真有人打开 AVIF，得到的是
# ImportError（images.py 那边本来就吞异常），而不是 import 期的崩溃。
_dropped = [b[0] for b in a.binaries
            if any(d in b[0].replace("\\", "/") for d in DROP_BINARIES)]
a.binaries = [b for b in a.binaries
              if not any(d in b[0].replace("\\", "/") for d in DROP_BINARIES)]
for _d in _dropped:
    print(f"[agent.spec] 去掉可选编解码器：{_d}")

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
