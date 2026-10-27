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


class ServerEntryImportTests(unittest.TestCase):
    """服务器版入口不许把界面和 Agent 链拖进来（server.spec 靠这个成立）。"""

    FORBIDDEN = ("tkinter", "_tkinter", "customtkinter", "PIL",
                 "openai", "pydantic", "agent_core", "bus", "llm",
                 "ui", "updater")

    def test_server_main_imports_no_gui(self):
        code = (
            "import sys, server_main;"
            "print([m for m in %r if m in sys.modules])"
            % (list(self.FORBIDDEN),)
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]",
                         "服务器版入口把不该要的模块拖进来了")

    def test_claude_config_imports_no_gui(self):
        """配置自愈会被 server_main 和桌面版两边用 —— 它自己必须是"干净"的。"""
        code = (
            "import sys, server.claude_config as c;"
            "print([m for m in %r if m in sys.modules])"
            % (list(self.FORBIDDEN),)
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]",
                         "server/claude_config.py 把界面/Agent 模块拖进来了")

    def test_billing_console_imports_no_gui(self):
        """余额/充值的措辞模块会被 server_main 直接 import —— 它也得是干净的。

        另外它用 segno 画二维码，那是延迟 import：导入这个模块本身不该把
        segno（或任何第三方包）拖进来。"""
        code = (
            "import sys, server.billing_console;"
            "bad = [m for m in %r if m in sys.modules];"
            "print(bad, 'segno' in sys.modules)"
            % (list(self.FORBIDDEN),)
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[] False",
                         "server/billing_console.py 把界面模块或 segno 拖进来了")

    def test_server_main_help_works_without_display(self):
        """没有 $DISPLAY 也必须能起来 —— 服务器就是这样。"""
        import os
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        r = subprocess.run(
            [sys.executable, "server_main.py", "--version"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ClaudeCodeAssistant", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
