# -*- coding: utf-8 -*-
"""服务器版的安装会话：账号引导 → 模型干活 → 验收 → 打开 claude。

分工（和 `server/term_chat.py` 一头一尾）：

    主线程             ui.start() → ui.run()（读键、写屏，直到会话结束）
    工作线程（本模块）  bootstrap_identity → run_one_task … → ui.finish()

**账号引导是唯一一段确定性的流程**，不让模型碰：`llm.get_client()` 没有
identity.json 就直接抛 `NoIdentity`，模型连话都说不上，怎么可能先把自己装上。
除了这一小段，装什么、怎么装、失败怎么办全交给模型。

★ `agent_core` / `prompts` / `openai` 全部**在函数里 import**：`import server_main`
  必须保持干净（tests/test_imports.py 盯着），server.spec 也靠这条成立。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from agent_bus import clear_cancel as _clear_cancel, user_input_queue
from server import agent_tools, identity
from server import prompts_server as P
from server.cli import EXIT_ACCOUNT, EXIT_FAILED, EXIT_OK, CliError
from server.term_chat import Outcome

SITE_URL = "https://aikaui.com"

# 模型调不通/跑不动时，给用户看的那句话 + 该去干什么。
# ★ 「连不上模型」不能含糊成「安装失败」：用户会以为是自己机器的问题。
_STATUS_MESSAGES = {
    "no_identity": ("本机没有账号凭证，模型用不了",
                    "重跑一次安装器；一直这样就用 --account-code 绑一个已有账号",
                    EXIT_ACCOUNT),
    "balance": ("账户余额已用完，模型暂时用不了",
                "充值：ClaudeCodeAssistant --recharge 20（会打印二维码）",
                EXIT_ACCOUNT),
    # 余额还在，是**这次请求的预估花费**超过了它（new-api 按 上下文 + max_tokens
    # 预扣费估算）。跟上面的"真没钱"分开说 —— 混成一句会让人白充钱。
    "request_too_big": ("这次请求的预估花费超过了剩余余额",
                        "把问题拆小、去掉图片/长文件后重试；要继续跑大任务就先 "
                        "ClaudeCodeAssistant --recharge 20",
                        EXIT_FAILED),
    "model": ("连不上模型服务器",
              f"先确认这台机器能打开 {SITE_URL}，然后重跑一次",
              EXIT_FAILED),
    "bad_response": ("模型服务器返回了看不懂的内容",
                     "等一会儿重跑；一直这样请联系我们",
                     EXIT_FAILED),
    "max_steps": ("Agent 跑满了步数上限还没装完",
                  "重跑一次通常会接着上次的进度",
                  EXIT_FAILED),
}


# ================= 账号引导（确定性） =================

def _answer_text(reply) -> str:
    """界面回的是「用户回答：xxx」，取回用户实际说的那一段。"""
    text = str(reply or "").strip()
    for prefix in ("用户回答：", "用户回答:"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _choose(reply, options, keywords, default=0) -> int:
    """把用户的回答对回选项下标。

    先认界面回传的选项原文（回车选中时就是它），再按关键词认自由输入。"""
    text = _answer_text(reply)
    for i, opt in enumerate(options):
        if text == opt or text.endswith(opt):
            return i
    for i, words in enumerate(keywords):
        if any(w in text for w in words):
            return i
    return default


def _ok(account_code="", recovered=False, note="") -> dict:
    return {"ok": True, "account_code": account_code, "recovered": bool(recovered),
            "note": note, "message": "", "hint": "", "exit_code": EXIT_OK}


def _bad(message, hint="", exit_code=EXIT_ACCOUNT) -> dict:
    return {"ok": False, "account_code": "", "recovered": False, "note": "",
            "message": message, "hint": hint, "exit_code": exit_code}


def bootstrap_identity(ctx, args, ui) -> dict:
    """在模型上场之前把账号定下来。返回 dict，不抛异常（失败靠返回值）。

    ★ 规矩只有一条最要紧：**本机有凭证就绝不重新注册**。重新注册 = 开一个新号，
      用户原来的余额就永远回不来了。
    """
    local = identity.load_identity()
    if local and local.get("api_key"):
        code = local.get("account_code") or "（缺账户码）"
        ui.ui_log(f"    本机已有账号：{code}")
        return _ok(local.get("account_code") or "", note="沿用本机已有的账号")

    # ① 用户明确给了账户码 —— 这是最强的一句意图，优先于别的
    if str(getattr(args, "account_code", "") or "").strip():
        ui.ui_log("    正在绑定你给的账号 …")
        data, err, code, _wait = identity.bind_account(
            args.account_code, getattr(args, "account_key", "") or "")
        if not data:
            hint = "账户码和账户密钥要跟截图上一字不差；2026-10-24 之前注册的老账号可能不需要密钥"
            if code == "account_key_required":
                hint = ("这个账号还需要账户密钥："
                        "加上 --account-key <账户密钥> 再跑一次")
            elif code == "too_many_attempts":
                hint = f"试得太频繁了，等 {_wait or 60} 秒再试"
            return _bad(f"绑定账号失败：{err}", hint)
        identity.adopt_identity(data)
        acc = data.get("account_code") or str(args.account_code).strip()
        return _ok(acc, bool(data.get("is_recovered")), note="已绑定到你给的账号")

    # ② 保险箱里还留着旧账号 —— 绝不能默默开新号（那等于把余额扔了）
    hint = identity.pending_recovery() or {}
    if hint.get("account_code") and not getattr(args, "new_account", False):
        code = hint["account_code"]
        options = [f"恢复这个账号（{code}）",
                   "开一个新账号（旧账号的余额不会跟过来）",
                   "先不装，退出"]
        pick = _choose(ui.ui_ask(
            f"这台机器以前用过账号 {code}，要用回它吗？", options),
            options, keywords=[("恢复", "用回", "继续用"), ("新",), ("退", "不装")])
        if pick == 2:
            return _bad("没有账号，安装停在这里",
                        "想用回原来那个账号：不带 --new-account 重跑一次\n"
                        f"想换别的账号：--account-code {code} --account-key <账户密钥>")
        if pick == 0:
            return _recover(ctx, ui, hint)
        ui.ui_log("    那就开一个新账号（旧账号的余额不会跟过来）")

    # ③ 全新机器
    else:
        options = ["现在领一个", "我有账户码", "先不装，退出"]
        pick = _choose(ui.ui_ask("这台机器还没有账号，怎么办？", options),
                       options, keywords=[("领", "注册", "新"), ("码", "有"), ("退", "不装")])
        if pick == 2:
            return _bad("没有账号，安装停在这里",
                        "已经有账号：--account-code <账户码> --account-key <账户密钥>")
        if pick == 1:
            return _bad(
                "安装停在这里：这一步不能输入账户密钥",
                "带上账户码和账户密钥重跑：\n"
                f"  curl -fsSL {SITE_URL}/install.sh | sh -s -- "
                "--account-code <账户码> --account-key <账户密钥>")

    return _register(ctx, ui)


def _recover(ctx, ui, cred) -> dict:
    """用保险箱里的凭证恢复账号。密钥可能没存（老版本只存了码）。"""
    code = cred.get("account_code") or ""
    key = cred.get("account_key") or ""
    ui.ui_log(f"    正在恢复账号 {code} …")
    data, err, rc, wait = identity.bind_account(code, key)
    if not data:
        hint = "重跑一次安装器；一直不行就用 --account-code + --account-key 手动绑"
        if rc == "account_key_required":
            hint = (f"这个账号还需要账户密钥：\n"
                    f"  curl -fsSL {SITE_URL}/install.sh | sh -s -- "
                    f"--account-code {code} --account-key <账户密钥>")
        elif rc == "too_many_attempts":
            hint = f"试得太频繁了，等 {wait or 60} 秒再试"
        return _bad(f"恢复账号失败：{err}", hint)
    identity.adopt_identity(data)
    return _ok(data.get("account_code") or code,
               bool(data.get("is_recovered")), note="已恢复原来的账号")


def _register(ctx, ui) -> dict:
    ui.ui_log("    正在向服务器申请账号 …")
    result = identity.register_device()
    if not str(result).startswith("{"):
        return _bad(
            f"领账号失败：{result}",
            "过一会儿再试；同一网络 24 小时内注册太多次会被限流。\n"
            "已有账号的话：--account-code <账户码> --account-key <账户密钥>")
    try:
        info = json.loads(result)
    except Exception:
        info = {}
    acc = info.get("account_code") or ""
    note = "服务器认出了这台机器原来的账号，余额已经还回来了" \
        if info.get("recovered") else "领到一个新账号"
    return _ok(acc, bool(info.get("recovered")), note)


# ================= 验收 / 收尾 =================

def verify_installed(state) -> bool:
    """四样东西都在才算装好。装完之后要不要自动打开 claude 就看它。"""
    try:
        if not (state.get("node") or {}).get("version"):
            return False
        claude_path = (state.get("claude") or {}).get("claude") or ""
        if not claude_path or not Path(claude_path).exists():
            return False
        if not identity.load_identity():
            return False
        return not agent_tools.config_problems()
    except Exception:
        return False


def child_env(environ=None) -> dict:
    """给 claude 子进程用的环境变量（纯函数）。

    PyInstaller 单文件运行时会把 `_MEIxxxx` 临时目录塞进 LD_LIBRARY_PATH；
    把它交给 claude，claude 自己的动态链接可能先撞上我们那份库。所以：
      · 去掉 `_MEI*` / `_MEI` / `_PYI_*` 这类键；
      · LD_LIBRARY_PATH 里带 `_MEI` 的条目整条摘掉；
      · `~/.local/bin` 顶到 PATH 最前面（刚装的 claude 就在那儿，而当前这个
        shell 还没读过 .bashrc）。
    """
    src = dict(os.environ if environ is None else environ)
    for key in list(src):
        if key == "_MEIPASS" or key.startswith("_MEI") or key.startswith("_PYI_"):
            src.pop(key, None)

    ld = src.get("LD_LIBRARY_PATH")
    if ld:
        keep = [p for p in ld.split(":") if p and "_MEI" not in p]
        if keep:
            src["LD_LIBRARY_PATH"] = ":".join(keep)
        else:
            src.pop("LD_LIBRARY_PATH", None)

    bin_dir = str(Path.home() / ".local" / "bin")
    parts = [p for p in (src.get("PATH") or "").split(os.pathsep) if p and p != bin_dir]
    src["PATH"] = os.pathsep.join([bin_dir] + parts)
    return src


def open_claude(claude_path="", workdir=None, env=None):
    """在 workdir 里把 claude 起起来。返回 (成不成, 给人看的一句话)。

    ★ 用 `subprocess.call` 而不是 `os.execvpe`：exec 之后 PyInstaller 的 `_MEI`
      临时目录（这次多了模型栈，几十兆）会永远留在 /tmp；而且
      `packaging/install.sh` 拿到的会是 claude 的退出码，不是安装器的。"""
    exe = str(claude_path or "") or shutil.which("claude") or ""
    if not exe:
        return False, "找不到 claude 命令"
    cwd = Path(workdir).expanduser() if workdir else Path.home()
    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        subprocess.call([exe], cwd=str(cwd), env=child_env(env))
        return True, ""
    except FileNotFoundError:
        return False, f"claude 不在 {exe}"
    except PermissionError:
        return False, f"{exe} 没有执行权限"
    except OSError as e:
        return False, f"起不来：{e}"


# ================= 会话 =================

def _facts(args, account, arch) -> dict:
    """交给提示词的环境事实。**只有不含密钥的东西。**"""
    workdir = Path(args.dir).expanduser() if getattr(args, "dir", "") else Path.home()
    return {
        "platform": sys.platform,
        "arch": arch or "",
        "home": str(Path.home()),
        "workdir": str(workdir),
        "account_code": account.get("account_code") or "",
        "account_note": account.get("note") or "",
        "is_root": hasattr(os, "geteuid") and os.geteuid() == 0,
        "recovered": account.get("recovered"),
    }


def session_worker(ctx, args, ui, state, preflight=None):
    """工作线程的本体。**不碰终端**，一切输出走 `ui`。"""
    try:
        _session(ctx, args, ui, state, preflight)
    except BaseException as e:                      # noqa: BLE001 —— 线程里不能抛
        try:
            ui.ui_log(f"\n[系统] 出了点意外：{e}")
        finally:
            ui.finish(Outcome(error=f"意外的错误：{e}", exit_code=EXIT_FAILED))


def _session(ctx, args, ui, state, preflight):
    # ① 环境自检（确定性）
    if preflight is not None:
        try:
            info = preflight(ctx, args) or {}
        except CliError as e:
            ui.ui_log(f"\n[系统] {e.message}")
            if e.hint:
                ui.ui_log("  " + str(e.hint))
            ui.finish(Outcome(error=e.message, exit_code=e.exit_code))
            return
        state["arch"] = info.get("arch") or ""

    # ② 账号（确定性，模型做不到这一步）
    account = bootstrap_identity(ctx, args, ui)
    state["account"] = account
    if not account["ok"]:
        ui.ui_log(f"\n[系统] {account['message']}")
        if account["hint"]:
            ui.ui_log("  " + account["hint"].replace("\n", "\n  "))
        ui.finish(Outcome(error=account["message"],
                          exit_code=account["exit_code"]))
        return
    state["account_code"] = account["account_code"]

    # ③ 模型上场
    from agent_core import _sanitize_messages, run_one_task

    tool_map, tools = agent_tools.build_tools(ctx, args, state, ui)
    messages = [{"role": "system",
                 "content": P.build_system_prompt(
                     _facts(args, account, state.get("arch") or ""))}]

    task_text = P.INITIAL_TASK
    while True:
        status = {}
        ui.ui_busy(True)
        _clear_cancel()                 # 新的一轮开工：把上一轮的中断牌子摘掉
        try:
            run_one_task(messages, task_text,
                         tools=tools, tool_map=tool_map, emit=ui.emit,
                         # 服务器不靠 install_done（它要 register_device 才触发），
                         # 用 finish_install 写进 state 的 finished
                         marks_complete=lambda *_: False,
                         status=status)
        finally:
            ui.ui_busy(False)

        # 聊天空了之后别让历史无限长
        if len(messages) > 22:
            messages = _sanitize_messages([messages[0]] + messages[-20:])

        if state.get("finished"):
            ui.finish(Outcome())
            return

        reason = status.get("error") or ""
        if reason:
            message, hint, code = _STATUS_MESSAGES.get(
                reason, (f"这一轮没走完（{reason}）", "重跑一次试试", EXIT_FAILED))
            if reason in ("balance", "request_too_big"):
                # run_one_task 已经把具体那句（"余额已用完" / "预估花费超过了剩余余额"）
                # 打出来了，这里只补怎么解决
                ui.ui_log(f"  {hint}")
            else:
                ui.ui_log(f"\n[系统] {message}")
                if hint:
                    ui.ui_log(f"  {hint}")
            detail = status.get("detail") or ""
            if detail and reason in ("no_identity", "bad_response"):
                ui.ui_log(f"  （{str(detail)[:200]}）")
            ui.finish(Outcome(error=message, exit_code=code))
            return

        # 模型觉得这一轮说完了，但还没通过验收 —— 让用户接着说
        if not _can_keep_talking(args):
            ui.ui_log("\n[系统] 模型停下来了，但还没通过验收（finish_install 没成功）。")
            ui.finish(Outcome(error="安装没有完成", exit_code=EXIT_FAILED))
            return

        ui.ui_log("\n[系统] 还没装完。接着说你想怎么办（比如「继续」「换个镜像再试一次」）。")
        user_text = user_input_queue.get()
        if user_text is None:                     # 主线程被 Ctrl-C / Ctrl-D 叫停了
            ui.finish(Outcome(aborted=True))
            return
        task_text = P.CONTINUE_TASK + str(user_text)


def _can_keep_talking(args) -> bool:
    """`--yes` 和管道里没人能回答，别等一个永远不会来的输入。"""
    try:
        from server.term_chat import is_interactive
        return is_interactive() and not getattr(args, "yes", False)
    except Exception:
        return False


def run_agent_install(ctx, args, ui, state, preflight=None):
    """起工作线程。返回线程对象（daemon）。"""
    import threading
    thread = threading.Thread(target=session_worker,
                              args=(ctx, args, ui, state, preflight),
                              name="agent-install", daemon=True)
    thread.start()
    return thread


__all__ = ["bootstrap_identity", "session_worker", "run_agent_install",
           "verify_installed", "open_claude", "child_env"]
