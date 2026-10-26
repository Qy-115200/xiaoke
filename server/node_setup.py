# -*- coding: utf-8 -*-
"""把 Node.js 装进用户自己的 `~/.local`，不碰系统目录、不调 sudo。

服务器上最常见的拦路虎是"这台机器没有 node"。桌面版让 Agent 去跑
`sudo apt-get install nodejs` —— 服务器上那等于让助手替你提权，不做。
这里改成：从 npmmirror 下 Node 官方二进制包，解到 `~/.local/node`，
再把 `~/.local/bin` 加进 PATH。全程都在用户主目录里。

为什么最低要 Node 22：`@anthropic-ai/claude-code` 的 package.json 里
`"engines": {"node": ">=22.0.0"}`。系统里那个 18 / 20 的 node 直接用会装不上。
"""

import os
import platform as _platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from server.cli import (
    EXIT_NODE, MARK_BEGIN, MARK_END, CliError, atomic_symlink,
    ensure_shell_path, safe_extract_tar, sha256_file,
)

NODE_INDEX_URL = "https://cdn.npmmirror.com/binaries/node/index.json"
NODE_BASE_URL = "https://cdn.npmmirror.com/binaries/node"

MIN_NODE_MAJOR = 22
MIN_NODE_TEXT = f"{MIN_NODE_MAJOR}.0.0"

# 从 ~/.local/node/bin 链到 ~/.local/bin 的命令
LINKED_COMMANDS = ("node", "npm", "npx", "corepack")


# ================= 纯函数（离线可测）=================

def parse_semver(text) -> tuple:
    """"v24.22.1" / "24.22.1" → (24, 22, 1)。解析不出来返回 ()。"""
    s = str(text or "").strip().lstrip("vV")
    parts = s.split(".")
    if not parts or not parts[0]:
        return ()
    nums = []
    for p in parts[:3]:
        digits = ""
        for ch in p:                      # "22.11.0-pre" 的预发布后缀不参与比较
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        nums.append(int(digits))
    return tuple(nums)


def version_major(text) -> int:
    v = parse_semver(text)
    return v[0] if v else 0


def node_version_ok(text, min_major: int = MIN_NODE_MAJOR) -> bool:
    return version_major(text) >= min_major


def detect_node_arch(machine=None) -> str | None:
    """Node 官方包里的架构名：x64 / arm64。认不出来返回 None。"""
    m = (machine or _platform.machine() or "").strip().lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return None


def files_key(arch: str) -> str:
    """index.json 的 `files` 数组里用的名字，例如 linux-x64。"""
    return f"linux-{arch}"


def select_latest_lts(index, arch: str, min_major: int = MIN_NODE_MAJOR):
    """从 index.json 里挑最合适的 LTS 版本。

    条件是：lts 为真、主版本够、这个架构有官方包。**不按顺序取** ——
    index.json 的顺序不是保证，版本号大小才是。"""
    if not isinstance(index, list):
        return None
    wanted = files_key(arch)
    best, best_ver = None, ()
    for entry in index:
        if not isinstance(entry, dict):
            continue
        if not entry.get("lts"):               # lts 是 false → 当前版，跳过
            continue
        ver = parse_semver(entry.get("version"))
        if not ver or ver[0] < min_major:
            continue
        if wanted not in (entry.get("files") or []):
            continue
        if ver > best_ver:
            best, best_ver = entry, ver
    return best


def pick_pinned(index, version):
    """在 index.json 里按版本号精确找一条（--node-version 用）。"""
    want = str(version or "").lstrip("vV")
    for entry in index if isinstance(index, list) else []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("version", "")).lstrip("vV") == want:
            return entry
    return None


def asset_name(version: str, arch: str) -> str:
    v = str(version).lstrip("vV")
    return f"node-v{v}-linux-{arch}.tar.xz"


def asset_url(version: str, arch: str, base: str = NODE_BASE_URL) -> str:
    v = str(version).lstrip("vV")
    return f"{base.rstrip('/')}/v{v}/{asset_name(v, arch)}"


def shasums_url(version: str, base: str = NODE_BASE_URL) -> str:
    v = str(version).lstrip("vV")
    return f"{base.rstrip('/')}/v{v}/SHASUMS256.txt"


def parse_shasums(text: str) -> dict:
    """`SHASUMS256.txt` → {文件名: sha256}。格式是 `<sha>  <name>`。"""
    out = {}
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest, name = parts[0].lower(), parts[-1].lstrip("*")
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            out[name] = digest
    return out


# ================= 找现成的 node =================

def _node_version(runner, exe) -> str:
    res = runner.run([str(exe), "-v"], timeout=30, stdin=subprocess.DEVNULL)
    return (res.out or "").strip() if res.ok else ""


def find_system_node(env, runner, min_major: int = MIN_NODE_MAJOR):
    """PATH 里的 node，够新才返回。返回 (路径|None, 版本字符串)。"""
    exe = shutil.which("node", path=env.get("PATH", ""))
    if not exe:
        return None, ""
    ver = _node_version(runner, exe)
    return (exe if node_version_ok(ver, min_major) else None), ver


def find_private_node(ctx, min_major: int = MIN_NODE_MAJOR):
    """上次装到 ~/.local/node 的那份（可能在 PATH 里还没生效）。"""
    exe = Path(ctx.prefix) / "node" / "bin" / "node"
    if not exe.is_file():
        return None, ""
    ver = _node_version(ctx.runner, exe)
    return (str(exe) if node_version_ok(ver, min_major) else None), ver


def _prepend(ctx, dirs):
    from platform_support.common import prepend_paths
    prepend_paths(ctx.env, [d for d in dirs if d])
    # runner 持有一份 env 引用；显式同步一次，防止有人传进来的是副本
    if getattr(ctx.runner, "env", None) is not None:
        ctx.runner.env = ctx.env


# ================= 主流程 =================

def ensure_node(ctx, arch=None, pinned=None,
                min_major: int = MIN_NODE_MAJOR) -> dict:
    """保证有够新的 node。返回

        {"node": 路径, "npm": 路径, "version": 版本,
         "install_dir": 目录, "installed": bool, "arch": 架构, "planned": bool}

    ★ 出错一律抛 CliError(exit_code=EXIT_NODE)：调用方只认这一种。"""
    arch = arch or detect_node_arch()
    if not arch:
        raise CliError(
            f"认不出这台机器的架构（{_platform.machine() or '未知'}），"
            f"只支持 x86_64 / aarch64",
            EXIT_NODE, hint="可以从 https://nodejs.org/zh-cn/download 手动装 Node 22+")

    # 1) PATH 里已经有够新的 —— 直接用，不碰 ~/.local
    exe, ver = find_system_node(ctx.env, ctx.runner, min_major)
    if exe:
        ctx.note(f"    node {ver}（{exe}）")
        npm = shutil.which("npm", path=ctx.env.get("PATH", "")) or ""
        return {"node": exe, "npm": npm, "version": ver,
                "install_dir": os.path.dirname(exe), "installed": False,
                "arch": arch, "planned": False}

    # 2) 上次装过、只是新 shell 还没读 PATH
    exe, ver = find_private_node(ctx, min_major)
    if exe:
        ctx.note(f"    node {ver}（{exe}，上次装的）")
        _prepend(ctx, [Path(ctx.prefix) / "node" / "bin", ctx.bin_dir])
        return {"node": exe, "npm": str(Path(ctx.prefix) / "node" / "bin" / "npm"),
                "version": ver, "install_dir": str(Path(ctx.prefix) / "node"),
                "installed": False, "arch": arch, "planned": False}

    ctx.note(f"    需要 Node ≥ {MIN_NODE_TEXT}："
             f"{('发现 ' + ver + '，太旧') if ver else '没找到 node'}")

    if ctx.dry_run:
        ctx.log(f"    （--dry-run）会下载 Node {pinned or '最新 LTS'} "
                f"({files_key(arch)}) 到 {Path(ctx.prefix) / 'node'}")
        return {"node": "", "npm": "", "version": "", "install_dir": "",
                "installed": False, "arch": arch, "planned": True}

    # 3) 从镜像取版本清单
    try:
        index = ctx.http.get_json(NODE_INDEX_URL, timeout=30)
    except Exception as e:
        raise CliError(
            f"连不上 Node 镜像：{e}", EXIT_NODE,
            hint=f"可以自己先装 Node {MIN_NODE_TEXT}+ 再重跑："
                 f"https://nodejs.org/zh-cn/download")

    entry = pick_pinned(index, pinned) if pinned else select_latest_lts(index, arch, min_major)
    if entry is None:
        raise CliError(
            (f"镜像里没有你指定的 Node 版本 {pinned}" if pinned
             else f"镜像里没有可用的 Node LTS（需要 ≥ {MIN_NODE_TEXT}）"),
            EXIT_NODE, hint="https://nodejs.org/zh-cn/download 上有官方包")

    version = str(entry.get("version", "")).lstrip("vV")
    name = asset_name(version, arch)
    target = Path(ctx.prefix) / f"node-v{version}"

    if (target / "bin" / "node").is_file():
        ctx.note(f"    Node {version} 已经装过了，直接用")
    else:
        ctx.log(f"    下载 Node {version}（{name}）…")
        tmp_root = Path(ctx.prefix)
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=str(tmp_root), dir=str(tmp_root)) as td:
            td = Path(td)
            archive = td / name
            try:
                ctx.http.download(asset_url(version, arch), archive, timeout=900)
            except CliError:
                raise
            except Exception as e:
                raise CliError(f"下载 Node 失败：{e}", EXIT_NODE,
                               hint="网络能连 cdn.npmmirror.com 吗？")
            want = _expected_digest(ctx, version, name)
            if want:
                got = sha256_file(archive)
                if got != want:
                    raise CliError(
                        f"Node 压缩包校验失败（下载损坏或被篡改）：\n"
                        f"  期望 {want}\n  实际 {got}", EXIT_NODE)
            else:
                ctx.note("    （镜像没给校验值，跳过 sha256 核对）")
            extracted = safe_extract_tar(archive, td / "x")
            root = extracted / f"node-v{version}-linux-{arch}"
            if not (root / "bin" / "node").is_file():
                raise CliError("压缩包里没找到 bin/node，可能下错了文件", EXIT_NODE)
            _replace_dir(root, target)

    install_dir = target
    for cmd in LINKED_COMMANDS:
        src = install_dir / "bin" / cmd
        if src.is_file():
            _safe_link(ctx, src, Path(ctx.bin_dir) / cmd)

    # ~/.local/node 指向这次装的版本。用户自己建过同名真目录就先挪开，不删。
    node_link = Path(ctx.prefix) / "node"
    if node_link.exists() and not node_link.is_symlink():
        aside = Path(ctx.prefix) / f"node.old-{int(os.path.getmtime(node_link))}"
        try:
            os.replace(node_link, aside)
            ctx.note(f"    {node_link} 原来是个目录，挪到了 {aside}")
        except Exception:
            pass
    _safe_link(ctx, install_dir, node_link)

    _prepend(ctx, [install_dir / "bin", ctx.bin_dir])
    changed = ensure_shell_path(ctx.home, ctx.prefix, extra_dirs=[install_dir / "bin"])
    if changed:
        ctx.note(f"    已把 PATH 写进：{', '.join(changed)}")

    node_bin = install_dir / "bin" / "node"
    ver = _node_version(ctx.runner, node_bin)
    if not node_version_ok(ver, min_major):
        raise CliError(f"Node 装完了但跑不起来（{ver or '无输出'}）", EXIT_NODE,
                       hint="可能缺 glibc 或架构不匹配，试试手动下载官方包")

    return {"node": str(node_bin), "npm": str(install_dir / "bin" / "npm"),
            "version": ver, "install_dir": str(install_dir),
            "installed": True, "arch": arch, "planned": False}


def _expected_digest(ctx, version: str, name: str) -> str:
    """SHASUMS256.txt 里这个文件的 sha256；拿不到就返回空串（只提示，不拦）。"""
    try:
        table = parse_shasums(ctx.http.get_text(shasums_url(version), timeout=30))
    except Exception:
        return ""
    return table.get(name, "")


def _replace_dir(src: Path, dst: Path) -> None:
    """把解压出来的目录原子地挪到最终位置。"""
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst, ignore_errors=True)     # 上次没解完的残留
        else:
            dst.unlink()
    os.replace(src, dst)


def _safe_link(ctx, src, dst) -> None:
    """建软链；建不了（Windows / 不支持软链的文件系统）就退回复制。

    ★ 装 Node 这件事不能因为"软链不允许"就失败 —— 有些容器和挂载点上
      os.symlink 直接 EPERM。"""
    src, dst = Path(src), Path(dst)
    try:
        atomic_symlink(src, dst)
        return
    except Exception:
        pass
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    except Exception as e:
        ctx.note(f"    （建不了 {dst}：{e}）")
