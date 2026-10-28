# -*- mode: python ; coding: utf-8 -*-
"""服务器版（无界面）的 PyInstaller 打包脚本。

    python -m PyInstaller server.spec --noconfirm --clean
    → dist/ClaudeCodeAssistant（单文件，带 console）

和 agent.spec 是**两份独立的脚本**，互不影响：

  agent.spec   入口 main.py       有界面，Windows / macOS / Linux 桌面
  server.spec  入口 server_main.py 只有终端，Linux 服务器

★ 产物名必须是 `ClaudeCodeAssistant`：updater._extract_linux 是按这个文件名
  在下载的压缩包里找可执行文件的。

★ excludes 保证的是**界面库一条都不进**：
  - customtkinter → 拖进 tkinter/_tkinter/Tcl/Tk，包大十几兆，而且没有 $DISPLAY
    的机器上初始化就失败；
  - `bus`（tk 版的消息队列）和 `prompts`（桌面提示词）—— 排它们的前提是
    `agent_core` 里那两句只在函数里 import，别挪回顶层；
  - 桌面版专属的 updater / notify / platform_support.windows|macos。

★ 模型栈（openai / pydantic / httpx2 …）**现在进包了**：服务器版改成 Agent 装了，
  和桌面版跑的是同一套 `agent_core.run_one_task`。代价约 +10MB（26 → 36.5MB，
  2026-10-28 量的 Linux 单文件）。
  但 `agent_core` / `llm` 仍然可以在 `server_main` 顶层之外延迟 import —— 见
  `server/agent_install._session()`，所以 `--balance` 这类命令不会拖起模型栈。

★ `helpers` **不能**排除：`server/term_render.py` 从它那里取 `norm_options` /
  `resolve_option`（桌面版和终端版的选项语义靠这一个来源对齐）。它只 import
  math / re，带进来是零成本的。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, SPECPATH)                          # noqa: F821

NAME = "ClaudeCodeAssistant"

# ================= 第三方包的隐式资源 =================
# ① 身份层要 requests + 它那串依赖（证书、idna、编码表都是数据文件，PyInstaller
#    不会自动收）。这些都是纯 Python，几十 KB。
# ② 模型栈：openai 3.x 真正依赖的是 **httpx2 / httpcore2**（不是 httpx/httpcore），
#    pydantic 2.x 的 core 是编译扩展。collect_all 会把它们的数据文件和子模块一起收，
#    漏一个就是在客户机上 ModuleNotFoundError（那时已经在装东西了，最难看）。
datas = []
binaries = []
hiddenimports = []
for pkg in ("requests", "certifi", "urllib3", "charset_normalizer", "idna",
            "typing_extensions",
            "openai", "pydantic", "pydantic_core", "annotated_types",
            "typing_inspection", "httpx2", "httpcore2", "h11", "truststore",
            "anyio", "jiter", "sniffio"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue                                      # 没装就跳过，别让打包失败
    datas += d
    binaries += b
    hiddenimports += h

# platform_support 是按 sys.platform 延迟 import 的，PyInstaller 跟不到
hiddenimports += ["platform_support.linux"]

# 服务器版自己的模块：入口只 import 到 server.* 的一部分，其余（尤其 Agent 那层）
# 是函数里 import 的，静态分析跟不到。点名列全 —— 少一个就是运行时 ModuleNotFoundError。
hiddenimports += ["agent_bus", "agent_core", "llm",
                  "server.agent_install", "server.agent_tools",
                  "server.prompts_server", "server.term_chat",
                  "server.term_keys", "server.term_render"]

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
    # 界面（这一条是硬要求：没有 $DISPLAY 的机器上导入 Tk 会直接炸）
    "tkinter", "_tkinter", "customtkinter", "PIL", "Pillow",
    "ui", "images", "deps",
    # ★ `helpers` 不在排除之列：term_render 要用它的选项语义（见文件头说明）。
    # 桌面版的提示词和 tk 版的消息队列：agent_core 里那句 `from prompts import …`
    # 在 `agent_worker()` 里，服务器版从不调它。排掉省得把 VSCode/托盘那一大段
    # （以及 `bus` 拖进来的 tkinter）带进包。
    "bus", "prompts",
    # 桌面版专属
    "updater", "notify",
    "platform_support.windows", "platform_support.macos", "win32com", "winreg",
    # 模型栈里用不到的可选依赖（openai 3.15 的源码里一次都没 import）
    "oauthlib", "distro", "tqdm",
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
