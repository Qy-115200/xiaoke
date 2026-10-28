# -*- coding: utf-8 -*-
"""安装会话：账号 → 模型 → 验收 → 收尾。全部用假 client / 假 UI 跑，不需要 tty。

这里钉的是用户这次提的那几条：
  · 干活时界面是 BUSY（不许打字），说完话才回 IDLE；
  · 模型调不通就**如实说连不上**，不能含糊成"安装失败"；
  · 没通过验收就不许说装好了；
  · 装完才打开 claude。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_core  # noqa: E402
from server import agent_install, identity  # noqa: E402
from server import agent_tools  # noqa: E402
from server.cli import EXIT_ACCOUNT, EXIT_ENV, EXIT_FAILED  # noqa: E402
from server.prompts_server import CONTINUE_TASK  # noqa: E402
from server.term_chat import Outcome  # noqa: E402


class FakeUI:
    def __init__(self):
        self.logs = []
        self.events = []
        self.busy = []
        self.outcome = None

    def ui_log(self, text):
        self.logs.append(str(text))

    def emit(self, event):
        self.events.append(event)

    def ui_busy(self, flag):
        self.busy.append(bool(flag))

    def finish(self, outcome=None):
        self.outcome = outcome or Outcome()

    @property
    def text(self):
        return "\n".join(self.logs)


class Args:
    node_version = ""
    claude_version = ""
    force = False
    dir = ""
    yes = False
    account_code = ""
    account_key = ""
    new_account = False


GOOD_ACCOUNT = {"ok": True, "account_code": "CC-111111-222222", "recovered": False,
                "note": "", "message": "", "hint": "", "exit_code": 0}


def run_session(run_one_task, *, account=None, preflight=None, ui=None, state=None):
    ui = ui or FakeUI()
    state = state if state is not None else {}
    account = account if account is not None else dict(GOOD_ACCOUNT)
    with mock.patch.object(agent_install, "bootstrap_identity", return_value=account), \
         mock.patch.object(agent_core, "run_one_task", side_effect=run_one_task):
        agent_install.session_worker(None, Args(), ui, state,
                                     preflight or (lambda ctx, args: {"arch": "x86_64"}))
    return ui, state


class HappyPathTests(unittest.TestCase):
    def test_finishing_sets_a_clean_outcome(self):
        state = {}

        def fake(messages, task, **kw):
            state["finished"] = True
            return True

        ui, _ = run_session(fake, state=state)
        self.assertIsNotNone(ui.outcome)
        self.assertIsNone(ui.outcome.error)
        self.assertFalse(ui.outcome.aborted)

    def test_busy_wraps_the_model_work(self):
        def fake(messages, task, **kw):
            return True        # 没装完 → 非交互时会收场报错

        ui, _ = run_session(fake)
        self.assertEqual(ui.busy[0], True, "开干之前先进入 BUSY")
        self.assertEqual(ui.busy[-1], False, "干完要回到 IDLE")

    def test_the_first_task_is_the_initial_one(self):
        seen = []

        def fake(messages, task, **kw):
            seen.append(task)
            messages.append({"role": "assistant", "content": "好"})
            return True

        run_session(fake)
        self.assertIn("装好", seen[0])

    def test_state_gets_the_account_code(self):
        state = {}

        def fake(messages, task, **kw):
            state["finished"] = True
            return True

        run_session(fake, state=state)
        self.assertEqual(state["account_code"], "CC-111111-222222")


class StopBeforeTheModelTests(unittest.TestCase):
    def test_preflight_failure_never_reaches_the_model(self):
        from server.cli import CliError

        def boom(ctx, args):
            raise CliError("这个安装器是给 Linux 服务器用的", EXIT_ENV, hint="去下载桌面版")

        ui = FakeUI()
        with mock.patch.object(agent_core, "run_one_task") as run:
            agent_install.session_worker(None, Args(), ui, {}, preflight=boom)
        run.assert_not_called()
        self.assertEqual(ui.outcome.exit_code, EXIT_ENV)
        self.assertIn("Linux", ui.outcome.error)
        self.assertIn("桌面版", ui.text)

    def test_account_failure_never_reaches_the_model(self):
        bad = {"ok": False, "account_code": "", "recovered": False, "note": "",
               "message": "没有账号，安装停在这里", "hint": "用 --account-code 绑一个",
               "exit_code": EXIT_ACCOUNT}
        ui = FakeUI()
        with mock.patch.object(agent_install, "bootstrap_identity", return_value=bad), \
             mock.patch.object(agent_core, "run_one_task") as run:
            agent_install.session_worker(None, Args(), ui, {})
        run.assert_not_called()
        self.assertEqual(ui.outcome.exit_code, EXIT_ACCOUNT)
        self.assertIn("--account-code", ui.text)


class ModelFailureTests(unittest.TestCase):
    def _fail_with(self, reason, detail=""):
        def fake(messages, task, **kw):
            kw["status"].update({"error": reason, "detail": detail})
            return False
        return run_session(fake)

    def test_model_unreachable_says_so(self):
        ui, _ = self._fail_with("model", "Connection refused")
        self.assertIn("连不上模型", ui.outcome.error)
        self.assertEqual(ui.outcome.exit_code, EXIT_FAILED)
        self.assertIn("aikaui.com", ui.text)

    def test_balance_points_at_recharge(self):
        ui, _ = self._fail_with("balance")
        self.assertIn("--recharge 20", ui.text)
        self.assertEqual(ui.outcome.exit_code, EXIT_ACCOUNT)

    def test_max_steps_is_not_reported_as_success(self):
        ui, _ = self._fail_with("max_steps")
        self.assertIn("步数", ui.outcome.error)
        self.assertEqual(ui.outcome.exit_code, EXIT_FAILED)

    def test_bad_response_shows_a_hint_of_the_detail(self):
        ui, _ = self._fail_with("bad_response", "Not Found")
        self.assertIn("Not Found", ui.text)

    def test_no_identity(self):
        ui, _ = self._fail_with("no_identity", "没有凭证")
        self.assertEqual(ui.outcome.exit_code, EXIT_ACCOUNT)


class UnfinishedTests(unittest.TestCase):
    def test_non_interactive_stops_instead_of_waiting(self):
        def fake(messages, task, **kw):
            messages.append({"role": "assistant", "content": "我看看"})
            return True

        with mock.patch.object(agent_install, "_can_keep_talking", return_value=False):
            ui, _ = run_session(fake)
        self.assertEqual(ui.outcome.error, "安装没有完成")
        self.assertEqual(ui.outcome.exit_code, EXIT_FAILED)

    def test_interactive_user_can_keep_talking(self):
        calls = []

        def fake(messages, task, **kw):
            calls.append(task)
            if len(calls) == 2:
                kw["status"]  # noqa: B018
                state["finished"] = True
            return True

        state = {}
        with mock.patch.object(agent_install, "_can_keep_talking", return_value=True), \
             mock.patch.object(agent_install.user_input_queue, "get",
                               return_value="继续，刚才卡在 npm"):
            ui, _ = run_session(fake, state=state)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].startswith(CONTINUE_TASK))
        self.assertIn("继续，刚才卡在 npm", calls[1])
        self.assertIsNone(ui.outcome.error)

    def test_abort_while_waiting_for_typing(self):
        def fake(messages, task, **kw):
            return True

        with mock.patch.object(agent_install, "_can_keep_talking", return_value=True), \
             mock.patch.object(agent_install.user_input_queue, "get", return_value=None):
            ui, _ = run_session(fake)
        self.assertTrue(ui.outcome.aborted)

    def test_an_exception_inside_the_thread_does_not_escape(self):
        def fake(messages, task, **kw):
            raise RuntimeError("炸了")

        ui, _ = run_session(fake)
        self.assertIn("炸了", ui.outcome.error)
        self.assertEqual(ui.outcome.exit_code, EXIT_FAILED)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.claude = tmp / "claude"
        self.claude.write_text("#!/bin/sh\n", encoding="utf-8")

        body = json.dumps({"hasCompletedOnboarding": True,
                           "env": {"ANTHROPIC_BASE_URL": "https://x/api",
                                   "ANTHROPIC_AUTH_TOKEN": "sk-donotprint"}})
        self.config = tmp / "config.json"
        self.settings = tmp / "settings.json"
        self.config.write_text(body, encoding="utf-8")
        self.settings.write_text(body, encoding="utf-8")

        for p in (mock.patch.object(agent_tools, "CLAUDE_CONFIG", self.config),
                  mock.patch.object(agent_tools, "CLAUDE_SETTINGS", self.settings),
                  mock.patch.object(identity, "load_identity",
                                    return_value={"api_key": "k"})):
            p.start()
            self.addCleanup(p.stop)
        self.state = {"node": {"version": "v22.11.0"},
                      "claude": {"claude": str(self.claude)}}

    def test_everything_present(self):
        self.assertTrue(agent_install.verify_installed(self.state))

    def test_missing_node(self):
        self.state["node"] = {}
        self.assertFalse(agent_install.verify_installed(self.state))

    def test_missing_claude_binary(self):
        self.state["claude"] = {"claude": str(self.claude) + "-gone"}
        self.assertFalse(agent_install.verify_installed(self.state))

    def test_missing_identity(self):
        with mock.patch.object(identity, "load_identity", return_value=None):
            self.assertFalse(agent_install.verify_installed(self.state))

    def test_broken_config(self):
        self.settings.write_text("{}", encoding="utf-8")
        self.assertFalse(agent_install.verify_installed(self.state))


class ChildEnvTests(unittest.TestCase):
    def test_strips_pyinstaller_leftovers(self):
        env = agent_install.child_env({
            "_MEIPASS": "/tmp/_MEI123",
            "_MEI1234": "1",
            "_PYI_APPLICATION_HOME_DIR": "/tmp/x",
            "PATH": "/usr/bin",
        })
        self.assertNotIn("_MEIPASS", env)
        self.assertNotIn("_MEI1234", env)
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)

    def test_cleans_ld_library_path(self):
        env = agent_install.child_env({
            "LD_LIBRARY_PATH": "/tmp/_MEIabc:/opt/lib::",
            "PATH": "/usr/bin",
        })
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")

    def test_drops_ld_library_path_when_nothing_is_left(self):
        env = agent_install.child_env({"LD_LIBRARY_PATH": "/tmp/_MEIabc"})
        self.assertNotIn("LD_LIBRARY_PATH", env)

    def test_puts_local_bin_first(self):
        env = agent_install.child_env({"PATH": "/usr/bin:/bin"})
        first = env["PATH"].split(os.pathsep)[0]   # Windows 上是 ; 不是 :
        self.assertEqual(first, str(Path.home() / ".local" / "bin"))

    def test_does_not_mutate_the_input(self):
        src = {"PATH": "/usr/bin"}
        agent_install.child_env(src)
        self.assertEqual(src, {"PATH": "/usr/bin"})


class OpenClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_calls_it_in_the_workdir(self):
        exe = Path(self.tmp.name) / "claude"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        with mock.patch.object(agent_install.subprocess, "call",
                               return_value=0) as call:
            ok, why = agent_install.open_claude(str(exe), workdir=self.tmp.name)
        self.assertTrue(ok, why)
        argv, kwargs = call.call_args
        self.assertEqual(argv[0][0], str(exe))
        self.assertEqual(kwargs["cwd"], self.tmp.name)
        self.assertIn("PATH", kwargs["env"])

    def test_missing_binary_is_reported_not_raised(self):
        with mock.patch.object(agent_install.shutil, "which", return_value=None):
            ok, why = agent_install.open_claude("")
        self.assertFalse(ok)
        self.assertIn("claude", why)

    def test_oserror_is_reported(self):
        with mock.patch.object(agent_install.subprocess, "call",
                               side_effect=PermissionError("不行")):
            ok, why = agent_install.open_claude("/usr/bin/claude")
        self.assertFalse(ok)
        self.assertIn("权限", why)

    def test_creates_the_workdir(self):
        target = Path(self.tmp.name) / "new" / "dir"
        with mock.patch.object(agent_install.subprocess, "call", return_value=0):
            ok, _ = agent_install.open_claude("/usr/bin/claude", workdir=str(target))
        self.assertTrue(ok)
        self.assertTrue(target.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
