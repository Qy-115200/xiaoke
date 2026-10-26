# -*- coding: utf-8 -*-
"""发布形状：Linux 的产物必须是"服务器版"，而且 updater 还认得它。

这三件事是绑在一起的，改一处忘一处就会发出去一个装不上的包：
  1. `server.spec` 真的把界面和 Agent 链排除掉了；
  2. CI 的 Linux 那一行用的是 `server.spec`，产物名还是 `ClaudeCodeAssistant`；
  3. `updater._extract_linux` 能从这个 tar.gz 里挑出可执行文件。
"""

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

SPEC = ROOT / "server.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"


class ServerSpecTests(unittest.TestCase):
    def setUp(self):
        self.text = SPEC.read_text(encoding="utf-8")

    def test_entry_point(self):
        self.assertIn('["server_main.py"]', self.text)

    def test_binary_name_matches_updater(self):
        # updater._extract_linux 按文件名找它
        self.assertIn('NAME = "ClaudeCodeAssistant"', self.text)

    def test_console_is_true(self):
        # False 的话 print 和报错会被吞掉，服务器上等于瞎跑
        self.assertIn("console=True", self.text)

    def test_ui_and_agent_are_excluded(self):
        for mod in ("tkinter", "customtkinter", "openai", "pydantic",
                    "agent_core", "ui", "bus", "llm", "updater"):
            with self.subTest(mod=mod):
                self.assertIn(f'"{mod}"', self.text)

    def test_lzma_and_linux_hidden(self):
        # 解 Node 的 .tar.xz 要 lzma；platform_support.linux 是延迟 import
        self.assertIn('"platform_support.linux"', self.text)
        self.assertIn('"lzma"', self.text)
        self.assertIn('"_lzma"', self.text)

    def test_does_not_touch_agent_spec(self):
        agent = (ROOT / "agent.spec").read_text(encoding="utf-8")
        self.assertIn('["main.py"]', agent)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_linux_row_uses_server_spec(self):
        self.assertIn("spec: server.spec", self.text)

    def test_build_step_is_matrix_driven(self):
        self.assertIn("matrix.spec", self.text)
        self.assertNotIn("PyInstaller agent.spec", self.text)

    def test_linux_payload_has_no_desktop_entry(self):
        # .desktop 是给"双击打开图形窗口"的；服务器版是终端程序
        self.assertNotIn("claude-code-assistant.desktop", self.text)

    def test_artifact_name_unchanged(self):
        # 改了名字，latest.json 和 install.sh 都得跟着改 —— 别顺手改
        self.assertIn("ClaudeCodeAssistant-linux-x64.tar.gz", self.text)


class ExtractTests(unittest.TestCase):
    """用真的 tar 跑一遍 updater 的解包逻辑（它和客户端同一份代码）。"""

    def _tar(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, body, mode in members:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mode = mode
                tf.addfile(info, io.BytesIO(body))
        return buf.getvalue()

    def test_finds_the_binary(self):
        data = self._tar([
            ("ClaudeCodeAssistant-linux-x64/ClaudeCodeAssistant",
             b"#!/bin/sh\necho hi\n", 0o755),
            ("ClaudeCodeAssistant-linux-x64/agent.png", b"png", 0o644),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pkg.tar.gz"
            archive.write_bytes(data)
            found = updater._extract_linux(archive, Path(tmp) / "out")
        self.assertEqual(found.name, "ClaudeCodeAssistant")

    def test_still_rejects_traversal(self):
        data = self._tar([("../evil.sh", b"x", 0o644)])
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "pkg.tar.gz"
            archive.write_bytes(data)
            with self.assertRaises(Exception):
                updater._extract_linux(archive, Path(tmp) / "out")


class ManifestFormatTests(unittest.TestCase):
    """latest.json 必须是 LF —— install.sh 里是按行匹配的 awk。

    2026-10-26 踩过：在 Windows 上生成清单，write_text 把 \\n 翻译成 \\r\\n，
    上线后 `curl | sh` 报「清单里没有 linux-x64 的 version」。JSON 解析不怕 CRLF，
    所以 updater 那边一切正常，问题只在 shell 里才露出来。
    """

    def test_make_manifest_writes_lf(self):
        names = ("windows-x64:ClaudeCodeAssistant-win-x64.exe",
                 "macos-arm64:ClaudeCodeAssistant-macos-arm64.zip",
                 "macos-x64:ClaudeCodeAssistant-macos-x64.zip",
                 "linux-x64:ClaudeCodeAssistant-linux-x64.tar.gz")
        with tempfile.TemporaryDirectory() as tmp:
            args = []
            for key, name in (n.split(":") for n in names):
                pkg = Path(tmp) / name
                pkg.write_bytes(b"x" * 123)
                args += ["--file", f"{key}={pkg}"]
            out = Path(tmp) / "latest.json"
            r = subprocess.run(
                [sys.executable, str(ROOT / "build_tools" / "make_manifest.py"),
                 "--version", "1.3.0",
                 "--base-url", "https://dl.example.invalid/releases/1.3.0",
                 *args, "--out", str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            raw = out.read_bytes()
        self.assertNotIn(b"\r", raw, "清单里混进了 CRLF，install.sh 会解析不出来")
        import json
        self.assertEqual(json.loads(raw)["latest"]["linux-x64"]["version"], "1.3.0")


class DeployScriptTests(unittest.TestCase):
    def test_install_sh_is_uploaded(self):
        text = (ROOT / "build_tools" / "deploy_downloads.sh").read_text(encoding="utf-8")
        # 官网那一行 curl | sh 能不能用，全靠这一步把 install.sh 传上去
        self.assertIn("packaging/install.sh", text)
        self.assertIn("install.sh", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
