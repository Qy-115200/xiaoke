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


# ================= 余额不足的判定 =================
# 线上实际链路（2026-10-17 由对端核实，源码 src/routes/chat.js）：
#
#   上游 New API 回 403 + code=insufficient_user_quota（正文"预扣费额度失败, …"）
#     ↓
#   api 代理看到 402/403 且正文匹配 /quota|额度|insufficient/ → **改写成 402**，
#   body 只剩一句中文 message（"余额已用完。…账户码：…"），没有 error.code。
#   非额度相关的 403（封号、鉴权、模型未开放）原样透传。
#
# 所以客户端实际拿到的是 **402 + 一句中文正文**，而不是 403 + 机器码。
# 403/机器码那两条判定留着当保险：代理哪天改回透传 403 也还能认出来。
#
# ★ 但状态码不能一概而论：封号、权限不足、模型没开放也可能是 403。
#   所以先认机器码（insufficient_user_quota 这类），再退回到正文里的特征串。
#
# ★ 代理的 402 正文里带**账户码明文**：这里只用来做判断，绝不把原文写进日志/
#   文件/上报，对外只说"账户余额不足"。

INSUFFICIENT_CODES = {
    "insufficient_user_quota",          # New API：用户额度不足 / 预扣费失败
    "insufficient_quota",               # OpenAI 风格：额度用完
    "pre_consume_token_quota_failed",   # New API 的另一条额度错误
    "quota_exceeded",
}
INSUFFICIENT_TEXTS = (
    "余额已用完",          # ★ 代理把 403 改写成 402 时用的就是这句（实测口径）
    "预扣费额度失败",
    "用户额度不足",
    "额度不足",
    "余额不足",
    "quota is not enough",
    "insufficient user quota",
    "insufficient_quota",
)


def _error_bits(e):
    """从 SDK 抛的异常里抠出 (错误码, 错误正文)。抠不到就给空串。

    openai SDK 的 APIStatusError.body 是服务器返回的原始 JSON（解析过的 dict），
    形如 {"error": {"message": …, "type": "new_api_error", "code": "insufficient_user_quota"}}。
    有的网关把 error 直接写成字符串，也一并兼容。"""
    code, msg = "", ""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = str(err.get("code") or err.get("type") or "")
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = err
        if not code:
            code = str(body.get("code") or "")
        if not msg:
            msg = str(body.get("message") or "")
    elif isinstance(body, str):
        msg = body
    return code.strip().lower(), (msg or str(e))


def insufficient_balance(e):
    """这个异常是不是"账户余额不足"？是就返回一句给用户看的原因，不是返回 None。

    调用方看到非 None 就该提示充值，而不是把原始报错甩给用户。"""
    status = getattr(e, "status_code", None)
    code, text = _error_bits(e)

    if code in INSUFFICIENT_CODES:
        return "账户余额不足"
    if status == 402:                       # ★ 线上实际走这条：代理把额度错误归一成 402
        return "账户余额不足"
    if status in (403, 429) and any(t in text for t in INSUFFICIENT_TEXTS):
        return "账户余额不足"                # 代理改回透传 403 时的保险
    return None
