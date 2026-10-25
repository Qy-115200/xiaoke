# -*- coding: utf-8 -*-
"""server/tools_vscode.py 的离线测试。

这里验的是"工具说什么话"：JSON 的字段、能不能分清"没装 / 装了没插件 / 查不出来"，
以及装插件失败时给模型的是不是一句人话（而不是一整段 npm 风格的英文日志）。
全程打桩 `code --list-extensions`，不碰真 VSCode、不联网。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_support                                        # noqa: E402
from server import tools_vscode                                # noqa: E402

INFO = {
    "exe": r"D:\vs code\Microsoft VS Code\Code.exe",
    "cli_argv": [r"D:\vs code\Microsoft VS Code\bin\code.cmd"],
    "launch_argv": [r"D:\vs code\Microsoft VS Code\Code.exe"],
    "source": "registry",
}


def _ok(stdout: bytes = b"") -> tuple:
    return stdout, b"", False, 0


class _FakeUI:
    def __init__(self):
        self.calls = []

    def ui_progress_start(self, text=""):
        self.calls.append(("start", text))

    def ui_progress_stop(self):
        self.calls.append(("stop",))


class CheckVscodeTests(unittest.TestCase):
    def test_not_installed(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=None):
            payload = json.loads(tools_vscode.check_vscode())
        self.assertFalse(payload["installed"])
        self.assertFalse(payload["extension_installed"])
        self.assertEqual(payload["extension_id"], "anthropic.claude-code")

    def test_installed_with_extension(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture",
                               return_value=_ok(b"anthropic.claude-code@2.1.300\n")):
            payload = json.loads(tools_vscode.check_vscode())

        self.assertTrue(payload["installed"])
        self.assertTrue(payload["extension_installed"])
        self.assertEqual(payload["extension_version"], "2.1.300")
        self.assertEqual(payload["exe"], INFO["exe"])
        self.assertEqual(payload["source"], "registry")

    def test_installed_without_extension(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", return_value=_ok(b"")):
            payload = json.loads(tools_vscode.check_vscode())
        self.assertTrue(payload["installed"])
        self.assertFalse(payload["extension_installed"])

    def test_unknown_when_cli_missing(self):
        # 没有 bin\code.cmd（装的时候没勾 PATH / 装得不完整）→ 必须是 None，不能撒谎说"没装插件"
        info = dict(INFO, cli_argv=[])
        with mock.patch.object(platform_support, "find_vscode", return_value=info):
            payload = json.loads(tools_vscode.check_vscode())
        self.assertIsNone(payload["extension_installed"])
        self.assertTrue(payload.get("note"))

    def test_unknown_on_timeout(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture",
                               return_value=(b"", b"", True, -1)):
            payload = json.loads(tools_vscode.check_vscode())
        self.assertIsNone(payload["extension_installed"])
        self.assertIn("超时", payload["note"])


class InstallVscodeToolTests(unittest.TestCase):
    def test_forwards_and_shows_progress(self):
        ui = _FakeUI()
        with mock.patch.object(platform_support, "install_vscode",
                               return_value="已安装 VSCode（x）"), \
             mock.patch.object(tools_vscode, "get_ui", return_value=ui):
            text = tools_vscode.install_vscode()

        self.assertEqual(text, "已安装 VSCode（x）")
        self.assertEqual(ui.calls[0][0], "start")
        self.assertIn("VSCode", ui.calls[0][1])
        self.assertEqual(ui.calls[-1], ("stop",))

    def test_progress_stops_even_on_exception(self):
        ui = _FakeUI()
        with mock.patch.object(platform_support, "install_vscode",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(tools_vscode, "get_ui", return_value=ui):
            with self.assertRaises(RuntimeError):
                tools_vscode.install_vscode()
        self.assertEqual(ui.calls[-1], ("stop",))


class InstallExtensionTests(unittest.TestCase):
    def test_no_vscode(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=None):
            text = tools_vscode.install_claude_extension()
        self.assertTrue(text.startswith("装插件失败"), text)
        self.assertIn("没找到 VSCode", text)

    def test_no_cli(self):
        info = dict(INFO, cli_argv=[])
        with mock.patch.object(platform_support, "find_vscode", return_value=info):
            text = tools_vscode.install_claude_extension()
        self.assertTrue(text.startswith("装插件失败"), text)
        self.assertIn("扩展面板", text)

    def test_already_installed_only_lists(self):
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture",
                               return_value=_ok(b"anthropic.claude-code@2.1.300\n")) as run:
            text = tools_vscode.install_claude_extension()

        self.assertTrue(text.startswith("已经装好"), text)
        self.assertEqual(run.call_count, 1)                    # 不碰网络
        self.assertIn("--list-extensions", run.call_args.args[0])

    def test_install_then_verify(self):
        results = [_ok(b"ms-python.python@1.0\n"),     # 第一次查：没有
                   _ok(b""),                           # 装
                   _ok(b"anthropic.claude-code@2.1.300\n")]   # 复查：有了
        ui = _FakeUI()
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results) as run, \
             mock.patch.object(tools_vscode, "get_ui", return_value=ui):
            text = tools_vscode.install_claude_extension()

        self.assertTrue(text.startswith("已装好"), text)
        self.assertEqual(run.call_count, 3)
        argv = run.call_args_list[1].args[0]
        self.assertEqual(argv, INFO["cli_argv"] + ["--install-extension",
                                                   "anthropic.claude-code"])
        self.assertEqual(run.call_args_list[1].kwargs["timeout"],
                         tools_vscode.INSTALL_TIMEOUT)
        # 进度条只包住"装"这一步，不是整个函数
        self.assertEqual(ui.calls[0], ("start", "正在装 Claude 的 VSCode 插件…"))
        self.assertEqual(ui.calls[-1], ("stop",))

    def test_installed_but_missing_from_list(self):
        results = [_ok(b""), _ok(b""), _ok(b"ms-python.python@1.0\n")]
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results), \
             mock.patch.object(tools_vscode, "get_ui", return_value=_FakeUI()):
            text = tools_vscode.install_claude_extension()
        self.assertTrue(text.startswith("装插件失败"), text)
        self.assertIn("扩展列表里没看到", text)

    def test_failure_network(self):
        results = [_ok(b""),
                   (b"", b"Failed to download extension: ETIMEDOUT", False, 1)]
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results), \
             mock.patch.object(tools_vscode, "get_ui", return_value=_FakeUI()):
            text = tools_vscode.install_claude_extension()
        self.assertIn("插件市场", text)
        self.assertIn("手动装", text)

    def test_failure_broken_cli(self):
        results = [_ok(b""),
                   (b"", b"'code' is not recognized as an internal command", False, 1)]
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results), \
             mock.patch.object(tools_vscode, "get_ui", return_value=_FakeUI()):
            text = tools_vscode.install_claude_extension()
        self.assertIn("命令行工具跑不起来", text)

    def test_failure_other_keeps_last_line(self):
        results = [_ok(b""),
                   (b"", b"some noise\nError: cannot install", False, 3)]
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results), \
             mock.patch.object(tools_vscode, "get_ui", return_value=_FakeUI()):
            text = tools_vscode.install_claude_extension()
        self.assertIn("命令返回 3", text)
        self.assertIn("cannot install", text)

    def test_failure_timeout(self):
        results = [_ok(b""), (b"", b"", True, -1)]
        with mock.patch.object(platform_support, "find_vscode", return_value=INFO), \
             mock.patch.object(tools_vscode, "run_capture", side_effect=results), \
             mock.patch.object(tools_vscode, "get_ui", return_value=_FakeUI()):
            text = tools_vscode.install_claude_extension()
        self.assertIn("超时", text)
        self.assertIn("手动装", text)


class OpenVscodeToolTests(unittest.TestCase):
    def test_forwards_directory(self):
        with mock.patch.object(platform_support, "open_vscode",
                               return_value="已打开 VSCode（x），工作目录：y") as fn:
            text = tools_vscode.open_vscode(r"C:\Users\me\Desktop\claude-projects")
        fn.assert_called_once_with(r"C:\Users\me\Desktop\claude-projects")
        self.assertTrue(text.startswith("已打开 VSCode"))

    def test_default_directory(self):
        with mock.patch.object(platform_support, "open_vscode",
                               return_value="已打开 VSCode（x）") as fn:
            tools_vscode.open_vscode()
        fn.assert_called_once_with("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
