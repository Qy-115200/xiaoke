# -*- coding: utf-8 -*-
"""bash 工具的超时必须真的返回（不联网）。"""

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.tools_general import bash  # noqa: E402


def _slow_cmd(seconds: int = 30) -> str:
    """一个必然跑够 seconds 秒的命令（Windows 用 ping，POSIX 用 sleep）。"""
    if os.name == "nt":
        return f"ping -n {seconds} 127.0.0.1 > nul"
    return f"sleep {seconds}"


class BashTimeoutTests(unittest.TestCase):
    def test_normal_command(self):
        out = bash("echo hello-agent", timeout=30)
        self.assertIn("hello-agent", out)

    def test_timeout_returns_promptly(self):
        # 回归：以前 subprocess.run(timeout=) 只杀 cmd.exe，curl/ping 还握着管道，
        # communicate() 会一直等 EOF —— 用户看到的就是"超时了也不返回"。
        t0 = time.time()
        out = bash(_slow_cmd(30), timeout=2)
        elapsed = time.time() - t0
        self.assertIn("[超时]", out)
        self.assertLess(elapsed, 15, f"超时后过了 {elapsed:.1f}s 才返回")


if __name__ == "__main__":
    unittest.main()
