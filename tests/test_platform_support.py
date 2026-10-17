# -*- coding: utf-8 -*-
"""platform_support 的离线测试（不联网、不真的开终端）。"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_support  # noqa: E402
from config import HOME  # noqa: E402
from platform_support.common import find_claude, safe_cwd  # noqa: E402
from server.tools_fixed import parse_protocol_url  # noqa: E402


class ProtocolUrlTests(unittest.TestCase):
    def test_recharge(self):
        self.assertEqual(
            parse_protocol_url("aikaui://recharge?user_id=7"),
            {"action": "recharge", "user_id": "7"},
        )

    def test_trailing_slash_and_case(self):
        self.assertEqual(parse_protocol_url("AIKAUI://Recharge/")["action"], "recharge")

    def test_not_ours(self):
        self.assertEqual(parse_protocol_url("https://aikaui.com"), {})
        self.assertEqual(parse_protocol_url(""), {})


class SafeCwdTests(unittest.TestCase):
    def test_empty_means_home(self):
        cwd, note = safe_cwd("")
        self.assertEqual(cwd, HOME)
        self.assertEqual(note, "")

    def test_inside_home(self):
        with tempfile.TemporaryDirectory(dir=HOME) as d:
            target = Path(d) / "projects" / "demo"
            cwd, note = safe_cwd(str(target))
            self.assertEqual(cwd.resolve(), target.resolve())
            self.assertEqual(note, "")
            self.assertTrue(target.is_dir())

    def test_outside_home_is_refused(self):
        outside = "C:\\Windows" if platform_support.is_windows() else "/etc"
        cwd, note = safe_cwd(outside)
        self.assertEqual(cwd, HOME)
        self.assertNotEqual(note, "")


class FindClaudeTests(unittest.TestCase):
    def test_no_candidates_does_not_crash(self):
        # 没有候选路径时会退回 shutil.which("claude")：这台机器可能真装了，
        # 所以只要求"返回 None 或路径字符串"，不假设一定没有。
        self.assertIn(type(find_claude([])), (str, type(None)))

    def test_existing_path_is_returned(self):
        with tempfile.NamedTemporaryFile(dir=HOME, suffix="-claude") as f:
            self.assertEqual(find_claude([Path(f.name)]), f.name)


class PlatformApiTests(unittest.TestCase):
    def test_platform_key_has_known_prefix(self):
        key = platform_support.platform_key()
        self.assertTrue(
            key.startswith(("windows-", "macos-", "linux-")), key
        )

    def test_shell_command_shape(self):
        argv, use_shell = platform_support.shell_command("echo hi")
        if platform_support.is_windows():
            self.assertIsInstance(argv, str)
            self.assertTrue(use_shell)
            self.assertIn("chcp 65001", argv)
        else:
            self.assertIsInstance(argv, list)
            self.assertFalse(use_shell)
            self.assertEqual(argv[-1], "echo hi")

    def test_desktop_dir_is_path(self):
        self.assertIsInstance(platform_support.desktop_dir(), Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
