# -*- coding: utf-8 -*-
"""「中断」：一块牌子 + 一份正在跑的子进程清单。

用户 2026-10-30 提的要求：装 winget / VSCode 可能几分钟，得有个明摆着的出口。
这里钉住三件事：

  · 牌子（cancel_event）能被立起来、摘掉，下一轮开工不会一上来就"已中断"；
  · run_capture 跑完/跑挂都要把自己从清单里摘掉，否则清单只会越长越长；
  · kill_active_children 真的把子进程掐掉，而且可以重复调（幂等）。

全部离线，不联网。
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_bus  # noqa: E402
from platform_support import common  # noqa: E402


class CancelFlagTests(unittest.TestCase):
    def setUp(self):
        agent_bus.clear_cancel()
        self.addCleanup(agent_bus.clear_cancel)

    def test_starts_clear(self):
        self.assertFalse(agent_bus.is_cancelled())

    def test_request_then_clear(self):
        agent_bus.request_cancel()
        self.assertTrue(agent_bus.is_cancelled())
        # 新的一轮开工必须能把上一轮的牌子摘掉
        agent_bus.clear_cancel()
        self.assertFalse(agent_bus.is_cancelled())

    def test_the_flag_is_the_same_object_as_the_event(self):
        """`is_cancelled()` 和 `cancel_event` 不能各说各话。"""
        agent_bus.cancel_event.set()
        self.assertTrue(agent_bus.is_cancelled())
        agent_bus.cancel_event.clear()
        self.assertFalse(agent_bus.is_cancelled())


class ActiveChildrenTests(unittest.TestCase):
    def setUp(self):
        common.kill_active_children()          # 别带着上一个用例残留进来
        self.addCleanup(common.kill_active_children)

    def test_registry_is_empty_after_a_normal_run(self):
        common.run_capture([sys.executable, "-c", "print(1)"],
                           timeout=60, env=dict(common.clean_env()))
        self.assertEqual(common._ACTIVE, [], "跑完要摘掉，不然清单只增不减")

    def test_registry_is_empty_after_a_failure(self):
        with self.assertRaises(Exception):
            common.run_capture(["this-command-does-not-exist-aikaui"],
                               timeout=10, env=dict(common.clean_env()))
        self.assertEqual(common._ACTIVE, [])

    def test_kills_a_running_child(self):
        started = []

        def slow():
            common.run_capture(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=120, env=dict(common.clean_env()))

        import threading
        t = threading.Thread(target=slow, daemon=True)
        t.start()
        deadline = time.time() + 10
        while not common._ACTIVE and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(common._ACTIVE, "子进程没登记进清单，中断按钮就掐不到它")
        started.append(True)

        self.assertEqual(common.kill_active_children(), 1)
        t.join(timeout=20)                     # 掐完 run_capture 得能返回，不能挂死
        self.assertFalse(t.is_alive())

    def test_idempotent(self):
        self.assertEqual(common.kill_active_children(), 0)
        self.assertEqual(common.kill_active_children(), 0)

    def test_one_bad_process_does_not_stop_the_rest(self):
        class Boom:
            pid = 0

        with mock.patch.object(common, "kill_tree", side_effect=RuntimeError("boom")):
            common._register(Boom())
            common._register(Boom())
        # kill_active_children 自己不吞 kill_tree 的异常，那是 kill_tree 的活；
        # 这里只验清单被清空，不会因为一个失败就退化成"永远杀不完"
        try:
            common.kill_active_children()
        except RuntimeError:
            pass
        self.assertEqual(common._ACTIVE, [])


class DownloadsStopTests(unittest.TestCase):
    """下 58MB / 240MB 的时候点了中断，循环里每块都要看一眼牌子。"""

    def setUp(self):
        agent_bus.clear_cancel()
        self.addCleanup(agent_bus.clear_cancel)

    def test_winget_download_returns_cancelled(self):
        from platform_support import windows as win_backend
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))

        class Resp:
            status_code = 200
            headers = {"content-length": str(8 << 20)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n=1 << 20):
                agent_bus.request_cancel()     # 第一块就点中断
                yield b"x" * (1 << 20)
                yield b"x" * (1 << 20)

        fake = mock.Mock()
        fake.get = mock.Mock(return_value=Resp())
        with mock.patch.dict(sys.modules, {"requests": fake}):
            why = win_backend._download("https://example/x", tmp / "a.msix")
        self.assertEqual(why, "已中断")
        self.assertFalse((tmp / "a.msix").exists())
        self.assertFalse((tmp / "a.msix.part").exists(), "半截文件要清掉")


if __name__ == "__main__":
    unittest.main()
