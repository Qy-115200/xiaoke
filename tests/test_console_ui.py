# -*- coding: utf-8 -*-
"""ConsoleUI：身份层那套进度钩子在终端里不能炸、不能刷屏。"""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.console_ui import ConsoleUI, make_console_ui  # noqa: E402


class FakeStream(io.StringIO):
    """StringIO 默认 isatty() 是 False；要测 tty 分支就让它说自己是终端。"""

    def __init__(self, tty=False):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


class ConsoleUITests(unittest.TestCase):
    def test_hooks_are_all_present(self):
        # server/identity.py 里那几个钩子全是 hasattr 保护的，少一个只是"没显示"，
        # 但少得不明不白 —— 这里钉死接口。
        ui = ConsoleUI(stream=FakeStream())
        for name in ("ui_log", "ui_progress_start", "ui_progress_stop",
                     "ui_pow_progress", "ui_recovered", "on_token_acquired", "ask"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(ui, name, None)))

    def test_pow_progress_piped_writes_occasionally(self):
        s = FakeStream(tty=False)
        ui = ConsoleUI(stream=s)
        for pct in range(0, 101):
            ui.ui_pow_progress("算力校验", pct)
        lines = [ln for ln in s.getvalue().splitlines() if ln.strip()]
        # 100 次里不能全打出来，但也要有进度
        self.assertLess(len(lines), 20)
        self.assertGreater(len(lines), 0)
        self.assertIn("100%", s.getvalue())

    def test_pow_progress_tty_stays_on_one_line(self):
        s = FakeStream(tty=True)
        ui = ConsoleUI(stream=s)
        for pct in (0, 50, 100):
            ui.ui_pow_progress("算力校验", pct)
        text = s.getvalue()
        self.assertNotIn("\n", text)          # 全在原地刷新
        self.assertIn("\r", text)

    def test_quiet_suppresses_progress(self):
        s = FakeStream()
        ui = ConsoleUI(stream=s, verbose=False)
        ui.ui_log("不该出现")
        ui.ui_progress_start("也不该出现")
        ui.ui_pow_progress("还是不该", 50)
        self.assertEqual(s.getvalue(), "")

    def test_ui_recovered_and_token_acquired(self):
        s = FakeStream()
        ui = ConsoleUI(stream=s)
        ui.on_token_acquired()                 # 不抛就算过
        ui.ui_recovered()
        self.assertIn("账号", s.getvalue())

    def test_write_never_raises_on_broken_stream(self):
        class Broken:
            def write(self, _):
                raise OSError("管道断了")
            def flush(self):
                raise OSError("管道断了")
        ui = ConsoleUI(stream=Broken())
        ui.ui_log("x")                         # 不能抛
        ui.ui_progress_stop()

    def test_ask_without_tty_does_not_block(self):
        ui = ConsoleUI(stream=FakeStream())
        answer = ui.ask("选一个", ["a", "b"])
        self.assertIn("用户回答", answer)


class FactoryTests(unittest.TestCase):
    def setUp(self):
        import os
        self._old = os.environ.get("AIKAUI_QUIET")

    def tearDown(self):
        import os
        if self._old is None:
            os.environ.pop("AIKAUI_QUIET", None)
        else:
            os.environ["AIKAUI_QUIET"] = self._old

    def test_quiet_env(self):
        import os
        os.environ["AIKAUI_QUIET"] = "1"
        self.assertFalse(make_console_ui(stream=FakeStream()).verbose)
        os.environ["AIKAUI_QUIET"] = "0"
        self.assertTrue(make_console_ui(stream=FakeStream()).verbose)


if __name__ == "__main__":
    unittest.main(verbosity=2)
