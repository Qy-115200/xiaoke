# -*- coding: utf-8 -*-
"""402 的两种含义不能混成一句「余额已用完」。

2026-10-28 的真实事故：一条 997KB 的请求（含图片 + 26 个工具定义）配
max_tokens=32000，new-api 按「上下文 + max_tokens」**预扣费估算**出超过用户
剩余 ¥0.749 的价格，于是拒绝。用户看到"余额已用完"就去充了钱 —— 其实下一条
请求就正常通过了，只扣 622 quota（≈¥0.009）。

代理现在用 `code` 区分两种，并把真实剩余额度放进 `remaining`（单位：元）。
这些测试把那个契约钉住，同时保证**老代理**（只有一句中文）仍然认得出来。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm  # noqa: E402


class _Err(Exception):
    """模仿 openai SDK 的 APIStatusError：有 status_code 和 body。"""

    def __init__(self, status, body=None, message="boom"):
        super().__init__(message)
        self.status_code = status
        self.body = body


def _quota_error(code, message="额度不够", **extra):
    return _Err(402, {"error": {"type": "new_api_error", "code": code,
                                "message": message, **extra}})


class OldProxyTests(unittest.TestCase):
    """v1.4.8 及以前的代理只发一句中文，没有 code / remaining。"""

    def test_402_with_chinese_text_is_empty(self):
        e = _Err(402, {"error": {"message": "余额已用完。请充值。账户码：CC-AAAAAA-BBBBBB"}})
        got = llm.insufficient_balance(e)
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "empty")
        self.assertIsNone(got["remaining"])

    def test_402_with_no_body_still_matches(self):
        # 代理哪天只回状态码 + 一句纯文本，也不能识别不出来
        self.assertEqual(llm.insufficient_balance(_Err(402, None))["kind"], "empty")

    def test_403_with_prepay_text_is_empty(self):
        # 代理改回透传 403 时的保险；分不出是哪一种，按老口径
        e = _Err(403, {"error": {"message": "预扣费额度失败, 剩余额度不足"}})
        self.assertEqual(llm.insufficient_balance(e)["kind"], "empty")

    def test_429_with_quota_text_is_empty(self):
        e = _Err(429, {"error": {"message": "quota is not enough"}})
        self.assertEqual(llm.insufficient_balance(e)["kind"], "empty")


class NewProxyTests(unittest.TestCase):
    """新代理（2026-10-29 起的契约）。"""

    def test_insufficient_user_quota_is_empty(self):
        got = llm.insufficient_balance(
            _quota_error("insufficient_user_quota", remaining=0, estimated=0.5))
        self.assertEqual(got["kind"], "empty")
        self.assertEqual(got["remaining"], 0.0)

    def test_quota_exceeded_for_request_is_too_big(self):
        got = llm.insufficient_balance(_quota_error(
            "quota_exceeded_for_request", remaining=0.749, estimated=1.21))
        self.assertEqual(got["kind"], "request_too_big")
        self.assertAlmostEqual(got["remaining"], 0.749)
        self.assertIn("¥0.75", got["text"])
        # 这句话**不能**再说"余额已用完"—— 余额还在
        self.assertNotIn("用完", got["text"])

    def test_remaining_above_zero_is_enough_to_tell(self):
        # 就算代理没给新 code，只要有 remaining 且 > 0，就说明余额还在
        e = _Err(402, {"error": {"message": "额度不够", "remaining": 2.5}})
        got = llm.insufficient_balance(e)
        self.assertEqual(got["kind"], "request_too_big")
        self.assertIn("¥2.50", got["text"])

    def test_too_big_without_remaining_still_says_the_right_thing(self):
        got = llm.insufficient_balance(_quota_error("quota_exceeded_for_request"))
        self.assertEqual(got["kind"], "request_too_big")
        self.assertIn("预估花费", got["text"])

    def test_flat_body_shape(self):
        # 有的网关把字段平铺在顶层，不套 error
        e = _Err(402, {"code": "quota_exceeded_for_request", "remaining": 0.3})
        self.assertEqual(llm.insufficient_balance(e)["kind"], "request_too_big")


class NotBalanceTests(unittest.TestCase):
    def test_server_error_is_not_balance(self):
        self.assertIsNone(llm.insufficient_balance(_Err(500, {"error": "boom"})))

    def test_403_without_quota_text_is_not_balance(self):
        # 封号、鉴权、模型未开放也是 403，别当成余额
        e = _Err(403, {"error": {"message": "account banned"}})
        self.assertIsNone(llm.insufficient_balance(e))

    def test_plain_runtime_error_is_not_balance(self):
        self.assertIsNone(llm.insufficient_balance(RuntimeError("connection refused")))


class PrivacyTests(unittest.TestCase):
    def test_account_code_never_leaks_into_the_message(self):
        """402 正文里带着账户码明文，我们只拿它做判断，一个字都不往外带。"""
        e = _Err(402, {"error": {
            "code": "insufficient_user_quota",
            "message": "余额已用完。账户码：CC-QQQQQQ-ZZZZZZ"}})
        got = llm.insufficient_balance(e)
        self.assertNotIn("CC-QQQQQQ-ZZZZZZ", got["text"])
        self.assertNotIn("QQQQQQ", str(got))


class MoneyTests(unittest.TestCase):
    def test_non_numbers_are_ignored(self):
        # bool 是 int 的子类，别把 True 当成 1 元
        for bad in (True, "0.75", None, [], {}):
            with self.subTest(value=bad):
                self.assertIsNone(llm._money(bad))

    def test_keeps_floats(self):
        self.assertAlmostEqual(llm._money(0.749), 0.749)
        self.assertEqual(llm._money(0), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
