# -*- coding: utf-8 -*-
"""设备身份与本地凭证。

这一层管的是"这台电脑是谁、这个账号是谁"：

  _device_id()       本机指纹（主机名 + MAC + 架构 的 sha256 前 16 位）
  register_device()  第一次运行时走 /api/challenge + /api/auto-register 开一个新账号，
                     把服务器给的专属 api_key 缓存到 identity.json
  load_identity()    读本地缓存（没文件 / 损坏 / 缺 api_key 都当没领过）
  bind_account()     拿「账户码 + 账户密钥」把本机绑到一个已有账号（换电脑 / 找回）
  _ensure_*()        给上层用：本地缺 user_id / account_code 时补领一次
  pending_recovery() identity.json 没了、但 server/vault.py 里还留着凭证时就返回它

★ device_id 和 server/fingerprint.py 那个硬件指纹都不是凭据（见那两处的说明）。
  能证明"这个账号是我的"的是账户码 + 账户密钥：码用来找到账号，密钥用来证明
  手里有注册时下发的那串随机数（服务端只存它的 sha256）。

★ 注册**不是幂等的**（2026-10 新契约）：/api/issue-token 已经恒返回 410，
  现在"注册"= 用 PoW 换一个新账号。所以 **只要有本地凭证就绝不再注册**，
  否则每次启动都会开一个新号、用户余额直接丢。老的"每次启动刷新一次配置"
  逻辑随旧接口一起删掉了。

★ 为什么 _ensure_account_code / _ensure_user_id 必须留在这个文件里：
  api.py 里 query_balance / create_recharge 要用它们，而它们又会回调
  register_device()。如果挪到 api.py，就成了 api ←→ identity 双向依赖（循环导入）。
  放在这儿，依赖方向永远是 api → identity，单向无环。

★ device_id 只是本机的本地标识（bind-account 用它做去重），**不再是身份凭证**。
  服务器不再凭它发 token，所以猜中别人的 device_id 也拿不到任何东西。
★ server/fingerprint.py 的硬件指纹同理：它连同一条注册请求发给服务器当反滥用
  线索，但**不能**单独拿来换账号（它不保密、同型号 CPU 还会撞号）。
★ 注册/绑定成功后额外把账户码存进 server/vault.py（注册表 / 配置目录）。
  identity.json 被删掉时靠它把账号找回来，而不是默默开一个新号。
"""

import hashlib
import json
import os
import platform
import uuid

from config import AGENT_KEY, IDENTITY_FILE, POW_MAX_ATTEMPTS, SERVER_URL
from server import fingerprint as fplib
from server import pow as powlib
from server import vault
from server.ui_bridge import get_ui

# 上一次自动注册失败的原因（ensure_identity 写，界面读）
_last_error = None

# ================= 辅助函数 =================

def _device_id() -> str:
    """生成设备指纹：同一台电脑每次算出来都一样，不同电脑不一样"""
    # platform.node()  = 主机名
    # uuid.getnode()   = MAC 地址（网卡物理地址）
    # platform.machine() = 架构（x86_64、AMD64 等）
    raw = f"{platform.node()}-{uuid.getnode()}-{platform.machine()}"
    # sha256 哈希，取前 16 个字符
    # 好处：固定长度，看不出原始信息（隐私）
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
def get_device_id() -> str:
    """拿当前设备的 device_id，优先用 identity.json 里已保存的那个。

    ★ 为什么要复用而不是每次现算：服务器用它把"同一台机器换账号"去重
      （bind-account 的日志和限流按它归并）。存过的就固定用。
    """
    ident = load_identity()
    if ident and ident.get("device_id"):
        return ident["device_id"]
    # 没注册过 / 旧版本文件里没这个字段 → 现场算一个（register_device 会把它存下来）
    return _device_id()
def _ensure_account_code(refresh: bool = False):
    """拿到本机的 account_code（形如 CC-XXXXXX-XXXXXX）。
    查余额 / 查用量都用它当参数（服务器认的是账户码，不是 user_id）。
    返回 (account_code, 错误说明)。

    ★ 这里**绝不注册**。新契约里注册 = 开一个新账号，所以：
      · 有 api_key 但没有码（老版本留下的文件）→ 再注册一次就把余额弄丢了，
        只能让用户用「换设备登录」重新输入码；
      · 连 api_key 都没有 → 该由启动流程（ensure_identity）去注册，
        余额轮询线程每 30 秒戳一下就注册一次，会变成注册风暴、还会被服务器限流。
      参数 refresh 留着只是为了不改调用方签名。"""
    ident = load_identity()
    if ident and ident.get("account_code"):
        return ident["account_code"], None
    if ident:
        return None, "本地凭证里没有账户码，请用「换设备登录」输入账户码和账户密钥"
    return None, "这台电脑还没有账号，请等注册完成（或检查网络）"


def _ensure_user_id():
    """拿到本机的 user_id（充值创单等接口用）。返回 (user_id, 错误说明)。

    ★ 同上：这里不注册。注册只发生在启动流程 / 用户明确点「一键安装」时。"""
    ident = load_identity()
    if ident and ident.get("user_id"):
        return ident["user_id"], None
    if ident:
        return None, "本地凭证里没有 user_id，请重新登录一次账号"
    return None, "这台电脑还没有账号，请等注册完成（或检查网络）"


def _account_code() -> str:
    """本地凭证里的账户码（充值、找回账号时给用户看的那串）"""
    ident = load_identity() or {}
    return ident.get("account_code") or ""


# ================= 注册（PoW） =================
# 服务器契约（见 token-service/src/routes/account.js）：
#   GET  /api/challenge     X-Agent-Key → {challenge, difficulty, expires_in}
#   POST /api/auto-register X-Agent-Key, body {challenge, nonce}
#        → {user_id, account_code, account_key, api_key, api_base, model,
#           fast_model, balance}
# 挑战绑定来源 IP、120 秒过期、只能用一次。
#
# ★ 人机校验在**后台线程**里跑（调用方本来就是 Agent 线程），失败最多重来
#   POW_MAX_ATTEMPTS 次；每一次都重新取挑战（旧的可能已经过期或被用过）。

# 这些错误码说明"这一轮不算数，换一个挑战再来"，其它错误直接放弃。
_RETRYABLE = {
    "challenge_invalid",     # 挑战不存在 / 过期
    "challenge_stale",       # 难度变了，要重新取
    "challenge_used",        # 被用掉了（重复提交）
    "challenge_ip_mismatch", # 出口 IP 变了（切了代理 / VPN）
    "pow_failed",            # nonce 不满足难度（几乎不会发生，发生就是算法不一致）
    "request_failed",        # 网络抖动
}

# 服务器错误码 → 给用户看的大白话。没列到的走原样透传。
_ERROR_TEXT = {
    "signup_closed": "服务器暂时关闭了新账号注册。已有账号请点「用量信息」页的「换设备登录」，输入账户码和账户密钥。",
    "too_many_registrations": "这个网络（或这一片网络）今天注册得太频繁了，服务器让过一会儿再试。",
    "invalid_ip": "服务器认不出这台电脑的网络出口，换个网络再试。",
    "forbidden": "这台设备被服务器拒绝了。换个网络再试，不行就联系客服。",
    "gone": "服务器接口已经升级，这个版本的客户端太旧了，请到 aikaui.com 下载新版。",
}


def _post_json(path: str, body: dict = None, method: str = "GET"):
    """调一个注册链路的接口。返回 (数据, 错误说明, 错误码, 建议等待秒数)。"""
    try:
        import requests
        fn = requests.post if method == "POST" else requests.get
        kwargs = {"headers": {"X-Agent-Key": AGENT_KEY}}
        if body is not None:
            kwargs["headers"]["Content-Type"] = "application/json"
            kwargs["json"] = body
        resp = fn(f"{SERVER_URL}{path}", timeout=15, **kwargs)
    except Exception as e:
        return None, f"请求失败：{e}", "request_failed", None

    code, wait = _resp_code(resp)
    if resp.status_code != 200:
        msg = _ERROR_TEXT.get(code)
        if msg is None:
            msg = f"注册失败 HTTP {resp.status_code}：{resp.text[:200]}"
        return None, msg, code, wait

    try:
        data = resp.json()
    except Exception:
        return None, f"服务器返回的不是 JSON：{resp.text[:200]}", "", None
    if not isinstance(data, dict):
        return None, "服务器返回的格式不对", "", None
    return data, None, "", None


def _run_pow(challenge: str, difficulty: int):
    """在本线程里跑 PoW，顺便把进度推给界面。返回 (nonce, 错误说明)。"""
    u = get_ui()
    powlib.reset_cancel()

    def progress(tried, expected):
        if not u:
            return
        fn = getattr(u, "ui_pow_progress", None)
        if fn is None:
            return
        pct = min(0.99, tried / expected) if expected else None
        fn(f"正在完成人机校验（已算 {tried:,} 次）…", pct)

    return powlib.solve(challenge, difficulty, on_progress=progress)


def _register_new_account():
    """走一次完整注册。成功返回 (身份 dict, None, "", None)，失败返回四个参数。"""
    last = (None, "注册失败", "", None)
    # 硬件指纹（取不到就是 ""）。它只是个反滥用线索，**不是凭据** —— 见
    # server/fingerprint.py 里的说明。为空时压根不发这个字段。
    fp = fplib.get_device_fingerprint()
    send_fp = bool(fp)
    for attempt in range(1, POW_MAX_ATTEMPTS + 1):
        data, err, code, wait = _post_json("/api/challenge")
        if not data:
            # 注册关闭 / 太频繁 / 认不出 IP：重试也一样，直接回去
            if code not in _RETRYABLE:
                return None, err, code, wait
            last = (None, err, code, wait)
            continue

        challenge = str(data.get("challenge") or "")
        difficulty = data.get("difficulty")
        if len(challenge) != 32 or not isinstance(difficulty, int) or isinstance(difficulty, bool):
            return None, "服务器给的挑战格式不对（可能是服务端版本不一致）", "bad_challenge", None
        nonce, pow_err = _run_pow(challenge, difficulty)
        if nonce is None:
            if powlib.is_cancelled():
                return None, "已取消注册", "cancelled", None
            last = (None, pow_err, "pow_timeout", None)
            continue

        body = {"challenge": challenge, "nonce": nonce}
        if send_fp:
            body["fingerprint"] = fp
        reg, err, code, wait = _post_json("/api/auto-register", body, method="POST")
        if reg:
            if not reg.get("api_key"):
                return None, "服务器返回异常：没给 api_key", "", None
            if fp:
                reg["fingerprint"] = fp          # 存进本地凭证，排查时能看到
            return reg, None, "", None

        last = (None, err, code, wait)

        # ★ 服务端还是老版本、不认识 fingerprint 这个字段时，可能整条请求被 400 掉。
        #   认不出的错误码先按"可能是字段不被接受"处理：去掉它再试一次。
        #   下一轮如果还失败，就原样把错误带回去（不再无限重试）。
        if send_fp and code not in _RETRYABLE and code not in _ERROR_TEXT:
            send_fp = False
            continue

        if code not in _RETRYABLE:
            return last
        # 可重试：换一个挑战再来（第 1 次失败最可能的原因是挑战过期 / 出口 IP 变了）

    if last[2] not in _RETRYABLE:
        return last
    return None, f"试了 {POW_MAX_ATTEMPTS} 次都没注册成功：{last[1]}", last[2], last[3]


# ================= 本地凭证 =================

def _save_identity(data: dict) -> None:
    """把服务器返回的身份信息写到本地文件（仅本用户可读）。"""
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)   # 确保目录存在
    tmp = IDENTITY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, IDENTITY_FILE)                            # 原子替换，别写出半个文件
    try:
        if os.name != "nt":
            os.chmod(IDENTITY_FILE, 0o600)                    # 里面有 api_key / account_key
    except OSError:
        pass

    # ★ 另存一份"命"到程序目录之外（注册表 / 配置目录）。identity.json 被删掉时，
    #   就靠这一份把账号找回来 —— 否则只能开新号，余额全丢。
    #   失败只是没存上，绝不影响注册本身。
    try:
        vault.save(data.get("account_code"), data.get("account_key"), data.get("user_id"))
    except Exception:
        pass


def load_identity():
    """读本地缓存的凭证。
    没有文件 / 文件损坏 / 缺 api_key 都返回 None（当作没领过）。"""
    if not IDENTITY_FILE.exists():
        return None
    try:
        data = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None                 # 文件损坏
    return data if data.get("api_key") else None


def remember_account_code(account_code) -> None:
    """把服务器回的**规范形态**的账户码写回本地。

    ★ 为什么不存用户输入的原样：服务器回的才是库里那张码的样子（大小写、位数
      都可能跟用户打的不一样，bind 的时候还能认全角、认夹在句子里的）。要是把
      用户输入的原样存下来，下一次查余额就会查不到——那是自己给自己挖的坑。
      服务器每次回 account_code 都该过一遍这里。"""
    code = str(account_code or "").strip()
    if not code:
        return
    ident = load_identity()
    if not ident or ident.get("account_code") == code:
        return                      # 已经是对的：不写文件（免得每次轮询都落盘）
    ident["account_code"] = code
    try:
        _save_identity(ident)
    except Exception:
        pass                        # 写不进去不影响这次查询


def _resp_code(resp):
    """从响应里抠出 (机器可读的错误码, 建议等待秒数)。

    服务器给形如 {"error": "too_many_attempts", "retry_after": 30}，
    429 还会带一个标准的 Retry-After 头（两者取其一，头是兜底）。"""
    code, wait = "", None
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        code = str(body.get("error") or body.get("code") or "").strip()
        wait = body.get("retry_after")
    if wait is None:
        wait = resp.headers.get("Retry-After")
    try:
        wait = int(wait) if wait is not None else None
    except (TypeError, ValueError):
        wait = None
    return code, wait


def adopt_identity(data: dict) -> dict:
    """把服务器回来的身份信息合并进本地文件并立刻生效。

    ★ 为什么是"合并"而不是"整份覆盖"：bind-account 的响应里**没有**
      model / fast_model 这两个字段。直接覆盖会把本地配好的模型配置擦掉，
      用户下次对话就换模型了。服务器没给的字段，一律留着本地的。

    ★ device_id 也必须是本机的那个：服务器响应里没有这个字段，而 bind / 日志
      都要用。"""
    merged = dict(load_identity() or {})
    merged.update({k: v for k, v in (data or {}).items() if v is not None})
    merged["device_id"] = get_device_id()
    _save_identity(merged)

    # 换了账号 ⇒ 旧 client 手里还是上一把 token，丢掉，下次重建
    u = get_ui()
    if u and hasattr(u, "on_token_acquired"):
        try:
            u.on_token_acquired()
        except Exception:
            pass
    return merged


def bind_account(account_code, account_key=""):
    """用「账户码 + 账户密钥」把本机绑到一个已有账号（换设备登录 / 找回账号）。

    ★ account_code **原样发出去，一个字都不清洗**：服务器会归一化大小写、横线、
      空格、全角（中文输入法打的），认 O↔0 / I↔1 的混淆，甚至能把码从一整句话里
      挑出来。客户端在这儿先做一道正则校验或者"帮你纠正一下"，就等于把用户
      明明打对了的码拦在门外——这是这个接口最容易踩的坑。

    ★ account_key 相反：**只 trim，不洗别的**。它是 64 位 hex，让用户从截图上
      手抄时大小写很容易错，所以服务端按大小写不敏感比对（那串 hex 大小写
      不携带任何信息）。客户端不在这里做任何"纠正"，原样发。

    ★ 2026-10-24 起服务端要求两样都给：账户码会露给旁人（截图、口述、页面上露
      前 4 位），单靠它就把整台机器的余额领走太薄。**2026-10-24 之前注册的老账号
      没有密钥**，服务端对这种账号维持"只要码"的旧行为。

    返回 (数据, 错误说明, 错误码, 建议等待秒数)；成功时数据是 dict，
    错误码形如 account_code not found / account_key_required /
    account_key_invalid / too_many_attempts。"""
    raw = "" if account_code is None else str(account_code)   # 不 strip、不 upper、不补横线
    key = "" if account_key is None else str(account_key).strip()
    try:
        import requests
        resp = requests.post(
            f"{SERVER_URL}/api/bind-account",
            headers={"Content-Type": "application/json"},
            json={"account_code": raw, "device_id": get_device_id(),
                  "account_key": key},
            timeout=15,
        )
    except Exception as e:
        return None, f"请求失败：{e}", "", None

    code, wait = _resp_code(resp)

    if resp.status_code != 200:
        if code == "too_many_attempts":
            return None, "试得太频繁了，服务器让等一会儿再试", code, wait
        if code == "account_code not found" or resp.status_code == 404:
            return None, "没找到这个账户码", "account_code not found", wait
        if code == "account_key_required":
            return None, "这个账号还需要账户密钥", code, wait
        if code == "account_key_invalid":
            return None, "账户密钥不对", code, wait
        return None, f"登录失败 HTTP {resp.status_code}：{resp.text[:200]}", code, wait

    try:
        data = resp.json()
    except Exception:
        return None, f"服务器返回的不是 JSON：{resp.text[:200]}", "", None
    if not data.get("api_key"):
        return None, "服务器返回异常：没给 api_key", "", None

    return data, None, "", None


def ensure_identity() -> bool:
    """确保本地有可用凭证，没有就注册一个。返回 True 表示有凭证可用。

    ★ 为什么需要在启动时主动做一次：
    AGENT_KEY 只是"敲门砖"——服务器只认它去取挑战 / 注册，不认它直接调模型接口。
    所以全新机器上如果用户先闲聊再安装，Agent 手里没有任何 api_key，答不了话。
    启动时先注册好，用户一上来问什么都正常。

    ★ 已有凭证时**一定不发请求**：注册就是开新账号，重复注册等于丢余额。

    失败原因放 `last_register_error()` 里——界面要原样告诉用户（注册关闭 /
    太频繁 / 网络不通，三种情况用户该做的事完全不同）。"""
    global _last_error
    if load_identity():
        _last_error = None
        return True
    result = register_device()
    if load_identity() is not None:
        _last_error = None
        return True
    _last_error = str(result)
    return False


def last_register_error() -> str:
    """上一次 ensure_identity() 失败的原因（成功了就是空串）。"""
    return _last_error or ""


def register_device() -> str:
    """拿到本机的账号凭证（首次运行时注册一个新账号）。

    成功返回一段 JSON（给 Agent 看，**不含任何 secret**）；
    失败返回一句给用户看的话（不以 `{` 开头，调用方靠这个区分）。

    ★ 已有凭证就不再注册：新契约里注册 = 开一个新账号，重来一次用户余额就没了。
      服务器配置（api_base / model）变了也不靠重新注册来刷新 —— 那些字段在
      identity.json 里存着，真要更新得靠新版客户端或换设备登录。
    """
    device_id = get_device_id()
    local = load_identity()

    if local:
        data, note = local, "设备已领取（用本地凭证，未重新注册）"
        if not data.get("account_code"):
            # 极端情况：文件里有 api_key 但没有账户码（老版本留下的）。没法补——
            # 服务器只能按码查账户，重新注册又会开新号。如实告诉上层。
            note = "本地凭证缺少账户码，请用「换设备登录」输入账户码和账户密钥"
        u = get_ui()                       # 通知界面刷新（幂等，重复调没关系）
        if u and hasattr(u, "on_token_acquired"):
            try:
                u.on_token_acquired()
            except Exception:
                pass
    else:
        # ---------- 首次注册：取挑战 → 算 PoW → 提交 ----------
        u = get_ui()
        if u and hasattr(u, "ui_progress_start"):
            try:
                u.ui_progress_start("正在向服务器申请账号…")
            except Exception:
                pass
        try:
            data, err, code, wait = _register_new_account()
        finally:
            powlib.reset_cancel()
            u = get_ui()
            if u and hasattr(u, "ui_progress_stop"):
                try:
                    u.ui_progress_stop()
                except Exception:
                    pass

        if not data:
            return err                      # 首次注册失败：必须报错，没法继续

        data = {**data, "device_id": device_id}    # ★ 把 device_id 一起存下来
        try:
            _save_identity(data)
        except Exception as e:
            return f"注册成功但凭证没存下来：{e}"
        note = "领取成功"

        # ★ 服务器按硬件指纹认出了旧账号，把原来的账号和余额还回来了
        #   （响应里 is_recovered: true）→ 让界面弹一次提示，别让用户以为是新号。
        #   走的还是 ui_bridge：这里在 Agent 线程上，真弹窗只能交给主线程。
        if data.get("is_recovered"):
            u = get_ui()
            if u and hasattr(u, "ui_recovered"):
                try:
                    u.ui_recovered()
                except Exception:
                    pass

        u = get_ui()
        if u and hasattr(u, "on_token_acquired"):
            try:
                u.on_token_acquired()
            except Exception:
                pass

    # 返回给 Agent 看的信息（**绝不包含 api_key / account_key**，避免泄露到对话历史）
    return json.dumps({
        "note": note,
        "api_base": data.get("api_base"),
        "model": data.get("model"),
        "fast_model": data.get("fast_model"),
        "device_id": device_id,
        "user_id": data.get("user_id"),            # 后续查余额 / 充值要用
        "account_code": data.get("account_code"),  # 账户码，充值 & 换机找回用
        "balance": data.get("balance"),            # 余额（注册时服务器下发）
        # 服务器按硬件指纹认出了旧账号、把原来的账号和余额还回来了
        "recovered": bool(data.get("is_recovered")),
    }, ensure_ascii=False)


def pending_recovery():
    """本地没有凭证、但"第二份"里还留着账户码 → 返回 {"account_code", "account_key"}。

    ★ 为什么不直接自动恢复：这是台共享机器（网吧、公司电脑）时，前一个人
      删掉了 identity.json，后一个人一开机就被"恢复"进前一个人的账号 —— 那是
      把别人的余额塞到陌生人手里。所以只把凭证交回给界面，由用户点一下确认。

    ★ 保险箱里存了 account_key 就一起带出来（换设备登录现在要两样）。老版本
      存进去的可能只有码，所以 `account_key` 可能为空 —— 调用方要能处理。

    没有可恢复的（本地已有凭证 / 保险箱里也没有）返回 None。
    调用方拿到非空值时**不要**再去注册新号。"""
    if load_identity():
        return None
    cred = vault.load() or {}
    code = cred.get("account_code")
    if not code:
        return None
    return {"account_code": code, "account_key": cred.get("account_key") or ""}
