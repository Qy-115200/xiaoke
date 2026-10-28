# -*- coding: utf-8 -*-
"""macOS 桌面快捷方式（.command）的内容与生成流程。

用户报过的事故是：双击一闪而过、看不到错误、桌面上还"没图标"。这些都不是
"跑一下就能发现"的 bug —— 它们只在用户的图形界面里发生，而且发生时用户已经
走开了。所以把脚本内容本身当数据来测：缺了哪一句，窗口就留不住。
"""

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from platform_support import macos  # noqa: E402


class ShortcutScriptTests(unittest.TestCase):
    """只看生成出来的文本，不落盘。"""

    def setUp(self):
        self.path = "/Users/someone/.npm-global/bin/claude"
        self.script = macos._shortcut_script(self.path)

    def test_shebang_is_bash(self):
        # `read -n 1` / `source` 都是 bash 的，/bin/sh 不保证有
        self.assertTrue(self.script.startswith("#!/bin/bash\n"))

    def test_is_utf8_friendly_and_has_no_template_left(self):
        self.assertNotIn("@@CLAUDE@@", self.script)
        self.assertIn("正在启动 Claude Code", self.script)

    def test_the_absolute_path_is_embedded(self):
        # shlex.quote 对没特殊字符的路径不加引号；带空格的见下面那条
        self.assertIn("CLAUDE=" + shlex.quote(self.path) + "\n", self.script)

    def test_falls_back_to_path_lookup(self):
        # claude 被挪走 / 换成别的安装方式时，还有一次机会
        self.assertIn('command -v claude', self.script)

    def test_never_execs(self):
        """★ 老版本是 `exec claude`：exec 把 bash 换成 claude，trap 永远不执行，
        窗口一闪就关。这条是这次事故的直接原因，必须钉住。"""
        for line in self.script.splitlines():
            with self.subTest(line=line):
                self.assertFalse(line.lstrip().startswith("exec "),
                                 "又用 exec 了，trap 里的「等按键」会失效")

    def test_waits_for_a_key_on_every_exit_path(self):
        self.assertIn("trap cleanup EXIT", self.script)
        self.assertIn("read -r -n 1 -s", self.script)

    def test_set_e_is_on(self):
        self.assertIn("\nset -e\n", self.script)

    def test_path_is_prepended_twice(self):
        """rc 文件里可能有自己的 export PATH=…，会覆盖第一次补的。"""
        self.assertGreaterEqual(self.script.count("export PATH="), 2)
        for d in ("$HOME/.npm-global/bin", "$HOME/.local/bin",
                  "/opt/homebrew/bin", "/usr/local/bin"):
            with self.subTest(dir=d):
                self.assertIn(d, self.script)

    def test_rc_files_are_optional(self):
        """~/.zshrc 不存在、或者读的时候报错，都不能让脚本死在 setup 上。"""
        self.assertIn('if [ -f "$HOME/.zshrc" ]', self.script)
        self.assertIn('if [ -f "$HOME/.bash_profile" ]', self.script)
        self.assertGreaterEqual(self.script.count("|| true"), 2)

    def test_uses_printf_not_echo_e(self):
        # macOS 的 /bin/sh 会把 -e 原样打出来；printf 到处一样
        self.assertNotIn("echo -e", self.script)
        self.assertIn("printf", self.script)

    def test_quotes_paths_with_spaces(self):
        weird = "/Users/some one/My Apps/claude"
        script = macos._shortcut_script(weird)
        self.assertIn("'/Users/some one/My Apps/claude'", script)

    def test_no_bash4_syntax(self):
        # macOS 自带的是 bash 3.2
        for token in ("readarray", "${", "&>"):
            if token == "${":
                continue
            with self.subTest(token=token):
                self.assertNotIn(token, self.script)

    @unittest.skipUnless(shutil.which("bash"), "没有 bash 可用来验语法")
    def test_the_script_parses(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shortcut.command"
            p.write_text(self.script, encoding="utf-8", newline="\n")
            r = subprocess.run(["bash", "-n", str(p)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class CreateShortcutTests(unittest.TestCase):
    """跑一遍 create_shortcut()，桌面目录换成临时目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.desktop = Path(self._tmp.name)
        self.claude = "/Users/someone/.npm-global/bin/claude"
        self.patches = [
            mock.patch.object(macos, "desktop_dir", return_value=self.desktop),
            mock.patch.object(macos, "find_claude_path", return_value=self.claude),
            mock.patch.object(macos.subprocess, "Popen"),
        ]
        started = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.popen = started[-1]

    def _created(self) -> Path:
        return self.desktop / macos.SHORTCUT_NAME

    def test_creates_the_command_file(self):
        msg = macos.create_shortcut()
        self.assertIn("已创建", msg)
        self.assertTrue(self._created().exists())

    def test_the_file_is_executable(self):
        with mock.patch.object(Path, "chmod") as chmod:
            macos.create_shortcut()
        chmod.assert_called_with(0o755)

    def test_written_as_lf_not_crlf(self):
        """CRLF 会让 bash 报 bad interpreter —— 打包/测试机器是 Windows。"""
        macos.create_shortcut()
        raw = self._created().read_bytes()
        self.assertNotIn(b"\r\n", raw)

    def test_finder_is_refreshed(self):
        macos.create_shortcut()
        self.popen.assert_called_with(
            ["killall", "Finder"],
            stdout=mock.ANY, stderr=mock.ANY)

    def test_a_dead_finder_does_not_break_the_shortcut(self):
        self.popen.side_effect = FileNotFoundError("no killall here")
        msg = macos.create_shortcut()
        self.assertIn("已创建", msg)

    def test_old_symlink_is_cleaned_up(self):
        legacy = self.desktop / "Claude Code"
        try:
            os.symlink(self.claude, legacy)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("这个系统建不了符号链接")
        macos.create_shortcut()
        self.assertFalse(legacy.is_symlink(), "老符号链接还留在桌面上")

    def test_missing_claude_is_reported_not_raised(self):
        with mock.patch.object(macos, "find_claude_path", return_value=None):
            msg = macos.create_shortcut()
        self.assertIn("找不到", msg)
        self.assertFalse(self._created().exists())

    def test_unwritable_desktop_is_reported_not_raised(self):
        with mock.patch.object(macos, "desktop_dir",
                               side_effect=OSError("nope")):
            msg = macos.create_shortcut()
        self.assertIn("失败", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
