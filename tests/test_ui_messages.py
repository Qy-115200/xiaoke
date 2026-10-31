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

    def show_login_dialog(self, prefill="", prefill_key="", notice=""):
        self.calls.append(("show_login_dialog", prefill, prefill_key, notice))

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
        bus.msg_queue.put(("recovery", {"account_code": "CC-AAAAAA-BBBBBB",
                                        "account_key": "k" * 64}))
        bus.poll_queue(gui)

        kinds = [c[0] for c in gui.calls]
        self.assertIn("show_login_dialog", kinds)
        call = next(c for c in gui.calls if c[0] == "show_login_dialog")
        self.assertEqual(call[1], "CC-AAAAAA-BBBBBB")
        # 登录现在要两样，保险箱里有密钥就一起预填
        self.assertEqual(call[2], "k" * 64)
        self.assertTrue(call[3], "自动找回必须解释清楚为什么弹这个窗口")

    def test_recovery_without_code_still_opens_dialog(self):
        # 消息里没带码也不该崩：弹一个空的登录窗，用户自己填
        gui = _FakeGUI()
        bus.msg_queue.put(("recovery", {}))
        bus.poll_queue(gui)
        call = next(c for c in gui.calls if c[0] == "show_login_dialog")
        self.assertEqual(call[1], "")
        self.assertEqual(call[2], "")

    def test_recovered_shows_notice(self):
        gui = _FakeGUI()
        bus.msg_queue.put(("recovered", {}))
        bus.poll_queue(gui)
        self.assertIn(("show_recovered_notice",), gui.calls)

    def test_config_repair_log_is_quiet(self):
        """配置自愈只往对话区写一行：不弹窗、不打断用户。"""
        from server import claude_config as cc
        gui = _FakeGUI()
        result = cc.Result(ok=True, repaired=True,
                           changes=[cc.Change(path=Path("/h/.claude/settings.json"),
                                              data={}, keys=["ANTHROPIC_AUTH_TOKEN"])])
        bus.msg_queue.put(("log", f"[系统] {result.summary}"))
        bus.poll_queue(gui)

        self.assertEqual([c[0] for c in gui.calls], ["write"])
        self.assertIn("settings.json", gui.calls[0][1])
        self.assertNotIn("sk-", gui.calls[0][1], "日志里不许出现 token")

    def test_unknown_kind_is_ignored(self):
        gui = _FakeGUI()
        bus.msg_queue.put(("no_such_kind", {"x": 1}))
        bus.poll_queue(gui)          # 不该抛
        self.assertEqual(gui.calls, [])


class _Btn:
    def __init__(self):
        self.state = None
        self.text = None

    def configure(self, state=None, text=None):
        self.state = state
        self.text = text


class _BusyGUI(_FakeGUI):
    def __init__(self):
        super().__init__()
        self.start_btn = _Btn()
        self.stop_btn = _Btn()


class StopButtonTests(unittest.TestCase):
    """「中断」按钮：Agent 干活的时候才亮，闲着的时候是灰的。"""

    def setUp(self):
        self._drain()
        self.addCleanup(self._drain)

    @staticmethod
    def _drain():
        while True:
            try:
                bus.msg_queue.get_nowait()
            except Exception:
                return

    def test_busy_turns_the_stop_button_on(self):
        import tkinter as tk
        gui = _BusyGUI()
        bus.msg_queue.put(("busy", True))
        bus.poll_queue(gui)
        self.assertEqual(gui.stop_btn.state, tk.NORMAL)
        self.assertEqual(gui.stop_btn.text, "中断")
        self.assertEqual(gui.start_btn.state, tk.DISABLED)

    def test_idle_turns_it_off(self):
        import tkinter as tk
        gui = _BusyGUI()
        bus.msg_queue.put(("busy", False))
        bus.poll_queue(gui)
        self.assertEqual(gui.stop_btn.state, tk.DISABLED)


class OnStopTests(unittest.TestCase):
    """点「中断」之后：立牌子、掐子进程、叫醒正在等回答的线程。"""

    def setUp(self):
        import agent_bus
        agent_bus.clear_cancel()
        agent_bus.input_holder["value"] = None
        agent_bus.input_event.clear()
        self.addCleanup(agent_bus.clear_cancel)

    def _gui(self, waiting=False):
        from ui.window import WindowMixin
        gui = object.__new__(WindowMixin)       # 不建真窗口
        gui.waiting_for_answer = waiting
        gui.stop_btn = _Btn()
        gui.written = []
        gui.write = gui.written.append
        return gui

    def test_sets_the_flag_and_reports(self):
        import agent_bus
        from unittest import mock
        gui = self._gui()
        with mock.patch("platform_support.common.kill_active_children",
                        return_value=2):
            gui.on_stop()
        self.assertTrue(agent_bus.is_cancelled())
        self.assertIn("已停掉 2 个子进程", gui.written[0])
        self.assertIn("不会回滚", gui.written[0])
        self.assertEqual(gui.stop_btn.state, "disabled")

    def test_wakes_up_ask_user(self):
        import agent_bus
        from unittest import mock
        gui = self._gui(waiting=True)
        with mock.patch("platform_support.common.kill_active_children",
                        return_value=0):
            gui.on_stop()
        self.assertTrue(agent_bus.input_event.is_set())
        self.assertEqual(agent_bus.input_holder["value"], "（用户中断）")


if __name__ == "__main__":
    unittest.main()
