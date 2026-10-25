# -*- coding: utf-8 -*-
"""工具层必须在任何平台上都能 import（以前 winreg 写在模块顶层，macOS 直接炸）。"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ImportTests(unittest.TestCase):
    def test_tool_layer_imports(self):
        code = (
            "import server.tools_fixed, server.tools_general, server.tools_vscode,"
            " notify, prompts; print('ok')"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_windows_module_not_loaded_eagerly(self):
        """platform_support 包本身只 import sys；具体平台模块是懒加载的。"""
        code = (
            "import sys, platform_support;"
            "print('platform_support.windows' in sys.modules,"
            "'platform_support.macos' in sys.modules,"
            "'platform_support.linux' in sys.modules)"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False False False")


if __name__ == "__main__":
    unittest.main(verbosity=2)
