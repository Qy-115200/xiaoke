# -*- coding: utf-8 -*-
"""大模型客户端。

Agent 永远走服务器代理（不直连 DeepSeek）：base_url 指向 aikaui.com/api，
api_key 用的是"半公开"的 AGENT_KEY，真正的鉴权靠 X-Device-Id 头——
服务器靠它找出这台设备的独立 token 来记账。

★ get_client / reset_client 里的 `global` 声明绝对不能删：丢了不会报错，
  但 client 每次都会重建、reset_client 变成空操作，是个很难发现的回归。
"""

from openai import OpenAI

from config import BUILTIN_API_KEY, BUILTIN_BASE_URL, BUILTIN_MODEL
from server.identity import get_device_id

_current_client = None          # 全局缓存：OpenAI 客户端实例（避免每次重建）
_current_model = None           # 全局缓存：当前用的模型名


def get_client():
    """Agent 永远走服务器代理"""
    global _current_client, _current_model
    if _current_client is not None:          # 已经有缓存的 client
        return _current_client, _current_model

    # ★ 必须带 X-Device-Id：服务器靠它找出这台设备的独立 token 来记账
    # 不带的话服务器会回退到共享 key（不推荐，用量算不到用户头上）
    _current_client = OpenAI(
        api_key=BUILTIN_API_KEY,
        base_url=BUILTIN_BASE_URL,
        default_headers={"X-Device-Id": get_device_id()},
    )
    _current_model = BUILTIN_MODEL
    return _current_client, _current_model


def reset_client():
    """丢掉缓存的 client，让下次 get_client() 重新读 device_id 重建"""
    global _current_client
    _current_client = None


# ================= 余额不足的判定 =================
# 模型通道是 New API，用户额度用完时它回的是 **403 insufficient_user_quota**
# （正文："预扣费额度失败, 用户剩余额度: …, 需要预扣费额度: …"），
# 老写法里的 402 反而不常见。只认 402 的话，钱花光了界面上只会打一句
# "调用模型失败"，用户既不知道是没钱了，也不会被提示充值——这就是那个一直没解决的
# "余额不足不提示"的问题。
#
# ★ 但 403 不能一律当成没钱：封号、权限不足、模型没开放也都是 403。
#   所以先认机器码（insufficient_user_quota 这类），再退回到正文里的特征串。

INSUFFICIENT_CODES = {
    "insufficient_user_quota",          # New API：用户额度不足 / 预扣费失败
    "insufficient_quota",               # OpenAI 风格：额度用完
    "pre_consume_token_quota_failed",   # New API 的另一条额度错误
    "quota_exceeded",
}
INSUFFICIENT_TEXTS = (
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
    if status == 402:                       # 老约定的"余额不足"
        return "账户余额不足"
    if status in (403, 429) and any(t in text for t in INSUFFICIENT_TEXTS):
        return "账户余额不足"
    return None
