# -*- coding: utf-8 -*-
"""发布形状：Linux 的产物必须是"服务器版"，而且 updater 还认得它。

这三件事是绑在一起的，改一处忘一处就会发出去一个装不上的包：
  1. `server.spec` 真的把界面和 Agent 链排除掉了；
  2. CI 的 Linux 那一行用的是 `server.spec`，产物名还是 `ClaudeCodeAssistant`；
  3. `updater._extract_linux` 能从这个 tar.gz 里挑出可执行文件。
"""

import io
import re
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


def _listed(text, name):
    """取出所有 `NAME = [...]` / `NAME += [...]` 里的字符串（可以有多次追加）。

    ★ 不能再用全文 `assertIn('"openai"', text)`：模型栈现在**该进包**，
      所以 `openai` 会出现在 collect_all 那个元组里。全文匹配会把
      「被排除」和「被打包」看成一回事。"""
    out = []
    for m in re.finditer(rf"^{re.escape(name)}\s*(?:\+?=)\s*\[([^\]]*)\]",
                         text, re.M):
        out += re.findall(r'"([^"]+)"', m.group(1))
    return out


def _collect_all_packages(text):
    m = re.search(r"for pkg in \((.*?)\):", text, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


class ServerSpecTests(unittest.TestCase):
    def setUp(self):
        self.text = SPEC.read_text(encoding="utf-8")
        self.excludes = _listed(self.text, "EXCLUDES")
        self.hidden = _listed(self.text, "hiddenimports")
        self.collected = _collect_all_packages(self.text)

    def test_entry_point(self):
        self.assertIn('["server_main.py"]', self.text)

    def test_binary_name_matches_updater(self):
        # updater._extract_linux 按文件名找它
        self.assertIn('NAME = "ClaudeCodeAssistant"', self.text)

    def test_console_is_true(self):
        # False 的话 print 和报错会被吞掉，服务器上等于瞎跑
        self.assertIn("console=True", self.text)

    def test_gui_is_excluded(self):
        self.assertTrue(self.excludes, "EXCLUDES 解析不出来")
        for mod in ("tkinter", "_tkinter", "customtkinter", "PIL",
                    "Pillow", "ui", "updater"):
            with self.subTest(mod=mod):
                self.assertIn(mod, self.excludes)

    def test_helpers_is_kept(self):
        """`server/term_render.py` 要从 helpers 取 norm_options / resolve_option。
        把它排除掉，服务器版一进对话界面就 ModuleNotFoundError。"""
        self.assertNotIn("helpers", self.excludes)
        self.assertTrue((ROOT / "server" / "term_render.py")
                        .read_text(encoding="utf-8").find("from helpers import") >= 0)

    def test_the_model_stack_is_bundled_not_excluded(self):
        """这次改成 Agent 装了：openai / pydantic 必须**进包**。"""
        for mod in ("openai", "pydantic", "pydantic_core", "httpx2",
                    "httpcore2", "agent_core", "llm"):
            with self.subTest(mod=mod):
                self.assertNotIn(mod, self.excludes)
        for pkg in ("openai", "pydantic", "pydantic_core", "httpx2",
                    "httpcore2", "anyio", "jiter", "sniffio"):
            with self.subTest(pkg=pkg):
                self.assertIn(pkg, self.collected)

    def test_the_desktop_prompt_and_tk_queue_stay_out(self):
        """`prompts`（整篇讲桌面/VSCode）和 `bus`（拖 tkinter）都不该进服务器包。

        ★ 这两条容易悄悄漏：`agent_core` 里确实写了 `from prompts import …`，
          只是挪进了 `agent_worker()`。PyInstaller 的静态分析跟得到**缩进后的**
          import（apply_hidden_imports 之后一样会收），所以这里连"是不是写在
          函数里"一起断言 —— 上一次 CI 产物里 `prompts` 就在包里。"""
        for mod in ("prompts", "bus"):
            with self.subTest(mod=mod):
                self.assertIn(mod, self.excludes)
        src = (ROOT / "agent_core.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if re.match(r"(from|import)\s+(prompts|bus)\b", line):
                self.fail(f"agent_core.py 顶层 import 了 {line.strip()!r}；"
                          "冻结包里会 ModuleNotFoundError")

    def test_new_server_modules_are_hidden_imports(self):
        """这些模块是函数里 import 的，PyInstaller 的静态分析跟不到。"""
        for mod in ("agent_bus", "agent_core", "llm",
                    "server.agent_install", "server.agent_tools",
                    "server.prompts_server", "server.term_chat",
                    "server.term_keys", "server.term_render"):
            with self.subTest(mod=mod):
                self.assertIn(mod, self.hidden)

    def test_lzma_and_linux_hidden(self):
        # 解 Node 的 .tar.xz 要 lzma；platform_support.linux 是延迟 import
        self.assertIn("platform_support.linux", self.hidden)
        self.assertIn("lzma", self.hidden)
        self.assertIn("_lzma", self.hidden)

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

    def test_ci_does_not_deploy_anymore(self):
        """★ 2026-10-28 起分工变了：CI 只构建 + 挂 Release，部署在本机跑。

        GitHub runner 到 49.233.72.225 的上行只有 ~17KB/s（136MB 要两小时），
        1.4.6 和 1.4.7 两次都是把 publish 取消掉、改本机发的。所以这里不只是
        "配了也不跑"，而是**根本不该再有这条路**：没有 secrets、没有 SSH、
        没有 environment。"""
        self.assertNotIn("secrets.DEPLOY", self.text)
        self.assertNotIn("environment: production", self.text)
        self.assertNotIn("Configure SSH", self.text)
        for line in self.text.splitlines():
            # 摘要里 echo 出来"给你抄"的那一行不算执行
            self.assertFalse(
                line.strip().startswith("run: bash build_tools/deploy_downloads.sh"),
                "CI 又在跑部署脚本了 —— 它会卡在 17KB/s 的上行上")

    def test_tag_attaches_a_github_release(self):
        """产物得有地方拿。挂在 Release 上，本机 gh release download 一条命令。"""
        self.assertIn("gh release create", self.text)
        self.assertIn("gh release upload", self.text)
        self.assertIn("release/*", self.text)
        self.assertIn("contents: write", self.text)

    def test_release_notes_come_from_the_annotated_tag(self):
        """说明只维护一份：tag 的正文。latest.json 的 notes 也从那儿来。"""
        self.assertIn("git tag -l --format='%(contents)'", self.text)

    def test_rerunning_a_build_does_not_fail_on_an_existing_release(self):
        # 构建重跑很常见（改 CI、重试失败的 runner）
        self.assertIn("gh release view", self.text)
        self.assertIn("--clobber", self.text)


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
