# -*- coding: utf-8 -*-
"""账号引导：模型调不通之前的最后一小段确定性流程。

这一段的规矩只有一条最要紧 —— **本机有凭证就绝不重新注册**。注册 = 开一个新号，
用户原来的余额就永远回不来了。其余每一条都是"用户选了哪一项就该走哪条路"。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import agent_install, identity  # noqa: E402


class FakeUI:
    def __init__(self, answers=None):
        self.logs = []
        self.questions = []
        self._answers = list(answers or [])

    def ui_log(self, text):
        self.logs.append(str(text))

    def ui_ask(self, question, options=None):
        self.questions.append((question, list(options or [])))
        answer = self._answers.pop(0) if self._answers else ""
        return f"用户回答：{answer}"

    @property
    def text(self):
        return "\n".join(self.logs)


class Args:
    def __init__(self, account_code="", account_key="", new_account=False):
        self.account_code = account_code
        self.account_key = account_key
        self.new_account = new_account
        self.dir = ""
        self.yes = False


def ok_json(code="CC-111111-222222", recovered=False):
    import json
    return json.dumps({"note": "领取成功", "account_code": code,
                       "recovered": recovered}, ensure_ascii=False)


class LocalCredentialTests(unittest.TestCase):
    def test_never_registers_when_local_credentials_exist(self):
        ui = FakeUI()
        with mock.patch.object(identity, "load_identity",
                               return_value={"api_key": "k",
                                             "account_code": "CC-1"}), \
             mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertTrue(result["ok"])
        reg.assert_not_called()
        self.assertEqual(ui.questions, [], "本机有账号就不该再问")
        self.assertIn("CC-1", ui.text)


class ExplicitCodeTests(unittest.TestCase):
    def test_binds_the_account_the_user_named(self):
        ui = FakeUI()
        data = {"api_key": "k", "account_code": "CC-222222-333333"}
        with mock.patch.object(identity, "load_identity", return_value=None), \
             mock.patch.object(identity, "bind_account",
                               return_value=(data, None, "", None)) as bind, \
             mock.patch.object(identity, "adopt_identity") as adopt, \
             mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(
                None, Args(account_code="CC-222222-333333", account_key="x"), ui)
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_code"], "CC-222222-333333")
        bind.assert_called_once_with("CC-222222-333333", "x")
        adopt.assert_called_once_with(data)
        reg.assert_not_called()

    def test_missing_key_tells_you_to_pass_it(self):
        ui = FakeUI()
        with mock.patch.object(identity, "load_identity", return_value=None), \
             mock.patch.object(identity, "bind_account",
                               return_value=(None, "这个账号还需要账户密钥",
                                             "account_key_required", None)), \
             mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(
                None, Args(account_code="CC-1"), ui)
        self.assertFalse(result["ok"])
        self.assertIn("--account-key", result["hint"])
        reg.assert_not_called()

    def test_rate_limit_says_wait(self):
        ui = FakeUI()
        with mock.patch.object(identity, "load_identity", return_value=None), \
             mock.patch.object(identity, "bind_account",
                               return_value=(None, "试得太频繁了",
                                             "too_many_attempts", 30)):
            result = agent_install.bootstrap_identity(
                None, Args(account_code="CC-1", account_key="x"), ui)
        self.assertFalse(result["ok"])
        self.assertIn("30", result["hint"])


class RecoveryTests(unittest.TestCase):
    """保险箱里有旧账号：三条路都得走得通，且**默认那条不能弄丢余额**。"""

    def setUp(self):
        self.cred = {"account_code": "CC-999999-888888", "account_key": "old-key"}
        self.patches = [mock.patch.object(identity, "load_identity", return_value=None),
                        mock.patch.object(identity, "pending_recovery",
                                          return_value=self.cred)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_recover_uses_the_vault(self):
        ui = FakeUI(["恢复这个账号（CC-999999-888888）"])
        data = {"api_key": "k", "account_code": "CC-999999-888888"}
        with mock.patch.object(identity, "bind_account",
                               return_value=(data, None, "", None)) as bind, \
             mock.patch.object(identity, "adopt_identity"), \
             mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertTrue(result["ok"])
        bind.assert_called_once_with("CC-999999-888888", "old-key")
        reg.assert_not_called()

    def test_new_account_does_register(self):
        ui = FakeUI(["开一个新账号（旧账号的余额不会跟过来）"])
        with mock.patch.object(identity, "register_device",
                               return_value=ok_json()), \
             mock.patch.object(identity, "bind_account") as bind:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertTrue(result["ok"])
        bind.assert_not_called()

    def test_quit_does_nothing(self):
        ui = FakeUI(["先不装，退出"])
        with mock.patch.object(identity, "register_device") as reg, \
             mock.patch.object(identity, "bind_account") as bind:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertFalse(result["ok"])
        reg.assert_not_called()
        bind.assert_not_called()
        self.assertIn("--new-account", result["hint"])

    def test_new_account_flag_skips_the_question(self):
        ui = FakeUI(["现在领一个"])
        with mock.patch.object(identity, "register_device",
                               return_value=ok_json()) as reg:
            result = agent_install.bootstrap_identity(
                None, Args(new_account=True), ui)
        self.assertTrue(result["ok"])
        reg.assert_called_once()
        self.assertNotIn("以前用过", ui.questions[0][0])

    def test_recovery_without_a_key_explains_how_to_bind(self):
        self.cred["account_key"] = ""
        ui = FakeUI(["恢复这个账号（CC-999999-888888）"])
        with mock.patch.object(identity, "bind_account",
                               return_value=(None, "这个账号还需要账户密钥",
                                             "account_key_required", None)):
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertFalse(result["ok"])
        self.assertIn("--account-code CC-999999-888888", result["hint"])


class FreshMachineTests(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(identity, "load_identity", return_value=None),
                  mock.patch.object(identity, "pending_recovery", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_register_a_new_one(self):
        ui = FakeUI(["现在领一个"])
        with mock.patch.object(identity, "register_device",
                               return_value=ok_json()) as reg:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_code"], "CC-111111-222222")
        reg.assert_called_once()

    def test_i_have_a_code(self):
        ui = FakeUI(["我有账户码"])
        with mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertFalse(result["ok"])
        self.assertIn("--account-code", result["hint"])
        reg.assert_not_called()

    def test_quit(self):
        ui = FakeUI(["先不装，退出"])
        with mock.patch.object(identity, "register_device") as reg:
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertFalse(result["ok"])
        reg.assert_not_called()

    def test_free_text_is_understood(self):
        ui = FakeUI(["随便吧，给我开一个就行"])
        with mock.patch.object(identity, "register_device",
                               return_value=ok_json()) as reg:
            agent_install.bootstrap_identity(None, Args(), ui)
        reg.assert_called_once()

    def test_register_failure_is_reported_verbatim(self):
        ui = FakeUI(["现在领一个"])
        with mock.patch.object(identity, "register_device",
                               return_value="注册关闭了"):
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertFalse(result["ok"])
        self.assertIn("注册关闭了", result["message"])

    def test_recovered_flag_is_kept(self):
        ui = FakeUI(["现在领一个"])
        with mock.patch.object(identity, "register_device",
                               return_value=ok_json(recovered=True)):
            result = agent_install.bootstrap_identity(None, Args(), ui)
        self.assertTrue(result["recovered"])
        self.assertIn("余额", result["note"])


class AnswerParsingTests(unittest.TestCase):
    def test_strips_the_ui_prefix(self):
        self.assertEqual(agent_install._answer_text("用户回答： 你好 "), "你好")

    def test_exact_option_wins(self):
        self.assertEqual(agent_install._choose("用户回答：B 项", ["A 项", "B 项"], []), 1)

    def test_keywords(self):
        self.assertEqual(
            agent_install._choose("用户回答：算了先不装", ["领一个", "退出"],
                                  [("领",), ("退", "不装")], default=0), 1)

    def test_falls_back_to_default(self):
        self.assertEqual(agent_install._choose("用户回答：？？？", ["a", "b"], []), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
