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
  open_vscode(directory)     打开工作目录，并把 Claude 面板亮出来

★ open_vscode 的返回值必须以「已打开 VSCode」开头：agent_core 靠这个前缀判断
  "安装流程真的把用户领进门了"，和 launch_claude 的「已打开新终端」是同一个机制。
★ "打开面板"不是白叫的：VSCode 命令行没有 --command 之类的东西，只能靠插件自己
  注册的 vscode://anthropic.claude-code/open 链接（见 EXTENSION_OPEN_URI）。
"""

import json
import subprocess
import time

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

# 插件自己注册的 URI 入口：vscode://anthropic.claude-code/open → 执行
# claude-vscode.primaryEditor.open，也就是"把 Claude 面板亮出来"。
# 这是插件 manifest 之外唯一的自动打开方式（VSCode 命令行没有 --command 这种东西），
# 见 extension.js 里的 registerUriHandler（path 就两种：/open 和 /install-plugin）。
EXTENSION_OPEN_URI = f"vscode://{EXTENSION_ID}/open"

# 发 URI 之前等多久：刚开出来的窗口要先完成启动、插件宿主（extension host）
# 才能注册 URI 处理器；太早发，链接会因为"没有处理程序"被丢掉。
# 已开着的窗口其实不用等，但这里分不出来，统一等一次；装机流程本来就要几分钟。
PANEL_SETTLE_SECONDS = 4.0

LIST_TIMEOUT = 60          # 列扩展是本地操作，秒级
INSTALL_TIMEOUT = 300      # 装扩展要下包，给 5 分钟

# 和 agent_core._OPENED_VSCODE_PREFIX 是同一个契约，改一处必须改另一处
_OPENED_PREFIX = "已打开 VSCode"

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

    机器上没有 winget 会先自动装（见 platform_support/windows.py 里那段注释），
    那一步要下 58MB；winget 整个不通时还要下微软官方的安装包（240MB）。
    两段下载都把百分比打进对话，免得用户以为卡住了。

    ★ 必须先 ask_user 得到用户同意再调用（prompts 里写死了）。"""
    u = get_ui()
    if u:
        u.ui_progress_start("正在安装 VSCode（可能要几分钟）…")

    reported = {"pct": -25, "what": ""}      # 第一次一定报（0%）
    # 两种下载说清楚是不同的东西，别让用户看到「58MB」却下了 240MB
    labels = {
        "winget": "正在准备 winget 包管理器（{pct}%，约 58MB，只有第一次需要）",
        "vscode": "正在下载 VSCode 安装包（{pct}%，约 240MB，几分钟）",
    }

    def on_progress(done, total, what="winget"):
        pct = int(done * 100 / total) if total else 0
        if what != reported["what"]:         # 换了一段下载，进度重新算
            reported["what"] = what
            reported["pct"] = -25
        if pct < reported["pct"] + 25:
            return
        reported["pct"] = pct
        cur = get_ui()
        if cur:
            cur.ui_log(labels.get(what, labels["winget"]).format(pct=pct))

    def on_note(msg):
        cur = get_ui()
        if cur and msg:
            cur.ui_log(msg)

    try:
        return platform_support.install_vscode(on_progress=on_progress,
                                               on_note=on_note)
    finally:                              # ★ finally：出异常也要把进度条收掉
        if get_ui():
            get_ui().ui_progress_stop()


# 这一轮是不是刚把插件装进了磁盘。刚装完必须开新窗口：已经开着的窗口是在装插件
# 之前加载扩展的，里面没有它，URI 发过去会因为"没有处理程序"被丢掉。
_extension_just_installed = False


def install_claude_extension() -> str:
    """给已经装好的 VSCode 装 Claude 插件（anthropic.claude-code）。"""
    global _extension_just_installed
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
    _extension_just_installed = True      # 下一步 open_vscode 要据此开新窗口
    return f"已装好 Claude 的 VSCode 插件（{EXTENSION_ID}）"


def _open_claude_panel(info) -> str:
    """把 Claude 面板亮出来。返回要接在"已打开 VSCode"后面的半句话。

    ★ 只有确认插件在列表里才发 URI：没处理程序时 VSCode 会弹一个"无法打开链接"
      的报错框，用户会以为是助手搞坏了什么。"""
    if not info:
        return ""
    cli = info.get("cli_argv") or []
    if not cli:
        return "；没找到 VSCode 的命令行工具，Claude 面板没能自动打开（点左边活动栏的 Claude 图标）"

    exts, _note = _list_extensions(cli)
    if exts is None:
        return "；Claude 插件状态查不出来，面板没能自动打开（点左边活动栏的 Claude 图标）"
    if EXTENSION_ID not in exts:
        return ""                        # 用户不要插件，就别硬开

    u = get_ui()
    if u:
        u.ui_progress_start("正在打开 Claude 面板…")
    try:
        time.sleep(PANEL_SETTLE_SECONDS)
        err = platform_support.open_uri(EXTENSION_OPEN_URI)
    finally:
        if get_ui():
            get_ui().ui_progress_stop()

    if err:
        return f"；Claude 面板没能自动打开（{err}），点左边活动栏的 Claude 图标就行"
    return "，Claude 面板也打开了"


def open_vscode(directory: str = "") -> str:
    """用 VSCode 打开工作目录，**并把里面的 Claude 面板亮出来**。

    ★ 返回值必须以「已打开 VSCode」开头（agent_core 靠它判断安装完成）。
    ★ 和 launch_claude 二选一：走了 VSCode 就不要再开终端。
    ★ trust=True：工作目录是助手替用户新建的，VSCode 一定把它当"未信任"，
      弹「是否信任这个文件夹的作者」。不点信任，插件就在受限模式里 ——
      面板开不出来、终端也开不了，用户看到的就是"插件没装上"。
      这个开关只影响**这一个窗口**，不改任何全局设置。"""
    global _extension_just_installed
    new_window = _extension_just_installed
    _extension_just_installed = False

    info = platform_support.find_vscode()
    text = platform_support.open_vscode(directory, new_window, trust=True)
    if not text.startswith(_OPENED_PREFIX):
        return text                      # 没打开就别再谈面板了
    return text + _open_claude_panel(info)

