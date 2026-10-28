# -*- coding: utf-8 -*-
"""服务器版提示词：规矩要在，密钥不许在。

这份提示词是模型的"岗位说明书"。这里钉的是几条**出过事**的规矩：
顺序、不许手敲 npm/apt、不许碰身份文件、收尾不许说"已经帮你打开了"。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import prompts_server as P  # noqa: E402


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.prompt = P.build_system_prompt({"platform": "linux", "arch": "x86_64",
                                             "home": "/root", "workdir": "/root"})

    def test_the_order_is_spelled_out(self):
        text = self.prompt
        positions = [text.index(name) for name in
                     ("ensure_node", "install_claude", "configure_claude", "finish_install")]
        self.assertEqual(positions, sorted(positions), "四步的书写顺序就是执行顺序")

    def test_forbids_hand_written_installs(self):
        for word in ("npm", "apt", "sudo"):
            self.assertIn(word, self.prompt)
        self.assertIn("不要自己跑", self.prompt)

    def test_forbids_touching_the_identity_directory(self):
        self.assertIn(".claude-agent", self.prompt)

    def test_forbids_printing_tokens(self):
        self.assertIn("token", self.prompt)
        self.assertIn("不要", self.prompt)

    def test_says_the_program_opens_claude_not_the_model(self):
        self.assertIn("已经帮你打开了", self.prompt)   # 这是"不许说的话"，原文里要出现
        self.assertIn("马上会", self.prompt)

    def test_says_it_is_a_server(self):
        self.assertIn("服务器", self.prompt)

    def test_ask_user_rules(self):
        self.assertIn("options", self.prompt)
        self.assertIn("编号", self.prompt)

    def test_facts_are_rendered(self):
        self.assertIn("/root", self.prompt)
        self.assertIn("x86_64", self.prompt)

    def test_root_note(self):
        text = P.build_system_prompt({"is_root": True})
        self.assertIn("/root", text)

    def test_non_root_note(self):
        self.assertIn("不需要任何系统权限", self.prompt)

    def test_recovered_note(self):
        text = P.build_system_prompt({"recovered": True})
        self.assertIn("余额", text)

    def test_account_code_is_shown_when_known(self):
        text = P.build_system_prompt({"account_code": "CC-111111-222222"})
        self.assertIn("CC-111111-222222", text)

    def test_no_secrets_in_the_prompt(self):
        self.assertNotIn("sk-", self.prompt)

    def test_works_without_facts(self):
        self.assertIn("ensure_node", P.build_system_prompt())

    def test_tasks_are_not_empty(self):
        self.assertTrue(P.INITIAL_TASK.strip())
        self.assertIn("用户", P.CONTINUE_TASK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
