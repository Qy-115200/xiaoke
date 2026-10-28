# -*- coding: utf-8 -*-
"""`build_tools/fetch_release.py`：本机取 Release 产物的那条路。

为什么要有这个脚本：这台机器上**没有 gh CLI**，而部署就在这台机器上跑。
所以 README 里写的那一步必须真的能执行，不能只是抄一句 `gh release download`。

这里只测纯函数和不碰网络的约定；真下载在发布时人工验一次。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build_tools"))

import fetch_release  # noqa: E402

SRC = (ROOT / "build_tools" / "fetch_release.py").read_text(encoding="utf-8")


class ParseRepoTests(unittest.TestCase):
    def test_https_with_git_suffix(self):
        self.assertEqual(
            fetch_release.parse_repo("https://github.com/Qy-115200/agent_demo.git"),
            "Qy-115200/agent_demo")

    def test_https_without_suffix(self):
        self.assertEqual(
            fetch_release.parse_repo("https://github.com/Qy-115200/agent_demo"),
            "Qy-115200/agent_demo")

    def test_ssh_form(self):
        self.assertEqual(
            fetch_release.parse_repo("git@github.com:Qy-115200/agent_demo.git"),
            "Qy-115200/agent_demo")

    def test_trailing_slash(self):
        self.assertEqual(
            fetch_release.parse_repo("https://github.com/Qy-115200/agent_demo/"),
            "Qy-115200/agent_demo")

    def test_garbage_is_rejected_loudly(self):
        with self.assertRaises(SystemExit):
            fetch_release.parse_repo("D:/some/local/repo")


class TokenHandlingTests(unittest.TestCase):
    """令牌不能进命令行，也不能进日志 —— 对话记录里连截断都算泄密。"""

    def test_token_is_never_printed(self):
        for line in SRC.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "print(f\"✖" in stripped:
                continue
            self.assertNotRegex(
                stripped,
                r"print\(.*\b(token|Authorization)\b",
                f"这一行会把令牌打进日志：{stripped!r}")

    def test_token_is_not_a_command_line_argument(self):
        # argparse 里不该有 --token；否则它会进 shell 历史和进程列表
        self.assertNotIn('"--token"', SRC)
        self.assertNotIn("'--token'", SRC)

    def test_falls_back_to_env_then_file(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "from-env"}):
            self.assertEqual(fetch_release.read_token(Path("nope.txt")), "from-env")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fetch_release.read_token(Path("nope.txt")), "")


class RedirectTests(unittest.TestCase):
    def test_redirect_handler_refuses_to_follow(self):
        """必须自己拿 Location 再裸取 —— 跟随的话 Authorization 会被带到 CDN，
        GitHub 一律 401（这个坑踩过）。"""
        self.assertIsNone(fetch_release._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://objects.example/x"))


class ArtifactListTests(unittest.TestCase):
    def test_checks_the_four_platforms(self):
        for name in ("ClaudeCodeAssistant-win-x64.exe",
                     "ClaudeCodeAssistant-macos-arm64.zip",
                     "ClaudeCodeAssistant-macos-x64.zip",
                     "ClaudeCodeAssistant-linux-x64.tar.gz"):
            self.assertIn(name, SRC)

    def test_downloads_into_a_part_file_first(self):
        # 中途断掉不能留下半个包给 deploy_downloads.sh 当成完整的
        self.assertIn(".part", SRC)
        self.assertIn("tmp.replace(dest)", SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
