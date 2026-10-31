# -*- coding: utf-8 -*-
"""Windows 专属实现。

原来的代码直接写在 server/tools_fixed.py 里，并且顶层 import winreg——那会让
macOS/Linux 在 import 阶段就崩掉。现在 winreg / win32com 全部收进函数内部，
模块本身在任何平台上都能安全 import。
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from config import HOME
from agent_bus import is_cancelled
from platform_support.common import (
    clean_env,
    download_page_fallback,
    exe_from_command,
    find_claude,
    launch_argv_for,
    run_capture,
    safe_cwd,
)

# 装 VSCode 可能要一两分钟（下载 + 安装 + 杀软扫一遍）。这个超时只给 install_vscode
# 自己的子进程用，跟 bash 工具的 600 秒上限无关。
VSCODE_INSTALL_TIMEOUT = 900

# winget 的源索引没准备好时，`source update` 要现下一份 20MB 的索引；
# 慢网络下别让它把整个安装挂死。
WINGET_SOURCE_TIMEOUT = 180

# ★ 最后一条路：不经过 winget，直接下微软官方的用户级安装包（和下载页给用户的
#   是同一个文件）。winget 的源索引在有些机器上就是起不来（代理、组策略、
#   App Installer 装不全），2026-10-30 用户在真机上撞到的就是这种：
#   `winget install` 当场返回 0x8A15004B（SourceOpenFailed）。
VSCODE_SETUP_URL = "https://update.code.visualstudio.com/latest/win32-x64-user/stable"
# 这个地址会 302 到 vscode.download.prss.microsoft.com 上带版本号的真实文件。
# 实测 2026-10-30：250MB 左右，国内直连 ~380KB/s，十几分钟。所以给足 30 分钟。
VSCODE_SETUP_TIMEOUT = 1800


def platform_key() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "windows-arm64"
    return "windows-x64"


def path_dirs() -> list[Path]:
    """刚装完 Node 时需要手动补进 PATH 的目录。"""
    return [
        Path(r"C:\Program Files\nodejs"),
        HOME / "AppData" / "Roaming" / "npm",
    ]


def taskbar_reserve_px() -> int:
    """底部任务栏大概占多高（充值窗定位用）。"""
    return 48


def desktop_dir() -> Path:
    """真实桌面路径。可能是 OneDrive 重定向过的，所以查注册表而不是拼 ~/Desktop。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        )
        return Path(winreg.QueryValueEx(key, "Desktop")[0])
    except Exception:
        return HOME / "Desktop"


def find_claude_path() -> str | None:
    return find_claude([
        HOME / "AppData" / "Roaming" / "npm" / "claude.cmd",                # npm 装的
        HOME / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",  # 官方原生装的
        Path("C:/Program Files/claude/claude.exe"),                          # 系统级装的
    ])


# ================= VSCode =================
# 找 VSCode 不能只看 PATH。真机上最常见的两种情况都靠不住 PATH：
#   1) 用户装的时候没勾「添加到 PATH」——安装目录里明明有 bin\code.cmd，PATH 里却没有；
#   2) 装到了非默认盘（本机就是 D:\vs code\Microsoft VS Code），不在任何标准目录里。
# 所以顺序是「注册表 → 标准目录 → PATH」，注册表排最前面，它一定是最准的。

def _read_reg_value(hive_name: str, subkey: str, value_name: str = "") -> str:
    """读一个注册表字符串值，读不到就返回空串（任何异常都不许冒出去）。"""
    try:
        import winreg
        hive = (winreg.HKEY_LOCAL_MACHINE if hive_name == "HKLM"
                else winreg.HKEY_CURRENT_USER)
        flags = winreg.KEY_READ
        if hive_name == "HKLM":
            # 32 位冻结包默认会被重定向到 WOW6432Node，显式要 64 位视图
            flags |= getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(hive, subkey, 0, flags) as key:
            return str(winreg.QueryValueEx(key, value_name)[0] or "")
    except Exception:
        return ""


def _vscode_registry_candidates() -> list[str]:
    """注册表里能给出 Code.exe 路径的几处，按可靠程度排。

    App Paths 最好：值本身就是干净的完整路径，不用解析。
    后面两条是"命令行"，形如 `"...\\Code.exe" "%1"`，得用 exe_from_command 抠。"""
    subkeys = [
        r"Software\Microsoft\Windows\CurrentVersion\App Paths\Code.exe",
        r"Software\Classes\Applications\Code.exe\shell\open\command",
        r"Software\Classes\vscode\shell\open\command",
    ]
    out = []
    for hive in ("HKCU", "HKLM"):
        for sub in subkeys:
            value = _read_reg_value(hive, sub)
            if value:
                out.append(value)
    return out


def _vscode_standard_paths() -> list[Path]:
    """几个常见安装位置（用户级安装器默认就落在 LOCALAPPDATA 那条）。"""
    roots = [HOME / "AppData" / "Local" / "Programs"]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    return [root / "Microsoft VS Code" / "Code.exe" for root in roots]


def _vscode_uninstall_candidates() -> list[str]:
    """从卸载表里找。★ 只有兜底价值：本机两个卸载表都匹配不到 "Visual Studio Code"，
    所以它排在最后，不能当主路。"""
    found = []
    try:
        import winreg
    except Exception:
        return found
    sub = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    # (读值时用的 hive 名, 真正的 hive 句柄, 视图标志)
    tables = [
        ("HKCU", winreg.HKEY_CURRENT_USER, 0),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0)),
    ]
    for hive_name, hive, view in tables:
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as key:
                count = winreg.QueryInfoKey(key)[0]
                names = []
                for i in range(count):
                    try:
                        names.append(winreg.EnumKey(key, i))
                    except OSError:
                        break
        except Exception:
            continue
        for name in names:
            base = sub + "\\" + name
            display = _read_reg_value(hive_name, base, "DisplayName")
            if "visual studio code" not in " ".join(display.lower().split()):
                continue
            for value_name in ("DisplayIcon", "InstallLocation", "Inno Setup: App Path"):
                value = _read_reg_value(hive_name, base, value_name)
                if not value:
                    continue
                path = Path(value.rstrip(",0").strip().strip('"'))
                if path.suffix.lower() != ".exe":
                    path = path / "Code.exe"
                found.append(str(path))
    return found


def _resolve_vscode_exe(raw: str) -> Path | None:
    """把一条候选（可能是路径、命令行、也可能是 code.cmd）变成 Code.exe 的路径。

    返回 None 表示这条候选不可用 —— 注册表键在卸载后经常残留，所以要真验文件存在。"""
    candidate = exe_from_command(raw)
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if path.name.lower() != "code.exe":
        # 拿到的是 bin\code.cmd（或某个 code 包装脚本）→ 反推安装根目录
        path = path.parent.parent / "Code.exe"
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def _vscode_info(exe: Path, source: str) -> dict:
    """把 Code.exe 路径补全成"怎么开窗 + 怎么跑 CLI"。

    ★ CLI 必须是 bin\\code.cmd，不能用 Code.exe：本机实测 `Code.exe --list-extensions`
      输出是空的，而 code.cmd 能正常列出扩展（Code.exe 是把参数转交给已运行实例的
      GUI 进程，不适合当命令行用）。"""
    cli = exe.parent / "bin" / "code.cmd"
    return {
        "exe": str(exe),
        "cli_argv": [str(cli)] if cli.is_file() else [],
        "launch_argv": [str(exe)],
        "source": source,
    }


def find_vscode() -> dict | None:
    """找 VSCode。只读、不起进程、不看 PATH 也基本能找到。

    返回 {"exe", "cli_argv", "launch_argv", "source"}，找不到返回 None。"""
    # (原始值, 来源标签)
    candidates: list[tuple[str, str]] = []
    candidates += [(v, "registry") for v in _vscode_registry_candidates()]
    candidates += [(str(p), "standard") for p in _vscode_standard_paths()]
    for name in ("code", "code.cmd", "Code.exe"):
        found = shutil.which(name)
        if found:
            candidates.append((found, "PATH"))
    candidates += [(v, "uninstall") for v in _vscode_uninstall_candidates()]

    seen = set()
    for raw, source in candidates:
        exe = _resolve_vscode_exe(raw)
        if exe is None:
            continue
        key = os.path.normcase(str(exe))
        if key in seen:          # 同一个 exe 被多条路径认出来，只用第一条（也就是最可靠的那条）
            continue
        seen.add(key)
        return _vscode_info(exe, source)
    return None


# ---------------------------------------------------------------- winget
#
# 为什么要有这一段：装 VSCode 靠 winget，可 Windows 10/11 的**家庭版精简镜像、
# 老版本 LTSC、服务器版**都可能根本没有 winget（App Installer 不在系统里）。
# 用户遇到的就是这一条：「自动安装没成功：这台机器上没有 winget」—— 装到一半
# 停在这里，还得自己去点下载页。
#
# 所以没 winget 时我们先把它装上，再拿它装 VSCode。
#
# ★ 包放在我们自己的 CDN 上，不是 GitHub：2026-10-30 实测国内直连
#   objects.githubusercontent.com 是 0 字节（走代理才 870KB/s），而 winget 的
#   msixbundle 有 200MB+。这里只取 bundle 里 x64 的那一个 msix（58MB），
#   内容和微软发布的完全一致（sha256 对得上），没有做任何改动。

WINGET_VERSION = "1.11.430"
WINGET_MSIX_NAME = "AppInstaller_x64.msix"
WINGET_MSIX_SIZE = 61258457
WINGET_MSIX_SHA256 = "c535a3e0fadf4f19f76b6cde8d3f760b134eb1938067252f9312c1407234f6d8"
# 先 CDN（快），再回源 aikaui.com（CDN 挂了还能装）。两个 URL 指向同一个文件。
WINGET_BASES = (
    f"https://dl.aikaui.com/releases/winget/{WINGET_VERSION}/",
    f"https://aikaui.com/releases/winget/{WINGET_VERSION}/",
)
WINGET_DOWNLOAD_TIMEOUT = 900          # 58MB，慢网络给足 15 分钟
WINGET_INSTALL_TIMEOUT = 300           # Add-AppxPackage 本地部署，通常十几秒

# ★ App Installer 这个 msix 不是"独立能装"的：它依赖两个框架包（UI.Xaml 和
#   VC++ 运行库）。微软的 msixbundle 旁边附了一个 Dependencies.zip 就是干这个的。
#   2026-10-30 用户机器上 Add-AppxPackage 直接返回 1，很可能就是缺这两个 ——
#   所以现在装主包之前先把它们装上（已经有了的话 Add-AppxPackage 会报"已安装"，
#   当成功处理）。
# 名字和哈希都钉死；文件是微软原包，从 Dependencies.zip 里原样取的 x64 那两份。
WINGET_DEPS = (
    ("Microsoft.VCLibs.140.00.UWPDesktop_14.0.33728.0_x64.appx", 6757465,
     "077a3d1a5d0622bd3004dca85f5e192d6e98ec79b83d4aa06766759ea6c09c3d"),
    ("Microsoft.UI.Xaml.2.8_8.2310.30001.0_x64.appx", 5121145,
     "249d2afb41cc009494841372bd6dd2df46f87386d535ddf8d9f32c97226d2e46"),
)


def _winget_portable_dir() -> Path:
    """便携版放哪：解压出来的 winget.exe 在这里，下次直接用，不重复下载。"""
    return HOME / "AppData" / "Local" / "aikaui" / "winget" / WINGET_VERSION


def find_winget(verify: bool = False) -> str | None:
    """找 winget：PATH → App Installer 的执行别名 → 我们解压出来的便携版。

    ★ 别名那条不能省：刚装完 App Installer 时，PATH 还是本进程启动时的那份，
      `shutil.which("winget")` 找不到它，但文件已经在那儿了。
    ★ verify=True 时要真跑一次 `--version`：WindowsApps 下的执行别名可能是个空壳
      （文件在，但那个包不是给这个用户装的），创建进程直接 WinError 3。
      本机 2026-10-30 就是这样 —— 不验就会拿一个跑不起来的路径去装东西。"""
    for cand in (shutil.which("winget"), shutil.which("winget.exe"),
                 str(HOME / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "winget.exe"),
                 str(_winget_portable_dir() / "winget.exe")):
        if not cand or not Path(cand).is_file():
            continue
        if verify and not _winget_runs(cand):
            continue
        return str(cand)
    return None


def _winget_runs(exe: str) -> bool:
    """这个 winget 真的起得来吗？只用来挡"执行别名是空壳"这一类。

    ★ 只看"能不能创建进程"，不看退出码也不看超时：返回非零、甚至卡住了，都算
      有东西（后面 `winget install` 自己会报具体错误，那条路有兜底）。
      这一条只用来挡"执行别名是空壳"—— 那种情况 Popen 直接抛 WinError 3。"""
    try:
        run_capture([exe, "--version"], timeout=30, env=clean_env(),
                    stdin=subprocess.DEVNULL)
    except Exception:
        return False
    return True


def _sha256_of(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: Path, on_progress=None, *, size: int = 0,
              sha256: str = "", kind: str = "winget") -> str:
    """下一份到 dest，边下边算 sha256。成功返回空串，失败返回一句原因。

    size / sha256 给了就校验（包要拿去装进系统，CDN 被劫持、文件被截断都得当场发现）；
    都不给就用 winget 那个包的值 —— 老调用方是这么用的。

    ★ 每 1MB 看一眼「中断」牌子：下 58MB / 240MB 的时候，用户点了中断得真的停下来，
      不然按钮就是个摆设。"""
    size = size or WINGET_MSIX_SIZE
    sha256 = sha256 or WINGET_MSIX_SHA256
    try:
        import requests
    except Exception as e:                     # 打包里一定有，纯属保险
        return f"没有请求库：{e}"

    tmp = dest.with_suffix(dest.suffix + ".part")
    cancelled = False
    try:
        with requests.get(url, stream=True, timeout=(15, 60)) as r:
            if r.status_code != 200:
                return f"HTTP {r.status_code}"
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if is_cancelled():
                        # ★ 只立个标记就跳出：文件还开着的时候 Windows 删不掉
                        #   它（unlink 悄悄不生效，.part 会留在临时目录里）
                        cancelled = True
                        break
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        try:
                            on_progress(done, total or size, kind)
                        except Exception:
                            pass
            if cancelled:
                _unlink_quiet(tmp)
                return "已中断"
            if total and total != size:
                return f"文件大小不对（{total} ≠ {size}）"
    except Exception as e:
        _unlink_quiet(tmp)
        return f"{type(e).__name__}：{str(e)[:80]}"

    got = _sha256_of(tmp)
    if got != sha256:
        _unlink_quiet(tmp)
        return f"校验不过（sha256 {got[:12]}…，期望 {sha256[:12]}…）"
    try:
        tmp.replace(dest)
    except OSError as e:
        _unlink_quiet(tmp)
        return f"改名失败：{str(e)[:80]}"
    return ""


def _fetch_winget_msix(on_progress=None) -> tuple[Path | None, str]:
    """按 WINGET_BASES 的顺序试，第一个成功的留下。返回 (文件, 空) 或 (None, 原因)。"""
    dest = Path(tempfile.gettempdir()) / f"aikaui-{WINGET_MSIX_NAME}"
    last = ""
    for base in WINGET_BASES:
        why = _download(base + WINGET_MSIX_NAME, dest, on_progress)
        if not why:
            return dest, ""
        last = f"{base.split('/')[2]}：{why}"
    return None, last or "下载失败"


# "这个包本来就在"的一堆 HRESULT —— 依赖包尤其容易撞上，都当成功。
_ALREADY_THERE = (
    "0x80073d06",        # 已装了更高版本
    "0x80073cfb",        # 同版本已经装了（Dependencies 重装时最常见）
    "0x80073d02",        # 资源包已注册
)
_ALREADY_THERE_WORDS = ("newer version", "already installed", "已安装", "已存在")


def _add_appx_package(path: Path) -> str:
    """Add-AppxPackage 一个包。成功（含"本来就在"）返回空串，失败返回一句原因。"""
    ps = ("Add-AppxPackage -LiteralPath '"
          + str(path).replace("'", "''")
          + "' -ErrorAction Stop")
    argv = ["powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", ps]
    try:
        out, err, timed_out, rc = run_capture(
            argv, timeout=WINGET_INSTALL_TIMEOUT, env=clean_env(),
            stdin=subprocess.DEVNULL)
    except Exception as e:
        return f"powershell 跑不起来：{e}"
    if timed_out:
        return f"Add-AppxPackage 超过 {WINGET_INSTALL_TIMEOUT} 秒"

    text = (err or out or b"").decode("utf-8", "replace").strip()
    low = text.lower()
    if any(code in low for code in _ALREADY_THERE) \
            or any(w in low for w in _ALREADY_THERE_WORDS):
        return ""
    if rc != 0:
        # ★ 整段错误都留着：2026-10-30 用户那条日志只剩「Add-AppxPackage 返回 1： and」，
        #   什么都判断不出来。取最后三行，比一行有用得多。
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        detail = " ".join(lines[-3:])[:240]
        return f"Add-AppxPackage 返回 {rc}" + (f"：{detail}" if detail else "")
    return ""


def _fetch_winget_deps() -> tuple[list[Path], str]:
    """把主包依赖的两个框架包下下来。返回 (文件列表, "") 或 ([], 原因)。

    以前下过的、大小和 sha256 都对得上就直接用，不再走网络。"""
    target = Path(tempfile.gettempdir()) / "aikaui-winget-deps"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return [], f"建目录失败：{e}"

    files = []
    for name, size, sha in WINGET_DEPS:
        dest = target / name
        try:
            if dest.is_file() and dest.stat().st_size == size and _sha256_of(dest) == sha:
                files.append(dest)
                continue
        except Exception:
            pass
        last = ""
        got = False
        for base in WINGET_BASES:
            why = _download(base + "deps/" + name, dest, size=size, sha256=sha,
                            kind="deps")
            if not why:
                got = True
                break
            last = why
        if not got:
            return [], f"下载 {name} 失败（{last}）"
        files.append(dest)
    return files, ""


def _install_appx(msix: Path, deps=()) -> str:
    """用 Add-AppxPackage 正式安装（用户级，不提权）。成功返回空串。

    ★ 这是微软签名的包，不需要开发者模式；装完 winget.exe 会出现在
      %LOCALAPPDATA%\\Microsoft\\WindowsApps\\ 下，用户以后自己也能用。

    deps：主包依赖的框架包（UI.Xaml / VCLibs），先装它们。装依赖失败不直接放弃
    —— 依赖可能本来就在，主包照样能装；两个都失败才把两句话一起报出去。"""
    dep_why = ""
    for dep in deps:
        why = _add_appx_package(Path(dep))
        if why:
            dep_why = (dep_why + "；" if dep_why else "") + f"依赖 {Path(dep).name}：{why}"

    why = _add_appx_package(msix)
    if why and dep_why:
        return f"{why}（{dep_why}）"
    return why


def _extract_portable(msix: Path) -> tuple[str | None, str]:
    """解压即用（不装 AppX）。

    老系统 / 没有商店 / 组策略禁了 AppX 时走这条。winget.exe 是自包含的，
    实测 `winget --version`、`winget search` 都能正常跑。
    只取 exe/dll/winmd，跳过 Assets（图标，几 MB 全是 png）。"""
    import zipfile

    target = _winget_portable_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(msix) as z:
            for info in z.infolist():
                name = info.filename
                if ".." in name or name.startswith("/"):
                    continue                       # 防目录穿越（包是我们自己的，仍然要挡）
                if name.lower().startswith("assets/"):
                    continue
                if not name.lower().endswith((".exe", ".dll", ".winmd")):
                    continue
                z.extract(info, target)
    except Exception as e:
        return None, f"解压失败：{e}"

    exe = target / "winget.exe"
    if not exe.is_file():
        return None, "解压后没有 winget.exe"
    # 真跑一次：缺 VC 运行库之类的会在这一步暴露，而不是等到装 VSCode 时才报怪错
    try:
        out, err, timed_out, rc = run_capture(
            [str(exe), "--version"], timeout=30, env=clean_env(),
            stdin=subprocess.DEVNULL)
    except Exception as e:
        return None, f"便携版跑不起来：{e}"
    if timed_out or rc != 0:
        return None, "便携版跑不起来（可能缺 VC 运行库）"
    return str(exe), ""


def _note(cb, msg: str) -> None:
    """往对话里打一句话（调用方没给回调就什么都不做）。回调出错绝不能影响安装。"""
    if not cb or not msg:
        return
    try:
        cb(msg)
    except Exception:
        pass


def ensure_winget(on_progress=None, on_note=None) -> dict:
    """确保这台机器上有 winget。返回 {"ok", "exe", "mode", "note"}。

    mode 取值：existing（本来就有）/ appx（这次装上的）/ portable（解压即用）。
    装不上时 ok=False，note 里是一句能直接讲给用户听的原因 —— 别让它变成异常。

    on_note(msg)：往对话里打一行进度/结论。2026-10-30 加了它，因为上次装失败
    时日志里只有下载百分比，看不出最后走的是 AppX 还是便携版。"""
    exe = find_winget(verify=True)          # 空壳别名不算数
    if exe:
        return {"ok": True, "exe": exe, "mode": "existing", "note": ""}

    if is_cancelled():
        return {"ok": False, "exe": "", "mode": "cancelled", "note": "已中断"}

    msix, why = _fetch_winget_msix(on_progress)
    if msix is None:
        if why == "已中断":
            return {"ok": False, "exe": "", "mode": "cancelled", "note": "已中断"}
        return {"ok": False, "exe": "", "mode": "download", "note": f"下载 winget 失败（{why}）"}

    try:
        # 主包依赖 UI.Xaml / VCLibs 这两个框架包，先装上。下不动不算致命：
        # 机器上可能本来就有，主包照样能装。
        deps, dep_why = _fetch_winget_deps()
        if dep_why:
            _note(on_note, f"依赖包没下下来（{dep_why}），先直接装主包试试")

        appx_why = _install_appx(msix, deps)
        if not appx_why:
            for _ in range(3):                  # 别名文件有几十毫秒的延迟
                exe = find_winget(verify=True)
                if exe:
                    _note(on_note, "已装好 winget（正式安装）")
                    return {"ok": True, "exe": exe, "mode": "appx", "note": ""}
                time.sleep(1)
            appx_why = "装完了却找不到能用的 winget.exe"

        _note(on_note, f"winget 装不上（{appx_why}），改用便携版")
        exe, port_why = _extract_portable(msix)
        if exe:
            _note(on_note, "已装好 winget（便携版，解压即用）")
            return {"ok": True, "exe": exe, "mode": "portable", "note": "",
                    "why_appx": appx_why}
        return {"ok": False, "exe": "", "mode": "install",
                "note": f"{appx_why}；便携版也不行（{port_why}）"}
    finally:
        # 58MB 的安装包留着没用（便携版已经把要的东西解出来了）
        try:
            msix.unlink(missing_ok=True)
        except Exception:
            pass


def _run_source_update(exe: str) -> str:
    """跑一次 `winget source update`。成功返回空串，失败返回一句原因。"""
    try:
        out, err, timed_out, rc = run_capture(
            [exe, "source", "update"], timeout=WINGET_SOURCE_TIMEOUT,
            env=clean_env(), stdin=subprocess.DEVNULL)
    except Exception as e:
        return f"跑不起来：{e}"
    if timed_out:
        return f"超过 {WINGET_SOURCE_TIMEOUT} 秒"
    if rc == 0:
        return ""
    detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
    return f"返回 {rc}" + (f"：{detail[-1][:120]}" if detail else "")


def _prepare_winget_sources(exe: str, note=None) -> str:
    """把 winget 的源索引准备好。成功返回空串，失败返回原因。

    ★ 新装的 winget（App Installer 装完、从没启动过的时候尤其）源索引是空的，
      任何 install 都会当场返回 0x8A15004B（SourceOpenFailedSuggestion）——
      用户 2026-10-30 撞到的就是这个。先 update 一次；还不行就 reset --force
      再 update 一次（winget 自己给的补救建议就是这两条）。"""
    why = _run_source_update(exe)
    if not why:
        return ""
    _note(note, f"winget 源索引更新失败（{why}），重置后重试…")
    try:
        run_capture([exe, "source", "reset", "--force"], timeout=60,
                    env=clean_env(), stdin=subprocess.DEVNULL)
    except Exception:
        pass
    return _run_source_update(exe)


def _winget_install_once(exe: str) -> str:
    """`winget install VSCode` 一次。成功返回空串，失败返回一句原因。"""
    argv = [
        exe, "install", "-e", "--id", "Microsoft.VisualStudioCode",
        "--scope", "user",                     # 用户级：不弹 UAC
        "--accept-package-agreements", "--accept-source-agreements",
    ]
    try:
        out, err, timed_out, rc = run_capture(
            argv, timeout=VSCODE_INSTALL_TIMEOUT, env=clean_env(),
            stdin=subprocess.DEVNULL)
    except Exception as e:
        return f"跑不起来：{e}"
    if timed_out:
        return f"超过 {VSCODE_INSTALL_TIMEOUT} 秒没装完"
    if rc != 0:
        # 只取最后一行有用信息，别把整段 winget 日志倒给模型
        detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
        return f"返回 {rc}" + (f"：{detail[-1][:120]}" if detail else "")
    return ""


_SOURCE_PROBLEM_HINTS = ("sourceopenfailed", "0x8a15", "source", "源")


def _looks_like_source_problem(why: str) -> bool:
    """这次失败像是"源索引没准备好"吗？只有这种才值得刷新源再试一次。

    ★ 超时不算：再试一次等于把 15 分钟的上限翻倍，宁可直接走下载安装包那条路。
      用户 2026-10-30 撞到的是 `SourceOpenFailedSuggestion`（0x8A15004B），
      属于这一类。"""
    low = (why or "").lower()
    if "超过" in low and "秒" in low:      # 超时
        return False
    return any(h in low for h in _SOURCE_PROBLEM_HINTS)


def _install_via_winget(exe: str, note=None) -> tuple[str, str]:
    """用 winget 装 VSCode。成功返回 ("已安装 VSCode（…）", "")，失败返回 ("", 原因)。"""
    why = _winget_install_once(exe)
    if why and _looks_like_source_problem(why):
        # 源索引没准备好是最常见的失败（0x8A15004B）。刷新一次再给一次机会 ——
        # 这一步很便宜，比直接放弃去下 240MB 划算。
        _note(note, f"winget 装失败了（{why}），刷新源索引后再试一次…")
        src = _prepare_winget_sources(exe, note)
        why2 = _winget_install_once(exe)
        if why2:
            tail = f"刷新源索引（{src}）后仍然" if src else "刷新源索引后仍然"
            return "", f"winget {why}；{tail}失败（{why2}）"
    elif why:
        return "", f"winget {why}"

    # 装是装完了，但 PATH 还是旧的（winget 写的是用户环境变量，本进程读不到），
    # 所以再走一遍注册表/标准目录。杀软和安装器收尾会拖一点，重试几次。
    for _ in range(3):
        found = find_vscode()
        if found:
            return f"已安装 VSCode（{found['exe']}）", ""
        time.sleep(2)
    return "", "winget 说装成功了，但找不到 VSCode 的安装位置"


def _unlink_quiet(path) -> None:
    """删文件，删不掉也不吭声。

    ★ 临时目录里的半截文件可能有别人占着句柄（杀毒扫描、上一版进程被强杀），
      这时候 `unlink` 抛 WinError 32。为它把整个安装流程炸掉不值 —— 留个垃圾
      文件比让用户看到「装失败」强得多。"""
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _fetch_vscode_setup(on_progress=None) -> tuple[Path | None, str]:
    """下微软官方的用户级安装包。返回 (文件, 空) 或 (None, 原因)。"""
    try:
        import requests
    except Exception as e:
        return None, f"没有请求库：{e}"

    dest = Path(tempfile.gettempdir()) / "aikaui-VSCodeUserSetup-x64.exe"
    tmp = Path(str(dest) + ".part")
    done = 0
    cancelled = False
    try:
        with requests.get(VSCODE_SETUP_URL, stream=True, timeout=(15, 60)) as r:
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            total = int(r.headers.get("content-length") or 0)
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if is_cancelled():
                        # 文件开着的时候删不掉它（见 _download 里的说明）
                        cancelled = True
                        break
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        try:
                            on_progress(done, total or done, "vscode")
                        except Exception:
                            pass
        if cancelled:
            _unlink_quiet(tmp)
            return None, "已中断"
        with open(tmp, "rb") as f:          # ★ 先关文件再删：Windows 上删不掉打开着的文件
            head = f.read(2)
        if head != b"MZ":
            _unlink_quiet(tmp)
            return None, "下下来的不是 Windows 可执行文件"
        if done < 50 * 1024 * 1024:
            _unlink_quiet(tmp)
            return None, f"下下来的文件太小（{done} 字节）"
    except Exception as e:
        _unlink_quiet(tmp)
        return None, f"{type(e).__name__}：{str(e)[:80]}"

    if is_cancelled():
        _unlink_quiet(tmp)
        return None, "已中断"

    try:
        tmp.replace(dest)
    except OSError as e:
        _unlink_quiet(tmp)
        return None, f"改名失败：{str(e)[:80]}"
    return dest, ""


def _install_from_setup(on_progress=None, note=None) -> tuple[str, str]:
    """最后一条路：不经过 winget，下微软官方的安装包静默装。

    winget 这条路整个不通时（源索引起不来、组策略挡了、AppX 装不上还解压不了）
    才会走到这儿。下的文件和下载页给用户的是同一个，签名也是微软的。"""
    _note(note, "winget 这条路走不通，改成直接下载微软官方的安装包（约 240MB，几分钟）…")
    setup, why = _fetch_vscode_setup(on_progress)
    if setup is None:
        return "", f"下载 VSCode 安装包失败（{why}）"

    try:
        argv = [str(setup), "/VERYSILENT", "/NORESTART", "/SP-",
                "/SUPPRESSMSGBOXES", "/MERGETASKS=!runcode"]
        try:
            out, err, timed_out, rc = run_capture(
                argv, timeout=VSCODE_SETUP_TIMEOUT, env=clean_env(),
                stdin=subprocess.DEVNULL)
        except Exception as e:
            return "", f"安装包跑不起来：{e}"
        if timed_out:
            return "", f"安装包超过 {VSCODE_SETUP_TIMEOUT} 秒没装完"
        if rc not in (0, 3010):                # 3010 = 装好了但要求重启
            detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
            return "", f"安装包返回 {rc}" + (f"：{detail[-1][:120]}" if detail else "")
    finally:
        try:
            setup.unlink(missing_ok=True)      # 240MB 别留在临时目录里
        except Exception:
            pass

    for _ in range(5):
        found = find_vscode()
        if found:
            return f"已安装 VSCode（{found['exe']}）", ""
        time.sleep(2)
    return "", "安装包说装完了，但找不到 VSCode 的安装位置"


def install_vscode(on_progress=None, on_note=None) -> str:
    """装 VSCode（用户级，不提权）。三条路依次试，都不行才打开下载页。

    1. winget 本来就有的机器：直接 `winget install`（最快，不用多下东西）；
    2. 没有 winget 就先装上（ensure_winget，58MB），再走第 1 条；
    3. winget 整个不通（源索引起不来 / AppX 装不上）时，直接下微软官方的
       用户级安装包静默安装 —— 这条不依赖 winget 的任何组件。

    on_progress(已下载字节, 总字节, "winget"|"vscode") 用来给界面报进度；
    on_note(msg) 用来往对话里打一行说明（走的哪条路、卡在哪一步）。"""
    found = find_vscode()
    if found:
        return f"已经装过 VSCode，不用重复安装（{found['exe']}）"

    reasons = []
    mode = ""

    winget = find_winget(verify=True)
    if not winget:
        got = ensure_winget(on_progress, on_note)
        if got["ok"]:
            winget = got["exe"]
            mode = got.get("mode") or ""
        elif got["note"] != "已中断":
            reasons.append(got["note"])

    # ★ 便携版 winget 装不了东西：它没有注册成 AppX 包，源索引起不来
    #   （`source update` 也失败），`winget install` 必定返回 0x8A15004B。
    #   2026-10-30 用户机器上就是这样 —— 与其让它去撞一次墙再改道，不如直接走
    #   下载安装包那条路，省两分钟。
    if winget and mode == "portable":
        _note(on_note, "便携版 winget 读不到软件源，直接改用官方安装包")
        reasons.append("便携版 winget 读不到软件源")
        winget = ""

    if winget:
        text, why = _install_via_winget(winget, on_note)
        if text:
            return text
        reasons.append(why)

    text, why = _install_from_setup(on_progress, on_note)
    if text:
        return text
    reasons.append(why)

    if is_cancelled():
        return "已中断安装 VSCode。"

    return download_page_fallback("；".join(r for r in reasons if r))


def open_vscode(directory: str = "", new_window: bool = False) -> str:
    """用 VSCode 打开指定目录（目录规则跟 launch_claude 一样：只在用户主目录里）。

    new_window=True 时强制开新窗口 —— 刚装完插件必须这样：已开的窗口是在装插件
    之前加载的，里面没有这个插件。"""
    info = find_vscode()
    if not info:
        return "找不到 VSCode，无法打开"
    cwd, note = safe_cwd(directory)
    try:
        subprocess.Popen(
            launch_argv_for(info, new_window, cwd),
            stdin=subprocess.DEVNULL,          # 别让子进程抢走终端的标准输入
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            # ★ clean_env 不能省：带着 ELECTRON_RUN_AS_NODE 会让 Code.exe 变成
            #   一个 node 解释器，跑完就退，既不弹窗也不报错。
            env=clean_env(),
            cwd=str(HOME),
            creationflags=subprocess.DETACHED_PROCESS,   # 助手退出后 VSCode 继续活着
        )
    except Exception as e:
        return f"打开 VSCode 失败：{e}"
    return f"已打开 VSCode（{info['exe']}），工作目录：{cwd}{note}"


def open_uri(uri: str) -> str:
    """把 vscode://xxx 这样的链接交给系统（VS Code 自己注册的协议）。

    成功返回空串，失败返回一句要转告用户的话。用 os.startfile 而不是拼
    `cmd /c start`：链接里带 `&` 之类的话，交给命令行会被拆开。"""
    try:
        os.startfile(uri)                    # noqa: S606 - ShellExecute，走系统协议处理
    except Exception as e:
        return f"打不开链接 {uri}：{e}"
    return ""


# ================= aikaui:// 协议 =================
# 支付页面付完款后，会打开 aikaui://recharge?user_id=7 这样的链接，
# Windows 靠注册表里的这个协议，把这个链接转成"启动我们的 exe 并带上参数"。

def register_url_protocol() -> str:
    """注册 aikaui:// 自定义协议（Windows）。已注册则覆盖成当前 exe 路径。"""
    import winreg

    if getattr(sys, "frozen", False):
        command = f'"{Path(sys.executable).resolve()}" "%1"'
    else:
        # 开发环境（python main.py）：带上解释器和脚本路径，方便本地调试
        script = Path(sys.argv[0]).resolve()
        # python -c / -m 之类没有真实脚本，写进注册表只会在点链接时炸掉
        if not script.is_file():
            return "跳过协议注册：开发环境没有可执行的脚本路径"
        command = f'"{Path(sys.executable).resolve()}" "{script}" "%1"'

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:小克")   # 描述
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")  # ★ 有这行才被认成协议
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\aikaui\shell\open\command"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except Exception as e:
        return f"注册协议失败：{e}"

    return "已注册协议 aikaui://"


# ================= 快捷方式 / 启动终端 =================

def create_shortcut() -> str:
    """在桌面创建 Claude Code 快捷方式。"""
    try:
        import win32com.client                  # Windows COM 接口（创建 .lnk 用）
    except ImportError:
        return "缺少 pywin32，无法创建快捷方式"

    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，可能没装好"

    shell = win32com.client.Dispatch("WScript.Shell")   # 创建 COM 对象
    lnk_path = desktop_dir() / "Claude Code.lnk"
    lnk = shell.CreateShortCut(str(lnk_path))

    if claude_path.endswith((".cmd", ".bat")):          # .cmd/.bat 要通过 cmd.exe 运行
        lnk.TargetPath = "cmd.exe"
        lnk.Arguments = f'/k "{claude_path}"'            # /k = 执行完后保留窗口
    else:
        lnk.TargetPath = claude_path

    lnk.WorkingDirectory = str(HOME)
    lnk.Description = "启动 Claude Code"
    lnk.Save()
    return f"已创建桌面快捷方式：{lnk_path}"


def launch_claude(directory: str = "") -> str:
    """打开新终端窗口，在指定目录里启动 Claude Code。"""
    claude_path = find_claude_path()
    if not claude_path:
        return "找不到 claude 命令，无法启动"

    cwd, note = safe_cwd(directory)

    try:
        # start "Claude Code" cmd /k "xxx"：
        #   start = 打开新窗口；cmd /k = 执行完命令后保留窗口
        subprocess.Popen(
            f'start "Claude Code" cmd /k "{claude_path}"',
            shell=True,
            cwd=str(cwd),
        )
        return f"已打开新终端启动 Claude Code（{claude_path}），工作目录：{cwd}{note}"
    except Exception as e:
        return f"启动失败：{e}"


# ================= 通知 =================

def notify(title: str, message: str) -> None:
    """桌面通知：win10toast → plyer → PowerShell 的 WinRT toast，全失败就静默。"""
    # ① win10toast（requirements 里的首选）
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, message, duration=8, threaded=False)
        return
    except Exception:
        pass

    # ② plyer（跨平台，装了就用）
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=8)
        return
    except Exception:
        pass

    # ③ PowerShell 的 WinRT toast（Windows 10/11 自带，啥都不用装）
    try:
        app_id = str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) \
            else "小克"

        def _esc(s):
            """XML 转义，避免标题/正文里的特殊字符把模板弄坏"""
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))

        template = (
            '<toast><visual><binding template="ToastText02">'
            f'<text id="1">{_esc(title)}</text>'
            f'<text id="2">{_esc(message)}</text>'
            "</binding></visual></toast>"
        )
        ps = (
            '[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, '
            'ContentType=WindowsRuntime] | Out-Null\n'
            '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
            'ContentType=WindowsRuntime] | Out-Null\n'
            f'$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n'
            f'$xml.LoadXml(@\'\n{template}\n\'@)\n'
            f'$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n'
            f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
            f'"{app_id}").Show($toast)\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                         encoding="utf-8-sig") as f:
            f.write(ps)
            script = f.name
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


# ================= 命令执行 =================

def shell_command(cmd: str) -> tuple[str, bool]:
    """返回 (真正交给 subprocess 的命令, 是否 shell=True)。

    chcp 65001 强制 UTF-8 输出（对程序输出有效），>nul 吞掉它自己的提示。"""
    return f"chcp 65001 >nul && {cmd}", True
