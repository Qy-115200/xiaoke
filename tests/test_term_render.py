# -*- coding: utf-8 -*-
"""活区排版：**每一行都不能超宽**。

超宽的行会被终端自己折成两行，而擦除是按"上移 N 行"算的 —— 少算一行，
屏幕上就留下擦不掉的字。所以这里把所有状态、所有宽度都过一遍。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import term_render as R  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(text):
    return ANSI.sub("", text)


class WidthTests(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(R.width("abc"), 3)

    def test_chinese_is_two_columns(self):
        self.assertEqual(R.width("你好"), 4)
        self.assertEqual(R.width("你a"), 3)

    def test_zero_width_symbols(self):
        self.assertEqual(R.width("á"), 1)

    def test_clip_never_overflows_and_marks_the_cut(self):
        for cols in (3, 5, 10, 21):
            got = R.clip("这是一段很长很长的中文 and some ascii", cols)
            self.assertLessEqual(R.width(got), cols, (cols, got))
        self.assertTrue(R.clip("abcdefg", 4).endswith("…"))

    def test_clip_leaves_short_text_alone(self):
        self.assertEqual(R.clip("abc", 10), "abc")


class InputLineTests(unittest.TestCase):
    def test_caret_marks_the_cursor(self):
        self.assertIn("你> a|bc", R.input_line("abc", 1, 40))

    def test_long_buffer_scrolls_to_keep_the_caret(self):
        buf = "x" * 200
        line = R.input_line(buf, 200, 30)
        self.assertLessEqual(R.width(line), 30)
        self.assertTrue(line.endswith("|"), "光标得留在可见范围里")

    def test_chinese_buffer_fits(self):
        line = R.input_line("你好世界" * 20, 40, 24)
        self.assertLessEqual(R.width(line), 24)


class AskBlockTests(unittest.TestCase):
    def check(self, lines, cols):
        for line in lines:
            self.assertLessEqual(R.width(plain(line)), cols, repr(line))

    def test_small_block_fits(self):
        lines = R.ask_block(["保留原来的", "换成我们的"], 0, "", 0, 40, 24)
        self.check(lines, 40)
        self.assertEqual(len(lines), 1 + 2 + 1 + 1)   # 提示 + 两项 + 自己打字 + 输入行

    def test_selected_row_is_reversed(self):
        lines = R.ask_block(["甲", "乙"], 1, "", 0, 40, 24)
        self.assertNotIn(R.REVERSE, lines[1])
        self.assertIn(R.REVERSE, lines[2])

    def test_free_text_row_is_last(self):
        lines = R.ask_block(["甲"], 0, "", 0, 40, 24)
        self.assertIn(R.FREE_TEXT, lines[-2])
        self.assertTrue(lines[-1].startswith(R.PROMPT))

    def test_numbering_starts_at_one_and_model_numbers_are_stripped(self):
        lines = R.ask_block(["1. 保留原来的", "2、换新"], 0, "", 0, 40, 24)
        self.assertIn("1. 保留原来的", plain(lines[1]))
        self.assertIn("2. 换新", plain(lines[2]))

    def test_long_options_are_clipped(self):
        lines = R.ask_block(["很长" * 60], 0, "", 0, 30, 24)
        self.check(lines, 30)

    def test_paging_when_there_are_many_options(self):
        options = [f"选项{i}" for i in range(20)]
        lines = R.ask_block(options, 0, "", 0, 40, 14)
        self.check(lines, 40)
        self.assertLessEqual(len(lines), 14, "活区不能比屏幕还高")
        self.assertTrue(any("下面还有" in plain(x) for x in lines))

    def test_paging_follows_the_selection(self):
        options = [f"选项{i}" for i in range(20)]
        top = R.ask_block(options, 0, "", 0, 40, 14)
        bottom = R.ask_block(options, 20, "", 0, 40, 14)
        self.assertIn("下面还有", "".join(plain(x) for x in top))
        self.assertIn("上面还有", "".join(plain(x) for x in bottom))

    def test_selection_wraps_within_the_block_not_here(self):
        # 越界的 selected 会被夹回去，不会 IndexError
        lines = R.ask_block(["甲"], 99, "", 0, 40, 24)
        self.check(lines, 40)

    def test_no_colour_uses_a_marker(self):
        lines = R.ask_block(["甲", "乙"], 1, "自己打的", 4, 40, 24, color=False)
        self.assertNotIn("\x1b", "".join(lines))
        self.assertTrue(lines[2].startswith("> "))

    def test_tiny_terminal_still_produces_lines(self):
        lines = R.ask_block(["甲", "乙", "丙"], 1, "x", 1, 12, 6)
        self.check(lines, 12)
        self.assertTrue(lines[-1].startswith(R.PROMPT))


class OptionLabelTests(unittest.TestCase):
    def test_resolve_option(self):
        labels = ["保留原来的", "换成我们的"]
        self.assertEqual(R.resolve_option("2", labels), "换成我们的")
        self.assertEqual(R.resolve_option(" 2. ", labels), "换成我们的")
        self.assertEqual(R.resolve_option("9", labels), "9")
        self.assertEqual(R.resolve_option("都行", labels), "都行")

    def test_norm_options_drops_model_numbering(self):
        self.assertEqual(R.norm_options(["1. 保留", "2、换新", "", {"label": "第三"}]),
                         ["保留", "换新", "第三"])


class EraseTests(unittest.TestCase):
    def test_nothing_to_erase(self):
        self.assertEqual(R.erase_live(0), "")

    def test_single_row(self):
        self.assertEqual(R.erase_live(1), "\r\x1b[J")

    def test_multiple_rows_moves_up_exactly(self):
        self.assertEqual(R.erase_live(3), "\x1b[2A\r\x1b[J")

    def test_reserve_returns_to_the_same_row(self):
        self.assertEqual(R.reserve(3), "\r\n\r\n\r\n\x1b[3A")

    def test_draw_lines_never_ends_with_a_newline(self):
        out = R.draw_lines(["a", "b"])
        self.assertFalse(out.endswith("\n"))
        self.assertIn("\x1b[2K", out)


class BusyLineTests(unittest.TestCase):
    def test_shows_elapsed_seconds(self):
        self.assertIn("12s", R.busy_line("正在安装", 12, 40))

    def test_is_clipped(self):
        self.assertLessEqual(R.width(R.busy_line("x" * 200, 1, 20)), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
