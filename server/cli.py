# -*- coding: utf-8 -*-
"""服务器版 CLI 的公共零件。

这里只放"跟具体步骤无关"的东西：错误类型、退出码、跑子进程、下载、
校验、解压、软链、往 shell 配置里塞 PATH。具体流程在
`server/node_setup.py`、`server/claude_setup.py`、`server_main.py`。

★ 这个模块**不许 import tkinter / customtkinter / ui / agent_core**：
  服务器版是靠 PyInstaller 单独打包的（`server.spec` 里把这些都 exclude 了），
  只要有一条 import 链把它们拖回来，包就会大十几兆、还可能在没有 X11 的
  机器上初始化失败。
"""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

# ================= 退出码 =================
# 写进 install.sh 和 README，值不要随手改：用户拿退出码去搜文档。
EXIT_OK = 0
EXIT_FAILED = 1        # 没归类的失败
EXIT_ARGS = 2          # 命令行参数不对
EXIT_ENV = 3           # 环境不满足（架构 / 无 TTY / 以 root 跑）
EXIT_ACCOUNT = 4       # 领账号 / 绑账号失败
EXIT_NODE = 5          # Node 装不上
EXIT_NPM = 6           # claude CLI 装不上
EXIT_CONFIG = 7        # 配置写不进去

# ================= PATH 块标记 =================
# ★ 这对标记是"契约"：`packaging/install.sh` 里写的是同一对字符串。
#   两边必须一模一样，否则同一个用户会看到两段重复的 PATH。
MARK_BEGIN = "# >>> aikaui >>>"
MARK_END = "# <<< aikaui <<<"


class CliError(RuntimeError):
    """一步失败，带上退出码和"接下来该怎么办"。

    message 是给用户看的人话（不带 traceback）；hint 是可选的修复建议。"""

    def __init__(self, message, exit_code=EXIT_FAILED, hint=""):
        super().__init__(message)
        self.message = str(message)
        self.exit_code = int(exit_code)
        self.hint = str(hint or "")


class CommandResult:
    """一次子进程调用的结果。out/err 已经解码成 str。"""

    __slots__ = ("argv", "code", "out", "err", "timed_out")

    def __init__(self, argv, code, out="", err="", timed_out=False):
        self.argv = list(argv or [])
        self.code = int(code)
        self.out = out or ""
        self.err = err or ""
        self.timed_out = bool(timed_out)

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def text(self, limit: int = 400) -> str:
        """out + err 拼成一段给人看的话（截断，免得日志被 npm 刷爆）。"""
        body = "\n".join(x.strip() for x in (self.out, self.err) if x.strip())
        if len(body) > limit:
            body = body[:limit] + "…"
        return body

    def __repr__(self):
        return f"CommandResult(code={self.code}, timed_out={self.timed_out})"


class CommandRunner:
    """包一层 `platform_support.common.run_capture`，补上解码和超时语义。

    ★ run_capture 是给 Agent 的 bash 工具写的，它对超时很谨慎（杀整棵进程树、
      再 communicate 10 秒）——那正是我们想要的：npm 卡死时安装器不能跟着卡死。"""

    def __init__(self, env=None, default_timeout: int = 600):
        self.env = env if env is not None else os.environ.copy()
        self.default_timeout = default_timeout

    def run(self, argv, timeout=None, stdin=subprocess.DEVNULL) -> CommandResult:
        from platform_support.common import run_capture, smart_decode
        argv = [str(a) for a in argv]
        try:
            out, err, timed_out, code = run_capture(
                argv, timeout=timeout or self.default_timeout,
                env=self.env, stdin=stdin)
        except OSError as e:
            # 命令根本不存在 / 没执行权限：当成一次失败的调用，别炸穿
            return CommandResult(argv, -1, "", str(e))
        return CommandResult(argv, code, smart_decode(out), smart_decode(err),
                             timed_out)

    def which(self, name: str):
        return shutil.which(name, path=self.env.get("PATH", "")) or None


# ================= 校验 / 解压 =================

def sha256_file(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unsafe_member(name: str) -> bool:
    """压缩包里的路径是不是想跑到解压目录外面去。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return True
    parts = Path(name.replace("\\", "/")).parts
    return ".." in parts or (len(parts) > 1 and parts[0] == "..")


def safe_extract_tar(archive, dest) -> Path:
    """解压 .tar.gz / .tar.xz 到 dest，返回 dest。

    ★ 逐条检查成员路径：发布包是从我们自己服务器下的，但"我们"也可能被换掉，
      一个 `../../.ssh/authorized_keys` 就够把整台机器交出去。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tf:
            for member in tf.getmembers():
                if _unsafe_member(member.name):
                    raise CliError(f"压缩包里有不安全的路径：{member.name}")
            try:
                tf.extractall(dest, filter="data")
            except TypeError:          # Python < 3.12 没有 filter 参数
                tf.extractall(dest)
    except CliError:
        raise
    except Exception as e:
        raise CliError(f"解压失败：{e}")
    return dest


def atomic_symlink(target, link) -> None:
    """原子地把 link 指向 target（先建临时链接再 replace）。

    ★ 直接 `os.symlink(target, link)` 在 link 已存在时会 FileExistsError，
      先 unlink 再 symlink 又有一个"链接不存在"的空窗。"""
    link = Path(link)
    target = Path(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.parent / f".{link.name}.tmp-{os.getpid()}"
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, link)
    finally:
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# ================= shell PATH =================

def shell_path_block(prefix, extra_dirs=()) -> str:
    """要追加进 shell 配置的那一段。"""
    parts = [str(Path(d)) for d in extra_dirs if d]
    parts.append(str(Path(prefix) / "bin"))
    path = ":".join(parts + ["$PATH"])
    return f'{MARK_BEGIN}\nexport PATH="{path}"\n{MARK_END}\n'


def has_shell_path_block(text: str) -> bool:
    return MARK_BEGIN in (text or "")


def rc_files(home, shell=None) -> list:
    """该往哪些文件里写 PATH。bash 用户覆盖 .bashrc / .profile；
    zsh 再加 .zshrc（zsh 不读 .bashrc）。"""
    home = Path(home)
    files = [home / ".bashrc", home / ".profile"]
    sh = str(shell if shell is not None else os.environ.get("SHELL") or "").lower()
    if "zsh" in sh:
        files.append(home / ".zshrc")
    return files


def ensure_shell_path(home, prefix, extra_dirs=(), shell=None) -> list:
    """幂等地写 PATH 块，返回被改过的文件。

    ★ 已经有标记就一个字都不动：用户可能自己调整过顺序。"""
    block = shell_path_block(prefix, extra_dirs)
    changed = []
    for path in rc_files(home, shell):
        try:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            old = ""
        if has_shell_path_block(old):
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                if old and not old.endswith("\n"):
                    f.write("\n")
                f.write(("\n" if old else "") + block)
            changed.append(str(path))
        except Exception:
            # 只读的 rc 文件不该让安装失败：当前进程的 PATH 已经改过了
            continue
    return changed


# ================= 网络 =================

class HttpClient:
    """下载 / 取文本。薄薄一层，方便测试里整个换掉。"""

    def __init__(self, timeout: int = 30, chunk: int = 1 << 20, progress=None):
        self.timeout = timeout
        self.chunk = chunk
        self.progress = progress          # callable(done_bytes, total_bytes|None)

    def get_text(self, url: str, timeout=None) -> str:
        import requests
        resp = requests.get(url, timeout=timeout or self.timeout)
        resp.raise_for_status()
        return resp.text

    def get_json(self, url: str, timeout=None):
        return json.loads(self.get_text(url, timeout=timeout))

    def download(self, url: str, dest, timeout=None, expect_sha256="",
                 expect_size=None) -> str:
        """下载到 dest，边下边算 sha256；给了 expect_sha256 就核对。"""
        import hashlib
        import requests
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        done = 0
        try:
            with requests.get(url, timeout=(20, timeout or 600), stream=True) as resp:
                resp.raise_for_status()
                total = None
                try:
                    total = int(resp.headers.get("Content-Length") or 0) or None
                except Exception:
                    total = None
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(self.chunk):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if self.progress:
                            try:
                                self.progress(done, total)
                            except Exception:
                                pass
        except Exception:
            try:
                dest.unlink()
            except Exception:
                pass
            raise

        if expect_size and done != int(expect_size):
            try:
                dest.unlink()
            except Exception:
                pass
            raise CliError(
                f"下载的文件大小不对：期望 {expect_size} 字节，实际 {done} 字节")
        if expect_sha256 and h.hexdigest().lower() != str(expect_sha256).lower():
            try:
                dest.unlink()
            except Exception:
                pass
            raise CliError(
                f"下载的文件校验失败（内容损坏或被篡改）：\n"
                f"  期望 sha256 {expect_sha256}\n  实际 sha256 {h.hexdigest()}")
        return str(dest)


# ================= 运行环境 =================

class CliContext:
    """一路传下去的运行上下文。测试里整份替换，不碰真实 HOME。"""

    def __init__(self, home=None, env=None, ui=None, dry_run: bool = False,
                 runner=None, http=None, log=None, quiet: bool = False):
        self.home = Path(home or Path.home())
        self.prefix = self.home / ".local"
        self.env = env if env is not None else os.environ.copy()
        self.ui = ui
        self.dry_run = bool(dry_run)
        self.runner = runner or CommandRunner(self.env)
        self.http = http or HttpClient()
        self.quiet = bool(quiet)
        self._log = log

    def log(self, text: str = ""):
        """打一行给用户看的进度。dry_run 下照打（用户就是要看计划）。"""
        if self._log:
            self._log(text)
            return
        if self.quiet:
            return
        try:
            print(text, flush=True)
        except Exception:
            pass

    def note(self, text: str = ""):
        """次要信息：--quiet 时不打。"""
        if self.quiet:
            return
        self.log(text)

    # 常用路径
    @property
    def bin_dir(self) -> Path:
        return self.prefix / "bin"

    @property
    def npm_cache(self) -> Path:
        return self.prefix / ".npm-cache"
