# -*- coding: utf-8 -*-
"""server_main：参数、退出码、无 TTY 保护、账号那一步的"绝不默默开新号"。"""

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server_main  # noqa: E402
from server import claude_config as cc  # noqa: E402
from server.cli import (  # noqa: E402
    EXIT_ACCOUNT, EXIT_CONFIG, EXIT_ENV, EXIT_OK, CliContext, CliError,
)


def _args(*extra):
    return server_main.build_parser().parse_args(list(extra))


class ExitCodeTests(unittest.TestCase):
    def test_codes_are_distinct_and_stable(self):
        # 文档、install.sh 和用户都按这些数字沟通，改之前先想清楚
        self.assertEqual(
            (server_main.EXIT_OK, server_main.EXIT_ARGS, server_main.EXIT_ENV,
             server_main.EXIT_ACCOUNT, server_main.EXIT_NODE,
             server_main.EXIT_NPM, server_main.EXIT_CONFIG),
            (0, 2, 3, 4, 5, 6, 7))
        self.assertEqual(len({server_main.EXIT_OK, server_main.EXIT_ARGS,
                              server_main.EXIT_ENV, server_main.EXIT_ACCOUNT,
                              server_main.EXIT_NODE, server_main.EXIT_NPM,
                              server_main.EXIT_CONFIG}), 7)


class ParserTests(unittest.TestCase):
    def test_defaults(self):
        a = _args()
        self.assertEqual(a.dir, "")
        self.assertEqual(a.account_code, "")
        self.assertEqual(a.account_key, "")
        self.assertFalse(a.yes)
        self.assertFalse(a.dry_run)
        self.assertFalse(a.allow_root)
        self.assertFalse(a.new_account)
        self.assertFalse(a.repair)

    def test_help_and_version_exit_zero(self):
        self.assertEqual(server_main.main(["--help"]), 0)
        self.assertEqual(server_main.main(["--version"]), 0)

    def test_bad_argument_exits_two(self):
        self.assertEqual(server_main.main(["--nope"]), 2)

    def test_cli_error_maps_to_exit_code(self):
        with mock.patch.object(server_main, "_run",
                               side_effect=CliError("坏了", EXIT_CONFIG)):
            self.assertEqual(server_main.main(["--yes"]), EXIT_CONFIG)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _ctx(self, dry_run=True):
        return CliContext(home=self.home, env={"PATH": ""}, quiet=True,
                          dry_run=dry_run)

    def test_rejects_non_linux(self):
        ctx = self._ctx()
        with mock.patch.object(sys, "platform", "win32"):
            with self.assertRaises(CliError) as cm:
                server_main._preflight(ctx, _args())
        self.assertEqual(cm.exception.exit_code, EXIT_ENV)
        self.assertIn("Linux", cm.exception.message)

    def test_rejects_root_unless_allowed(self):
        ctx = self._ctx()
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.object(os, "geteuid", lambda: 0, create=True):
            with self.assertRaises(CliError) as cm:
                server_main._preflight(ctx, _args())
            self.assertEqual(cm.exception.exit_code, EXIT_ENV)
            # 明确给 root 一个出口，但不推荐
            self.assertIn("--allow-root", cm.exception.hint)
            server_main._preflight(ctx, _args("--allow-root"))   # 不抛

    def test_dry_run_creates_nothing(self):
        ctx = self._ctx(dry_run=True)
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.object(os, "geteuid", lambda: 1000, create=True):
            info = server_main._preflight(ctx, _args())
        self.assertIn(info["arch"], ("x64", "arm64"))
        self.assertFalse((self.home / ".local").exists())

    def test_unwritable_prefix_is_reported(self):
        ctx = self._ctx(dry_run=False)
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.object(os, "geteuid", lambda: 1000, create=True), \
             mock.patch.object(Path, "mkdir",
                               side_effect=PermissionError("只读文件系统")):
            with self.assertRaises(CliError) as cm:
                server_main._preflight(ctx, _args())
        self.assertEqual(cm.exception.exit_code, EXIT_ENV)
        self.assertIn("不可写", cm.exception.message)


class NoTtyTests(unittest.TestCase):
    def test_no_tty_does_not_hang_or_claim_success(self):
        with mock.patch.object(server_main, "_is_tty", return_value=False), \
             mock.patch.object(server_main, "_relaunch_in_terminal", return_value=False), \
             mock.patch("platform_support.notify", lambda *a, **k: None, create=True):
            code = server_main.main([])
        self.assertEqual(code, EXIT_ENV)      # 不是 0：什么都没装成

    def test_no_tty_logs_something(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "cli.log"
        with mock.patch.object(server_main, "LOG_FILE", log), \
             mock.patch.object(server_main, "_is_tty", return_value=False), \
             mock.patch.object(server_main, "_relaunch_in_terminal", return_value=False), \
             mock.patch("platform_support.notify", lambda *a, **k: None, create=True):
            server_main.main([])
        self.assertTrue(log.exists())
        self.assertIn("no tty", log.read_text(encoding="utf-8"))


class AccountStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.ctx = CliContext(home=self.home, env={"PATH": ""}, quiet=True)

    def test_existing_identity_is_reused_and_never_reregistered(self):
        with mock.patch.object(server_main.identity, "load_identity",
                               return_value={"api_key": "k", "account_code": "AB-1"}), \
             mock.patch.object(server_main.identity, "register_device") as reg:
            info = server_main._account_step(self.ctx, _args("--yes"))
        self.assertTrue(info["reused"])
        self.assertEqual(info["account_code"], "AB-1")
        reg.assert_not_called()

    def test_account_code_binds_instead_of_registering(self):
        data = {"api_key": "k", "account_code": "AB-2"}
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "bind_account",
                               return_value=(data, None, "", None)) as bind, \
             mock.patch.object(server_main.identity, "adopt_identity") as adopt, \
             mock.patch.object(server_main.identity, "register_device") as reg:
            info = server_main._account_step(
                self.ctx, _args("--account-code", "AB-2", "--account-key", "KEY"))
        bind.assert_called_once_with("AB-2", "KEY")
        adopt.assert_called_once_with(data)
        reg.assert_not_called()
        self.assertEqual(info["account_code"], "AB-2")

    def test_bind_failure_is_exit_4(self):
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "bind_account",
                               return_value=(None, "账户密钥不对", "account_key_invalid", None)):
            with self.assertRaises(CliError) as cm:
                server_main._account_step(
                    self.ctx, _args("--account-code", "AB-2", "--account-key", "KEY"))
        self.assertEqual(cm.exception.exit_code, EXIT_ACCOUNT)
        self.assertIn("账户密钥不对", cm.exception.message)

    def test_account_key_never_leaks_into_error_text(self):
        secret = "SECRET-KEY-DO-NOT-PRINT"
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "bind_account",
                               return_value=(None, "账户密钥不对", "", None)):
            with self.assertRaises(CliError) as cm:
                server_main._account_step(
                    self.ctx, _args("--account-code", "AB-2", "--account-key", secret))
        self.assertNotIn(secret, cm.exception.message)
        self.assertNotIn(secret, cm.exception.hint)

    def test_vault_hint_blocks_silent_new_account(self):
        # ★ 保险箱里还有旧账号时默默注册 = 用户余额直接丢
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "pending_recovery",
                               return_value={"account_code": "OLD-9", "account_key": "x"}), \
             mock.patch.object(server_main.identity, "register_device") as reg:
            with self.assertRaises(CliError) as cm:
                server_main._account_step(self.ctx, _args("--yes"))
        self.assertEqual(cm.exception.exit_code, EXIT_ACCOUNT)
        self.assertIn("OLD-9", cm.exception.message)
        reg.assert_not_called()

    def test_new_account_flag_overrides_hint(self):
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "pending_recovery",
                               return_value={"account_code": "OLD-9"}), \
             mock.patch.object(server_main.identity, "register_device",
                               return_value=json.dumps({"account_code": "NEW-1"})):
            info = server_main._account_step(
                self.ctx, _args("--yes", "--new-account"))
        self.assertEqual(info["account_code"], "NEW-1")

    def test_register_failure_is_exit_4(self):
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "pending_recovery", return_value=None), \
             mock.patch.object(server_main.identity, "register_device",
                               return_value="注册暂时关闭"):
            with self.assertRaises(CliError) as cm:
                server_main._account_step(self.ctx, _args("--yes"))
        self.assertEqual(cm.exception.exit_code, EXIT_ACCOUNT)
        self.assertIn("注册暂时关闭", cm.exception.message)

    def test_dry_run_does_not_register(self):
        ctx = CliContext(home=self.home, env={"PATH": ""}, quiet=True, dry_run=True)
        with mock.patch.object(server_main.identity, "load_identity", return_value=None), \
             mock.patch.object(server_main.identity, "pending_recovery", return_value=None), \
             mock.patch.object(server_main.identity, "register_device") as reg:
            info = server_main._account_step(ctx, _args("--yes", "--dry-run"))
        self.assertTrue(info["planned"])
        reg.assert_not_called()


class ConfigStepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_dry_run_writes_nothing(self):
        ctx = CliContext(home=self.home, env={"PATH": ""}, quiet=True, dry_run=True)
        server_main._config_step(ctx, _args("--dry-run"))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_identity_error_is_exit_7(self):
        ctx = CliContext(home=self.home, env={"PATH": ""}, quiet=True)
        with mock.patch("server.tools_fixed.bypass_region_check", return_value="ok"), \
             mock.patch("server.tools_fixed.set_model_config",
                        return_value="错误：身份文件里缺 api_key"):
            with self.assertRaises(CliError) as cm:
                server_main._config_step(ctx, _args())
        self.assertEqual(cm.exception.exit_code, EXIT_CONFIG)

    def test_write_error_is_exit_7(self):
        ctx = CliContext(home=self.home, env={"PATH": ""}, quiet=True)
        with mock.patch("server.tools_fixed.bypass_region_check",
                        side_effect=OSError("只读")):
            with self.assertRaises(CliError) as cm:
                server_main._config_step(ctx, _args())
        self.assertEqual(cm.exception.exit_code, EXIT_CONFIG)


class RepairTests(unittest.TestCase):
    """`--repair`：只对账配置，不装 Node、不装 claude、不注册账号。"""

    IDENT = {"api_key": "sk-test-repair", "api_base": "https://aikaui.com/v1",
             "model": "pro-x", "fast_model": "fast-x", "account_code": "AB-1"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.config = self.root / ".claude.json"
        self.settings = self.root / ".claude" / "settings.json"
        self._saved = (cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS)
        cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS = self.config, self.settings
        self.addCleanup(self._restore)

    def _restore(self):
        cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS = self._saved

    def run_repair(self, *extra, identity="default", tty=False):
        ident = self.IDENT if identity == "default" else identity
        with ExitStack() as stack:
            guard = stack.enter_context(mock.patch.object(server_main, "_no_tty_guard"))
            node = stack.enter_context(mock.patch.object(server_main.node_setup, "ensure_node"))
            claude = stack.enter_context(
                mock.patch.object(server_main.claude_setup, "ensure_claude"))
            reg = stack.enter_context(
                mock.patch.object(server_main.identity, "register_device"))
            stack.enter_context(mock.patch.object(server_main, "_is_tty", return_value=tty))
            stack.enter_context(mock.patch.object(server_main.identity, "load_identity",
                                                  return_value=ident))
            code = server_main.main(["--repair", *extra])
        guard.assert_not_called()          # 没终端也不重开终端、不假装成功
        node.assert_not_called()
        claude.assert_not_called()
        reg.assert_not_called()
        return code

    def test_conflicts_with_account_flags(self):
        self.assertEqual(server_main.main(["--repair", "--account-code", "AB-1"]),
                         server_main.EXIT_ARGS)
        self.assertEqual(server_main.main(["--repair", "--new-account"]),
                         server_main.EXIT_ARGS)

    def test_no_identity_exits_4(self):
        self.assertEqual(self.run_repair(identity=None), EXIT_ACCOUNT)
        self.assertFalse(self.config.exists())

    def test_repairs_without_a_tty(self):
        self.assertEqual(self.run_repair(), EXIT_OK)
        self.assertIs(self.config.exists(), True)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8"))
                         ["env"]["ANTHROPIC_AUTH_TOKEN"], self.IDENT["api_key"])

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_repair("--dry-run"), EXIT_OK)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.settings.exists())

    def test_broken_file_exits_7(self):
        self.config.write_text("{ broken", encoding="utf-8")
        self.assertEqual(self.run_repair(), EXIT_CONFIG)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "{ broken")

    def test_foreign_provider_is_reported_not_forced(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}}), encoding="utf-8")
        raw = self.settings.read_bytes()
        self.assertEqual(self.run_repair(), EXIT_OK)
        self.assertEqual(self.settings.read_bytes(), raw)
        self.assertFalse(self.config.exists(),
                         "另一份是别人的中转，不能顺手再建一份我们的配置")

    def test_missing_base_is_exit_7(self):
        self.assertEqual(self.run_repair(identity={"api_key": "k"}), EXIT_CONFIG)


class DryRunFlowTests(unittest.TestCase):
    def test_whole_run_touches_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)

        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.object(os, "geteuid", lambda: 1000, create=True), \
             mock.patch.object(Path, "home", return_value=home), \
             mock.patch.object(server_main.identity, "load_identity",
                               return_value={"api_key": "k", "account_code": "AB-1"}):
            code = server_main.main(["--dry-run", "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(list(home.iterdir()), [])

    def test_report_never_prints_secrets(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        ctx = CliContext(home=home, env={"PATH": ""}, quiet=True)
        args = _args()
        import io
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            server_main._report(ctx, args,
                                {"version": "v24", "installed": True},
                                {"version": "2.1.310"},
                                {"account_code": "AB-1"})
        text = out.getvalue()
        self.assertIn("AB-1", text)
        self.assertIn("claude", text)
        self.assertNotIn("api_key", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
