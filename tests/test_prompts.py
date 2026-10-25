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


class VscodePromptTests(unittest.TestCase):
    """VSCode 那一段是新加的，最容易在改提示词时被顺手删掉，所以钉住关键几句。"""

    def setUp(self):
        self.text = _prompt_for("Windows")

    def test_four_vscode_tools_mentioned(self):
        for name in ("check_vscode()", "install_vscode()",
                     "install_claude_extension()", "open_vscode(directory)"):
            with self.subTest(tool=name):
                self.assertIn(name, self.text)

    def test_detection_comes_before_asking(self):
        # 先拿事实（check_vscode）再说话；不许凭猜
        self.assertIn("先拿事实，再说话，不许猜", self.text)

    def test_two_questions_verbatim(self):
        # 两句问法要能原样被模型抄出去；选项里不能带编号（第三节的硬规矩）
        self.assertIn("你电脑上还没装 VSCode。", self.text)
        self.assertIn("要我现在帮你装吗？", self.text)
        self.assertIn('options=["好，帮我装 VSCode", "不用了，用终端就行"]', self.text)
        self.assertIn("你电脑上已经有 VSCode 了。", self.text)
        self.assertIn('options=["好，装一下插件", "不用了，用终端就行"]', self.text)

    def test_only_one_launch(self):
        self.assertIn("两个只能开一个，绝不都开", self.text)

    def test_no_bash_for_vscode_install(self):
        self.assertIn("不要**用 bash 去跑", self.text)

    def test_old_hands_off_wording_removed(self):
        # 旧提示词写死了"不要自作主张去装 VSCode"，跟新流程相反
        self.assertNotIn("不要自作主张去装 VSCode", self.text)

    def test_plan_has_both_launch_versions(self):
        self.assertIn("步骤 6", self.text)
        self.assertIn("步骤 7", self.text)
        # 用户拒绝 VSCode 之后，收尾里不能再出现 VSCode 推销
        self.assertIn("一个字都不要提 VSCode", self.text)

    def test_extension_installed_null_treated_as_false(self):
        self.assertIn("`extension_installed` 是 null 时当成 false", self.text)

    def test_install_failure_does_not_promise_waiting(self):
        # 自动装失败那一路照常收尾退出，所以不许承诺"我在这里等你回话"——
        # 窗口都关了，承诺就变成骗人
        self.assertIn("不要承诺", self.text)
        self.assertIn("情况 I", self.text)

    def test_open_vscode_also_opens_panel(self):
        # open_vscode 不只是开窗：面板要一起开，而且失败时不许假装成功
        self.assertIn("Claude 面板也打开了", self.text)
        self.assertIn("点左边活动栏的 Claude 图标", self.text)
        self.assertIn("在面板没打开时说", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
