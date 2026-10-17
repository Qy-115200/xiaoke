# -*- coding: utf-8 -*-
"""固定工具：Agent 必须按顺序调用，不能自己用 bash 实现的那几个。

  register_protocol / parse_protocol_arg   aikaui:// 自定义协议
  bypass_region_check                      绕过地区检查
  set_model_config                         把中转地址和 token 写进 ~/.claude.json
  create_shortcut                          桌面快捷方式
  launch_claude                            开新终端启动 Claude Code

（register_device 在 identity.py，因为那一层管身份。五个固定工具的调用顺序是
 register_device → bypass_region_check → set_model_config → create_shortcut → launch_claude）
"""

import json
import platform
import subprocess
import sys
import winreg
from pathlib import Path
from urllib.parse import parse_qsl

from config import CLAUDE_CONFIG, CLAUDE_SETTINGS, HOME, IDENTITY_FILE

def _normalize_base(base: str) -> str:
    """规范化 base URL：
    Claude Code 会自己拼 /v1/messages，
    如果 base 以 /v1 结尾，会拼成 /v1/v1/messages → 404。
    所以这里统一去掉尾部 /v1。"""
    base = base.rstrip("/")              # 先去掉所有尾部斜杠
    if base.endswith("/v1"):             # 如果以 /v1 结尾
        base = base[:-3]                 # 去掉最后 3 个字符（也就是 "/v1"）
    return base
# ================= Windows 自定义协议 aikaui:// =================
# 支付页面付完款后，会打开 aikaui://recharge?user_id=7 这样的链接，
# Windows 靠注册表里的这个协议，把这个链接转成"启动我们的 exe 并带上参数"。

def register_protocol() -> str:
    """注册 aikaui:// 自定义协议（Windows）。已注册则覆盖成当前 exe 路径。"""
    if platform.system() != "Windows":
        return "非 Windows 系统，跳过"

    # ---------- 找出"要启动什么" ----------
    if getattr(sys, "frozen", False):
        # 打包成 exe 后：直接启动自己，把 URL 当参数传进去
        command = f'"{Path(sys.executable).resolve()}" "%1"'
    else:
        # 开发环境（python main.py）：带上解释器和脚本路径，方便本地调试
        script = Path(sys.argv[0]).resolve()
        command = f'"{Path(sys.executable).resolve()}" "{script}" "%1"'

    try:
        # HKEY_CURRENT_USER\Software\Classes\aikaui
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:AI 管家")   # 描述
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")  # ★ 有这行才被认成协议
        # ...\aikaui\shell\open\command
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui\shell\open\command"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except Exception as e:
        return f"注册协议失败：{e}"

    return "已注册协议 aikaui://"


def parse_protocol_arg() -> dict:
    """从 sys.argv 解析 aikaui:// 协议参数。
    例：sys.argv[1] = "aikaui://recharge?user_id=7"
    返回：{"action": "recharge", "user_id": "7"}；没有参数返回 {}"""
    for arg in sys.argv[1:]:
        if not arg.startswith("aikaui://"):
            continue
        rest = arg[len("aikaui://"):]            # "recharge?user_id=7"
        action, _, query = rest.partition("?")   # action="recharge", query="user_id=7"
        # Windows 把链接交给协议处理程序时，末尾常常多一个斜杠（aikaui://recharge/），
        # 不剥掉的话 action 会变成 "recharge/"，认不出来。
        action = action.strip().rstrip("/").lower()
        out = {"action": action}
        for k, v in parse_qsl(query):            # 解析查询串
            out[k] = v
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
    """把中转地址 / token / 模型配置写进 ~/.claude.json 和 ~/.claude/settings.json。
    从 identity.json 读取 token（register_device 存的）。

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
        return f"错误：身份文件字段缺失：{identity}"

    api_base = _normalize_base(api_base)           # 去掉 /v1 后缀（防路径重复）

    # 读现有 Claude 配置（保留用户其他字段）
    data = {}
    if CLAUDE_CONFIG.exists():
        try:
            data = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ★ 合并 env（不整体覆盖，保留用户可能设的其他 env 变量）
    env = data.get("env") or {}
    env["ANTHROPIC_BASE_URL"] = api_base            # 中转地址
    env["ANTHROPIC_AUTH_TOKEN"] = api_key           # ★ 注意：是 AUTH_TOKEN，不是 API_KEY

    # --- 默认层：全部指向快模型 ---
    env["ANTHROPIC_MODEL"] = fast_model              # 新会话的主模型
    env["ANTHROPIC_SMALL_FAST_MODEL"] = fast_model   # 后台小任务
    env["ANTHROPIC_DEFAULT_MODEL"] = fast_model      # 新会话的起始默认（比 settings 低一级）
    # ★ Claude Code 的默认档就是 sonnet 档。这一行原来映射的是强模型，
    #   导致用户没主动选模型时也会打到 pro 上——这就是"偷偷调用 pro"的来源。
    env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = fast_model
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = fast_model

    # --- 显式层：只有用户自己 /model 选 opus 时才落到强模型 ---
    env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model

    data["env"] = env                              # 写回 env 字段
    data["hasCompletedOnboarding"] = True          # 顺便再保证一次绕过地区

    CLAUDE_CONFIG.write_text(                       # 写文件
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # ★ 双保险：再钉一次 settings.json 的 "model" 字段。
    #   ~/.claude/settings.json 里的 model 是"持久化的默认模型"，优先级低于
    #   用户在 /model 里的显式选择 —— 所以钉住它 = 默认走 flash，
    #   但用户显式选 opus 依然能切到 pro，正好是我们想要的语义。
    settings_note = _pin_settings_model(fast_model)

    # 返回"人话版"配置总结给 Agent（token 只显示前 12 位，避免泄露）
    return (
        f"已配置：\n"
        f"  BASE_URL      = {api_base}\n"
        f"  AUTH_TOKEN    = {api_key[:12]}...\n"
        f"  默认模型      = {fast_model}（sonnet / haiku 档都指向它）\n"
        f"  显式 opus 时  = {model}\n"
        f"  {settings_note}"
    )


def _pin_settings_model(fast_model: str) -> str:
    """把 ~/.claude/settings.json 的 "model" 钉成 fast_model（保留其它字段）。

    ★ 用完整模型名而不是别名：别名还要再走一遍 ANTHROPIC_DEFAULT_* 映射，
    直接写完整名可以完全绕开别名解析。

    写不进去不算致命错误（env 那一层已经生效了），所以这里吞掉异常只回一句说明。
    """
    try:
        s = {}
        if CLAUDE_SETTINGS.exists():
            try:
                s = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
            except Exception:
                s = {}                       # 文件损坏就当空的重建
        if not isinstance(s, dict):
            s = {}

        if s.get("model") == fast_model:
            return f"默认模型已钉住（{CLAUDE_SETTINGS}，无需改动）"

        s["model"] = fast_model              # ★ 只动这一个键，modelSettings 等原样保留
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"已把默认模型钉进 settings.json（{CLAUDE_SETTINGS}）"
    except Exception as e:
        return f"（settings.json 写入失败，env 已生效：{e}）"


def create_shortcut() -> str:
    """在桌面创建 Claude Code 快捷方式（Windows 专属）"""
    if platform.system() != "Windows":          # 只支持 Windows
        return "非 Windows 系统，跳过"

    try:
        import win32com.client                  # Windows COM 接口（创建 .lnk 用）
    except ImportError:
        return "缺少 pywin32，无法创建快捷方式"

    # ---------- 找 claude 可执行文件 ----------
    claude_path = None
    for p in [
        HOME / "AppData" / "Roaming" / "npm" / "claude.cmd",                  # npm 装的
        HOME / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",    # 官方原生装的
        Path("C:/Program Files/claude/claude.exe"),                            # 系统级装的
    ]:
        if p.exists():
            claude_path = str(p)
            break
    if not claude_path:                          # 都没找到
        import shutil
        claude_path = shutil.which("claude")     # 从 PATH 里找
    if not claude_path:
        return "找不到 claude 命令，可能没装好"

    # ---------- 查真实桌面路径 ----------
    # 不能直接用 Path.home()/"Desktop"，因为可能被 OneDrive 重定向
    # 查注册表拿到真实桌面路径（兼容所有情况）
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
    )
    desktop = Path(winreg.QueryValueEx(key, "Desktop")[0])
    # QueryValueEx 返回 (值, 类型) 的元组，[0] 取值

    # ---------- 创建快捷方式 ----------
    shell = win32com.client.Dispatch("WScript.Shell")   # 创建 COM 对象
    lnk_path = desktop / "Claude Code.lnk"              # 快捷方式路径
    lnk = shell.CreateShortCut(str(lnk_path))           # 创建快捷方式对象

    if claude_path.endswith((".cmd", ".bat")):          # .cmd/.bat 要通过 cmd.exe 运行
        lnk.TargetPath = "cmd.exe"                       # 目标：cmd.exe
        lnk.Arguments = f'/k "{claude_path}"'            # /k = 执行完后保留窗口
    else:
        lnk.TargetPath = claude_path                     # 直接指向 exe

    lnk.WorkingDirectory = str(HOME)                     # 工作目录设为用户主目录
    lnk.Description = "启动 Claude Code"                 # 备注
    lnk.Save()                                           # 保存
    return f"已创建桌面快捷方式：{lnk_path}"


def launch_claude(directory: str = "") -> str:
    """打开新终端窗口，在指定目录里启动 Claude Code（Windows 专属）。

    directory —— 工作目录。Claude Code 只看得到这一个目录里的文件，
                 所以启动前应该先问用户"以后在哪个文件夹里干活"，
                 再把那个目录传进来（见 prompts.py 步骤 5）。
                 留空 = 用户主目录。

    ★ 只接受用户主目录里的路径：越界的一律拒绝，退回主目录并说明原因
      —— 跟 read_file / write_file 用的是同一条安全规则。"""
    if platform.system() != "Windows":
        return "非 Windows 系统，跳过"

    # 找 claude（同上）
    claude_path = None
    for p in [
        HOME / "AppData" / "Roaming" / "npm" / "claude.cmd",
        HOME / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
    ]:
        if p.exists():
            claude_path = str(p)
            break
    if not claude_path:
        import shutil
        claude_path = shutil.which("claude")
    if not claude_path:
        return "找不到 claude 命令，无法启动"

    # ---------- 定工作目录 ----------
    note = ""
    cwd = HOME
    if directory and str(directory).strip():
        try:
            cand = Path(str(directory).strip()).expanduser()
            cand.resolve().relative_to(HOME.resolve())    # ★ 越界检查
            cand.mkdir(parents=True, exist_ok=True)       # 目录不存在就建出来
            cwd = cand
        except ValueError:
            note = f"（{directory} 不在用户主目录里，为了安全改在主目录启动）"
        except Exception as e:
            note = f"（目录 {directory} 用不了：{e}，改在主目录启动）"

    try:
        # ★ subprocess.Popen：非阻塞启动（立即返回，不等待进程结束）
        # start "Claude Code" cmd /k "xxx" 是 Windows 命令：
        #   start = 打开新窗口
        #   "Claude Code" = 窗口标题
        #   cmd /k = 执行完命令后保留窗口
        subprocess.Popen(
            f'start "Claude Code" cmd /k "{claude_path}"',
            shell=True,
            cwd=str(cwd),                   # ★ 工作目录 = 用户选的那个文件夹
        )
        return f"已打开新终端启动 Claude Code（{claude_path}），工作目录：{cwd}{note}"
    except Exception as e:
        return f"启动失败：{e}"
