# -*- coding: utf-8 -*-
"""PoW（工作量证明）求解。

注册链路是：`GET /api/challenge` 拿一个 32 位 hex 的挑战 → 本地找一个 nonce，
使 `SHA256(challenge + nonce)` 的**前 difficulty 个二进制位**全为 0 →
`POST /api/auto-register` 把 (challenge, nonce) 交回去。

★ 和服务器 `src/lib/pow.js` 必须逐位对齐，尤其是"按二进制位"这一点：
  difficulty=16 等价于 4 个 hex 0，但 22 这种不是 4 的倍数，按 hex 字符判断
  会把难度算错 2~4 倍（客户端算出来的 nonce 服务器不认）。

★ 这个循环会跑几秒（difficulty 16 平均 6.5 万次哈希，22 平均 400 万次），
  所以它必须：① 能报进度 ② 能取消 ③ 自己会超时。调用方永远不该被它卡死。

★ 取消是**进程级**的一个 Event（`request_cancel()`），因为取消信号来自界面线程，
  而求解跑在 Agent 线程。同一时刻只会有一次注册，所以一个全局 Event 够用。
"""

import hashlib
import threading
import time

# 单次求解最多跑多久。服务器给的挑战 120 秒过期，留出取挑战 + 提交的余量。
POW_TIMEOUT = 90

# 每算这么多次看一次时钟 / 取消标志。数太小会拖慢求解，太大反应迟钝。
_CHECK_EVERY = 4096

# 每隔这么久报一次进度（秒）。按时间节流，不按次数——CPU 快慢差很多。
_PROGRESS_EVERY = 0.15

_cancel = threading.Event()


def reset_cancel() -> None:
    """开始一次新的求解前清掉上一次的取消标记。"""
    _cancel.clear()


def request_cancel() -> None:
    """界面线程调：让正在跑的求解尽快停下来。"""
    _cancel.set()


def is_cancelled() -> bool:
    return _cancel.is_set()


def has_leading_zero_bits(digest: bytes, bits: int) -> bool:
    """digest 的前 bits 个**二进制位**是否全为 0。

    逐字节比整字节，最后一个不满 8 位的用掩码看高位 —— 和服务器
    `pow.js::hasLeadingZeroBits` 同一套算法。"""
    if bits < 0 or bits > 64:
        return False
    full = bits // 8
    for i in range(full):
        if digest[i] != 0:
            return False
    rem = bits % 8
    if not rem:
        return True
    mask = (0xFF << (8 - rem)) & 0xFF
    return (digest[full] & mask) == 0


def verify(challenge: str, nonce, difficulty: int) -> bool:
    """校验一个 (challenge, nonce) 是否满足给定难度（测试和自检用）。"""
    if not isinstance(difficulty, int) or isinstance(difficulty, bool):
        return False
    if not isinstance(challenge, str) or len(challenge) != 32:
        return False
    if nonce is None:
        return False
    s = str(nonce)
    if not s or len(s) > 128:
        return False
    digest = hashlib.sha256((challenge + s).encode("utf-8")).digest()
    return has_leading_zero_bits(digest, difficulty)


def solve(challenge: str, difficulty: int, timeout: float = POW_TIMEOUT,
          on_progress=None):
    """找一个满足难度的 nonce。

    返回 (nonce, 错误说明)：成功时 nonce 是十进制字符串、错误说明是 None；
    失败（超时 / 被取消 / 参数不对）时 nonce 是 None、错误说明是一句给人看的话。

    `on_progress(tried, expected)`：每隔约 0.15 秒调一次，tried 是已试次数，
    expected 是期望次数（2^difficulty，用来画进度条）。**它跑在调用线程里**，
    所以别在里面做重活。"""
    if not isinstance(difficulty, int) or isinstance(difficulty, bool) or not 0 <= difficulty <= 64:
        return None, f"服务器给的难度不对（{difficulty!r}）"
    if not isinstance(challenge, str) or len(challenge) != 32:
        return None, "服务器给的挑战格式不对"

    expected = 1 << difficulty if difficulty <= 40 else None
    prefix = challenge.encode("utf-8")
    started = time.monotonic()
    last_report = started
    i = 0

    while True:
        nonce = str(i)
        digest = hashlib.sha256(prefix + nonce.encode("ascii")).digest()
        if has_leading_zero_bits(digest, difficulty):
            if on_progress:
                try:
                    on_progress(i + 1, expected)
                except Exception:
                    pass
            return nonce, None
        i += 1

        if i % _CHECK_EVERY:
            continue

        now = time.monotonic()
        if _cancel.is_set():
            return None, "已取消"
        if now - started > timeout:
            return None, f"计算超过 {int(timeout)} 秒还没算完（这台电脑有点慢，或网络出口被降速）"
        if on_progress and now - last_report >= _PROGRESS_EVERY:
            last_report = now
            try:
                on_progress(i, expected)
            except Exception:
                pass
