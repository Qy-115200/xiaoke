# -*- coding: utf-8 -*-
"""自动更新：查清单 → 后台下载 → **下次启动时**替换自己。

设计上的几条硬规矩（改了要重新想一遍）：

1. **不 import tkinter**。这个模块在窗口建出来之前就要跑（apply_pending_update），
   任何界面依赖都会让它没法在启动最前面调用。
2. **绝不让更新把程序搞崩**。所有对外函数都吞异常、写日志、返回 None/False；
   启动时替换失败就继续用旧版本跑，下次再试。
3. **只信 https + SHA-256**。清单里的 url 必须是 https，下载边写边算哈希，
   对不上就删掉 `.part`，绝不替换。
4. **静默**。不在界面上弹窗、不打断用户；下好了只在日志区写一行，
   真正的替换发生在下一次启动、创建 Tk 之前。
5. **失败有上限**。同一个版本连续替换失败 3 次就写 failed.json、删掉暂存，
   不再拿它骚扰用户（可能是只读目录或杀毒软件锁着文件）。

清单形状（服务端 https://aikaui.com/downloads/latest.json）：

    {"schema_version": 1,
     "latest": {
       "windows-x64": {"version": "1.0.1", "url": "https://…/x.exe",
                       "sha256": "<64 位十六进制>", "size": 12345678,
                       "min_version": "1.0.0", "notes": "…"},
       "macos-arm64": {...}, "macos-x64": {...}, "linux-x64": {...}}}
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

import platform_support
from config import (
    APP_VERSION,
    UPDATES_DIR,
    UPDATE_CHECK_INTERVAL,
    UPDATE_INITIAL_DELAY,
    UPDATE_LOCK_STALE,
    UPDATE_MANIFEST_URL,
    UPDATE_MAX_SIZE,
    UPDATE_STAGED_TTL,
)

MANIFEST_CACHE = UPDATES_DIR / "manifest.json"
PENDING_FILE = UPDATES_DIR / "pending.json"
APPLIED_FILE = UPDATES_DIR / "applied.json"
FAILED_FILE = UPDATES_DIR / "failed.json"
LOCK_FILE = UPDATES_DIR / ".lock"
LOG_FILE = UPDATES_DIR / "update.log"
STAGED_DIR = UPDATES_DIR / "staged"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# ================= 小工具 =================

def _log(message: str) -> None:
    """写一行更新日志。日志写不进去也绝不影响主流程。"""
    try:
        UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data) -> bool:
    """原子写：先写 .tmp 再 replace，读的一方不会看到半个文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        _log(f"写 {path.name} 失败：{e}")
        return False


def _rm(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except Exception:
        pass


# ================= 版本与清单 =================

def parse_version(text):
    """'1.2.3' → (1, 2, 3)；不合法返回 None。只接受三段纯数字。"""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate, current) -> bool:
    """candidate 比 current 新吗？任何一边解析不了都返回 False（宁可不更新）。"""
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)


def validate_manifest(data):
    """只做形状校验；具体平台的条目在 select_release 里逐项校验。"""
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != 1:
        return None
    if not isinstance(data.get("latest"), dict):
        return None
    return data


def _valid_entry(entry, current):
    """把清单里的一个平台条目校验并归一化；不合格返回 None。"""
    if not isinstance(entry, dict):
        return None

    version = entry.get("version")
    if parse_version(version) is None:
        return None
    if not is_newer(version, current):
        return None

    url = entry.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None

    digest = entry.get("sha256")
    if not isinstance(digest, str) or not _SHA_RE.match(digest.lower()):
        return None

    size = entry.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= UPDATE_MAX_SIZE:
        return None

    min_version = entry.get("min_version")
    if min_version is not None:
        floor = parse_version(min_version)
        mine = parse_version(current)
        if floor is None or mine is None or mine < floor:
            return None

    return {
        "version": version,
        "url": url,
        "sha256": digest.lower(),
        "size": size,
        "min_version": min_version,
        "notes": str(entry.get("notes") or ""),
    }


def select_release(manifest, key=None, current=APP_VERSION):
    """挑出当前平台该装的那一条；没有/不合法/不比当前新 → None。"""
    if not isinstance(manifest, dict):
        return None
    latest = manifest.get("latest")
    if not isinstance(latest, dict):
        return None
    key = key or platform_support.platform_key()
    return _valid_entry(latest.get(key), current)


def fetch_manifest(url=None, timeout=(5, 15)):
    """拉清单；网络失败就退回本地缓存。全部失败返回 None（静默）。"""
    url = url or UPDATE_MANIFEST_URL
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": f"ClaudeCodeAssistant/{APP_VERSION}"},
        )
        if r.status_code == 200:
            data = validate_manifest(r.json())
            if data is not None:
                _write_json(MANIFEST_CACHE, data)
                return data
            _log(f"清单格式不对：{url}")
    except Exception as e:
        _log(f"拉清单失败：{e}")

    cached = validate_manifest(_read_json(MANIFEST_CACHE))
    if cached is not None:
        _log("使用本地缓存的清单")
    return cached


# ================= 下载与暂存 =================

def _safe_name(url: str, fallback="update.bin") -> str:
    name = Path(urlparse(url).path).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return name or fallback


def download_update(entry):
    """流式下载并校验。成功返回文件路径，失败返回 None 且不留 .part。"""
    dest_dir = STAGED_DIR / entry["version"]
    dest = dest_dir / _safe_name(entry["url"])
    part = dest.with_name(dest.name + ".part")

    digest = hashlib.sha256()
    total = 0
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with requests.get(entry["url"], stream=True, timeout=(10, 60)) as r:
            r.raise_for_status()
            length = r.headers.get("Content-Length")
            if length and int(length) > UPDATE_MAX_SIZE:
                raise ValueError("更新包超过大小上限")
            with open(part, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > UPDATE_MAX_SIZE:
                        raise ValueError("更新包超过大小上限")
                    digest.update(chunk)
                    f.write(chunk)

        if total != entry["size"]:
            raise ValueError(f"大小不符：{total} != {entry['size']}")
        if digest.hexdigest().lower() != entry["sha256"]:
            raise ValueError("SHA-256 校验失败")

        os.replace(part, dest)
        _log(f"下载完成 v{entry['version']}（{total} bytes）")
        return dest
    except Exception as e:
        _log(f"下载 v{entry['version']} 失败：{e}")
        _rm(part)
        return None


def _safe_extract_zip(archive: Path, dest: Path) -> None:
    """拒绝绝对路径 / .. / 符号链接，再解压。"""
    base = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            p = Path(info.filename)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"不安全的压缩包路径：{info.filename}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"压缩包里不允许符号链接：{info.filename}")
            target = (base / p).resolve()
            target.relative_to(base)
        zf.extractall(base)


def _extract_macos(archive: Path, dest: Path):
    """解出 .app；优先 ditto（保留签名/权限），退化到 zipfile。"""
    dest.mkdir(parents=True, exist_ok=True)
    ok = False
    if shutil.which("ditto"):
        try:
            r = subprocess.run(
                ["ditto", "-x", "-k", str(archive), str(dest)],
                capture_output=True, timeout=300,
            )
            ok = r.returncode == 0
        except Exception:
            ok = False
    if not ok:
        _rm(dest)
        dest.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(archive, dest)

    apps = [p for p in dest.rglob("*.app") if p.is_dir()]
    if len(apps) != 1:
        raise ValueError(f"压缩包里应当正好有一个 .app，实际 {len(apps)} 个")
    return apps[0]


def _extract_linux(archive: Path, dest: Path):
    """解出 Linux 单文件可执行程序。tarfile 的 data 过滤器会挡住越界/设备文件。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:                     # Python < 3.12 没有 filter 参数
            for m in tf.getmembers():
                p = Path(m.name)
                if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
                    raise ValueError(f"不安全的压缩包条目：{m.name}")
            tf.extractall(dest)

    candidates = []
    for p in dest.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if p.name == "ClaudeCodeAssistant":
            return p
        if os.access(p, os.X_OK):
            candidates.append(p)
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError("压缩包里找不到可执行文件")


def stage_update(entry, downloaded: Path):
    """把下载到的包展开成"可以替换上去的东西"：
    Windows/Linux → 单文件；macOS → .app 目录。"""
    if platform_support.is_macos():
        payload = STAGED_DIR / entry["version"] / "payload"
        _rm(payload)
        app = _extract_macos(downloaded, payload)
        _log(f"已解出 {app.name}")
        return app

    if platform_support.is_linux():
        payload = STAGED_DIR / entry["version"] / "payload"
        _rm(payload)
        binary = _extract_linux(downloaded, payload)
        try:
            binary.chmod(binary.stat().st_mode | 0o111)
        except Exception:
            pass
        _log(f"已解出 {binary.name}")
        return binary

    return downloaded                      # Windows：exe 本身就是替换目标


def read_pending():
    data = _read_json(PENDING_FILE)
    return data if isinstance(data, dict) else None


def write_pending(entry, staged: Path) -> bool:
    return _write_json(PENDING_FILE, {
        "version": entry["version"],
        "platform": platform_support.platform_key(),
        "staged": str(staged),
        "sha256": entry["sha256"],
        "size": entry["size"],
        "notes": entry.get("notes", ""),
        "created_at": int(time.time()),
        "attempts": 0,
    })


def prepare_update(manifest=None):
    """完整走一遍"选版本 → 下载 → 暂存 → 写待安装标记"。返回 (entry, staged) 或 None。"""
    entry = select_release(manifest or fetch_manifest())
    if entry is None:
        return None

    pending = read_pending()
    if (pending
            and pending.get("version") == entry["version"]
            and pending.get("sha256") == entry["sha256"]):
        staged = Path(str(pending.get("staged", "")))
        if staged.exists():
            _log(f"v{entry['version']} 已经准备好，等下次启动")
            return entry, staged

    downloaded = download_update(entry)
    if downloaded is None:
        return None
    try:
        staged = stage_update(entry, downloaded)
    except Exception as e:
        _log(f"展开更新包失败：{e}")
        return None

    if not write_pending(entry, staged):
        return None
    return entry, staged


# ================= 下次启动：替换自己 =================

def _target_path() -> Path:
    """真正要被替换的东西：Windows/Linux 是可执行文件，macOS 是整个 .app。"""
    exe = Path(sys.executable).resolve()
    if platform_support.is_macos():
        parts = exe.parts
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].endswith(".app"):
                return Path(*parts[: i + 1])
    return exe


def _acquire_lock() -> bool:
    try:
        UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age <= UPDATE_LOCK_STALE:
                return False
            _log("清理陈旧的更新锁")
            _rm(LOCK_FILE)
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except Exception as e:
        _log(f"拿不到更新锁：{e}")
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


def _replace(staged: Path, target: Path) -> bool:
    """rename 旧的 → 放新的 → 失败就换回去。Windows 上运行中的 exe 可以改名。"""
    backup = target.with_name(f"{target.name}.old-{os.getpid()}")
    fresh = target.with_name(f"{target.name}.new-{os.getpid()}")
    _rm(fresh)

    try:
        os.replace(target, backup)
    except Exception as e:
        _log(f"重命名旧版本失败：{e}")
        return False

    try:
        if staged.is_dir():
            shutil.copytree(staged, fresh, symlinks=True)
        else:
            shutil.copy2(staged, fresh)
            fresh.chmod(fresh.stat().st_mode | 0o111)
        os.replace(fresh, target)
        _log(f"替换成功：{target}")
        return True
    except Exception as e:
        _log(f"写入新版本失败：{e}，回滚")
        _rm(fresh)
        try:
            os.replace(backup, target)
        except Exception as e2:
            _log(f"回滚失败（需要手动重装）：{e2}")
        return False


def _relaunch(target: Path, argv) -> bool:
    """用原来的参数重新启动自己。成功返回 True（调用方随后 sys.exit）。"""
    env = os.environ.copy()
    rest = list(argv[1:])
    try:
        if platform_support.is_macos():
            url = next((a for a in rest if a.startswith("aikaui://")), None)
            if url:
                subprocess.Popen(["open", url], env=env)
            else:
                cmd = ["open", "-n", str(target)]
                if rest:
                    cmd += ["--args", *rest]
                subprocess.Popen(cmd, env=env)
        else:
            subprocess.Popen(
                [str(target), *rest],
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception as e:
        _log(f"重新启动失败：{e}（下次手动打开就是新版本）")
        return False


def _clear_staged(pending) -> None:
    staged = pending.get("staged") if isinstance(pending, dict) else None
    if not staged:
        return
    path = Path(str(staged))
    # 只清 updates/staged 下面的东西，绝不按 marker 里的路径乱删。
    # 以暂存路径自己的第一层目录为准（marker 里的 version 可能和它不一致，
    # 比如版本不够新被丢弃时）。
    try:
        rel = path.resolve().relative_to(STAGED_DIR.resolve())
    except Exception:
        return
    if rel.parts:
        _rm(STAGED_DIR / rel.parts[0])


def apply_pending_update(argv=None) -> bool:
    """启动时、创建 Tk 之前调用。替换成功并重新拉起自己时 sys.exit(0)。"""
    argv = list(argv if argv is not None else sys.argv)
    if not getattr(sys, "frozen", False) and os.environ.get("AGENT_UPDATE_TEST") != "1":
        return False

    pending = read_pending()
    if not pending:
        return False

    version = pending.get("version")
    if not is_newer(version, APP_VERSION):
        _log(f"待安装的 v{version} 不比当前 v{APP_VERSION} 新，丢掉")
        _clear_staged(pending)
        _rm(PENDING_FILE)
        return False

    staged = Path(str(pending.get("staged", "")))
    if not staged.exists():
        _log("暂存文件不见了，丢掉待安装标记")
        _rm(PENDING_FILE)
        return False

    if not _acquire_lock():
        _log("已有另一个实例在更新，本次跳过")
        return False

    try:
        target = _target_path()
        # 开发环境（AGENT_UPDATE_TEST=1）里 _target_path() 会指向 python.exe 本身。
        # 绝不能在没打包时把解释器换掉。
        if not getattr(sys, "frozen", False) and target.resolve() == Path(sys.executable).resolve():
            _log("开发环境：拒绝替换解释器本身")
            return False
        if not _replace(staged, target):
            attempts = int(pending.get("attempts") or 0) + 1
            pending["attempts"] = attempts
            if attempts >= 3:
                _write_json(FAILED_FILE, {
                    "version": version,
                    "reason": "连续 3 次替换失败",
                    "at": int(time.time()),
                })
                _clear_staged(pending)
                _rm(PENDING_FILE)
                _log(f"v{version} 放弃（失败 {attempts} 次）")
            else:
                _write_json(PENDING_FILE, pending)
            return False

        _write_json(APPLIED_FILE, {
            "version": version,
            "from": APP_VERSION,
            "at": int(time.time()),
            "notified": False,
        })
        _clear_staged(pending)
        _rm(PENDING_FILE)
        os.environ["CLAUDE_AGENT_UPDATE_APPLIED"] = str(version)

        if _relaunch(target, argv):
            sys.exit(0)
        return True                   # 替换成功但没拉起来：继续用内存里的旧代码
    finally:
        _release_lock()


def consume_applied_notice():
    """更新后第一次启动时返回一句"已更新到 vX"，只给一次。"""
    data = _read_json(APPLIED_FILE)
    if not isinstance(data, dict):
        return None
    if data.get("version") != APP_VERSION or data.get("notified"):
        return None
    data["notified"] = True
    _write_json(APPLIED_FILE, data)
    return f"已自动更新到 v{APP_VERSION}。"


def cleanup_stale_files() -> None:
    """清掉过期暂存 / 残留 .part / 旧版本备份。失败静默。"""
    now = time.time()
    try:
        if STAGED_DIR.exists():
            for p in STAGED_DIR.iterdir():
                try:
                    if p.is_dir() and now - p.stat().st_mtime > UPDATE_STAGED_TTL:
                        shutil.rmtree(p, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

    try:
        for p in UPDATES_DIR.rglob("*.part"):
            try:
                if now - p.stat().st_mtime > 24 * 3600:
                    p.unlink()
            except Exception:
                pass
    except Exception:
        pass

    try:
        target = _target_path()
        for pattern in (f"{target.name}.old-*", f"{target.name}.new-*"):
            for p in target.parent.glob(pattern):
                try:
                    if now - p.stat().st_mtime > 24 * 3600:
                        _rm(p)
                except Exception:
                    pass
    except Exception:
        pass


def start_background_check(on_ready=None, delay=None, interval=None):
    """起一条 daemon 线程检查更新。只在打包后的程序里跑（测试可用 AGENT_UPDATE_TEST=1）。"""
    if not getattr(sys, "frozen", False) and os.environ.get("AGENT_UPDATE_TEST") != "1":
        return None

    delay = UPDATE_INITIAL_DELAY if delay is None else delay
    interval = UPDATE_CHECK_INTERVAL if interval is None else interval

    def _run():
        time.sleep(delay)
        while True:
            try:
                ready = prepare_update()
                if ready:
                    entry = ready[0]
                    text = f"新版本 v{entry['version']} 已下载，下次启动自动更新。"
                    _log(text)
                    if on_ready:
                        try:
                            on_ready(text)
                        except Exception:
                            pass
                    return                    # 已经准备好了，不用再查
            except Exception as e:
                _log(f"检查更新出错：{e}")
            time.sleep(interval)

    t = threading.Thread(target=_run, daemon=True, name="update-check")
    t.start()
    return t
