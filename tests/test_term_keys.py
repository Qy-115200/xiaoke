# -*- coding: utf-8 -*-
"""字节 → 按键的翻译层。纯函数，不需要真终端。

这里要钉住的是**半截序列**：SSH / tmux 下字节被拆成两批是常态，翻译层要是
把半截 `ESC [` 当成乱码丢掉，剩下的 `A` 就会跑进输入框，用户按一下方向键
屏幕上就多一个 "A"。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.term_keys import KeyDecoder  # noqa: E402


class BasicTests(unittest.TestCase):
    def decode(self, data):
        return KeyDecoder().feed(data)

    def test_plain_chars(self):
        self.assertEqual(self.decode(b"ab"), [("char", "a"), ("char", "b")])

    def test_enter_accepts_both_byte_forms(self):
        self.assertEqual(self.decode(b"\r"), [("enter", "")])
        self.assertEqual(self.decode(b"\n"), [("enter", "")])

    def test_backspace(self):
        self.assertEqual(self.decode(b"\x7f"), [("backspace", "")])
        self.assertEqual(self.decode(b"\x08"), [("backspace", "")])

    def test_tab(self):
        self.assertEqual(self.decode(b"\t"), [("tab", "")])

    def test_ctrl_letters(self):
        self.assertEqual(self.decode(b"\x03"), [("ctrl", "c")])
        self.assertEqual(self.decode(b"\x04"), [("ctrl", "d")])
        self.assertEqual(self.decode(b"\x15"), [("ctrl", "u")])
        self.assertEqual(self.decode(b"\x17"), [("ctrl", "w")])
        self.assertEqual(self.decode(b"\x0c"), [("ctrl", "l")])

    def test_unknown_control_bytes_are_dropped(self):
        self.assertEqual(self.decode(b"\x00"), [])


class Utf8Tests(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(KeyDecoder().feed("你".encode("utf-8")), [("char", "你")])

    def test_char_split_across_two_reads(self):
        d = KeyDecoder()
        raw = "你".encode("utf-8")
        self.assertEqual(d.feed(raw[:2]), [], "半截字符不能当成乱码吐出来")
        self.assertEqual(d.feed(raw[2:]), [("char", "你")])

    def test_emoji_split_across_two_reads(self):
        d = KeyDecoder()
        raw = "😀".encode("utf-8")
        self.assertEqual(d.feed(raw[:3]), [])
        self.assertEqual(d.feed(raw[3:]), [("char", "😀")])

    def test_bad_byte_does_not_desync(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\xff"), [("char", "�")])
        self.assertEqual(d.feed(b"a"), [("char", "a")])


class ArrowTests(unittest.TestCase):
    def test_csi_arrows(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D"),
                         [("up", ""), ("down", ""), ("right", ""), ("left", "")])

    def test_ss3_arrows(self):
        self.assertEqual(KeyDecoder().feed(b"\x1bOA\x1bOB"), [("up", ""), ("down", "")])

    def test_home_end(self):
        self.assertEqual(KeyDecoder().feed(b"\x1b[H\x1b[F"),
                         [("home", ""), ("end", "")])
        self.assertEqual(KeyDecoder().feed(b"\x1bOH\x1bOF"),
                         [("home", ""), ("end", "")])

    def test_delete_tilde(self):
        self.assertEqual(KeyDecoder().feed(b"\x1b[3~"), [("delete", "")])

    def test_half_a_sequence_waits(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b["), [])
        self.assertEqual(d.feed(b"A"), [("up", "")])

    def test_half_a_tilde_sequence_waits(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[1"), [])
        self.assertEqual(d.feed(b"~"), [("home", "")])

    def test_unknown_sequence_keeps_sync(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[99X"), [])
        self.assertEqual(d.feed(b"a"), [("char", "a")])

    def test_alt_key_swallows_the_letter(self):
        # ESC 后面跟一个普通字母：认不出是什么组合键，但绝不能把字母漏进输入框
        self.assertEqual(KeyDecoder().feed(b"\x1ba"), [])


class EscapeTests(unittest.TestCase):
    def test_lone_esc_flushes(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b"), [])
        self.assertEqual(d.flush(), [("esc", "")])

    def test_esc_then_flush_then_more(self):
        d = KeyDecoder()
        d.feed(b"\x1b")
        self.assertEqual(d.flush(), [("esc", "")])
        self.assertEqual(d.feed(b"a"), [("char", "a")])

    def test_esc_in_the_same_batch_is_not_a_key(self):
        # 同一批里 ESC 后面还有东西 → 是方向键序列，不是 Esc 键
        self.assertEqual(KeyDecoder().feed(b"\x1b[A"), [("up", "")])


class PasteTests(unittest.TestCase):
    def test_bracketed_paste_is_one_event(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[200~hello\nworld\x1b[201~"),
                         [("paste", "hello\nworld")])

    def test_paste_split_across_reads(self):
        d = KeyDecoder()
        self.assertEqual(d.feed(b"\x1b[200~hel"), [])
        self.assertEqual(d.feed(b"lo\x1b[201~"), [("paste", "hello")])

    def test_paste_end_marker_split(self):
        d = KeyDecoder()
        d.feed(b"\x1b[200~hi\x1b[20")
        self.assertEqual(d.feed(b"1~"), [("paste", "hi")])

    def test_paste_without_end_flushes_as_text(self):
        d = KeyDecoder()
        d.feed(b"\x1b[200~hi")
        self.assertEqual(d.flush(), [("paste", "hi")])

    def test_chinese_paste(self):
        d = KeyDecoder()
        self.assertEqual(d.feed("\x1b[200~你好\x1b[201~".encode("utf-8")),
                         [("paste", "你好")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
