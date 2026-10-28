# -*- coding: utf-8 -*-
"""服务器版给模型的手脚（12 个工具）。

和 `server/tool_specs.py` 那 19 个的区别：

  · **没有** VSCode / 快捷方式 / aikaui:// 协议 / launch_claude —— 服务器上没有这些；
  · **没有** register_device / login_with_account_code —— 账号在模型上场之前就定好了，
    账户密钥更不能进模型的上下文；
  · **多了** ensure_node / install_claude / configure_claude / finish_install ——
    这些包的是已经写好、已经测过的确定性函数（`node_setup` / `claude_setup` /
    `tools_fixed`），模型只决定"什么时候调"，不用自己敲 npm。

★ 为什么要包一层而不是让模型直接 `bash("npm install -g ...")`：npm ≥ 11 会拦
  postinstall、optional 依赖失败它还不报错、132MB 原生包会断流 —— 这几条都写在
  `claude_setup` 里了（带 `--allow-scripts`、原生包重试、旧包清理）。让模型现编
  一遍，就是这次容器事故的原样重演。

★ `bash` 必须绑 `stdin=DEVNULL`：真终端里 stdin 是 tty，一个 `read` 就会把用户的
  按键吃掉，界面上表现为"打字没反应"。
"""

import json
import re
import subprocess
from pathlib import Path

from config import CLAUDE_CONFIG, CLAUDE_SETTINGS
from server import api, claude_setup, identity, node_setup, tools_general
from server.cli import CliError

# ================= 安全过滤 =================

_SUDO = re.compile(r"(?:^|[;&|]\s*)sudo\b", re.I)
_PKG = re.compile(r"\b(?:apt|apt-get|dnf|yum|zypper|pacman|apk|snap)\b[^\n]*\binstall\b", re.I)
_NPM_INSTALL = re.compile(r"\bnpm\b[^\n]*\b(?:install|i|add|ci)\b", re.I)
_IDENTITY_DIR = ".claude-agent"


def _protected(path) -> bool:
    """`~/.claude-agent/` 里是 api_key 和日志，模型碰不得。"""
    try:
        target = Path(str(path)).expanduser().resolve()
        secret = (Path.home() / _IDENTITY_DIR).resolve()
        target.relative_to(secret)
        return True
    except (ValueError, OSError):
        return False


def _fail(err) -> str:
    """把 CliError 变成模型看得懂的一句话（不带 traceback）。"""
    hint = getattr(err, "hint", "")
    text = f"失败：{getattr(err, 'message', err)}"
    if hint:
        text += f"\n提示：{hint}"
    return text


# ================= 验收 =================

def config_problems(config_path=None, settings_path=None) -> list:
    """两份配置是不是真的写对了。只报**缺哪个键**，绝不带值。"""
    problems = []
    config_path = Path(config_path or CLAUDE_CONFIG)
    settings_path = Path(settings_path or CLAUDE_SETTINGS)

    data = _read_json(config_path, problems)
    if data is not None:
        if data.get("hasCompletedOnboarding") is not True:
            problems.append(f"{config_path.name} 里没有 hasCompletedOnboarding（地区检查还会拦）")
        if not _env_ok(data):
            problems.append(f"{config_path.name} 的 env 里缺 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN")

    data = _read_json(settings_path, problems)
    if data is not None and not _env_ok(data):
        problems.append(f"{settings_path.name} 的 env 里缺 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN")
    return problems


def _read_json(path: Path, problems: list):
    if not path.exists():
        problems.append(f"{path} 不存在")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        problems.append(f"{path} 不是合法 JSON")
        return None
    if not isinstance(data, dict):
        problems.append(f"{path} 的内容不是 JSON 对象")
        return None
    return data


def _env_ok(data) -> bool:
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return False
    return bool(env.get("ANTHROPIC_BASE_URL")) and bool(env.get("ANTHROPIC_AUTH_TOKEN"))


# ================= 工具集 =================

def build_tools(ctx, args, state, ui=None):
    """造这一轮的 (tool_map, tools)。

    ctx    `server.cli.CliContext`
    args   argparse 的 Namespace（只读 ``node_version`` / ``claude_version`` / ``force``）
    state  可变的 dict，跨工具共享进度：arch / node / claude / account_code / finished
    ui     终端界面（工具不直接用，留着给以后加确认用）
    """
    node_version = str(getattr(args, "node_version", "") or "")
    claude_version = str(getattr(args, "claude_version", "") or "")

    # ---------- 安装三步 ----------
    def ensure_node():
        try:
            info = node_setup.ensure_node(
                ctx, arch=state.get("arch") or None,
                pinned=node_version or None)
        except CliError as e:
            state["node"] = {}
            return _fail(e)
        state["node"] = info or {}
        if (info or {}).get("planned"):
            return "（--dry-run）这里会下载并解压 Node，没有真的做"
        version = info.get("version") or "?"
        where = info.get("node") or ""
        return (f"Node {version} 可用（{where}）"
                if where else f"Node {version} 可用")

    def install_claude():
        node_info = state.get("node") or {}
        if not node_info.get("version"):
            return "失败：Node 还没装好，先调 ensure_node"
        try:
            info = claude_setup.ensure_claude(ctx, node_info, version=claude_version)
        except CliError as e:
            state["claude"] = {}
            return _fail(e)
        state["claude"] = info or {}
        if (info or {}).get("planned"):
            return "（--dry-run）这里会 npm 装 claude，没有真的做"
        return (f"claude {info.get('version') or '?'} 可用（{info.get('claude') or ''}）")

    def configure_claude():
        from server import tools_fixed

        try:
            tools_fixed.bypass_region_check()
            text = tools_fixed.set_model_config()
        except Exception as e:
            return f"失败：写配置时出错：{e}"
        if str(text).startswith("错误"):
            return f"失败：{text}"
        state["config_written"] = True
        return f"{text}"

    def finish_install():
        problems = []
        if not (state.get("node") or {}).get("version"):
            problems.append("Node 还没装好（先调 ensure_node）")
        claude_path = (state.get("claude") or {}).get("claude") or ""
        if not claude_path or not Path(claude_path).exists():
            problems.append("claude 命令还没装好（先调 install_claude）")
        ident = identity.load_identity()
        if not ident:
            problems.append("本机还没有账号凭证（这个不该发生，让用户重跑一次安装器）")
        problems.extend(config_problems())

        if problems:
            state["finished"] = False
            return ("还不能算装好，还缺：\n  - " + "\n  - ".join(problems)
                    + "\n按顺序补齐，然后再调一次 finish_install。")

        state["finished"] = True
        state["account_code"] = (ident or {}).get("account_code") or ""
        return ("已装好：Node、claude、账号、两份配置都齐了。\n"
                "现在用两三句话告诉用户：装好了、账户码是哪个、余额还剩多少，"
                "并说一句马上会帮他打开 claude。不用再调工具了。")

    # ---------- 通用手脚 ----------
    def run_bash(cmd, timeout=600, show_progress=False):
        text = str(cmd or "")
        if _SUDO.search(text):
            return "拒绝：服务器版不跑 sudo（不需要系统权限，装东西全在用户主目录里）"
        if _PKG.search(text):
            return "拒绝：不要用包管理器装东西。装 Node 请调 ensure_node"
        if _NPM_INSTALL.search(text):
            return "拒绝：不要自己跑 npm 装 claude，请调 install_claude"
        if _IDENTITY_DIR in text:
            return "拒绝：不能碰 ~/.claude-agent/（里面有密钥）"
        return tools_general.bash(text, timeout=timeout,
                                  show_progress=show_progress,
                                  stdin=subprocess.DEVNULL)

    def read_file(path):
        if _protected(path):
            return "拒绝：不能读 ~/.claude-agent/ 下的文件（里面有密钥）"
        return tools_general.read_file(path)

    def write_file(path, content):
        if _protected(path):
            return "拒绝：不能写 ~/.claude-agent/ 下的文件（里面有密钥）"
        return tools_general.write_file(path, content)

    def ask_user(question, options=None):
        return tools_general.ask_user(question, options or [])

    tool_map = {
        "ensure_node": ensure_node,
        "install_claude": install_claude,
        "configure_claude": configure_claude,
        "finish_install": finish_install,
        "bash": run_bash,
        "read_file": read_file,
        "write_file": write_file,
        "ask_user": ask_user,
        "query_balance": api.query_balance,
        "query_usage": api.query_usage,
        "query_pay_methods": api.query_pay_methods,
        "create_recharge": api.create_recharge,
    }
    tools = _schemas()
    return tool_map, tools


# ================= 说明书 =================

def _schemas() -> list:
    def fn(name, description, properties=None, required=None):
        params = {"type": "object", "properties": properties or {}}
        if required:
            params["required"] = required
        return {"type": "function", "function": {
            "name": name, "description": description, "parameters": params}}

    return [
        fn("ensure_node",
           "保证这台机器上有 Node 22+。已经有了就什么都不做；没有就下到 ~/.local/node "
           "（不需要 root，也不用包管理器）。装 claude 之前必须先调它。"
           "返回「Node x.y.z 可用」= 成功；返回以「失败：」开头 = 没成，照实告诉用户。"),
        fn("install_claude",
           "装 claude 命令（@anthropic-ai/claude-code）。这一步最慢，可能要几分钟，"
           "慢网络下不刷新进度是正常的。里面已经处理了 npm 拦安装脚本、"
           "原生包下载失败重试这些坑，**所以你不用自己跑 npm**。"
           "必须先调过 ensure_node。返回「claude x.y.z 可用」= 成功。"),
        fn("configure_claude",
           "把中转地址、token 和默认模型写进 ~/.claude.json 和 ~/.claude/settings.json。"
           "token 从本机账号里读，返回值里不会出现它。"),
        fn("finish_install",
           "验收：Node、claude、账号、两份配置都齐了才回「已装好」。"
           "没齐会逐条告诉你缺什么 —— 照着补，然后**再调一次**这个工具。"
           "只有它说「已装好」才算装完；别自己宣布成功。"),
        fn("bash",
           "执行一条 shell 命令（查版本、看磁盘、看日志用）。"
           "★ 禁止用 sudo，禁止用 apt/dnf/yum/snap 装东西，禁止用 npm 装 claude —— "
           "这几样会被直接拒绝。装东西请用专用工具。",
           {"cmd": {"type": "string", "description": "要执行的命令"},
            "timeout": {"type": "integer", "description": "超时秒数，默认 600"},
            "show_progress": {"type": "boolean",
                              "description": "耗时命令设 true，界面上会转圈"}},
           ["cmd"]),
        fn("read_file", "读一个文件（只能读主目录里面的）。",
           {"path": {"type": "string"}}, ["path"]),
        fn("write_file", "写一个文件（只能写主目录里面的）。",
           {"path": {"type": "string"}, "content": {"type": "string"}},
           ["path", "content"]),
        fn("ask_user",
           "向用户提问。只有真的要他拿主意时才问（装软件、改配置不用问）。"
           "question 里**不要写编号**，选项放 options（2~4 个）。",
           {"question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}},
           ["question"]),
        fn("query_balance",
           "查当前账号的余额（元）。返回 yuan、account_code 等字段。"
           "总结的时候用得上。"),
        fn("query_usage",
           "查 token 用量（今日 / 本月）。用户问「用了多少」时调。"),
        fn("query_pay_methods",
           "查服务器现在支持哪些充值渠道（支付宝 / 微信）。"),
        fn("create_recharge",
           "创建充值订单并拿到付款二维码。用户说「充值」「充 20 块」时调。",
           {"amount": {"type": "integer", "description": "金额（元），常见 20 / 50 / 100"},
            "channel": {"type": "string", "enum": ["alipay", "wechat"],
                        "description": "支付渠道，默认支付宝"}},
           ["amount"]),
    ]


__all__ = ["build_tools"]
