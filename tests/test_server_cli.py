# -*- coding: utf-8 -*-
"""server_main：参数、退出码、无 TTY 保护、账号那一步的"绝不默默开新号"。"""

import io
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
    EXIT_ACCOUNT, EXIT_ARGS, EXIT_CONFIG, EXIT_ENV, EXIT_OK, CliContext, CliError,
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

    def test_root_is_allowed_with_a_warning(self):
        """root 默认放行。容器里常常只有 root，之前那道开关把人卡在第一步
        （2026-10-27 两次），挡住的不是风险，是安装本身。"""
        ctx = self._ctx()
        clean_env = {k: v for k, v in os.environ.items() if k != "AIKAUI_ALLOW_ROOT"}
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.dict(os.environ, clean_env, clear=True), \
             mock.patch.object(ctx, "log") as log, \
             mock.patch.object(os, "geteuid", lambda: 0, create=True):
            server_main._preflight(ctx, _args())                  # 不抛
        said = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn("/root", said, "得说清楚东西落在哪")

    def test_allow_root_flag_is_still_accepted(self):
        """老的 install.sh / 文档还在传 --allow-root，别让它变成错误。"""
        ctx = self._ctx()
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.object(server_main.node_setup, "detect_node_arch",
                               return_value="x64"), \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(os, "geteuid", lambda: 0, create=True):
            server_main._preflight(ctx, _args("--allow-root"))    # 不抛

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

    def test_foreign_provider_is_a_failure_not_silence(self):
        """指向别人的中转 = 没配好。以前这里返回 0，等于告诉用户"一切正常"，
        可终端根本不会走我们的通道（用户 52 就是这么漏过去的）。"""
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}}), encoding="utf-8")
        raw = self.settings.read_bytes()
        self.assertEqual(self.run_repair(), EXIT_CONFIG)
        self.assertEqual(self.settings.read_bytes(), raw)      # 不加 --force 不动它
        self.assertFalse(self.config.exists(),
                         "另一份是别人的中转，不能顺手再建一份我们的配置")

    def test_force_takes_over_and_keeps_a_backup(self):
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid",
                     "ANTHROPIC_AUTH_TOKEN": "sk-theirs"}}), encoding="utf-8")
        self.assertEqual(self.run_repair("--force"), EXIT_OK)
        merged = json.loads(self.settings.read_text(encoding="utf-8"))["env"]
        self.assertEqual(merged["ANTHROPIC_BASE_URL"], "https://aikaui.com")
        bak = self.settings.with_name(self.settings.name + ".aikaui-bak")
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8"))["env"]
                         ["ANTHROPIC_BASE_URL"], "https://example.invalid")

    def test_missing_base_is_exit_7(self):
        self.assertEqual(self.run_repair(identity={"api_key": "k"}), EXIT_CONFIG)


class BillingModeArgTests(unittest.TestCase):
    """--balance / --recharge 是"只做一件事"的模式，参数组合要先挡住。"""

    def test_defaults(self):
        a = _args()
        self.assertFalse(a.balance)
        self.assertIsNone(a.recharge)
        self.assertEqual(a.channel, "")

    def test_only_one_mode_at_a_time(self):
        for argv in (["--repair", "--balance"],
                     ["--repair", "--recharge", "20"],
                     ["--balance", "--recharge", "20"]):
            self.assertEqual(server_main.main(argv), EXIT_ARGS, argv)

    def test_channel_only_makes_sense_with_recharge(self):
        self.assertEqual(server_main.main(["--channel", "wechat"]), EXIT_ARGS)

    def test_recharge_amount_is_bounded(self):
        for bad in ("0", "-5", "10001"):
            self.assertEqual(server_main.main(["--recharge", bad]), EXIT_ARGS, bad)

    def test_read_only_modes_refuse_account_flags(self):
        """--account-code 是"装的时候绑账号"，和只查账/只充值的路径冲突。"""
        self.assertEqual(server_main.main(["--balance", "--account-code", "AB-1"]),
                         EXIT_ARGS)
        self.assertEqual(server_main.main(["--recharge", "20", "--new-account"]),
                         EXIT_ARGS)


class BalanceTests(unittest.TestCase):
    IDENT = {"api_key": "k", "account_code": "CC-1", "user_id": 7}

    def run_balance(self, *extra, balance=None, usage=None, identity="default"):
        ident = self.IDENT if identity == "default" else identity
        out = io.StringIO()
        with mock.patch.object(server_main, "_no_tty_guard") as guard, \
             mock.patch.object(server_main.identity, "load_identity",
                               return_value=ident), \
             mock.patch.object(server_main.api, "query_balance",
                               return_value=json.dumps(balance or {})) as bal, \
             mock.patch.object(server_main.api, "query_usage",
                               return_value=json.dumps(usage or {})) as use, \
             mock.patch.object(sys, "stdout", out):
            code = server_main.main(["--balance", *extra])
        guard.assert_not_called()          # 没终端也照跑，不重开终端
        return code, out.getvalue(), bal, use

    def test_without_an_account_it_says_so_and_queries_nothing(self):
        code, out, bal, use = self.run_balance(identity=None)
        self.assertEqual(code, EXIT_ACCOUNT)
        bal.assert_not_called()
        use.assert_not_called()

    def test_shows_balance_and_usage(self):
        code, out, _, _ = self.run_balance(
            balance={"account_code": "CC-1", "yuan": "5.00", "quota": 2500000},
            usage={"today": {"tokens": 12000}, "month": {"tokens": 345000}})
        self.assertEqual(code, EXIT_OK)
        self.assertIn("¥5.00", out)
        self.assertIn("12,000 tokens", out)
        self.assertIn("345,000 tokens", out)

    def test_empty_balance_points_at_both_ways_to_pay(self):
        code, out, _, _ = self.run_balance(balance={"account_code": "CC-1", "yuan": "0"})
        self.assertEqual(code, EXIT_OK)
        self.assertIn("--recharge 20", out)       # 就地充
        self.assertIn("桌面版", out)               # 或者绑桌面版
        self.assertIn("CC-1", out)

    def test_healthy_balance_does_not_nag(self):
        _, out, _, _ = self.run_balance(balance={"account_code": "CC-1", "yuan": "30"})
        self.assertNotIn("余额不够", out)

    def test_a_broken_usage_endpoint_does_not_hide_the_balance(self):
        """用量查不到是小事，余额该照常显示（quiet 调用）。"""
        code, out, _, _ = self.run_balance(
            balance={"account_code": "CC-1", "yuan": "8.00"},
            usage={"error": "试得太频繁了"})
        self.assertEqual(code, EXIT_OK)
        self.assertIn("¥8.00", out)

    def test_balance_error_is_reported_not_swallowed(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(server_main, "_no_tty_guard"), \
             mock.patch.object(server_main.identity, "load_identity",
                               return_value=self.IDENT), \
             mock.patch.object(server_main.api, "query_balance",
                               return_value=json.dumps({"error": "没找到这个账户码"})), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err):
            code = server_main.main(["--balance"])
        self.assertNotEqual(code, EXIT_OK)
        self.assertIn("没找到这个账户码", err.getvalue())

    def test_dry_run_does_not_touch_the_network(self):
        code, out, bal, _ = self.run_balance("--dry-run")
        self.assertEqual(code, EXIT_OK)
        bal.assert_not_called()
        self.assertIn("--dry-run", out)


class RechargeTests(unittest.TestCase):
    IDENT = {"api_key": "k", "account_code": "CC-1", "user_id": 7}
    ORDER = {"order_no": "AI1", "amount": 20, "money": "20.00",
             "channel": "wechat", "qrcode": "weixin://wxpay/bizpayurl?pr=abc",
             "img": "https://aikaui.com/qrcode/abc.jpg"}

    def run_recharge(self, *extra, order="default", identity="default"):
        ident = self.IDENT if identity == "default" else identity
        payload = json.dumps(self.ORDER if order == "default" else order)
        out = io.StringIO()
        with mock.patch.object(server_main, "_no_tty_guard") as guard, \
             mock.patch.object(server_main.identity, "load_identity",
                               return_value=ident), \
             mock.patch.object(server_main.api, "create_recharge",
                               return_value=payload) as create, \
             mock.patch.object(sys, "stdout", out):
            code = server_main.main(["--recharge", "20", *extra])
        guard.assert_not_called()
        return code, out.getvalue(), create

    def test_prints_the_qr_and_the_order(self):
        code, out, create = self.run_recharge()
        self.assertEqual(code, EXIT_OK)
        create.assert_called_once_with(20, None)
        self.assertIn("AI1", out)
        self.assertIn("█", out)                      # 终端里画出来的二维码
        self.assertIn("aikaui.com/qrcode/abc.jpg", out)

    def test_channel_is_passed_through(self):
        _, _, create = self.run_recharge("--channel", "wechat")
        create.assert_called_once_with(20, "wechat")

    def test_without_an_account_it_does_not_create_an_order(self):
        code, _, create = self.run_recharge(identity=None)
        self.assertEqual(code, EXIT_ACCOUNT)
        create.assert_not_called()

    def test_creation_error_is_a_failure_not_a_fake_order(self):
        err = io.StringIO()
        with mock.patch.object(server_main, "_no_tty_guard"), \
             mock.patch.object(server_main.identity, "load_identity",
                               return_value=self.IDENT), \
             mock.patch.object(server_main.api, "create_recharge",
                               return_value=json.dumps({"error": "支付平台那边开单失败"})), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", err):
            code = server_main.main(["--recharge", "20"])
        self.assertNotEqual(code, EXIT_OK)
        self.assertIn("支付平台那边开单失败", err.getvalue())

    def test_dry_run_creates_nothing(self):
        code, out, create = self.run_recharge("--dry-run")
        self.assertEqual(code, EXIT_OK)
        create.assert_not_called()
        self.assertIn("--dry-run", out)


class InstallReportTests(unittest.TestCase):
    """装完最后那屏：要能一眼看到钱还剩多少、下一步敲什么。"""

    def report(self, balance):
        out = io.StringIO()
        ctx = CliContext(home=Path(tempfile.mkdtemp()), env={"PATH": ""})
        with mock.patch.object(server_main, "_peek_balance", return_value=balance), \
             mock.patch.object(server_main.claude_setup, "needs_path_reload",
                               return_value=False), \
             mock.patch.object(sys, "stdout", out):
            server_main._report(ctx, _args(), {"version": "v24"},
                                {"version": "2.1.310"},
                                {"account_code": "CC-1"})
        return out.getvalue()

    def test_lists_the_commands_that_work_here(self):
        text = self.report({"yuan": "3.00", "account_code": "CC-1"})
        self.assertIn("--balance", text)
        self.assertIn("--recharge 20", text)
        self.assertIn("--repair", text)

    def test_shows_the_balance(self):
        self.assertIn("¥3.00", self.report({"yuan": "3.00"}))

    def test_zero_balance_warns_right_away(self):
        text = self.report({"yuan": "0", "account_code": "CC-1"})
        self.assertIn("余额不够", text)
        self.assertIn("桌面版", text)

    def test_unreachable_balance_does_not_break_the_ending(self):
        text = self.report(None)
        self.assertIn("✅ 装好了", text)
        self.assertNotIn("None", text)

    def test_no_secrets_in_the_ending(self):
        text = self.report({"yuan": "0", "account_code": "CC-1"})
        self.assertNotIn("api_key", text)
        self.assertNotIn("sk-", text)


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
