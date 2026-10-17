# -*- coding: utf-8 -*-
"""设备身份与本地凭证。

这一层管的是"这台电脑是谁"：

  _device_id()      本机指纹（主机名 + MAC + 架构 的 sha256 前 16 位）
  register_device() 拿指纹去服务器 /api/issue-token 换 token，并缓存到 identity.json
  load_identity()   读本地缓存（没文件 / 损坏 / 缺 api_key 都当没领过）
  _ensure_*()       给上层用：本地缺 user_id / account_code 时自动补领一次

★ 为什么 _ensure_account_code / _ensure_user_id 必须留在这个文件里：
  api.py 里 query_balance / create_recharge 要用它们，而它们又会回调
  register_device()。如果挪到 api.py，就成了 api ←→ identity 双向依赖（循环导入）。
  放在这儿，依赖方向永远是 api → identity，单向无环。

★ device_id 是服务器记账的 key（余额、用量都挂它上面），一旦注册过就必须
  复用文件里存的，不能每次现算 —— 否则服务器会当成新设备再发一份 token。
"""

import hashlib
import json
import platform
import uuid

from config import AGENT_KEY, IDENTITY_FILE, SERVER_URL
from server.ui_bridge import get_ui

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

    ★ 为什么要复用而不是每次现算：
    device_id 既是本地指纹，也是服务器记账的 key（余额、用量都挂在这上面）。
    如果每次重新生成，服务器会当成新设备再发一份 token，用户余额也看不到。
    所以一旦注册过，就固定用文件里那个。"""
    ident = load_identity()
    if ident and ident.get("device_id"):
        return ident["device_id"]
    # 没注册过 / 旧版本文件里没这个字段 → 现场算一个（register_device 会把它存下来）
    return _device_id()
def _ensure_account_code(refresh: bool = False):
    """拿到本机的 account_code（形如 CC-XXXXXX-XXXXXX）。
    查余额 / 查用量都用它当参数（服务器认的是账户码，不是 user_id）。
    旧版本 identity.json 里可能没这个字段，这时自动重新注册一次补上。
    返回 (account_code, 错误说明)。

    ★ refresh=True：**不管本地有没有，都重新问一次服务器**。
      本地文件里存的那串可能是过期的（服务器改过账户码的形态，老机器上的文件
      还留着旧码），这时查余额会直接被服务器 404 掉。正常轮询走 refresh=False
      （不发请求）；只有服务器明确说"这个码我不认"时才用 refresh=True 自愈，
      见 api.py 的 _account_get()。"""
    ident = load_identity()
    if ident and ident.get("account_code") and not refresh:
        return ident["account_code"], None

    # 缺 account_code → 重新领一份（接口幂等，同设备返回同一个账号）
    try:
        register_device()
    except Exception as e:
        return None, f"重新注册失败：{e}"

    ident = load_identity() or {}
    if not ident.get("account_code"):
        return None, "本地凭证里没有 account_code，请先完成一次设备注册"
    return ident["account_code"], None


def _ensure_user_id():
    """拿到本机的 user_id（充值创单等接口用）。
    缺了同样自动重新注册一次补上。返回 (user_id, 错误说明)。"""
    ident = load_identity()
    if ident and ident.get("user_id"):
        return ident["user_id"], None

    try:
        register_device()
    except Exception as e:
        return None, f"重新注册失败：{e}"

    ident = load_identity() or {}
    if not ident.get("user_id"):
        return None, "本地凭证里没有 user_id，请先完成一次设备注册"
    return ident["user_id"], None


def _account_code() -> str:
    """本地凭证里的账户码（充值、找回账号时给用户看的那串）"""
    ident = load_identity() or {}
    return ident.get("account_code") or ""
def _fetch_identity(device_id: str):
    """向服务器领一次身份信息。
    返回 (data, err)：成功时 data 是 dict、err 是 None；失败时 data 是 None、err 是错误说明。"""
    try:
        import requests                  # 延迟导入（不用 requests 的场景没必要加载）
        resp = requests.post(            # 发 POST 请求
            f"{SERVER_URL}/api/issue-token",   # 你的服务器接口
            headers={
                "X-Agent-Key": AGENT_KEY,      # 身份凭证（服务器校验）
                "Content-Type": "application/json",
            },
            json={"device_id": device_id},     # body 里带设备指纹
            timeout=15,                        # 15 秒超时，避免一直卡着
        )
    except Exception as e:
        return None, f"请求失败：{e}"         # 网络问题等

    if resp.status_code == 401:               # 身份无效
        return None, "领取失败：Agent 身份无效（401）"
    if resp.status_code != 200:               # 其他错误
        return None, f"领取失败 HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        data = resp.json()                    # 解析 JSON 响应
    except Exception:
        return None, f"服务器返回的不是 JSON：{resp.text[:200]}"

    if not data.get("api_key"):               # 响应里没 token，异常
        return None, f"服务器返回异常：{json.dumps(data, ensure_ascii=False)}"

    return data, None
def _save_identity(data: dict) -> None:
    """把服务器返回的身份信息写到本地文件"""
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)   # 确保目录存在
    IDENTITY_FILE.write_text(                                  # 写文件
        json.dumps(data, ensure_ascii=False, indent=2),        # 格式化 JSON
        encoding="utf-8"
    )


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
      model / fast_model 这两个字段（issue-token 有）。直接覆盖会把本地配好的
      模型配置擦掉，用户下次对话就换模型了。服务器没给的字段，一律留着本地的。

    ★ device_id 也必须是本机的那个：服务器响应里没有这个字段，而后续所有
      请求都要带它。"""
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


def bind_account(account_code):
    """用账户码把本机绑到一个已有账号（换设备登录 / 找回账号）。

    ★ account_code **原样发出去，一个字都不清洗**：服务器会归一化大小写、横线、
      空格、全角（中文输入法打的），认 O↔0 / I↔1 的混淆，甚至能把码从一整句话里
      挑出来。客户端在这儿先做一道正则校验或者"帮你纠正一下"，就等于把用户
      明明打对了的码拦在门外——这是这个接口最容易踩的坑。

    返回 (数据, 错误说明, 错误码, 建议等待秒数)；成功时数据是 dict，
    错误码形如 account_code not found / too_many_attempts。"""
    raw = "" if account_code is None else str(account_code)   # 不 strip、不 upper、不补横线
    try:
        import requests
        resp = requests.post(
            f"{SERVER_URL}/api/bind-account",
            headers={
                "X-Agent-Key": AGENT_KEY,
                "Content-Type": "application/json",
            },
            json={"account_code": raw, "device_id": get_device_id()},
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
        return None, f"登录失败 HTTP {resp.status_code}：{resp.text[:200]}", code, wait

    try:
        data = resp.json()
    except Exception:
        return None, f"服务器返回的不是 JSON：{resp.text[:200]}", "", None
    if not data.get("api_key"):
        return None, "服务器返回异常：没给 api_key", "", None

    return data, None, "", None


def ensure_identity() -> bool:
    """确保本地有可用凭证，没有就领一份。返回 True 表示有凭证可用。

    ★ 为什么需要在启动时主动做一次：
    AGENT_KEY 只是"敲门砖"——服务器只认它去换 token（/api/issue-token），
    不认它直接调模型接口（会返回 401 Invalid token）。
    所以全新机器上如果用户先闲聊再安装，Agent 手里只有 AGENT_KEY，答不了话。
    启动时先换好 token，用户一上来问什么都正常。"""
    if load_identity():
        return True                  # 已有凭证（正常情况，不发请求）
    register_device()                # 首次运行：领一份
    return load_identity() is not None
def register_device() -> str:
    """向服务器领取 API token。
    这是"发钥匙"步骤：Agent 拿到 token 后，用户才能用 Claude Code。

    ★ 已领取过的设备也会再向服务器确认一次并刷新本地缓存。
    原因：服务器可能改过 api_base（比如从裸 IP 换成域名），
    如果只看本地文件就"跳过"，老用户会一直用着过期的地址，
    装完 Claude Code 打开就报错。接口是幂等的，同设备返回同一个 token。
    """
    device_id = get_device_id()          # ★ 用已保存的 device_id（没有才现场算）

    local = load_identity()              # 本地缓存（可能没有，返回 None）
    first_time = local is None           # 是否首次领取

    # ---------- 向服务器领 / 刷新 ----------
    data, err = _fetch_identity(device_id)
    if first_time:
        if not data:
            return err                   # 首次领取失败：必须报错，没法继续
        note = "领取成功"
        data = {**data, "device_id": device_id}    # ★ 把 device_id 一起存下来
        _save_identity(data)
    elif data:
        # 已有缓存 + 刷新成功：用服务器最新值覆盖本地（api_base 变过也能自愈）
        # 只比较真正影响运行的字段（issued_at 每次都会变，不算"配置变了"）
        changed = any(
            data.get(k) != local.get(k)
            for k in ("api_base", "api_key", "model", "fast_model")
        )
        note = "设备已领取，配置已刷新" if changed else "设备已领取，跳过"
        # ★ 合并：服务器下发的字段 + 本地保留的 device_id
        # （服务器响应里没有 device_id，但后续 /api/chat/completions 必须带）
        data = {**data, "device_id": device_id}
        _save_identity(data)
    else:
        # ★ 已有缓存、只是这次刷新失败（断网等）：不阻塞流程，继续用本地缓存
        data = local
        note = f"设备已领取，跳过（刷新失败，继续用本地缓存：{err}）"

    # 通知 GUI 丢掉旧 client，下次 get_client() 用新 token 重建
    # hasattr 检查 _ui 有没有这个方法，避免报错
    u = get_ui()
    if u and hasattr(u, "on_token_acquired"):
        try:
            u.on_token_acquired()
        except Exception:
            pass                         # 切换失败也不影响主流程

    # 返回给 Agent 看的信息（不包含完整 token，避免泄露到对话历史）
    return json.dumps({
        "note": note,
        "api_base": data.get("api_base"),
        "model": data.get("model"),
        "fast_model": data.get("fast_model"),
        "device_id": device_id,
        "user_id": data.get("user_id"),            # 后续查余额 / 充值要用
        "account_code": data.get("account_code"),  # 账户码，充值 & 换机找回用
        "balance": data.get("balance"),            # 余额（注册时服务器下发）
    }, ensure_ascii=False)
