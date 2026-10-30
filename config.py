# -*- coding: utf-8 -*-
"""唯一的常量源。

这里集中放着整个程序用到的所有字面量：服务器地址、内置凭证、路径、
配色、以及几个全局阈值。其它模块只从这里 import，不要再自己写一份。

这些常量会被 PyInstaller 打进入 exe，用户反编译能看到。里面唯一像凭证的是
AGENT_KEY，但它只是注册链路的"敲门砖"（服务器只拿它换一次挑战），不是秘密，
也绝不能当秘密使——真正的身份是每台设备注册后拿到的专属 api_key。
"""

import os
from pathlib import Path

# ================= 版本 =================

# 唯一真源：git tag、Windows 文件版本、macOS Info.plist、更新清单都用它。
# 只允许 X.Y.Z 三个数字段（自动更新按数字比较，不要写 beta/日期）。
APP_VERSION = "1.5.0"
APP_NAME = "小克"
APP_ID = "com.aikaui.claudeassistant"

# ================= 服务器 =================

SERVER_URL = "https://aikaui.com"           # 服务器根地址（注册 / 查余额 / 生成订单）
BUILTIN_BASE_URL = "https://aikaui.com/api"  # ★ 助手自己聊天用的入口
                                             # SDK 会拼成 https://aikaui.com/api/chat/completions

# Agent 的"敲门砖"。**只用于注册链路**（/api/challenge 取挑战、
# /api/auto-register 提交），服务器不再接受它去调模型接口。
# 打完包反编译就能看到，所以它不是秘密，也绝不能当秘密使。
AGENT_KEY = "78530877000c2cd6b2ab2890aebc573e416c69f6aaf1500f"

BUILTIN_MODEL = "deepseek-flash"            # 兜底模型（正常用服务器下发的 fast_model）

# ================= 注册（PoW） =================
# /api/challenge 给一个 32 位 hex 挑战，客户端找 nonce 让
# SHA256(challenge+nonce) 的前 difficulty 个**二进制位**为 0，再交给
# /api/auto-register。一次注册最多重来这么多次（每次都重新取挑战）。
# 求解本身的超时在 server/pow.py 里（POW_TIMEOUT）。
POW_MAX_ATTEMPTS = 3

# ================= 路径 =================

HOME = Path.home()                                    # 用户主目录
                                                      # 后面所有文件操作都限制在这个目录内（安全考虑）
CLAUDE_CONFIG = HOME / ".claude.json"                 # Claude Code 的主配置文件
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"  # Claude Code 的用户级设置（钉默认模型用）
IDENTITY_FILE = HOME / ".claude-agent" / "identity.json"   # Agent 自己的身份文件（存 token）
UPDATES_DIR = HOME / ".claude-agent" / "updates"      # 自动更新：下载暂存、清单缓存、日志

# ================= 自动更新 =================
# 清单放在自家服务器上（nginx 静态文件），下载完先暂存，下次启动时替换自己。
# 环境变量 AGENT_UPDATE_MANIFEST_URL 可以临时指向测试清单（本机验证用）。
UPDATE_MANIFEST_URL = os.environ.get(
    "AGENT_UPDATE_MANIFEST_URL", f"{SERVER_URL}/latest.json")
UPDATE_INITIAL_DELAY = 25                 # 启动后多久第一次检查（秒）——别跟启动抢网络
UPDATE_CHECK_INTERVAL = 6 * 3600          # 之后每 6 小时看一次
UPDATE_MAX_SIZE = 300 * 1024 * 1024       # 超过 300MB 的"更新包"一律不信
UPDATE_STAGED_TTL = 7 * 24 * 3600         # 暂存文件保留 7 天
UPDATE_LOCK_STALE = 10 * 60               # 替换锁超过 10 分钟算陈旧

# 第一个带 updater 的版本。比它老的用户程序里没有更新代码，只能手动重装。
# 清单里的 min_version 用它，而不是发布时的 APP_VERSION —— 否则每个新版本都会
# 把上一版用户挡在门外（1.0.0 < 1.0.1，永远"不够新"）。
APP_MIN_UPDATE_FROM = "1.0.0"

# ================= 运行参数 =================

MAX_STEPS = 40                      # 一轮任务最多调 40 次模型（防死循环）。
                                    # 原来是 25：装 VSCode 遇到没 winget 的机器，
                                    # 下载 + 装 winget + 装 VSCode 三步挤在一起，
                                    # 用户那边撞线了。40 是给这条长尾留的余量。

# ================= 配色 =================

BG = "#1a1a1a"          # 窗口底色
PANEL = "#242424"       # 面板 / 顶栏底色
LOG_BG = "#141414"      # 日志区底色
BORDER = "#333333"
TEXT = "#e6e6e6"        # 主文字
DIM = "#8a8a8a"         # 次要文字
BLUE = "#3b8ed0"        # 主题蓝
ORANGE = "#e8a33d"      # 余额偏低
RED = "#e05a5a"         # 余额为 0
GREEN = "#4caf7d"       # 充值成功 / 到账

# ================= 用量信息页 / 图表 =================
# 这一页是程序里唯一跟钱有关的地方：卡片比窗口底色亮一档，图表直接画在卡片上。

SURFACE = "#1f1f1f"           # 卡片 / 图表底色（比 BG 亮一档）
GRID = "#2e2e2c"              # 图表网格线（发丝线，比卡片再亮一档）
AXIS = "#3d3d3a"              # 基线（0 刻度那条）
SERIES = "#3987e5"            # 柱子的颜色。单序列 = 单色：模型身份靠轴上的名字，
                              # 不靠颜色，所以只需要一种蓝，也就不需要图例。
SERIES_HOVER = "#5598e7"      # 悬停时同一色阶提亮一档（配色校验：≥3:1 对比度）
TIP_BG = "#2b2b2b"            # 图表悬停提示框底色
WARN_BG = "#2e2415"           # 底部"是否立即充值"询问条的底色（暗琥珀）
RAIL_W = 152                  # 左侧导航栏宽度

# 选项列表（ask_user）用的颜色
OPTION_BG = "#1f1f1f"         # 未选中
OPTION_HOVER = "#2b2b2b"      # 鼠标悬停
OPTION_SEL = "#2f5d8a"        # 当前选中（键盘 ↑↓ 或鼠标点击）
OPTION_SEL_HOVER = "#3a6fa3"  # 选中项被悬停
OPTION_SEL_TEXT = "#ffffff"

# ask_user 永远附加的最后一个选项：让用户可以自己打字
FREE_TEXT_LABEL = "✏️  输入点别的"
