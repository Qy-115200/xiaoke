# -*- coding: utf-8 -*-
"""billing_console：余额 / 充值在终端里怎么说。

这个模块决定"用户看到什么"，所以边界都钉住：缺字段就少打一行、不编数字；
二维码画不出来要退回链接（不能让人付不了钱）；任何一行都不能出现密钥。
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import billing_console as bc  # noqa: E402

ESC = re.compile(r"\033\[[0-9;]*m")


def plain(lines):
    """去掉颜色转义，方便断言宽度和内容。"""
    return [ESC.sub("", line) for line in lines]


class YuanTests(unittest.TestCase):
    def test_understands_the_shapes_the_server_sends(self):
        self.assertEqual(bc.yuan_of("5.00"), 5.0)
        self.assertEqual(bc.yuan_of(5), 5.0)
        self.assertEqual(bc.yuan_of("0"), 0.0)
        self.assertEqual(bc.yuan_of(0), 0.0)

    def test_unknown_is_none_not_zero(self):
        """看不懂就是看不懂 —— 编一个 0 会让用户以为钱没了。"""
        for bad in (None, "", "N/A", [], {}):
            self.assertIsNone(bc.yuan_of(bad), bad)

    def test_low_line_matches_the_desktop(self):
        self.assertTrue(bc.is_low(0))
        self.assertTrue(bc.is_low(0.5))
        self.assertFalse(bc.is_low(1.0))
        self.assertFalse(bc.is_low(None), "查不到不算余额不足")


class BalanceLinesTests(unittest.TestCase):
    def test_full_block(self):
        text = "\n".join(bc.balance_lines(
            {"account_code": "CC-F43D-5152", "yuan": "5.00", "quota": 2500000},
            {"today": {"tokens": 12000, "yuan": "0.12"},
             "month": {"tokens": 345000, "yuan": "3.45"}}))
        self.assertIn("CC-F43D-5152", text)
        self.assertIn("¥5.00", text)
        self.assertIn("2,500,000", text)
        self.assertIn("12,000 tokens", text)
        self.assertIn("345,000 tokens", text)

    def test_missing_fields_print_nothing(self):
        """服务器没给 quota、用量接口挂了 → 少打几行，不出现 None。"""
        text = "\n".join(bc.balance_lines({"yuan": "3"}, {}))
        self.assertIn("¥3.00", text)
        self.assertNotIn("None", text)
        self.assertNotIn("tokens", text)

    def test_empty_everything(self):
        self.assertEqual(bc.balance_lines({}, {}), [])

    def test_zero_balance_is_printed(self):
        """0 元要显示出来：这正是要提醒充值的那个数。"""
        self.assertIn("¥0.00", "\n".join(bc.balance_lines({"yuan": "0"})))


class HintTests(unittest.TestCase):
    def test_both_ways_out_are_listed(self):
        lines = bc.recharge_hint_lines("CC-ABCD-1234")
        text = "\n".join(lines)
        self.assertIn("--recharge 20", text)          # 就地充
        self.assertIn("桌面版", text)                  # 绑桌面版
        self.assertIn("CC-ABCD-1234", text)           # 账户码要能照着抄
        self.assertIn("aikaui.com", text)

    def test_without_account_code_it_does_not_invent_one(self):
        text = "\n".join(bc.recharge_hint_lines(""))
        self.assertIn("--balance", text)
        self.assertNotIn("None", text)

    def test_never_mentions_a_secret(self):
        text = "\n".join(bc.recharge_hint_lines("CC-ABCD-1234"))
        self.assertNotIn("api_key", text)
        self.assertIn("account_key", text)          # 告诉用户密钥在哪，但不打印值
        self.assertIn("不要发给别人", text)


class QrTests(unittest.TestCase):
    def test_draws_a_square_and_readable_block(self):
        lines = bc.qr_lines("https://example.invalid/pay", color=False)
        self.assertGreater(len(lines), 10)
        widths = {len(line) for line in lines}
        self.assertEqual(len(widths), 1, "每行宽度要一样，不然是歪的")
        self.assertGreater(widths.pop(), 10)
        self.assertTrue(set("".join(lines)) <= set("█▀▄ "))

    def test_same_payload_is_stable(self):
        self.assertEqual(bc.qr_lines("x", color=False), bc.qr_lines("x", color=False))

    def test_color_forces_black_on_white(self):
        """深色终端里不强制配色，黑白是反的 —— 有些扫码器不认反色。"""
        lines = bc.qr_lines("https://example.invalid/pay", color=True)
        self.assertTrue(all(line.startswith("\033[30;47m") for line in lines))
        self.assertTrue(all(line.endswith("\033[0m") for line in lines))

    def test_empty_payload_draws_nothing(self):
        self.assertEqual(bc.qr_lines(""), [])

    def test_without_segno_it_gives_up_instead_of_crashing(self):
        with mock.patch.dict(sys.modules, {"segno": None}):
            self.assertEqual(bc.qr_lines("https://example.invalid/pay"), [])


class RechargeLinesTests(unittest.TestCase):
    ORDER = {
        "order_no": "AI1791648189654DC6C4C",
        "amount": 20,
        "money": "20.00",
        "channel": "wechat",
        "qrcode": "weixin://wxpay/bizpayurl?pr=abc",
        "pay_url": None,
        "img": "https://aikaui.com/qrcode/abc.jpg",
    }

    def test_order_details_are_all_there(self):
        text = "\n".join(plain(bc.recharge_lines(self.ORDER, color=False)))
        self.assertIn("AI1791648189654DC6C4C", text)
        self.assertIn("¥20.00", text)
        self.assertIn("微信", text)
        self.assertIn("aikaui.com/qrcode/abc.jpg", text)
        self.assertIn("--balance", text)

    def test_it_draws_the_qr_in_the_terminal(self):
        text = "\n".join(plain(bc.recharge_lines(self.ORDER, color=False)))
        self.assertIn("█", text)
        self.assertIn("扫下面的二维码", text)

    def test_pay_url_becomes_the_qr_when_there_is_no_code(self):
        """微信直连常常只给链接。扫一个能打开支付页的链接，也比什么都没有强。"""
        order = dict(self.ORDER, qrcode=None, pay_url="https://pay.example.invalid/x")
        text = "\n".join(plain(bc.recharge_lines(order, color=False)))
        self.assertIn("█", text)
        self.assertIn("https://pay.example.invalid/x", text)

    def test_img_alone_still_works(self):
        order = {"order_no": "A1", "money": "20.00", "img": "https://x/y.jpg"}
        text = "\n".join(plain(bc.recharge_lines(order, color=False)))
        self.assertIn("https://x/y.jpg", text)
        self.assertNotIn("服务器没给二维码", text)

    def test_nothing_to_scan_is_said_out_loud(self):
        text = "\n".join(plain(bc.recharge_lines({"order_no": "A1"}, color=False)))
        self.assertIn("服务器没给二维码", text)

    def test_switched_channel_note_is_shown(self):
        order = dict(self.ORDER, switched_from="alipay",
                     note="支付宝现在用不了，已自动改用微信")
        self.assertIn("已自动改用微信",
                      "\n".join(plain(bc.recharge_lines(order, color=False))))

    def test_never_prints_secrets(self):
        order = dict(self.ORDER, api_key="sk-super-secret", account_key="SECRET")
        text = "\n".join(plain(bc.recharge_lines(order, color=False)))
        self.assertNotIn("sk-super-secret", text)
        self.assertNotIn("SECRET", text)


class CommandLinesTests(unittest.TestCase):
    def test_lists_what_the_user_can_run(self):
        text = "\n".join(bc.command_lines())
        for flag in ("--balance", "--recharge", "--repair", "--account-code", "--help"):
            self.assertIn(flag, text)
        # 密钥位置只给占位符，绝不把真值写进帮助文字
        self.assertIn("<密钥>", text)
        self.assertNotIn("sk-", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
