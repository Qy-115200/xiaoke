# -*- coding: utf-8 -*-
"""余额 / 用量 / 充值的 HTTP 层。

统一走 https://aikaui.com，统一带 X-Agent-Key 鉴权。

设计原则：**永远不抛异常**。失败就返回 (None, 错误说明) 或 {"error": "..."}，
这样余额查不到、支付平台挂了，都不会把整个程序搞崩——错误原文会被翻译成
大白话带到界面上给用户看。

query_balance / query_usage / query_pay_methods / create_recharge 的返回值是
**JSON 字符串**（因为它们同时是给模型看的"工具"）；refresh_balance 返回 **dict**
（给界面直接用的）。
"""

import json

from config import AGENT_KEY, SERVER_URL
from server.claude_config import sync_from_identity_file
from server.identity import (
    _ensure_account_code,
    _ensure_user_id,
    _save_identity,
    adopt_identity,
    bind_account,
    load_identity,
    remember_account_code,
)

# 服务器限流时回的错误码。**它不是"出错了"**：意思是"你问得太勤，缓一缓再来"，
# 界面上不该按"服务挂了"处理，更不该弹错误给用户（见 _account_get / refresh_balance）。
RATE_LIMITED = "too_many_attempts"

# ================= 服务器接口调用（余额 / 用量 / 充值） =================
# 这几个接口统一走 https://aikaui.com，统一带 X-Agent-Key 鉴权。
# 设计原则：**永远不抛异常**，失败就返回 (None, 错误说明)，
# 这样余额查不到也不会把整个程序搞崩。

def _api_get(path: str, params: dict = None):
    """GET 请求。返回 (数据, 错误说明, 错误码, 建议等待秒数)，
    失败时数据是 None，其余三个里至少错误说明不是空的。"""
    try:
        import requests                  # 延迟导入
        resp = requests.get(
            f"{SERVER_URL}{path}",       # 比如 https://aikaui.com/api/balance
            params=params,               # 查询参数（?user_id=7）
            headers={"X-Agent-Key": AGENT_KEY},
            timeout=15,
        )
    except Exception as e:
        return None, f"请求失败：{e}", "", None

    if resp.status_code != 200:
        return None, _api_error(resp), _api_code(resp), _api_wait(resp)
    try:
        return resp.json(), None, "", None
    except Exception:
        return None, f"返回的不是 JSON：{resp.text[:200]}", "", None


def _api_post_ex(path: str, body: dict):
    """POST 请求，额外把服务器的**错误码**和**建议等待秒数**带回来。
    返回 (数据, 错误说明, 错误码, 建议等待秒数)。

    为什么单开一条：有个错误码不是"出错了"而是"换一条路再试一次"
    （`channel_unavailable` = 这个支付渠道现在用不了）；
    还有个是"你问得太勤"（`too_many_attempts`，带 retry_after）。
    把它们压成一个给人看的字符串，就没法区分"失败了"和这两种情况了。"""
    try:
        import requests
        resp = requests.post(
            f"{SERVER_URL}{path}",
            json=body,
            headers={"X-Agent-Key": AGENT_KEY, "Content-Type": "application/json"},
            timeout=15,
        )
    except Exception as e:
        return None, f"请求失败：{e}", "", None

    if resp.status_code != 200:
        return None, _api_error(resp), _api_code(resp), _api_wait(resp)
    try:
        return resp.json(), None, "", None
    except Exception:
        return None, f"返回的不是 JSON：{resp.text[:200]}", "", None


def _api_wait(resp):
    """服务器说要等几秒再来（429 的 retry_after / Retry-After 头），没有就 None。"""
    try:
        body = resp.json()
    except Exception:
        body = None
    wait = body.get("retry_after") if isinstance(body, dict) else None
    if wait is None:
        wait = resp.headers.get("Retry-After")
    try:
        return int(wait) if wait is not None else None
    except (TypeError, ValueError):
        return None


def _api_code(resp) -> str:
    """把服务器响应里的机器可读错误码抠出来（没有就空字符串）。

    服务器给形如 {"error": "channel_unavailable", ...}。"""
    try:
        body = resp.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("error") or body.get("code") or "").strip()


# 服务器各种报错的原文 → 给用户看的大白话
# 服务器返回形如 {"error": "zpay create failed", "detail": "..."}
_ERROR_TEXT = {
    "zpay create failed": "支付平台那边开单失败，请稍后再试",
    "wechat create failed": "微信支付那边开单失败，请稍后再试",
    "type must be alipay or wxpay": "支付方式不对（只支持支付宝 / 微信）",
    "invalid_channel": "支付渠道不对（客户端传了个服务器不认的值）",
    "channel_unavailable": "这个支付渠道现在用不了，换另一个试试",
    "user not found": "服务器找不到这个账号，请重启程序重新领取账号",
    "account_code not found": "没找到这个账户码，检查一下有没有抄错",
    "too_many_attempts": "试得太频繁了，等一会儿再试",
    "amount must be positive": "充值金额必须大于 0",
    "amount too small": "充值金额太小了，建议至少充 20 元",
    "amount too large": "充值金额超出限制，请分批充值",
}


def _api_error(resp) -> str:
    """把服务器的错误响应翻译成人话。

    服务器报错长这样：
      {"error": "zpay create failed", "detail": "上游返回 500"}
      {"error": "type must be alipay or wxpay"}
    detail 是给开发看的，但比光秃秃一个 502 有用，所以优先拼进去。"""
    code = resp.status_code
    try:
        body = resp.json()
    except Exception:
        return f"HTTP {code}：{resp.text[:200]}"

    if not isinstance(body, dict):
        return f"HTTP {code}：{str(body)[:200]}"

    raw = str(body.get("error") or "").strip()
    detail = str(body.get("detail") or "").strip()
    # 服务器没给 error 也可能给 message（New API 网关的习惯）
    if not raw:
        raw = str(body.get("message") or "").strip()

    msg = _ERROR_TEXT.get(raw)
    if msg is None:
        msg = raw or f"HTTP {code}"
    if detail and detail not in msg:
        msg = f"{msg}（{detail}）"
    return msg if raw or detail else f"HTTP {code}：{json.dumps(body, ensure_ascii=False)[:200]}"
def _account_get(path: str):
    """查一个"按账户码查"的接口（/api/balance、/api/usage）。

    比 _api_get 多两件正事：
      ① 服务器回 200 时，把它回显的**规范形态**账户码写回本地
         （本地文件里那串可能早就过期了）；
      ② 服务器说"这个账户码我不认"（account_code not found）时，**自动重领一次**
         再查一遍——老机器上存的是旧形态的码，不重领会一直查不到。

    返回 (数据, 错误说明, 错误码, 建议等待秒数)，跟 _api_get 同一套。"""
    account_code, err = _ensure_account_code()
    if account_code is None:
        return None, err, "", None

    last = (None, err, "", None)
    for attempt in (1, 2):
        data, err, code, wait = _api_get(path, {"account_code": account_code})
        if data:
            if isinstance(data, dict):
                # 服务器没回显就补上我们发的那个，别让上层拿到 None
                data["account_code"] = data.get("account_code") or account_code
            remember_account_code(data.get("account_code"))
            return data, None, "", None
        last = (None, err, code, wait)
        # 只有"码不认"才值得重领；限流、断网这些重领也没用
        if code != "account_code not found" or attempt == 2:
            break
        fresh, _ = _ensure_account_code(refresh=True)
        if not fresh or fresh == account_code:
            break                      # 领回来的还是同一串：别再白试一次
        account_code = fresh
    return last


def query_balance() -> str:
    """查余额。返回 JSON 字符串，形如
    {"user_id": 8, "account_code": "CC-F43D-5152", "yuan": "5.00", "quota": 2500000}

    服务器查不到时，退回本地凭证里存的余额（注册时服务器下发的那个），
    这样余额接口还没上线 / 暂时挂了，界面上也能显示一个大致数字。"""
    data, err, code, wait = _account_get("/api/balance")

    if data:
        # 只挑需要的字段返回，缺的用本地补上
        return json.dumps({
            "user_id": data.get("user_id"),
            "account_code": data.get("account_code"),
            "yuan": data.get("yuan"),
            "quota": data.get("quota"),
        }, ensure_ascii=False)

    # ★ 限流（429）不是"查不到"：余额还是刚才那个数，界面照着旧的显示就行。
    #   打上 rate_limited 让上层别把它当错误弹出来，也别退回去显示注册时的快照
    #   （那个数字更旧）。retry_after 是服务器让我们缓多久。
    if code == RATE_LIMITED:
        return json.dumps({
            "error": err, "rate_limited": True, "retry_after": wait or 60,
        }, ensure_ascii=False)

    # ---------- 兜底：用注册时服务器给的余额 ----------
    ident = load_identity() or {}
    if ident.get("balance") is not None:
        return json.dumps({
            "user_id": ident.get("user_id"),
            "account_code": ident.get("account_code"),
            "yuan": ident.get("balance"),
            "note": f"查询失败，这是注册时的余额快照（{err}）",
        }, ensure_ascii=False)

    return json.dumps({"error": err}, ensure_ascii=False)


def query_usage() -> str:
    """查 token 消耗。标准返回 {"today": {...}, "month": {...}}

    ★ 服务器如果只给总量（形如 {"quota":..., "yuan":...}），
    这里会把它塞进 today，保证界面永远有东西可显示。

    ★ 用量信息页要按"每个模型用了多少 token"画柱状图，所以这里尽量把
    按模型的明细透传出去（键名 by_model / models 都认），细节见
    helpers._norm_by_model()。服务器还没给这个字段时，界面会自动退化成
    一根"全部模型"的柱子，不会白屏。"""
    data, err, code, wait = _account_get("/api/usage")

    if data:
        today = data.get("today") or {}
        month = data.get("month") or {}
        # 服务器只返回了扁平的总量字段 → 当成"今日"用
        if not today and not month:
            if data.get("tokens") is not None or data.get("yuan") is not None:
                today = {"tokens": data.get("tokens"), "yuan": data.get("yuan")}
        # 按模型的明细如果挂在顶层（而不是塞在 today/month 里），归到今日
        if isinstance(today, dict) and isinstance(data.get("by_model") or data.get("models"), dict):
            today.setdefault("by_model", data.get("by_model") or data.get("models"))
        return json.dumps({
            "account_code": data.get("account_code"),
            "today": today,
            "month": month,
        }, ensure_ascii=False)

    # 同上：限流不是出错，界面该保持原样（见 query_balance 里的说明）
    if code == RATE_LIMITED:
        return json.dumps({
            "error": err, "rate_limited": True, "retry_after": wait or 60,
        }, ensure_ascii=False)

    return json.dumps({"error": err}, ensure_ascii=False)
# ================= 支付渠道 =================
# 渠道的"唯一写法"只有两个：alipay / wechat。别的写法都往这两个上归。
# 归一是必须的：模型嘴里可能是 wxpay / wx / 微信，老代码里也还留着 pay_type。
# 服务器只认 alipay / wechat（大小写不敏感），中间这些别名在这一层消化掉。

CHANNEL_ALIASES = {
    "alipay": "alipay", "zfb": "alipay", "ali": "alipay",
    "zhifubao": "alipay", "alipay_qr": "alipay", "支付宝": "alipay",
    "wechat": "wechat", "wxpay": "wechat", "wx": "wechat",
    "weixin": "wechat", "wechatpay": "wechat", "微信": "wechat",
}
CHANNEL_NAMES = {"alipay": "支付宝", "wechat": "微信"}
DEFAULT_CHANNELS = ("alipay", "wechat")


def channel_of(value, default="alipay"):
    """把各种写法归一到 alipay / wechat。认不出来返回 None（调用方自己决定怎么办）。"""
    if value is None or str(value).strip() == "":
        return default
    return CHANNEL_ALIASES.get(str(value).strip().lower())


def query_pay_methods() -> str:
    """问服务器现在开了哪些支付渠道。返回 JSON 字符串，形如
    {"methods": [{"channel":"alipay", "name":"支付宝", "provider":"zpay",
                  "ready":true, "reason":""}, ...]}

    ★ 界面拿它画按钮，**不硬编码**。将来开关渠道只改服务器，客户端不用发版。
      ready=false 的按钮置灰，reason 显示在旁边。

    ★ 问不到就退回"两个都亮"：接口还没上线 / 服务器暂时挂了的时候，
      让用户还能照旧充值，比甩他一个空界面强。"""
    fallback = [{"channel": c, "name": CHANNEL_NAMES[c], "provider": "",
                 "ready": True, "reason": ""} for c in DEFAULT_CHANNELS]
    data, _, _, _ = _api_get("/api/pay-methods")
    raw = data.get("methods") if isinstance(data, dict) else None
    methods = []
    if isinstance(raw, list):
        for m in raw:
            if not isinstance(m, dict):
                continue
            ch = channel_of(m.get("channel") or m.get("type") or m.get("value"), default=None)
            if ch is None:
                continue          # 服务器将来加了别的渠道（比如银行卡），认不出来就当没有
            methods.append({
                "channel": ch,
                "name": str(m.get("name") or CHANNEL_NAMES.get(ch, ch)),
                "provider": str(m.get("provider") or ""),
                "ready": bool(m.get("ready", True)),
                "reason": str(m.get("reason") or ""),
            })
    return json.dumps({"methods": methods or fallback}, ensure_ascii=False)


def create_recharge(amount: int, channel: str = None, pay_type: str = None) -> str:
    """创建充值订单，拿到付款二维码。

    amount   —— 充值金额，单位元（20 / 50 / 100 这样）
    channel  —— 支付渠道：alipay（支付宝，默认）/ wechat（微信）。
                wxpay、wx、weixin、zfb 这些老写法也认，大小写不敏感。
    pay_type —— 老的参数名，等价于 channel。新代码别再用它。

    返回 JSON 字符串，成功时形如：
      {
        "order_no": "AI1791648189654DC6C4C",   # 订单号，存本地对账用
        "amount": 20,
        "channel": "wechat",                     # ★ 新字段
        "pay_type": "wxpay",                     # 老字段，保留（历史订单/界面还在读）
        "money": "20.00",                        # 服务器确认的金额（字符串）
        "qrcode": "weixin://wxpay/bizpayurl?pr=xxx",  # 二维码**内容**（字符串）
        "pay_url": "https://...",                # 浏览器能打开的（微信直连常常是 null）
        "img": "https://.../qrcode/xxx.jpg",     # ★ 现成的二维码图片，UI 优先显示这个
        "switched_from": "wechat",               # 只在自动换渠道时出现
        "note": "..."
      }
    失败时返回 {"error": "错误说明"}。

    ★ 设计原则：不发异常、不重试**业务**错误。服务器说余额不足 / 支付平台挂了，
      都老老实实把原因带回来给用户看，绝不编一个假链接。
      唯一的例外是 503 channel_unavailable —— 那不是"失败"，是"换条路还能成"，
      所以会自动改用另一个渠道再来一次，并在结果里说明换过。"""
    # ---------- 参数校验（模型可能传字符串或乱值）----------
    try:
        amount = int(float(amount))      # "20" / 20.0 都能接受
    except (TypeError, ValueError):
        return json.dumps({"error": f"金额不合法：{amount}"}, ensure_ascii=False)

    if amount <= 0:
        return json.dumps({"error": "充值金额必须大于 0"}, ensure_ascii=False)
    if amount > 10000:
        return json.dumps({"error": "单笔充值不要超过 10000 元，请分几次充"}, ensure_ascii=False)

    ch = channel_of(channel if channel else pay_type)
    if ch is None:
        return json.dumps({"error": "支付方式只支持支付宝或微信（channel=alipay / wechat）"},
                          ensure_ascii=False)

    # ---------- user_id：服务器认这个，不认 account_code ----------
    user_id, err = _ensure_user_id()
    if user_id is None:
        return json.dumps({"error": err}, ensure_ascii=False)

    body = {"user_id": user_id, "amount": amount, "channel": ch}
    data, err, code, _wait = _api_post_ex("/api/create-order", body)

    # 这个渠道现在用不了 → 换另一个再试一次。用户点的是"充值"，不是"必须用微信"。
    switched_from = None
    if not data and code == "channel_unavailable":
        other = "wechat" if ch == "alipay" else "alipay"
        data2, err2, _c2, _w2 = _api_post_ex("/api/create-order",
                                             {"user_id": user_id, "amount": amount, "channel": other})
        if data2:
            switched_from, ch, data, err = ch, other, data2, None
        else:
            err = err2 or err       # 换过也不行：报后一次的原因，那是当前真实的状况

    if not data:
        return json.dumps({"error": err}, ensure_ascii=False)

    # ---------- 读响应：新字段优先，老字段兜底 ----------
    # 服务器把支付相关的东西收进了 payment 这个对象（type / qrcode / url），
    # 但老字段（code_url / pay_url / qrcode / img）也一直保留着，两个都认。
    payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    ch_out = channel_of(data.get("channel"), default=ch) or ch

    order = {
        "order_no": data.get("order_no"),
        "amount": amount,
        "channel": ch_out,
        "pay_type": "wxpay" if ch_out == "wechat" else "alipay",   # 老字段，保留
        "money": data.get("money") or f"{amount:.2f}",
        # 二维码"内容"（字符串）。微信直连给的是 weixin://... 这种，要编码成图才能扫。
        "qrcode": payment.get("qrcode") or data.get("code_url") or data.get("qrcode"),
        # 浏览器能打开的支付页。微信直连常常是 null，那就没有这条备用路。
        "pay_url": payment.get("url") or data.get("pay_url") or data.get("url"),
        # ★ 现成的二维码图片地址，UI 优先显示这个（自己编码容易跟平台对不上）
        "img": data.get("img") or payment.get("img"),
    }
    if switched_from:
        order["switched_from"] = switched_from
        order["note"] = "%s现在用不了，已自动改用%s" % (
            CHANNEL_NAMES[switched_from], CHANNEL_NAMES[ch_out])
    _save_order(order)                   # 存起来，方便后续对账 / 重开程序还能看到
    return json.dumps(order, ensure_ascii=False)


def _save_order(order: dict) -> None:
    """把最近一笔充值订单记到本地凭证里（对账 / 展示用）"""
    ident = load_identity()
    if not ident:
        return
    try:
        ident["last_order"] = order
        _save_identity(ident)
    except Exception:
        pass                              # 记不上不影响充值本身


def last_order():
    """读最近一笔充值订单（没有就返回 None）"""
    ident = load_identity() or {}
    return ident.get("last_order")

# ================= 账户码登录（换设备 / 找回账号） =================

def login_with_account_code(account_code) -> str:
    """用账户码把本机登录到一个已有账号。返回 JSON 字符串。

    ★ 这是**换电脑**的唯一入口：同一份身份文件拷到新机器也能用，但用户手上
      如果只有那串账户码（显示在「用量信息」页），就得靠这里。

    ★ account_code **原样发给服务器，不做任何清洗**。服务器会归一化大小写、
      横线、空格、全角，认 O↔0 / I↔1，甚至能把码从一整句话里挑出来。
      在这里加正则校验或者"顺手帮用户改一下"，只会把打对了的人挡在外面。
      所以下面除了"压根没给"以外，一个字符都不动。

    成功时返回 {"ok": true, "account_code":..., "user_id":..., "balance":...,
    "note":..., "next":...}；失败返回 {"error": "..."}。"""
    raw = "" if account_code is None else str(account_code)
    if not raw.strip():
        return json.dumps({"error": "没收到账户码。把账户码发我，或者点「用量信息」页的「换设备登录」。"},
                          ensure_ascii=False)

    data, err, code, wait = bind_account(raw)      # ★ 原样透传

    if data:
        adopt_identity(data)                       # 合并进本地文件 + 丢掉旧 client
        # ★ 换账号必须同时改 Claude Code 自己的配置（~/.claude.json 和
        #   ~/.claude/settings.json）。只改 identity.json 的话，界面显示的是新账号，
        #   终端里的 Claude Code 还在烧旧账号的 token——用户看到"余额没变"。
        synced, sync_note = sync_from_identity_file()
        return json.dumps({
            "ok": True,
            "account_code": data.get("account_code"),
            "user_id": data.get("user_id"),
            "balance": data.get("balance"),
            "note": "登录成功，这台电脑已经切到这个账号了。",
            "commands_synced": synced,
            "commands_note": sync_note,
            "next": ("命令行配置也一起换好了，终端里重开一次 Claude Code 就用新账号。"
                     if synced else
                     f"命令行配置没改成功（{sync_note}），"
                     "要让终端也用这个账号，再点一次「一键安装 Claude Code」。"),
        }, ensure_ascii=False)

    if code == RATE_LIMITED:
        # ★ 限流和"码错了"必须说成两回事，否则用户会以为自己抄错了码，
        #   然后越试越多，越试越登不上。
        return json.dumps({
            "error": f"试得太多了，服务器让等 {wait or 60} 秒再试。"
                     f"**这不是账户码打错了**，别改码，也别连着试。",
            "rate_limited": True,
            "retry_after": wait or 60,
        }, ensure_ascii=False)

    if code == "account_code not found":
        return json.dumps({
            "error": "没找到这个账户码。检查一下有没有抄错、有没有少几位——"
                     "账户码就是「用量信息」页里显示的那串。",
        }, ensure_ascii=False)

    return json.dumps({"error": err}, ensure_ascii=False)


# ================= 余额刷新（UI 用） =================
# 和 query_balance() 的区别：那个是给模型看的工具（返回 JSON 字符串），
# 这个是给界面直接用的（返回 dict），付款后点"我已完成付款"就调它。

def refresh_balance():
    """查一次余额，返回 dict：{"user_id":..., "account_code":..., "yuan":"5.00", "quota":...}
    查不到返回 {"error": "..."}。永远不抛异常。

    ★ 服务端收到支付回调后是自动加额度的，客户端不需要轮询确认，
      用户点一下刷新就行——金额变了就说明到账了。"""
    try:
        return json.loads(query_balance())
    except Exception as e:
        return {"error": f"余额查询失败：{e}"}
