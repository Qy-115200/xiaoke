# -*- coding: utf-8 -*-
"""VSCode 相关工具：检测 → 装 VSCode → 装 Claude 插件 → 打开 VSCode。

为什么单独一个文件、不塞进 tools_fixed.py：
  tools_fixed 是"装 Claude Code 必须按顺序走完的五个固定工具"，
  而 VSCode 这一组是**有条件的**（用户可能已经有了、也可能直接不要），
  混在一起会把那条顺序的语义搅乱。

四个工具的分工是刻意的——一个动词一个工具：
  check_vscode()             只读，拿事实（JSON）
  install_vscode()           装 VSCode 本体
  install_claude_extension() 装 anthropic.claude-code 插件
  open_vscode(directory)     打开并带上工作目录

★ open_vscode 的返回值必须以「已打开 VSCode」开头：agent_core 靠这个前缀判断
  "安装流程真的把用户领进门了"，和 launch_claude 的「已打开新终端」是同一个机制。
"""

import json
import subprocess

import platform_support
from platform_support.common import (
    clean_env,
    parse_extension_list,
    run_capture,
    smart_decode,
)
from server.ui_bridge import get_ui

# Claude Code 官方 VSCode 扩展的 ID（真机上 `code --list-extensions` 抄下来的）
EXTENSION_ID = "anthropic.claude-code"

LIST_TIMEOUT = 60          # 列扩展是本地操作，秒级
INSTALL_TIMEOUT = 300      # 装扩展要下包，给 5 分钟

# 装扩展失败时，把常见的英文报错归成一句人话。别把整段日志倒给模型。
_NETWORK_HINTS = (
    "etimedout", "enotfound", "econnrefused", "eai_again", "getaddrinfo",
    "failed to download", "net::", "socket hang up",
)
_BROKEN_CLI_HINTS = (
    "bad option", "not recognized", "no such file", "不是内部或外部命令",
)


def _list_extensions(cli_argv):
    """跑 `code --list-extensions --show-versions`。

    返回 (dict | None, 说明)。dict 是 {扩展id: 版本}；None 表示**没查出来**
    （没有命令行工具 / 超时 / 出错），这时调用方要当成"未知"，不要当成"没装"。"""
    if not cli_argv:
        return None, ("找不到 VSCode 的命令行工具，插件状态查不出来"
                      "（应该是装的时候没勾「添加到 PATH」）")
    try:
        out, err, timed_out, rc = run_capture(
            list(cli_argv) + ["--list-extensions", "--show-versions"],
            timeout=LIST_TIMEOUT, env=clean_env(), stdin=subprocess.DEVNULL)
    except Exception as e:
        return None, f"查插件列表失败：{e}"
    if timed_out:
        return None, f"查插件列表超时（{LIST_TIMEOUT} 秒）"
    if rc != 0:
        detail = (smart_decode(out) + smart_decode(err)).strip().splitlines()
        tail = detail[-1][:120] if detail else f"退出码 {rc}"
        return None, f"查插件列表失败：{tail}"
    return parse_extension_list(smart_decode(out)), ""


def _classify_failure(text: str, rc: int) -> str:
    """把插件安装的报错归成一句用户能懂、模型能转述的话。"""
    low = (text or "").lower()
    if any(h in low for h in _NETWORK_HINTS):
        return ("连不上 VSCode 插件市场（网络问题），稍后可以再试，"
                "或者在 VSCode 左边的扩展面板里搜 Claude Code 手动装")
    if any(h in low for h in _BROKEN_CLI_HINTS):
        return ("VSCode 的命令行工具跑不起来，可能装得不完整；"
                "在 VSCode 的扩展面板里搜 Claude Code 手动装")
    line = next((l.strip() for l in reversed((text or "").splitlines()) if l.strip()), "")
    return f"命令返回 {rc}" + (f"：{line[:200]}" if line else "")


# ================= 工具实现 =================

def check_vscode() -> str:
    """检查这台电脑有没有 VSCode、里面有没有 Claude 插件。只读，不装任何东西。

    返回 JSON（模型读的是这个，不是给人看的文字）：
        installed            有没有 VSCode
        extension_installed  有/没有/None(没查出来 —— 调用方当成"没有"处理)
    """
    info = platform_support.find_vscode()
    if not info:
        return json.dumps({
            "installed": False,
            "extension_id": EXTENSION_ID,
            "extension_installed": False,
        }, ensure_ascii=False)

    payload = {
        "installed": True,
        "exe": info.get("exe") or "",
        "source": info.get("source") or "",
        "extension_id": EXTENSION_ID,
    }
    exts, note = _list_extensions(info.get("cli_argv") or [])
    if exts is None:
        payload["extension_installed"] = None
        payload["note"] = note
    else:
        payload["extension_installed"] = EXTENSION_ID in exts
        version = exts.get(EXTENSION_ID)
        if version:
            payload["extension_version"] = version
    return json.dumps(payload, ensure_ascii=False)


def install_vscode() -> str:
    """装 VSCode。Windows 走 winget 用户级安装，macOS 走 brew；装不上自动打开下载页。

    ★ 必须先 ask_user 得到用户同意再调用（prompts 里写死了）。"""
    u = get_ui()
    if u:
        u.ui_progress_start("正在安装 VSCode（可能要几分钟）…")
    try:
        return platform_support.install_vscode()
    finally:                              # ★ finally：出异常也要把进度条收掉
        if get_ui():
            get_ui().ui_progress_stop()


def install_claude_extension() -> str:
    """给已经装好的 VSCode 装 Claude 插件（anthropic.claude-code）。"""
    info = platform_support.find_vscode()
    if not info:
        return "装插件失败：这台机器上没找到 VSCode，得先装 VSCode"
    cli = info.get("cli_argv") or []
    if not cli:
        return ("装插件失败：找不到 VSCode 的命令行工具，"
                "请在 VSCode 左边的扩展面板里搜 Claude Code 手动装")

    # 先查一遍：已经装了就别再去碰网络（用户重跑安装流程很常见）
    exts, _note = _list_extensions(cli)
    if exts is not None and EXTENSION_ID in exts:
        return f"已经装好 Claude 的 VSCode 插件（{EXTENSION_ID}），不用重复装"

    u = get_ui()
    if u:
        u.ui_progress_start("正在装 Claude 的 VSCode 插件…")
    try:
        out, err, timed_out, rc = run_capture(
            list(cli) + ["--install-extension", EXTENSION_ID],
            timeout=INSTALL_TIMEOUT, env=clean_env(), stdin=subprocess.DEVNULL)
    except Exception as e:
        return f"装插件失败：{e}"
    finally:
        if get_ui():
            get_ui().ui_progress_stop()

    if timed_out:
        return (f"装插件失败：超时（{INSTALL_TIMEOUT} 秒），可能网络太慢；"
                "可以在 VSCode 的扩展面板里搜 Claude Code 手动装")

    text = (smart_decode(out) + "\n" + smart_decode(err)).strip()
    if rc != 0:
        return "装插件失败：" + _classify_failure(text, rc)

    # 命令说成功不算数，得真在列表里看见它（装到别的 profile 之类会漏）
    exts, _note = _list_extensions(cli)
    if exts is not None and EXTENSION_ID not in exts:
        return ("装插件失败：命令说装完了，但扩展列表里没看到；"
                "可以在 VSCode 的扩展面板里搜 Claude Code 手动装")
    return f"已装好 Claude 的 VSCode 插件（{EXTENSION_ID}）"


def open_vscode(directory: str = "") -> str:
    """用 VSCode 打开指定工作目录。

    ★ 返回值必须以「已打开 VSCode」开头（agent_core 靠它判断安装完成）。
    ★ 和 launch_claude 二选一：走了 VSCode 就不要再开终端。"""
    return platform_support.open_vscode(directory)
