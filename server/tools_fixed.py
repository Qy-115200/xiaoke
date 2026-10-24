# -*- coding: utf-8 -*-
"""固定工具：Agent 必须按顺序调用，不能自己用 bash 实现的那几个。

  register_protocol / parse_protocol_url / parse_protocol_arg   aikaui:// 自定义协议
  bypass_region_check                      绕过地区检查
  set_model_config                         把中转地址和 token 写进 ~/.claude.json
  create_shortcut                          桌面快捷方式
  launch_claude                            开新终端启动 Claude Code

（register_device 在 identity.py，因为那一层管身份。五个固定工具的调用顺序是
 register_device → bypass_region_check → set_model_config → create_shortcut → launch_claude）
"""

import json
import sys
from urllib.parse import parse_qsl

import platform_support
from config import CLAUDE_CONFIG, IDENTITY_FILE
from server.claude_config import (
    apply as apply_identity_config,
    normalize_base as _normalize_base,
)

# ================= Windows 自定义协议 aikaui:// =================
# 支付页面付完款后，会打开 aikaui://recharge?user_id=7 这样的链接，
# Windows 靠注册表里的这个协议，把这个链接转成"启动我们的 exe 并带上参数"。

def register_protocol() -> str:
    """注册 aikaui:// 自定义协议（具体怎么做交给 platform_support）。

    Windows 写注册表；Linux 写 .desktop + xdg-mime；macOS 由 Info.plist 声明。"""
    return platform_support.register_url_protocol()


def parse_protocol_url(url: str) -> dict:
    """解析一条 aikaui:// 链接。
    例："aikaui://recharge?user_id=7" → {"action": "recharge", "user_id": "7"}
    不是 aikaui:// 开头就返回 {}。

    ★ macOS 的 Apple Event 处理器（platform_support.macos）也复用这个函数，
      保证三平台解析规则完全一致。"""
    prefix = "aikaui://"
    # 协议名按 RFC 3986 是大小写不敏感的，别因为系统把链接写成 AIKaui:// 就认不出来
    if not isinstance(url, str) or url[:len(prefix)].lower() != prefix:
        return {}
    rest = url[len(prefix):]                 # "recharge?user_id=7"
    action, _, query = rest.partition("?")   # action="recharge", query="user_id=7"
    # Windows 把链接交给协议处理程序时，末尾常常多一个斜杠（aikaui://recharge/），
    # 不剥掉的话 action 会变成 "recharge/"，认不出来。
    action = action.strip().rstrip("/").lower()
    out = {"action": action}
    for k, v in parse_qsl(query):            # 解析查询串
        out[k] = v
    return out


def parse_protocol_arg() -> dict:
    """从 sys.argv 解析 aikaui:// 协议参数；没有参数返回 {}。"""
    for arg in sys.argv[1:]:
        out = parse_protocol_url(arg)
        if out:
            return out
    return {}
def bypass_region_check() -> str:
    """绕过 Claude Code 的地区检查。
    做法：在 ~/.claude.json 里加一行 "hasCompletedOnboarding": true。
    这行会让 Claude Code 以为你"已经完成过首次引导"，跳过地区检测。"""
    data = {}                                  # 初始化为空字典
    if CLAUDE_CONFIG.exists():                 # 配置文件存在
        try:
            data = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))   # 读现有配置
        except Exception:
            pass                               # 解析失败就当作空文件

    data["hasCompletedOnboarding"] = True      # ★ 关键字段
    CLAUDE_CONFIG.write_text(                   # 写回（保留原有其他字段）
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return f"已写入 hasCompletedOnboarding → {CLAUDE_CONFIG}"


def set_model_config() -> str:
    """把中转地址 / token / 模型配置写进 ~/.claude.json **和** ~/.claude/settings.json。
    从 identity.json 读取 token（register_device 存的）。

    ★ 两个文件都要写：settings.json 的 env 优先级更高，只写 ~/.claude.json
      会出现"界面上换了账号、终端还在烧旧账号"（见 server/claude_config.py）。

    ★ 模型档位怎么分的（别改错，改错了用户会莫名其妙被计费到贵模型上）：
      · 默认层 = fast_model（便宜快）。Claude Code 没被明确指定模型时走的是
        "sonnet" 档，所以 ANTHROPIC_DEFAULT_SONNET_MODEL 必须映射到 fast_model——
        原来这里映射的是强模型，结果用户什么都没选就被"偷偷"调到 pro 上。
      · 显式层 = model（强模型）。只有用户自己在 /model 里选 opus 时才走 pro。
    """
    # 先检查身份文件存在（必须先领 token）
    if not IDENTITY_FILE.exists():
        return "错误：还没领取 token，请先调用 register_device()"

    identity = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))   # 读身份文件
    api_base = identity.get("api_base", "")        # 中转地址
    api_key = identity.get("api_key", "")          # 虚拟 token
    model = identity.get("model", "deepseek-v4-pro")   # 强模型（显式选 opus 时才用）
    fast_model = identity.get("fast_model") or model   # 快模型（没配就用强模型）

    if not api_key or not api_base:                # 缺字段
        # ★ 不把 identity 整个打出来：里面有 api_key / account_key
        missing = "、".join(k for k, v in (("api_key", api_key), ("api_base", api_base)) if not v)
        return f"错误：身份文件里缺 {missing}，请重新调用 register_device()"

    api_base = _normalize_base(api_base)           # 去掉 /v1 后缀（防路径重复）

    ok, settings_note = apply_identity_config(identity)
    if not ok:
        return f"错误：{settings_note}"

    # 返回"人话版"配置总结给 Agent。
    # ★ 不打印 token 的任何片段：截断 8 位也算泄露（聊天记录会被截图 / 上报）。
    return (
        f"已配置：\n"
        f"  BASE_URL      = {api_base}\n"
        f"  AUTH_TOKEN    = 已写入（不显示）\n"
        f"  默认模型      = {fast_model}（sonnet / haiku 档都指向它）\n"
        f"  显式 opus 时  = {model}\n"
        f"  {settings_note}"
    )


def create_shortcut() -> str:
    """在桌面创建 Claude Code 快捷方式（Windows: .lnk / macOS: 符号链接 / Linux: .desktop）"""
    return platform_support.create_shortcut()


def launch_claude(directory: str = "") -> str:
    """打开新终端窗口，在指定目录里启动 Claude Code。

    directory —— 工作目录。Claude Code 只看得到这一个目录里的文件，
                 所以启动前应该先问用户"以后在哪个文件夹里干活"，
                 再把那个目录传进来（见 prompts.py 步骤 5）。
                 留空 = 用户主目录。

    ★ 只接受用户主目录里的路径：越界的一律拒绝，退回主目录并说明原因
      —— 跟 read_file / write_file 用的是同一条安全规则。
    ★ 返回值必须以「已打开新终端」开头：agent_core 靠这个前缀判断安装完成。"""
    return platform_support.launch_claude(directory)
