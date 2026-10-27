# -*- coding: utf-8 -*-
"""余额 / 用量 / 充值在终端里怎么说话。

分工和 `relay_gate.py` 那套一样：`server/api.py` 负责拿数据（返回 JSON 字符串），
这里只负责把数据变成人能看懂的几行字 —— 金额、用量、二维码、下一步敲什么。
不碰网络、不 import 界面库，所以离线可测，措辞也不会随界面改动漂来漂去。

★ 任何一行里都不出现 api_key / account_key：只有账户码、金额、订单号和支付链接。
  账户密钥在提示里写成 <账户密钥> 占位符 —— 用户得去桌面版里看，不能从这儿漏。
"""

from server.api import CHANNEL_NAMES

# 和桌面版"余额见底"那条线一致（README：跌破 ¥1 才提醒，不主动弹充值页）
LOW_BALANCE_YUAN = 1.0

# 单笔上限，和 server/api.py 的校验保持一致
MAX_RECHARGE = 10000

# 终端里画二维码用的半个方块。一个字符宽 : 高 ≈ 1 : 2，上下各占一个模块，
# 拼出来的二维码才是方的（不然扫不出来）。
_PAIR = {(1, 1): "█", (1, 0): "▀", (0, 1): "▄", (0, 0): " "}

# 深底/浅底终端都要能扫：强制黑字白底，否则深色主题下黑白是反的，有些扫码器不认。
_QR_ON = "\033[30;47m"
_QR_OFF = "\033[0m"


# ================= 小工具 =================

def _cols(text: str) -> int:
    """按终端列算宽度：中文 / 全角符号占两格。只用来对齐，不参与判断。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(label: str, want: int = 10) -> str:
    return label + " " * max(1, want - _cols(label))


def _tokens(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def yuan_of(value):
    """把 "5.00" / 5 / "0" 变成 float；看不懂就 None —— 宁可少打一行，也不编数字。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_low(yuan) -> bool:
    return yuan is not None and yuan < LOW_BALANCE_YUAN


# ================= 余额 / 用量 =================

def _usage_line(label: str, block) -> str:
    if not isinstance(block, dict) or not block:
        return ""
    parts = []
    if block.get("tokens") is not None:
        parts.append(f"{_tokens(block['tokens'])} tokens")
    yuan = yuan_of(block.get("yuan"))
    if yuan is not None:
        parts.append(f"¥{yuan:.2f}")
    if not parts:
        return ""
    return f"  {_pad(label)}{'  '.join(parts)}"


def balance_lines(balance: dict, usage: dict = None) -> list:
    """余额 + 用量。入参就是 `api.query_balance()` / `query_usage()` 解出来的 dict。

    缺字段就少打一行：服务器没给 quota、用量接口挂了，都不该让用户看到 None。"""
    balance = balance or {}
    out = []
    code = balance.get("account_code")
    if code:
        out.append(f"  {_pad('账户码')}{code}")
    yuan = yuan_of(balance.get("yuan"))
    if yuan is not None:
        out.append(f"  {_pad('余额')}¥{yuan:.2f}")
    if balance.get("quota") is not None:
        out.append(f"  {_pad('剩余额度')}{_tokens(balance['quota'])} tokens")
    note = balance.get("note")
    if note:
        out.append(f"  {_pad('注')}{note}")

    usage = usage or {}
    for label, key in (("今日用量", "today"), ("本月用量", "month")):
        line = _usage_line(label, usage.get(key))
        if line:
            out.append(line)
    return out


def recharge_hint_lines(account_code: str = "") -> list:
    """余额见底时给的两条路：就地充值，或者绑到桌面版扫码。

    ★ 绑桌面版这条不是"退而求其次"：有些用户本来就在别的机器上用桌面版，
      绑上去用图形界面充更顺手，余额是记在账号上的，这台服务器立刻能用。"""
    out = [
        "",
        "  ⚠ 余额不够了，终端里的 claude 会开始报「额度不足」。",
        "    两条路，任选一条：",
        "",
        "    ① 直接在这儿充（会打印二维码，用手机扫）：",
        "         claude-code-assistant --recharge 20",
        "",
        "    ② 绑到桌面版，用图形界面扫码：",
        "         在 Windows / macOS 上打开 https://aikaui.com 的桌面版，选「已有账号」，填：",
    ]
    out.append(f"           账户码    {account_code or '<先运行 --balance 看账户码>'}")
    out += [
        "           账户密钥  本机 ~/.claude-agent/identity.json 里的 account_key",
        "                     （只填进桌面版里，不要发给别人）",
        "         进去后打开「用量信息」→「充值」扫码。余额记在账号上，这边立刻能用。",
    ]
    return out


# ================= 二维码 =================

def qr_lines(payload: str, *, color: bool = True) -> list:
    """把二维码画成终端里的几行方块。画不了就返回 []（调用方退回到打印链接）。

    · 用 segno（纯 Python，没装也不影响其它功能）生成矩阵
    · micro=False：Micro QR 很多扫码器不认，宁可用大一点的普通二维码
    · color=True 时强制黑字白底 —— 深色终端里不这么做，黑白是反的"""
    payload = str(payload or "").strip()
    if not payload:
        return []
    try:
        import segno
    except Exception:
        return []
    try:
        qr = segno.make(payload, error="m", micro=False)
        rows = [[1 if cell else 0 for cell in row] for row in qr.matrix]
    except Exception:
        return []

    border = 2                      # 静区：太小了扫不出来
    size = len(rows) + border * 2
    blank = [0] * size
    grid = [blank[:] for _ in range(border)]
    grid += [[0] * border + row + [0] * border for row in rows]
    grid += [blank[:] for _ in range(border)]

    out = []
    for y in range(0, size, 2):
        top = grid[y]
        bottom = grid[y + 1] if y + 1 < size else blank
        line = "".join(_PAIR[(t, b)] for t, b in zip(top, bottom))
        out.append(f"{_QR_ON}{line}{_QR_OFF}" if color else line)
    return out


# ================= 充值订单 =================

def recharge_lines(order: dict, *, color: bool = True) -> list:
    """把 `api.create_recharge()` 的结果变成终端输出。

    二维码优先用服务器给的 `qrcode`（就是扫码器要解的那串），没有就拿
    `pay_url` 顶上 —— 让用户用手机扫一个能打开支付页的链接，也比什么都没有强。"""
    order = order or {}
    out = ["", "✅ 订单已创建"]
    if order.get("order_no"):
        out.append(f"  {_pad('订单号')}{order['order_no']}")

    money = yuan_of(order.get("money"))
    if money is None:
        money = yuan_of(order.get("amount"))
    channel = CHANNEL_NAMES.get(str(order.get("channel") or ""), "")
    amount = f"¥{money:.2f}" if money is not None else ""
    out.append(f"  {_pad('金额')}{amount}{f'（{channel}）' if channel else ''}")

    note = order.get("note")
    if note:
        out.append(f"  {_pad('说明')}{note}")

    qr_payload = order.get("qrcode") or order.get("pay_url")
    qr = qr_lines(qr_payload, color=color) if qr_payload else []
    if qr:
        out += ["", "  用手机扫下面的二维码付款（支付宝 / 微信的扫一扫）：", ""]
        out += ["  " + line for line in qr]
    out.append("")

    pay_url = order.get("pay_url")
    if pay_url:
        out.append("  也可以在你自己的电脑上打开这个链接付款：")
        out.append(f"    {pay_url}")
    img = order.get("img")
    if img:
        out.append("  二维码图片（存下来用手机扫）：")
        out.append(f"    {img}")
    if not qr and not pay_url and not img:
        out.append("  ⚠ 服务器没给二维码也没给付款链接 —— 过一会儿重跑一次 --recharge。")

    out += [
        "",
        "  付完款余额会自动到账（通常几秒）。确认一下：",
        "    claude-code-assistant --balance",
    ]
    return out


# ================= 装完之后列命令 =================

def command_lines(binary: str = "claude-code-assistant") -> list:
    """装完之后给用户的"这台机器上还能干什么"。"""
    return [
        "  常用命令",
        f"    {binary} --balance                  查余额和用量",
        f"    {binary} --recharge 20              充 20 元（打印二维码）",
        f"    {binary} --recharge 50 --channel wechat    指定微信支付",
        f"    {binary} --repair                   配置被改坏了，重新对账",
        f"    {binary} --account-code <码> --account-key <密钥>    把别的机器绑到这个账号",
        f"    {binary} --help                     全部参数",
    ]
