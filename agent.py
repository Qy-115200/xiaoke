import os
import json
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# 从 .env 文件加载环境变量（API Key、Base URL、模型名等）
# Path(__file__).parent 保证无论从哪个目录运行脚本，都能找到同目录下的 .env
load_dotenv(Path(__file__).parent / ".env")

# 创建 OpenAI 兼容客户端
# DeepSeek 兼容 OpenAI 接口，所以只需换 base_url 即可
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),          # 从环境变量读取密钥
    base_url=os.getenv("OPENAI_BASE_URL") or None, # 若未配置则回退到 OpenAI 官方地址
)

# 模型名称，默认 deepseek-chat
MODEL = os.getenv("MODEL", "deepseek-chat")

# 沙箱目录：Agent 只能在此目录内操作，防止误改系统文件
ROOT = (Path(__file__).parent / "sandbox").resolve()  # 转为绝对路径
ROOT.mkdir(exist_ok=True)                              # 不存在时自动创建


def safe_path(path: str) -> Path:
    """
    将相对路径安全地解析到 sandbox 目录内。
    如果解析后的路径不在 sandbox 内，抛出异常，防止路径穿越攻击。
    """
    p = (ROOT / path).resolve()                # 拼接并解析为绝对路径
    if not str(p).startswith(str(ROOT)):       # 检查是否仍在 sandbox 内
        raise ValueError("路径越界，只允许操作 sandbox 目录")
    return p


def read_file(path: str) -> str:
    """读取 sandbox 内文件内容，最多返回 4000 字符，避免上下文过长。"""
    return safe_path(path).read_text(encoding="utf-8")[:4000]


def write_file(path: str, content: str) -> str:
    """向 sandbox 写入文件，自动创建父目录。"""
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)   # 例如 src/utils/ 这类子目录
    p.write_text(content, encoding="utf-8")       # 真正写文件
    return f"已写入 {path}"                        # 返回给模型作为执行结果


def bash(cmd: str) -> str:
    """
    在 sandbox 目录内执行 shell 命令。
    包含简单的黑名单拦截、超时控制和输出截断。
    """
    # 危险命令黑名单，命中则直接拒绝执行
    forbidden = ["rm -rf", "sudo", "curl", "wget", "format", "del /f"]
    if any(x in cmd for x in forbidden):
        return "命令被安全策略拒绝"

    try:
        # 在 sandbox 下执行命令，捕获 stdout 和 stderr，最长 30 秒
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ROOT,
        )
        # 合并标准输出和错误输出，截断至 4000 字符
        return (r.stdout + r.stderr)[:4000]
    except subprocess.TimeoutExpired:
        return "命令超时"


# 工具名到实际函数的映射，模型通过名字调用对应工具
tool_map = {
    "read_file": read_file,
    "write_file": write_file,
    "bash": bash,
}

# 提供给模型的工具定义（JSON Schema 格式）
# 模型根据这些描述决定何时调用哪个工具、传什么参数
tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 sandbox 内文件内容",
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
            "description": "写入文件到 sandbox",
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
            "name": "bash",
            "description": "在 sandbox 目录执行 shell 命令",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
]

# 对话历史，system 消息设定 Agent 的角色和边界
messages = [
    {
        "role": "system",
        "content": (
            "你是一个编码代理，只能在 sandbox 目录内工作。"
            "需要时调用工具，完成后直接回答。"
        ),
    }
]

# 从终端读取用户任务，并作为第一条 user 消息加入历史
task = input("请输入任务：")
messages.append({"role": "user", "content": task})

# Agent 核心循环：最多 10 轮，防止模型无限调用工具
for step in range(10):
    # 将完整对话历史和工具定义发给模型
    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=tools,
    )

    # 取出模型本轮回复（可能包含文本，也可能包含工具调用请求）
    msg = resp.choices[0].message
    messages.append(msg)  # 把模型回复也加入历史，供下一轮使用

    # 如果模型没有要求调用工具，说明它给出了最终回答
    if not msg.tool_calls:
        print("\nAgent:", msg.content)
        break

    # 遍历模型要求的每一次工具调用
    for tc in msg.tool_calls:
        name = tc.function.name               # 工具名，如 "write_file"
        try:
            # 模型返回的参数是 JSON 字符串，需要解析为字典
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            args = {}                         # 解析失败时给空字典，避免崩溃

        print(f"[工具] {name} {args}")        # 打印出来便于观察

        try:
            # 根据工具名找到函数并用关键字参数调用
            result = tool_map[name](**args)
        except Exception as e:
            # 工具执行出错时不中断程序，而是把错误信息返回给模型
            result = f"错误: {e}"

        # 将工具执行结果作为 role="tool" 的消息追加到历史
        # tool_call_id 必须与本次调用的 id 一致，否则 API 会报错
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": str(result),
        })

# for...else：如果循环正常结束（即 10 轮都没 break），说明未完成任务
else:
    print("达到最大步数，停止。")