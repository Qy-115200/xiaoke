# -*- coding: utf-8 -*-
"""大模型客户端。

Agent 永远走服务器代理（不直连 DeepSeek）：base_url 指向 aikaui.com/api，
api_key 用**这台设备注册后拿到的专属 api_key**（identity.json 里的那个）。

★ 2026-10 新契约：/api/chat/completions 只认 `Authorization: Bearer <用户 api_key>`，
  不再接受 AGENT_KEY / SHARED_API_KEY / X-Device-Id。拿 AGENT_KEY 直接聊天会 401，
  所以没有本地凭证时**根本不能建 client**——必须先把账号注册好（见 agent_worker）。

★ get_client / reset_client 里的 `global` 声明绝对不能删：丢了不会报错，
  但 client 每次都会重建、reset_client 变成空操作，是个很难发现的回归。
"""

from openai import OpenAI

from config import BUILTIN_BASE_URL, BUILTIN_MODEL
from server.identity import load_identity

_current_client = None          # 全局缓存：OpenAI 客户端实例（避免每次重建）
_current_model = None           # 全局缓存：当前用的模型名


class NoIdentity(RuntimeError):
    """本地还没有账号凭证（没注册成功 / 文件被删）。界面应当提示先完成注册。"""


def get_client():
    """Agent 永远走服务器代理，用本机账号的专属 api_key。"""
    global _current_client, _current_model
    if _current_client is not None:          # 已经有缓存的 client
        return _current_client, _current_model

    ident = load_identity()
    if not ident:
        raise NoIdentity("还没有账号凭证，请先完成一次账号注册（检查网络后重试）")

    _current_client = OpenAI(
        api_key=ident["api_key"],
        base_url=BUILTIN_BASE_URL,
        # ★ 不再带 X-Device-Id：服务器只按 Bearer 里的 api_key 认人
    )
    _current_model = ident.get("fast_model") or BUILTIN_MODEL
    return _current_client, _current_model


def reset_client():
    """丢掉缓存的 client，让下次 get_client() 重新读本地凭证重建"""
    global _current_client
    _current_client = None


# ================= 额度不够的判定 =================
# 线上实际链路（2026-10-17 由对端核实，源码 src/routes/chat.js）：
#
#   上游 New API 回 403 + code=insufficient_user_quota（正文"预扣费额度失败, …"）
#     ↓
#   api 代理看到 402/403 且正文匹配 /quota|额度|insufficient/ → **改写成 402**，
#   非额度相关的 403（封号、鉴权、模型未开放）原样透传。
#
# ★ 但"额度不够"有两种，含义完全不同，给用户的话也必须不同：
#
#   ① 余额真的用完了            → 该充值
#   ② 这次请求的预估花费超过余额 → 余额还在（可能还有 ¥0.75），是 new-api 按
#                                 「上下文 + max_tokens」**预扣费估算**出来的价格
#                                 超过了它。要么精简请求，要么充值。
#
#   2026-10-28 踩过一次：997KB 的请求 + max_tokens=32000，估算超过剩下的 ¥0.749，
#   用户看到"余额已用完"就去充钱 —— 其实下一条请求就正常通过了（只扣 622 quota，
#   ≈¥0.009）。所以代理现在用 `code` 区分两种，并把真实剩余额度放在 `remaining`
#   （单位：元）：
#       "insufficient_user_quota"     → ①
#       "quota_exceeded_for_request"  → ②
#
# ★ 老代理（v1.4.8 及以前）只发一句中文、没有 code/remaining，那就当 ① —— 和以前
#   的行为一致，不会因为服务端还没升级就崩掉。反过来，新代理先上线也安全。
#
# ★ 正文里可能带**账户码明文**：这里只用来做判断，绝不把原文写进日志/文件/上报。

#: 余额真的用完了
EMPTY_CODES = {
    "insufficient_user_quota",          # New API：用户额度不足 / 预扣费失败
    "insufficient_quota",               # OpenAI 风格：额度用完
    "pre_consume_token_quota_failed",   # New API 的预扣费估算失败 —— 余额为 0 和
                                        # "估算超余额"都会报这个，分不出来，按老口径当①
    "quota_exceeded",
}
#: 余额还在，是这次请求的预估花费超过了它
TOO_BIG_CODES = {
    "quota_exceeded_for_request",       # ★ 2026-10-29 起代理区分出来的新码
}
INSUFFICIENT_TEXTS = (
    "余额已用完",          # ★ 老代理把 403 改写成 402 时用的就是这句（实测口径）
    "预扣费额度失败",
    "用户额度不足",
    "额度不足",
    "余额不足",
    "quota is not enough",
    "insufficient user quota",
    "insufficient_quota",
)


def _error_bits(e):
    """从 SDK 抛的异常里抠出 (错误码, 错误正文, extra)。抠不到就给空串 / 空 dict。

    openai SDK 的 APIStatusError.body 是服务器返回的原始 JSON（解析过的 dict），
    形如 {"error": {"message": …, "type": "new_api_error",
                    "code": "insufficient_user_quota",
                    "remaining": 0.749, "estimated": 1.21}}。
    有的网关把 error 直接写成字符串，也一并兼容。"""
    code, msg, extra = "", "", {}
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            msg = str(err.get("message") or "")
            extra = err
        elif isinstance(err, str):
            msg = err
        if not code:
            code = str(body.get("code") or "")
        if not msg:
            msg = str(body.get("message") or "")
        if not extra:                       # 有的是平铺在顶层的
            extra = body
    elif isinstance(body, str):
        msg = body
    return code.strip().lower(), (msg or str(e)), (extra or {})


def _money(value):
    """`remaining` / `estimated` → 一个能说的金额。不是数字就 None。

    单位是**元**（服务端约定，不是 quota）。0.749 显示成 ¥0.75。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _yuan(value):
    return f"¥{value:.2f}" if value < 10 else f"¥{value:.0f}"


def insufficient_balance(e):
    """这个异常是不是"额度不够"？不是就返回 None。

    是的话返回一个 dict（**不要**把它当字符串用）：
        {"kind": "empty" | "request_too_big",
         "text": "给用户看的一句话",
         "remaining": 0.749 或 None}

    调用方按 kind 决定说什么、要不要劝充值 —— 把 ② 说成"余额已用完"会让人白充钱。"""
    status = getattr(e, "status_code", None)
    code, text, extra = _error_bits(e)

    matched = (code in EMPTY_CODES or code in TOO_BIG_CODES
               or status == 402                  # ★ 线上实际走这条：代理把额度错误归一成 402
               or (status in (403, 429)
                   and any(t in text for t in INSUFFICIENT_TEXTS)))
    if not matched:
        return None

    remaining = _money(extra.get("remaining"))
    # 新码直接说"请求太大"；否则只要拿到的剩余额度**大于 0**，那也一定是这件事
    # （余额真的为 0 时 remaining 会是 0）。剩下分不出来的，按老口径当①。
    too_big = (code in TOO_BIG_CODES
               or (remaining is not None and remaining > 0))

    if too_big:
        if remaining is not None:
            msg = f"这次请求的预估花费超过了剩余余额（还剩 {_yuan(remaining)}）"
        else:
            msg = "这次请求的预估花费超过了剩余余额"
        return {"kind": "request_too_big", "text": msg, "remaining": remaining}
    return {"kind": "empty", "text": "账户余额已用完", "remaining": remaining}
