# -*- coding: utf-8 -*-
"""bus.poll_queue 的兜底测试：有没有把消息送到对的地方。

这里只能用**假 GUI**：离线测试不建 Tk 窗口（CI 上没有显示器）。假对象只记下
"谁被调了、带了什么参数"，正好是我们想验的东西。

现在盯两条 2026-10 加的路径：
  "recovery"  本地凭证丢了、保险箱里还有账户码 → 必须弹「换设备登录」并把码预填好
  "recovered" 服务器说账号已恢复 → 必须弹提示
这两条走错一步，用户就是"账号没了、还不知道为什么"。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bus                                              # noqa: E402


class _Root:
    def after(self, *_a, **_k):
        return None


class _FakeGUI:
    def __init__(self):
        self.root = _Root()
        self.calls = []

    def write(self, text):
        self.calls.append(("write", text))

    def show_login_dialog(self, prefill="", notice=""):
        self.calls.append(("show_login_dialog", prefill, notice))

    def show_recovered_notice(self):
        self.calls.append(("show_recovered_notice",))


class PollQueueTests(unittest.TestCase):
    def setUp(self):
        # 上一个测试可能留下没取完的消息，先清空
        self._drain()
        self.addCleanup(self._drain)

    @staticmethod
    def _drain():
        while True:
            try:
                bus.msg_queue.get_nowait()
            except Exception:
                return

    def test_recovery_opens_login_with_prefill(self):
        gui = _FakeGUI()
        bus.msg_queue.put(("recovery", {"account_code": "CC-AAAAAA-BBBBBB"}))
        bus.poll_queue(gui)

        kinds = [c[0] for c in gui.calls]
        self.assertIn("show_login_dialog", kinds)
        call = next(c for c in gui.calls if c[0] == "show_login_dialog")
        self.assertEqual(call[1], "CC-AAAAAA-BBBBBB")
        self.assertTrue(call[2], "自动找回必须解释清楚为什么弹这个窗口")

    def test_recovery_without_code_still_opens_dialog(self):
        # 消息里没带码也不该崩：弹一个空的登录窗，用户自己填
        gui = _FakeGUI()
        bus.msg_queue.put(("recovery", {}))
        bus.poll_queue(gui)
        call = next(c for c in gui.calls if c[0] == "show_login_dialog")
        self.assertEqual(call[1], "")

    def test_recovered_shows_notice(self):
        gui = _FakeGUI()
        bus.msg_queue.put(("recovered", {}))
        bus.poll_queue(gui)
        self.assertIn(("show_recovered_notice",), gui.calls)

    def test_unknown_kind_is_ignored(self):
        gui = _FakeGUI()
        bus.msg_queue.put(("no_such_kind", {"x": 1}))
        bus.poll_queue(gui)          # 不该抛
        self.assertEqual(gui.calls, [])


if __name__ == "__main__":
    unittest.main()
