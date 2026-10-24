# -*- coding: utf-8 -*-
"""PoW 求解的离线测试（不联网）。

下面三组 (challenge, difficulty, nonce) 是用本模块算出来的，再拿**线上服务器
那份 pow.js**（token-service/src/lib/pow.js，`verifyPow`）逐条核对过为 true：
两边必须都认，否则客户端算出来的 nonce 会被服务器判成 pow_failed。
其中 difficulty=9 / 22 故意选非 8 的倍数，专门盯"按二进制位而不是按 hex 字符"。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import pow as powmod  # noqa: E402

VECTORS = [
    ("0123456789abcdef0123456789abcdef", 16, "58454"),
    ("fedcba9876543210fedcba9876543210", 22, "10774341"),
    ("00000000000000000000000000000000", 9, "411"),
]


class VerifyTests(unittest.TestCase):
    def test_server_agreed_vectors(self):
        for challenge, difficulty, nonce in VECTORS:
            with self.subTest(difficulty=difficulty):
                self.assertTrue(powmod.verify(challenge, nonce, difficulty))

    def test_neighbour_nonce_fails(self):
        self.assertFalse(powmod.verify(VECTORS[0][0], "58453", 16))

    def test_leading_zero_bits_is_bitwise(self):
        # 9 位：第一字节必须为 0，第二字节最高位必须为 0
        self.assertTrue(powmod.has_leading_zero_bits(bytes([0, 0x00]), 9))
        self.assertTrue(powmod.has_leading_zero_bits(bytes([0, 0x7F]), 9))
        self.assertFalse(powmod.has_leading_zero_bits(bytes([0, 0x80]), 9))
        self.assertFalse(powmod.has_leading_zero_bits(bytes([1, 0x00]), 9))
        # 0 位 = 谁都满足；非 8 的倍数不能退化成"看 hex 字符"
        self.assertTrue(powmod.has_leading_zero_bits(b"\xff" * 32, 0))
        self.assertTrue(powmod.has_leading_zero_bits(bytes([0x0F]), 4))    # 0000 1111
        self.assertFalse(powmod.has_leading_zero_bits(bytes([0x1F]), 4))   # 0001 1111
        self.assertTrue(powmod.has_leading_zero_bits(bytes([0x0F]), 3))
        self.assertFalse(powmod.has_leading_zero_bits(bytes([0x20]), 3))   # 0010 0000

    def test_bad_input(self):
        self.assertFalse(powmod.verify("too-short", "1", 8))
        self.assertFalse(powmod.verify(VECTORS[0][0], "", 8))
        self.assertFalse(powmod.verify(VECTORS[0][0], "1", 65))
        self.assertFalse(powmod.verify(VECTORS[0][0], "1", True))
        self.assertFalse(powmod.verify(VECTORS[0][0], None, 8))


class SolveTests(unittest.TestCase):
    def setUp(self):
        powmod.reset_cancel()

    def tearDown(self):
        powmod.reset_cancel()

    def test_solve_low_difficulty(self):
        nonce, err = powmod.solve("11111111111111111111111111111111", 8, timeout=20)
        self.assertIsNone(err)
        self.assertIsInstance(nonce, str)
        self.assertTrue(powmod.verify("11111111111111111111111111111111", nonce, 8))

    def test_progress_is_reported(self):
        seen = []
        nonce, err = powmod.solve(
            "22222222222222222222222222222222", 16, timeout=60,
            on_progress=lambda tried, expected: seen.append((tried, expected)))
        self.assertIsNone(err)
        self.assertTrue(powmod.verify("22222222222222222222222222222222", nonce, 16))
        # 至少会在结束时报一次，expected 是 2^difficulty
        self.assertTrue(seen)
        self.assertEqual(seen[-1][1], 1 << 16)

    def test_cancel(self):
        powmod.request_cancel()
        nonce, err = powmod.solve("33333333333333333333333333333333", 40, timeout=60)
        self.assertIsNone(nonce)
        self.assertEqual(err, "已取消")

    def test_timeout(self):
        nonce, err = powmod.solve("44444444444444444444444444444444", 40, timeout=0)
        self.assertIsNone(nonce)
        self.assertIn("超过", err)

    def test_bad_difficulty(self):
        self.assertIsNotNone(powmod.solve("55555555555555555555555555555555", "16")[1])
        self.assertIsNotNone(powmod.solve("短了", 16)[1])


if __name__ == "__main__":
    unittest.main()
