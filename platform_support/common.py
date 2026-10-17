# -*- coding: utf-8 -*-
"""三个平台共用的零碎逻辑。

这里只放"跟操作系统无关"的东西：把工作目录限制在用户主目录里、按候选列表找
claude、往 PATH 前面塞目录。平台专属的实现放在 windows.py / macos.py / linux.py。
"""

import os
import shutil
from pathlib import Path

from config import HOME


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
