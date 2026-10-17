# -*- coding: utf-8 -*-
"""提示词必须跟着平台走：非 Windows 上不能出现 winget / %USERPROFILE% / C:\\Program Files。"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import prompts  # noqa: E402


def _prompt_for(system: str) -> str:
    with mock.patch("platform.system", return_value=system):
        return importlib.reload(prompts).SYSTEM_PROMPT


class PlatformPromptTests(unittest.TestCase):
    def test_windows_prompt(self):
        text = _prompt_for("Windows")
        self.assertIn("winget", text)
        self.assertIn("%USERPROFILE%", text)

    def test_macos_prompt(self):
        text = _prompt_for("Darwin")
        self.assertIn("brew", text)
        self.assertIn("$HOME/Desktop", text)
        for forbidden in ("winget", "%USERPROFILE%", "C:\\Program Files"):
            self.assertNotIn(forbidden, text)

    def test_linux_prompt(self):
        text = _prompt_for("Linux")
        self.assertIn("apt-get", text)
        self.assertIn("$HOME/Desktop", text)
        for forbidden in ("winget", "%USERPROFILE%", "C:\\Program Files"):
            self.assertNotIn(forbidden, text)

    def test_restored_after_tests(self):
        # 其它测试文件接着用这份模块时，应该还是本机平台的那一份
        importlib.reload(prompts)
        self.assertIn("Claude Code", prompts.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
