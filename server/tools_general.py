# -*- coding: utf-8 -*-
"""通用工具：Agent 干活要用的手脚。

  bash(cmd, timeout, show_progress)   执行命令（Windows 上强制 UTF-8 输出，智能解码）
  read_file(path)                     读文件（限制在用户主目录内）
  write_file(path, content)            写文件（同上）
  ask_user(question, options)          向用户提问（有 GUI 走 GUI，否则命令行）

这几个工具都是"有 GUI 就顺手更新界面"：进度条和提问都靠 server/ui_bridge 里
注入的对象，取的时候一律走 get_ui()。
"""

import os
import platform
import subprocess
from pathlib import Path

from config import HOME
from server.ui_bridge import get_ui

# ================= 通用工具 =================

def _smart_decode(b: bytes) -> str:
    """智能解码：先试 UTF-8，失败回退 GBK。
    Windows 的 cmd.exe 内置错误信息是 GBK，其他程序输出可能是 UTF-8，
    所以要"猜"编码。"""
    if not b:
        return ""
    # 优先 UTF-8
    try:
        s = b.decode("utf-8")
        if "\ufffd" not in s:              # \ufffd 是"替换字符"，出现说明解错了
            return s
    except UnicodeDecodeError:             # 不是合法 UTF-8
        pass
    # 回退 GBK
    try:
        return b.decode("gbk", errors="replace")   # errors="replace" 让解不出的字符变 ?
    except Exception:
        return b.decode("utf-8", errors="replace")  # 兜底


def bash(cmd: str, timeout: int = 600, show_progress: bool = False) -> str:
    """执行 shell 命令，返回输出。
    参数：
      cmd           要执行的命令
      timeout       超时秒数，默认 600（10 分钟）
      show_progress 是否显示进度条
    """
    # ---------- 安全过滤：拒绝危险命令 ----------
    forbidden = ["rm -rf /", "sudo rm", "format c:", "del /f /s /q c:"]
    if any(x.lower() in cmd.lower() for x in forbidden):
        return "命令被安全策略拒绝"

    # ---------- 环境变量：补 PATH ----------
    # 刚装完 Node，需要手动把安装目录加进 PATH
    env = os.environ.copy()               # 复制当前环境变量
    for p in [
        r"C:\Program Files\nodejs",       # Node 的默认安装目录
        str(HOME / "AppData" / "Roaming" / "npm"),   # npm 全局包目录
    ]:
        if Path(p).exists() and p not in env.get("PATH", ""):
            env["PATH"] = p + os.pathsep + env.get("PATH", "")   # 前置到 PATH
            # os.pathsep 在 Windows 是 ;，Linux/macOS 是 :

    # ---------- 编码处理 ----------
    if platform.system() == "Windows":
        cmd = f'chcp 65001 >nul && {cmd}'    # 强制 UTF-8 输出（对程序输出有效）
                                             # >nul 是重定向，把 chcp 的提示吞掉

    # ---------- 显示进度条 ----------
    u = get_ui()
    if show_progress and u:
        short = cmd[:80] + ("..." if len(cmd) > 80 else "")   # 命令太长就截断
        u.ui_progress_start(f"正在执行：{short}")

    try:
        # ★ 关键：不加 text=True，拿 bytes（原始字节）
        # 因为 Windows 的 cmd 输出编码不确定，拿 bytes 自己解码更稳
        r = subprocess.run(
            cmd,
            shell=True,                  # 通过系统 shell 执行（支持管道、&& 等）
            capture_output=True,         # 捕获 stdout 和 stderr
            timeout=timeout,
            env=env,                     # 传入补了 PATH 的环境变量
        )
        stdout = _smart_decode(r.stdout or b"")   # 手动解码
        stderr = _smart_decode(r.stderr or b"")
        out = (stdout + stderr).strip()           # 合并 + 去首尾空白
        return out[:3000] or f"(完成，退出码 {r.returncode})"   # 截断到 3000 字
    except subprocess.TimeoutExpired:
        return f"[超时] 命令超过 {timeout} 秒"
    except Exception as e:
        return f"[失败] {e}"
    finally:                             # ★ finally：无论成功失败都执行
        if show_progress and get_ui():
            get_ui().ui_progress_stop()  # 关闭进度条


def read_file(path: str) -> str:
    """读文件（限制在用户主目录内）"""
    p = Path(path).expanduser()          # expanduser() 把 ~ 展开成用户主目录
    # ★ 安全检查：确保路径在 HOME 里面，防止读取系统敏感文件
    try:
        p.resolve().relative_to(HOME.resolve())
        # resolve() 把路径转成绝对路径（解掉 .. 等）
        # relative_to() 判断是否是 HOME 的子路径，不是就抛异常
    except ValueError:
        return "错误：只能读取用户主目录内的文件"

    if not p.exists():
        return f"文件不存在：{p}"
    try:
        return p.read_text(encoding="utf-8")[:6000]   # 最多返回 6000 字
    except Exception as e:
        return f"读取失败：{e}"


def write_file(path: str, content: str) -> str:
    """写文件（限制在用户主目录内）"""
    p = Path(path).expanduser()
    try:
        p.resolve().relative_to(HOME.resolve())    # 同上安全检查
    except ValueError:
        return "错误：只能写入用户主目录内的文件"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)    # 确保父目录存在
        # parents=True 递归创建，exist_ok=True 已存在不报错
        p.write_text(content, encoding="utf-8")
        return f"已写入 {p}"
    except Exception as e:
        return f"写入失败：{e}"


def ask_user(question: str, options: list = None) -> str:
    """向用户提问。
    如果有 GUI，用 GUI 弹窗；否则用命令行（调试用）。"""
    u = get_ui()
    if u:
        return u.ui_ask(question, options or [])
    # 无 GUI 的 fallback（比如命令行测试）
    print(question)
    for i, opt in enumerate(options or [], 1):
        print(f"  {i}. {opt}")
    return f"用户回答：{input('> ')}"
