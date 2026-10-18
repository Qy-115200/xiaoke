# -*- coding: utf-8 -*-
"""通用工具：Agent 干活要用的手脚。

  bash(cmd, timeout, show_progress)   执行命令（Windows 上强制 UTF-8 输出，智能解码）
  read_file(path)                     读文件（限制在用户主目录内）
  write_file(path, content)            写文件（同上）
  ask_user(question, options)          向用户提问（有 GUI 走 GUI，否则命令行）

这几个工具都是"有 GUI 就顺手更新界面"：进度条和提问都靠 server/ui_bridge 里
注入的对象，取的时候一律走 get_ui()。

平台差异（补 PATH、用哪个 shell、怎么解码）都在 platform_support 里，
这里只负责"跑命令 + 把输出交回去"。
"""

import locale
import os
import signal
import subprocess
from pathlib import Path

import platform_support
from config import HOME
from server.ui_bridge import get_ui

# ================= 通用工具 =================

def _decode_with(b: bytes, encoding: str) -> str | None:
    """按指定编码解，解出替换字符就当失败（返回 None）。"""
    try:
        s = b.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None
    return None if "�" in s else s


def _smart_decode(b: bytes) -> str:
    """智能解码：先试 UTF-8，失败再试系统本地编码。

    Windows 的 cmd.exe 内置错误信息是 GBK（中文系统）或别的 ANSI 代码页，
    其它程序输出又可能是 UTF-8，所以要"猜"编码；macOS/Linux 的本地编码
    通常就是 UTF-8，这一步直接跳过。"""
    if not b:
        return ""
    s = _decode_with(b, "utf-8")           # 优先 UTF-8
    if s is not None:
        return s
    local = locale.getpreferredencoding(False) or "utf-8"
    if local.lower() in ("utf-8", "utf8"):
        return b.decode("utf-8", errors="replace")
    s = _decode_with(b, local)             # 中文 Windows 上这里就是 GBK/CP936
    if s is not None:
        return s
    return b.decode(local, errors="replace")   # 兜底，解不出的变 ?


def _kill_tree(proc) -> None:
    """把整棵进程树杀掉。

    只 kill 直接子进程是不够的：Windows 上是 cmd.exe 起了 curl，curl 还握着管道
    的写端，后面 communicate() 要等到 EOF 才返回 —— 于是"超时了但命令照样不返回"。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_capture(argv, *, shell: bool, timeout: int, env: dict):
    """跑命令并抓输出，返回 (stdout, stderr, 是否超时, 退出码)。

    和 subprocess.run(timeout=) 的区别：超时后连孙进程一起杀，第二次
    communicate 也带超时，保证这条工具调用**一定会返回**，不会挂死。
    """
    kwargs = dict(shell=shell, env=env,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True      # 独立会话，才能整组杀

    proc = subprocess.Popen(argv, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or b"", err or b"", False, proc.returncode
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)   # 实在杀不干净也不再等
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return out or b"", err or b"", True, -1


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
    for p in platform_support.path_dirs():
        p = str(p)
        if Path(p).exists() and p not in env.get("PATH", ""):
            env["PATH"] = p + os.pathsep + env.get("PATH", "")   # 前置到 PATH
            # os.pathsep 在 Windows 是 ;，Linux/macOS 是 :

    # ---------- 交给平台的 shell ----------
    # Windows: "chcp 65001 >nul && xxx" + shell=True
    # macOS/Linux: [$SHELL, "-lc", xxx] + shell=False（登录 shell 才拿得到 brew/npm 的 PATH）
    argv, use_shell = platform_support.shell_command(cmd)

    # ---------- 显示进度条 ----------
    u = get_ui()
    if show_progress and u:
        short = cmd[:80] + ("..." if len(cmd) > 80 else "")   # 命令太长就截断
        u.ui_progress_start(f"正在执行：{short}")

    try:
        stdout, stderr, timed_out, code = _run_capture(
            argv, shell=use_shell, timeout=timeout, env=env)
        if timed_out:
            return (f"[超时] 命令超过 {timeout} 秒，已终止。"
                    "如果是网络下载：加上 --connect-timeout / --max-time，或者换国内镜像再试。")
        out = (_smart_decode(stdout) + _smart_decode(stderr)).strip()   # 合并 + 去首尾空白
        return out[:3000] or f"(完成，退出码 {code})"   # 截断到 3000 字
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
