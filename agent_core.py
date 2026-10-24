# -*- coding: utf-8 -*-
"""Agent 核心：一轮任务怎么跑，以及围绕它的两个常驻线程。

  run_one_task()   从用户输入开始，循环调模型 → 执行工具 → 回填结果，直到模型不再调工具
  agent_worker()   Agent 常驻线程，从 user_input_queue 取消息
  balance_worker() 余额轮询线程（每 30 秒）
  refresh_balance() 查一次余额 + 用量，丢进 msg_queue 给界面显示

★ 注意这里有个和 server/api.py 同名的 refresh_balance：
  那个查完**返回 dict**（给界面判断到没到账用），
  这个查完**丢进队列**（给顶栏显示用）。两个都要用，别搞混。
"""

import json
import time

from bus import msg_queue, user_input_queue
from config import MAX_STEPS
from llm import get_client, insufficient_balance, NoIdentity
from prompts import SYSTEM_PROMPT
from server.api import query_balance, query_usage
from server.identity import (
    ensure_identity,
    last_register_error,
    load_identity,
    pending_recovery,
)
from server.tool_specs import tool_map, tools

def _sanitize_messages(messages):
    """清理历史消息，确保每条 tool 消息前面都有 assistant 的 tool_calls
    （否则 DeepSeek 会报 400 错误）"""
    cleaned = []                    # 新的消息列表
    last_had_tc = False             # 上一条 assistant 消息是否带 tool_calls
    for m in messages:              # 遍历每条消息
        # 兼容 dict 和 OpenAI SDK 对象两种形式
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role == "assistant":
            if isinstance(m, dict):
                has_tc = bool(m.get("tool_calls"))
            else:
                has_tc = bool(getattr(m, "tool_calls", None))
            last_had_tc = has_tc     # 记录状态，给后面的 tool 消息用
            cleaned.append(m)
        elif role == "tool":
            if last_had_tc:          # 只有前面有 tool_calls 才保留
                cleaned.append(m)
            # 否则丢弃（孤立的 tool 消息）
        else:
            # system / user 消息，重置状态
            last_had_tc = False
            cleaned.append(m)
    return cleaned
def run_one_task(messages, task_text):
    """跑一轮任务：从用户输入开始，直到模型不再调工具为止

    ★ 这一轮如果**完整走完了安装流程**（register_device → … → launch_claude 成功），
      最后会给界面发一条 ("install_done", …)：让助手在用户看完收尾说明后自动退出。
      单独再调一次 launch_claude（用户只是说"再帮我打开一次"）不算安装流程，不退出。"""
    messages.append({"role": "user", "content": task_text})   # 把用户输入加入历史

    installing = False      # 这一轮调过 register_device（安装流程的起点）
    launched = False        # launch_claude 真的把 Claude Code 打开了

    for _step in range(MAX_STEPS):      # 最多循环 25 次
        messages = _sanitize_messages(messages)   # 每次调用前先清理（防被裁剪破坏结构）

        try:
            client, model = get_client()    # 取当前 client 和模型
        except NoIdentity as e:
            # 没有本地账号凭证就压根没法聊天（服务器只认 Bearer 里的用户 api_key）
            msg_queue.put(("log", f"\n[系统] {e}"))
            return False
        try:
            resp = client.chat.completions.create(   # ★ 调大模型
                model=model, messages=messages, tools=tools
            )
        except Exception as e:
            # 账户没钱了：上游 New API 回 403 insufficient_user_quota，代理层会先把它
            # 归一成 **402 + 一句中文**（非额度类 403 原样透传）。两种形状都认。
            # 这时候不能只打一句"调用模型失败"——用户根本看不出是余额的事，
            # 得把话说明白，并让主线程把底部那条"是否立即充值"冒出来。
            reason = insufficient_balance(e)
            if reason:
                msg_queue.put(("log", (
                    f"\n[系统] {reason}，模型暂时用不了了。\n"
                    f"       点下面「立即充值」（或左边「用量信息」页右上角），扫码付完款，"
                    f"把刚才那句话再发一次就行。"
                )))
                msg_queue.put(("recharge", {"reason": reason}))
            else:
                msg_queue.put(("log", f"\n[系统] 调用模型失败: {e}"))
            return False

        # ★ 服务器返回的不是标准对象（base_url 写错、被网关拦了等）时，
        # SDK 有时不报错，而是把响应原文当结果返回，后面访问 .choices 就会
        # 报 "'str' object has no attribute 'choices'"，很难懂。这里提前拦一下。
        if not hasattr(resp, "choices"):
            msg_queue.put(("log", (
                f"\n[系统] 服务器返回了非预期内容，请检查 base_url：\n"
                f"  {str(resp)[:200]}"
            )))
            return False

        msg = resp.choices[0].message   # 取第一条回复

        # 手动组装 assistant 消息（不用 SDK 对象，避免序列化格式问题）
        assistant_msg = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if msg.tool_calls:              # 如果模型要调工具
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_msg)

        if not msg.tool_calls:           # 模型没调工具 = 任务完成
            msg_queue.put(("log", f"\nAgent: {msg.content}"))
            # ★ 安装流程真的走完了 → 让界面过几秒自动退出（别一直占着托盘）。
            #   放在最后一条 Agent 消息之后发，用户才有时间看完收尾说明。
            if installing and launched:
                msg_queue.put(("install_done", {}))
            return True

        for tc in msg.tool_calls:        # 遍历每个工具调用
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}                # 解析失败就用空字典（容错）

            if name != "ask_user":       # ask_user 不打印"执行"，它自己会显示问题
                brief = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())
                msg_queue.put(("log", f"\n[执行] {name}({brief})"))

            try:
                result = tool_map[name](**args)   # ★ 调工具
            except KeyError:
                result = f"错误: 没有这个工具 {name}"
            except Exception as e:
                result = f"错误: {e}"             # 工具执行出错，把错误当结果返回

            # 记录安装流程走到哪了（只用于收尾时判断要不要自动退出）。
            # register_device 成功时回的是 JSON（带 note/api_base），失败回"领取失败…"。
            if name == "register_device" and str(result).startswith("{"):
                installing = True
            elif name == "launch_claude" and str(result).startswith("已打开新终端"):
                launched = True

            if name != "ask_user":
                # ★ 注册结果是 JSON，里面有账户码（新契约下它就是账号凭据）。
                #   日志窗口是用户会截图 / 复制的地方，所以只打一句结论。
                shown = str(result)
                if name == "register_device" and shown.startswith("{"):
                    try:
                        shown = json.loads(shown).get("note") or "领取成功"
                    except Exception:
                        shown = "领取成功"
                msg_queue.put(("log", f"[结果] {shown[:400]}"))

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,   # ★ 必须跟前面 assistant 的 tool_calls[].id 对应
                "content": str(result),
            })

    msg_queue.put(("log", "\n[系统] 达到最大步数，本轮结束"))
    return False
def agent_worker():
    """Agent 常驻线程：从启动就等着接收用户消息"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ★ 先确认"要不要恢复"，再决定"要不要注册"。
    #   本地凭证丢了、但 server/vault.py 里还留着上次的账户码时，**不能**直接注册 ——
    #   注册 = 开一个新号，原来那个账号和余额就永远回不来了。把码交给界面，
    #   由用户点一下确认（共享电脑上不能替人做主，见 pending_recovery 的说明）。
    try:
        recover_code = pending_recovery()
    except Exception:
        recover_code = None

    if recover_code:
        msg_queue.put(("log", (
            "[系统] 这台电脑的本地凭证不见了，但还留着上次的账户码。\n"
            "       正在问您要不要恢复 —— 点「登录」确认后，账号和余额会一起回来。"
        )))
        msg_queue.put(("recovery", {"account_code": recover_code}))
    else:
        # ★ 启动时先确保有正式凭证（首次运行会走 PoW 注册开一个账号）
        try:
            if not ensure_identity():
                msg_queue.put(("log", (
                    "[系统] 注册账号没成功：" + (last_register_error() or "未知原因") + "\n"
                    "       已有账号可以点「用量信息」页的「换设备登录」，输入账户码。"
                )))
        except Exception as e:
            msg_queue.put(("log", f"[系统] 注册账号出错：{e}"))

    refresh_balance()                # 立刻刷一次余额/用量

    while True:
        task_text = user_input_queue.get()   # ★ 阻塞等待消息
        if task_text is None:                # None 是"退出信号"
            break

        msg_queue.put(("busy", True))
        try:
            run_one_task(messages, task_text)
        except Exception as e:
            msg_queue.put(("log", f"\n[系统] 任务异常: {e}"))
        finally:
            if len(messages) > 22:       # 历史太长
                messages = [messages[0]] + messages[-20:]
                messages = _sanitize_messages(messages)
            refresh_balance()            # 每次任务后刷一下余额（花掉的钱要及时反映）
            msg_queue.put(("busy", False))
def refresh_balance():
    """查一次余额 + 用量，把结果丢给主线程显示。
    两个查询都不抛异常（失败会返回 {"error": ...}），所以这里只管转发。

    ★ 还没有账号凭证就直接跳过：不然每 30 秒的轮询会一直打一个必定失败的接口，
      界面上还会反复冒同一句错误。注册只由 ensure_identity / 用户点按钮触发。

    返回"服务器让我们等几秒"（限流时才有，没有就是 0），给 balance_worker 退避用。"""
    if not load_identity():
        return 0
    try:
        bal = json.loads(query_balance())
    except Exception as e:
        bal = {"error": str(e)}
    try:
        usage = json.loads(query_usage())
    except Exception as e:
        usage = {"error": str(e)}
    msg_queue.put(("balance", {"balance": bal, "usage": usage}))

    # ★ 被限流（429）的话，服务器会说 retry_after=N。把这个带回去让轮询线程
    #   真的歇 N 秒——硬着头皮每 30 秒接着问，只会一直撞在限流上。
    waits = [b.get("retry_after") for b in (bal, usage)
             if isinstance(b, dict) and b.get("rate_limited")]
    waits = [w for w in waits if isinstance(w, int) and w > 0]
    return max(waits) if waits else 0


def balance_worker():
    """后台线程：每 30 秒刷新一次余额和用量"""
    while True:
        backoff = 0
        try:
            backoff = refresh_balance()
        except Exception:
            pass                     # 网络抖动等，静默跳过，下一轮再试
        # 限流时按服务器说的退避（下限还是 30 秒，别退成 0 秒疯狂重试）
        time.sleep(max(30, int(backoff or 0)))
