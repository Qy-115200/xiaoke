# -*- coding: utf-8 -*-
"""唯一的常量源。

这里集中放着整个程序用到的所有字面量：服务器地址、内置凭证、路径、
配色、以及几个全局阈值。其它模块只从这里 import，不要再自己写一份。

这些常量会被 PyInstaller 打进入 exe，用户反编译能看到，
但里面没有 DeepSeek key，只有一个"半公开"的 AGENT_KEY（服务器会校验 + 限流）。
"""

from pathlib import Path

# ================= 服务器 =================

SERVER_URL = "https://aikaui.com"           # 服务器根地址（领 token / 查余额 / 生成订单）
BUILTIN_BASE_URL = "https://aikaui.com/api"  # ★ 服务器代理入口
                                             # SDK 会拼成 https://aikaui.com/api/chat/completions

# Agent 的身份凭证。服务器用它判断"是不是我们的 Agent"。
# 注意：BUILTIN_API_KEY 和 AGENT_KEY 是同一个字面量，别写成两个不一样的值。
AGENT_KEY = "78530877000c2cd6b2ab2890aebc573e416c69f6aaf1500f"
BUILTIN_API_KEY = AGENT_KEY

BUILTIN_MODEL = "deepseek-flash"            # 默认模型（便宜快，够 Agent 打招呼/答疑）

# ================= 路径 =================

HOME = Path.home()                                    # 用户主目录
                                                      # 后面所有文件操作都限制在这个目录内（安全考虑）
CLAUDE_CONFIG = HOME / ".claude.json"                 # Claude Code 的主配置文件
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"  # Claude Code 的用户级设置（钉默认模型用）
IDENTITY_FILE = HOME / ".claude-agent" / "identity.json"   # Agent 自己的身份文件（存 token）

# ================= 运行参数 =================

MAX_STEPS = 25                      # 一轮任务最多调 25 次模型（防死循环）

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
