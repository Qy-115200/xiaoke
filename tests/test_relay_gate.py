# -*- coding: utf-8 -*-
"""relay_gate：别的中转一律拦住。

这个判断决定"客户端到底能不能用"，所以每条边界都要钉住：
指向别处要拦、指向我们（带不带 /v1）不拦、没配不拦、文件坏了不拦（判断不了
就不替用户做主）；切换要借 apply() 留备份；给用户看的文字里不能出现 token。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import relay_gate  # noqa: E402

OURS = "https://aikaui.com/v1"
THEIRS = "http://118.190.105.66"


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.config = self.root / ".claude.json"
        self.settings = self.root / ".claude" / "settings.json"

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _check(self, identity=None):
        return relay_gate.check(identity or {"api_base": OURS},
                                config_path=self.config,
                                settings_path=self.settings)

    def test_nothing_configured_is_not_blocked(self):
        gate = self._check()
        self.assertFalse(gate.blocked)
        self.assertEqual(gate.lines(), [])

    def test_foreign_in_settings_is_blocked(self):
        self._write(self.settings, {"env": {"ANTHROPIC_BASE_URL": THEIRS}})
        gate = self._check()
        self.assertTrue(gate.blocked)
        self.assertEqual(gate.conflicts[0].path, self.settings)
        self.assertEqual(gate.conflicts[0].host, "118.190.105.66")

    def test_foreign_in_config_json_is_blocked_too(self):
        self._write(self.config, {"env": {"ANTHROPIC_BASE_URL": THEIRS}})
        self.assertTrue(self._check().blocked)

    def test_ours_with_or_without_v1_is_fine(self):
        self._write(self.settings, {"env": {"ANTHROPIC_BASE_URL": "https://aikaui.com/v1"}})
        self._write(self.config, {"env": {"ANTHROPIC_BASE_URL": "https://aikaui.com/"}})
        self.assertFalse(self._check().blocked)

    def test_token_without_base_is_not_a_conflict(self):
        self._write(self.settings, {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-x"}})
        self.assertFalse(self._check().blocked)

    def test_broken_file_is_not_judged(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text("{ broken", encoding="utf-8")
        gate = self._check()
        self.assertFalse(gate.blocked, "读不出来就别拦，repair 会单独报")
        self.assertEqual(gate.unreadable[0][1], "bad-json")

    def test_env_that_is_not_an_object_is_not_judged(self):
        self._write(self.settings, {"env": ["nope"]})
        gate = self._check()
        self.assertFalse(gate.blocked)
        self.assertEqual(gate.unreadable[0][1], "bad-env")

    def test_without_identity_falls_back_to_the_site_url(self):
        self._write(self.settings, {"env": {"ANTHROPIC_BASE_URL": "https://aikaui.com/v1"}})
        self.assertFalse(self._check(identity={}).blocked)
        self.assertEqual(relay_gate.expected_base({}), "https://aikaui.com")

    def test_host_only_never_the_token(self):
        self._write(self.settings, {"env": {"ANTHROPIC_BASE_URL": THEIRS,
                                            "ANTHROPIC_AUTH_TOKEN": "sk-super-secret"}})
        gate = self._check()
        text = "\n".join(gate.lines())
        self.assertIn("118.190.105.66", text)
        self.assertNotIn("sk-super-secret", text)


class HostTests(unittest.TestCase):
    def test_host_parsing(self):
        self.assertEqual(relay_gate.host_of("http://118.190.105.66"),
                         "118.190.105.66")
        self.assertEqual(relay_gate.host_of("https://relay.example.com:8443/v1"),
                         "relay.example.com:8443")
        self.assertEqual(relay_gate.host_of(""), "")


class SwitchTests(unittest.TestCase):
    def test_uses_the_existing_identity_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": THEIRS}}),
                                encoding="utf-8")
            ident = {"api_key": "sk-test", "api_base": OURS}
            with mock.patch("server.claude_config.CLAUDE_SETTINGS", settings), \
                 mock.patch("server.claude_config.CLAUDE_CONFIG",
                            Path(tmp) / ".claude.json"):
                ok, message = relay_gate.switch_to_ours(ident)
            self.assertTrue(ok, message)
            bak = settings.with_name(settings.name + ".aikaui-bak")
            self.assertTrue(bak.exists(), "换之前必须留一份原样")

    def test_registers_when_there_is_no_identity_yet(self):
        with mock.patch("server.identity.load_identity",
                        side_effect=[None, {"api_key": "sk-new", "api_base": OURS}]), \
             mock.patch("server.identity.ensure_identity", return_value=True), \
             mock.patch("server.claude_config.apply", return_value=(True, "已更新")) as ap:
            ok, message = relay_gate.switch_to_ours()
        self.assertTrue(ok)
        self.assertEqual(message, "已更新")
        ap.assert_called_once_with({"api_key": "sk-new", "api_base": OURS})

    def test_registration_failure_is_reported_not_swallowed(self):
        with mock.patch("server.identity.load_identity", return_value=None), \
             mock.patch("server.identity.ensure_identity", return_value=False), \
             mock.patch("server.identity.last_register_error", return_value="注册太频繁"):
            ok, message = relay_gate.switch_to_ours()
        self.assertFalse(ok)
        self.assertIn("注册太频繁", message)

    def test_half_written_config_is_not_reported_as_success(self):
        """apply() 是逐文件写的：一份成功一份失败时它照样说 ok，这里必须再判一次，
        不然"已换成我们的"是假的，终端可能还在走别人的通道。"""
        with mock.patch("server.claude_config.apply", return_value=(True, "已更新 一份")), \
             mock.patch.object(relay_gate, "check") as chk:
            chk.return_value = relay_gate.Gate(
                conflicts=[relay_gate.Conflict(path=Path("/home/u/.claude/settings.json"),
                                               host="118.190.105.66")])
            ok, message = relay_gate.switch_to_ours({"api_key": "k", "api_base": OURS})
        self.assertFalse(ok)
        self.assertIn("settings.json", message)
        self.assertIn("手动改", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
