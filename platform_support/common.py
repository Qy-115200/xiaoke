# -*- coding: utf-8 -*-
"""三个平台共用的零碎逻辑。

这里只放"跟操作系统无关"的东西：把工作目录限制在用户主目录里、按候选列表找
claude / VSCode、往 PATH 前面塞目录、跑子进程抓输出、调浏览器。
平台专属的实现放在 windows.py / macos.py / linux.py。
"""

import locale
import os
import re
import shutil
import signal
import subprocess
import threading
import webbrowser
from pathlib import Path

from config import HOME

VSCODE_DOWNLOAD_URL = "https://code.visualstudio.com/download"


def safe_cwd(directory: str) -> tuple[Path, str]:
    """把用户给的目录变成可用的工作目录，并返回一句要转告用户的话。

    ★ 只接受用户主目录里的路径：越界的一律拒绝、退回主目录并说明原因
      —— 跟 read_file / write_file 用的是同一条安全规则。

    返回 (cwd, note)。note 为空表示用户给的目录可以直接用。"""
    note = ""
    cwd = HOME
    if directory and str(directory).strip():
        try:
            cand = Path(str(directory).strip()).expanduser()
            cand.resolve().relative_to(HOME.resolve())    # 越界检查
            cand.mkdir(parents=True, exist_ok=True)
            cwd = cand
        except ValueError:
            note = f"（{directory} 不在用户主目录里，为了安全改在主目录启动）"
        except Exception as e:
            note = f"（目录 {directory} 用不了：{e}，改在主目录启动）"
    return cwd, note


def find_claude(paths) -> str | None:
    """按候选路径找 claude，最后再问一次 PATH。"""
    for p in paths:
        if not p:
            continue
        p = Path(p).expanduser()
        if p.exists():
            return str(p)
    return shutil.which("claude")


def prepend_paths(env: dict, dirs) -> dict:
    """把存在的目录前置到 env["PATH"]（原地修改并返回）。

    刚装完 Node，当前进程的环境变量不会自动更新，得手动补。"""
    for d in dirs:
        d = str(d)
        if not d or not Path(d).exists():
            continue
        parts = env.get("PATH", "").split(os.pathsep)
        if d not in parts:
            env["PATH"] = d + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------- 子进程

def clean_env(env: dict | None = None) -> dict:
    """复印一份环境变量，把 Electron / VSCode / Claude Code 的私有变量摘掉。

    ★ 这条不是洁癖，是必须的：从 VSCode 的集成终端启动助手时，环境里会带着
      ELECTRON_RUN_AS_NODE=1。带着它去跑 Code.exe，Code.exe 会把自己当成一个
      node 解释器 —— `Code.exe <目录>` 不弹窗、不报错，跑完就退出，用户只会
      觉得"点了一下什么也没发生"。VSCODE_* / CLAUDE_CODE_* 同理，都是注入给
      子 Electron 进程的。

    env 传进来就复印，不原地改（调用方可能还要用原样的一份）。"""
    src = os.environ if env is None else env
    out = dict(src)
    for key in list(out):
        upper = key.upper()
        if upper == "ELECTRON_RUN_AS_NODE" or upper.startswith("VSCODE_") \
                or upper.startswith("CLAUDE_CODE_"):
            out.pop(key, None)
    return out


def _decode_with(b: bytes, encoding: str) -> str | None:
    """按指定编码解，解出替换字符就当失败（返回 None）。"""
    try:
        s = b.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None
    return None if "\ufffd" in s else s


def smart_decode(b: bytes) -> str:
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


def kill_tree(proc) -> None:
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


# 正在跑的子进程。用户点「中断」时要能立刻把它们掐掉 —— 不然装 winget / VSCode
# 这种几分钟的子进程会让"中断"变成"点了没反应"，人只会以为按钮坏了。
# ★ 用 list + 锁而不是 set：Popen 对象可哈希但没必要，插入顺序也更直观。
_ACTIVE = []
_ACTIVE_LOCK = threading.Lock()


def kill_active_children() -> int:
    """掐掉所有正在跑的子进程，返回掐了几个。幂等，失败也不抛。"""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
        _ACTIVE.clear()
    for proc in procs:
        kill_tree(proc)
    return len(procs)


def _register(proc):
    with _ACTIVE_LOCK:
        _ACTIVE.append(proc)


def _unregister(proc):
    with _ACTIVE_LOCK:
        try:
            _ACTIVE.remove(proc)
        except ValueError:
            pass


def run_capture(argv, *, timeout: int, env: dict, stdin=None,
                shell: bool = False):
    """跑命令并抓输出，返回 (stdout, stderr, 是否超时, 退出码)。

    和 subprocess.run(timeout=) 的区别：超时后连孙进程一起杀，第二次
    communicate 也带超时，保证这条工具调用**一定会返回**，不会挂死。

    stdin 默认 None（沿用父进程的标准输入，bash 工具就靠它）；装软件的调用
    应该传 subprocess.DEVNULL，免得 brew / winget 等一个永远不会来的回车。
    """
    kwargs = dict(shell=shell, env=env, stdin=stdin,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True      # 独立会话，才能整组杀

    proc = subprocess.Popen(argv, **kwargs)
    _register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return out or b"", err or b"", False, proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)   # 实在杀不干净也不再等
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return out or b"", err or b"", True, -1
    finally:
        _unregister(proc)


def open_url(url: str) -> bool:
    """用系统默认浏览器打开一个网址。打不开就返回 False（别抛）。"""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def launch_argv_for(info: dict, new_window: bool, directory) -> list[str]:
    """拼出"用 VSCode 打开这个目录"的完整 argv。

    ★ 要带参数（比如 -n 开新窗口）时**必须走 CLI**：Windows 上 `Code.exe` 收到参数
      会转手给已运行的实例，不按命令行语义处理（本机实测 `Code.exe --list-extensions`
      是空输出，`code.cmd` 才正常）。所以有 CLI 就用 CLI，没有才退回 launch_argv
      —— 那种情况下只是开不出新窗口，照样能打开。"""
    cli = list(info.get("cli_argv") or [])
    if new_window and cli:
        return cli + ["-n", str(directory)]
    return list(info.get("launch_argv") or []) + [str(directory)]


def download_page_fallback(reason: str) -> str:
    """自动装失败时的统一说法：先把下载页打开，再如实告诉模型发生了什么。"""
    if open_url(VSCODE_DOWNLOAD_URL):
        return f"没能自动装 VSCode（{reason}），已打开下载页：{VSCODE_DOWNLOAD_URL}"
    return (f"没能自动装 VSCode（{reason}），也没能自动打开浏览器，"
            f"请手动打开：{VSCODE_DOWNLOAD_URL}")


# ---------------------------------------------------------------- VSCode

def exe_from_command(command: str) -> str:
    """从注册表里的命令行 / DisplayIcon 值里抠出可执行文件的路径。

    要认的形状（都是真机上见过的）：
        "D:\\vs code\\Microsoft VS Code\\Code.exe" "%1"
        "C:\\...\\Code.exe" --open-url -- "%1"
        "C:\\...\\Code.exe",0            ← DisplayIcon 的图标序号
        C:\\VSCode\\Code.exe -n %1        ← 没加引号
    认不出来就返回空串。"""
    s = (command or "").strip()
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        if end == -1:
            return ""
        return s[1:end]
    # 没加引号：切到第一个 .exe（含）为止
    m = re.match(r"(?i)(.+?\.exe)", s)
    if m:
        return m.group(1).strip().rstrip(",0").strip()
    # 既没引号也没 .exe：整串当路径，去掉 ,0
    return s.rstrip(",0").strip()


def parse_extension_list(text: str) -> dict:
    """把 `code --list-extensions --show-versions` 的输出解析成 {id: version}。

    行形如 `anthropic.claude-code@2.1.300`；没带 --show-versions 时没有 @ 部分。
    扩展 ID 一律转小写（VSCode 自己也是这么比的）。"""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line:
            ext_id, _, version = line.rpartition("@")
        else:
            ext_id, version = line, ""
        ext_id = ext_id.strip().lower()
        if ext_id:
            out[ext_id] = version.strip()
    return out
