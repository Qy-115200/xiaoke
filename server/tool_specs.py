# -*- coding: utf-8 -*-
"""工具说明书：工具名 → Python 函数的映射表，以及发给模型的 JSON Schema 列表。

tool_map 是 Agent 的执行入口（模型说"调用 bash"，这里就找 tool_map["bash"] 执行）；
tools 是给模型看的说明书，告诉它有哪些工具、参数长什么样。
两边必须一一对应（15 个工具）。
"""

from server.api import (
    create_recharge,
    login_with_account_code,
    query_balance,
    query_pay_methods,
    query_usage,
)
from server.identity import register_device
from server.tools_fixed import (
    bypass_region_check,
    create_shortcut,
    launch_claude,
    register_protocol,
    set_model_config,
)
from server.tools_general import ask_user, bash, read_file, write_file

# ================= 工具注册 =================

# tool_map：Agent 拿到模型返回的工具名后，用这个名字找对应的 Python 函数执行
# 例如：模型说"调用 bash"，Agent 就找 tool_map["bash"] 然后执行
tool_map = {
    "register_device": register_device,
    "bypass_region_check": bypass_region_check,
    "set_model_config": set_model_config,
    "create_shortcut": create_shortcut,
    "launch_claude": launch_claude,
    "query_balance": query_balance,        # ★ 新增：查余额
    "query_usage": query_usage,            # ★ 新增：查 token 用量
    "query_pay_methods": query_pay_methods,  # ★ 新增：有哪些支付渠道
    "create_recharge": create_recharge,    # ★ 新增：创建充值订单
    "login_with_account_code": login_with_account_code,   # ★ 新增：账户码登录（换设备）
    "register_protocol": register_protocol,  # ★ 新增：注册 aikaui:// 协议
    "bash": bash,
    "read_file": read_file,
    "write_file": write_file,
    "ask_user": ask_user,
}

# tools：发给模型的"工具说明书"
# 模型看到这个列表，就知道"我能调这些工具，参数是什么样的"
# 格式：OpenAI Function Calling 标准（JSON Schema）
tools = [
    {
        "type": "function",              # 固定写法
        "function": {
            "name": "register_device",   # 工具名（跟 tool_map 的 key 一致）
            "description": "注册本机账号并拿到本机专属的 API key。首次运行会做一次人机校验，可能要等几秒。已经有账号时直接用本地凭证，不会重复注册。装完 Claude Code 后必须调用。",
            # ↑ description 非常重要！模型靠它判断"什么时候该调这个工具"
            "parameters": {              # 参数 schema
                "type": "object",
                "properties": {},         # 无参数
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bypass_region_check",
            "description": "绕过「当前国家或地区不被支持」报错。在 register_device 之后、set_model_config 之前调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_model_config",
            "description": "配置 Claude Code：把中转地址和 token 写进 ~/.claude.json 的 env，并把默认模型钉成 flash（便宜快）写进 ~/.claude/settings.json。会自动读取 register_device 保存的 token。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_shortcut",
            "description": "在桌面创建 Claude Code 快捷方式。所有配置完成后调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_claude",
            "description": (
                "打开新终端并启动 Claude Code。所有配置完成后调用，让用户立刻能用。"
                "★ 先用 ask_user 问清楚用户想在哪个文件夹里干活（工作目录），"
                "把那个目录传给 directory —— Claude Code 只看得到这一个目录里的文件。"
                "目录必须在用户主目录内，不存在会自动创建。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "工作目录的绝对路径（用户主目录之内）。留空则在用户主目录启动。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_balance",
            "description": (
                "查询用户当前账户余额（人民币元）。"
                "用户问「我还剩多少钱」「余额多少」「还能用多久」时调用。"
                "返回字段：yuan（余额，字符串）、user_id、account_code（账户码）。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_usage",
            "description": (
                "查询用户的 token 消耗情况（今日 / 本月）。"
                "用户问「用了多少 token」「今天花了多少」「这个月用了多少」「哪个模型用得多」时调用。"
                "返回 today 和 month 两组数据，各含 tokens（token 数）和 yuan（花了多少钱）；"
                "服务器给了按模型的明细时，里面还有 by_model（每个模型用了多少 token）。"
                "界面上的柱状图在「用量信息」页，不用你画图。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_pay_methods",
            "description": (
                "查服务器现在开了哪些支付渠道（支付宝 / 微信），以及各自能不能用。"
                "用户问「能用微信充值吗」「支持哪些付款方式」时调用。"
                "返回 methods 数组，每项含 channel、name、ready（false 表示这条渠道现在用不了）、reason。"
                "注意：充值窗的按钮本来就是照这个列表画的，不用你去画；这个工具只是让你能如实回答。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_recharge",
            "description": (
                "创建充值订单，拿到付款二维码。"
                "用户说「我要充值」「怎么充值」「充 20 块」时调用。"
                "返回 order_no（订单号）、channel（渠道）、img（二维码图片地址）、money（金额）。"
                "调用后告诉用户：二维码已经显示在充值窗口里，用支付宝/微信扫一下就行；"
                "付完点窗口里的「我已完成付款」刷新余额。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "充值金额，单位元。常见 20 / 50 / 100",
                    },
                    "channel": {
                        "type": "string",
                        "enum": ["alipay", "wechat"],
                        "description": "支付渠道：alipay=支付宝（默认），wechat=微信",
                    },
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "login_with_account_code",
            "description": (
                "用账户码登录，把当前电脑切到一个已有账号（换设备 / 找回账号用）。"
                "用户把账户码说给你（形如 CC-XXXXXX-XXXXXX）时调用。"
                "★ 把用户说的原话**原样**放进 account_code：不要自己更正、"
                "不要补横线或去横线、不要先判断格式对不对、也不要把语病修掉——"
                "服务器会自己归一化，能从一整句话里把码挑出来。"
                "成功返回 ok=true；失败返回 error。"
                "如果 error 里带 rate_limited（试太多次被限流），"
                "照实说'等一会儿再试'，**不要**说成'你的码错了'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "account_code": {
                        "type": "string",
                        "description": (
                            "用户给的账户码原文。可以把用户整句话都放进来——"
                            "服务器会自己把码挑出来，你不用先替它清理。"
                        ),
                    },
                },
                "required": ["account_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_protocol",
            "description": "注册 aikaui:// 自定义协议，让网页能唤起本程序（充值回调用）。通常不需要主动调用，启动时已自动注册。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令。用于检测环境、装 Node / Claude Code、查看信息等。",
            "parameters": {
                "type": "object",
                "properties": {              # 参数定义
                    "cmd": {"type": "string"},   # 命令字符串
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒数，默认 600"
                    },
                    "show_progress": {
                        "type": "boolean",
                        "description": "是否显示进度条。耗时操作（装 Node、装 Claude Code）应设为 true。",
                    },
                },
                "required": ["cmd"],         # 必填参数列表
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取用户主目录下的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入文件到用户主目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "向用户提问。以下情况必须调用："
                "1) 任何有副作用的操作（装软件、改配置）之前；"
                "2) 命令失败或遇到不确定的情况，或执行命令时信息不足。"
                "可以给 2-4 个选项。"
                "注意：question 里不要写'1. 2.'编号，选项放 options 参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},   # 字符串数组
                    },
                },
                "required": ["question"],
            },
        },
    },
]
