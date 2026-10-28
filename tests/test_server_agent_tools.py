# -*- coding: utf-8 -*-
"""服务器版工具集：说明书和实现必须一一对应，验收闸门必须挑得出缺项。

这些工具是模型唯一的"手脚"，所以两条最要紧的断言在这儿：
  · `tools` 里的名字和 `tool_map` 的键**一个不多一个不少**；
  · `finish_install` 在四样东西没齐的时候**绝不说"已装好"**。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import agent_tools, identity, node_setup, tools_general  # noqa: E402
from server import claude_setup  # noqa: E402
from server.cli import CliError  # noqa: E402


class Args:
    node_version = ""
    claude_version = ""
    force = False


def make(tmp, node=True, claude="", account=True, config=True, settings=True,
         completed=True):
    """造一份临时主目录（两份配置 + 一个假的 claude 可执行文件）。"""
    tmp = Path(tmp)
    config_path = tmp / "config.json"
    settings_path = tmp / "settings.json"

    if config:
        body = {"env": {"ANTHROPIC_BASE_URL": "https://example/api",
                        "ANTHROPIC_AUTH_TOKEN": "sk-never-print-me"}}
        if completed:
            body["hasCompletedOnboarding"] = True
        config_path.write_text(json.dumps(body), encoding="utf-8")
    if settings:
        settings_path.write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "https://example/api",
                     "ANTHROPIC_AUTH_TOKEN": "sk-never-print-me"}}), encoding="utf-8")

    state = {"arch": "x86_64"}
    if node:
        state["node"] = {"version": "v22.11.0", "node": "/usr/local/bin/node"}
    if claude:
        exe = tmp / "claude"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        state["claude"] = {"claude": str(exe), "version": "2.1.312"}

    ident = {"api_key": "sk-abc", "account_code": "CC-111111-222222"} if account else None
    return state, config_path, settings_path, ident


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.state = {"arch": "x86_64"}
        self.tool_map, self.tools = agent_tools.build_tools(None, Args(), self.state)

    def test_every_schema_has_an_implementation(self):
        names = [t["function"]["name"] for t in self.tools]
        self.assertEqual(sorted(names), sorted(self.tool_map))
        self.assertEqual(len(names), len(set(names)), "工具名不能重复")

    def test_every_schema_is_well_formed(self):
        for t in self.tools:
            with self.subTest(name=t["function"]["name"]):
                self.assertEqual(t["type"], "function")
                self.assertTrue(t["function"]["description"].strip())
                self.assertEqual(t["function"]["parameters"]["type"], "object")

    def test_no_desktop_only_tools(self):
        for name in ("launch_claude", "open_vscode", "check_vscode",
                     "install_vscode", "install_claude_extension",
                     "create_shortcut", "register_protocol", "register_device",
                     "login_with_account_code"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.tool_map)

    def test_the_install_four_are_there(self):
        for name in ("ensure_node", "install_claude", "configure_claude",
                     "finish_install"):
            self.assertIn(name, self.tool_map)


class BashGuardTests(unittest.TestCase):
    def setUp(self):
        self.state = {"arch": "x86_64"}
        self.tool_map, _ = agent_tools.build_tools(None, Args(), self.state)
        self.bash = self.tool_map["bash"]

    def test_refuses_sudo(self):
        self.assertIn("拒绝", self.bash("sudo apt install -y nodejs"))

    def test_refuses_package_managers(self):
        self.assertIn("拒绝", self.bash("apt-get install -y curl"))
        self.assertIn("拒绝", self.bash("yum install -y git"))

    def test_refuses_npm_install(self):
        self.assertIn("拒绝", self.bash("npm install -g @anthropic-ai/claude-code"))
        self.assertIn("拒绝", self.bash("npm i -g @anthropic-ai/claude-code"))

    def test_refuses_the_identity_dir(self):
        self.assertIn("拒绝", self.bash("cat ~/.claude-agent/identity.json"))

    def test_simple_commands_pass_through_with_no_stdin(self):
        with mock.patch.object(tools_general, "bash", return_value="v22.11.0") as fake:
            out = self.bash("node --version")
        self.assertEqual(out, "v22.11.0")
        _args, kwargs = fake.call_args
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL,
                      "子进程不许抢终端的按键")

    def test_npm_version_is_not_blocked(self):
        with mock.patch.object(tools_general, "bash", return_value="10.9.0"):
            self.assertEqual(self.bash("npm --version"), "10.9.0")


class FileGuardTests(unittest.TestCase):
    def setUp(self):
        self.tool_map, _ = agent_tools.build_tools(None, Args(), {"arch": "x86_64"})
        self.secret = str(Path.home() / ".claude-agent" / "identity.json")

    def test_read_refuses_identity(self):
        self.assertIn("拒绝", self.tool_map["read_file"](self.secret))

    def test_write_refuses_identity(self):
        self.assertIn("拒绝", self.tool_map["write_file"](self.secret, "x"))

    def test_normal_paths_go_through(self):
        with mock.patch.object(tools_general, "read_file", return_value="ok"):
            self.assertEqual(self.tool_map["read_file"]("~/notes.txt"), "ok")
        with mock.patch.object(tools_general, "write_file", return_value="ok"):
            self.assertEqual(self.tool_map["write_file"]("~/notes.txt", "hi"), "ok")


class InstallStepsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = {"arch": "x86_64"}
        self.tool_map, _ = agent_tools.build_tools(None, Args(), self.state)

    def test_ensure_node_records_the_result(self):
        info = {"version": "v22.11.0", "node": "/x/bin/node"}
        with mock.patch.object(node_setup, "ensure_node", return_value=info):
            text = self.tool_map["ensure_node"]()
        self.assertIn("v22.11.0", text)
        self.assertEqual(self.state["node"], info)

    def test_ensure_node_turns_a_cli_error_into_words(self):
        err = CliError("连不上 Node 镜像", hint="换个网络")
        with mock.patch.object(node_setup, "ensure_node", side_effect=err):
            text = self.tool_map["ensure_node"]()
        self.assertTrue(text.startswith("失败："))
        self.assertIn("换个网络", text)
        self.assertEqual(self.state["node"], {})

    def test_install_claude_needs_node_first(self):
        self.assertIn("先调 ensure_node", self.tool_map["install_claude"]())

    def test_install_claude_records_the_result(self):
        self.state["node"] = {"version": "v22.11.0", "npm": "/x/npm"}
        info = {"claude": "/x/claude", "version": "2.1.312", "installed": True}
        with mock.patch.object(claude_setup, "ensure_claude", return_value=info):
            text = self.tool_map["install_claude"]()
        self.assertIn("2.1.312", text)
        self.assertEqual(self.state["claude"], info)

    def test_configure_claude_reports_a_bad_identity(self):
        with mock.patch("server.tools_fixed.bypass_region_check", return_value="ok"), \
             mock.patch("server.tools_fixed.set_model_config",
                        return_value="错误：还没领取 token"):
            text = self.tool_map["configure_claude"]()
        self.assertTrue(text.startswith("失败："))


class FinishInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, **kw):
        state, config_path, settings_path, ident = make(self.tmp.name, **kw)
        patches = [mock.patch.object(agent_tools, "CLAUDE_CONFIG", config_path),
                   mock.patch.object(agent_tools, "CLAUDE_SETTINGS", settings_path),
                   mock.patch.object(identity, "load_identity", return_value=ident)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        tool_map, _ = agent_tools.build_tools(None, Args(), state)
        return state, tool_map["finish_install"]

    def test_missing_node(self):
        state, finish = self.build(node=False)
        text = finish()
        self.assertIn("Node", text)
        self.assertNotIn("已装好", text)
        self.assertFalse(state["finished"])

    def test_missing_claude(self):
        state, finish = self.build(claude="")
        self.assertIn("claude", finish())
        self.assertFalse(state["finished"])

    def test_missing_account(self):
        state, finish = self.build(claude="ok", account=False)
        self.assertIn("账号", finish())
        self.assertFalse(state["finished"])

    def test_missing_config_file(self):
        state, finish = self.build(claude="ok", settings=False)
        self.assertIn("settings.json", finish())
        self.assertFalse(state["finished"])

    def test_config_without_env(self):
        state, finish = self.build(claude="ok")
        (Path(self.tmp.name) / "settings.json").write_text("{}", encoding="utf-8")
        self.assertIn("ANTHROPIC_BASE_URL", finish())
        self.assertFalse(state["finished"])

    def test_config_without_onboarding_flag(self):
        state, finish = self.build(claude="ok", completed=False)
        self.assertIn("hasCompletedOnboarding", finish())
        self.assertFalse(state["finished"])

    def test_everything_present(self):
        state, finish = self.build(claude="ok")
        text = finish()
        self.assertIn("已装好", text)
        self.assertTrue(state["finished"])
        self.assertEqual(state["account_code"], "CC-111111-222222")

    def test_never_prints_the_token(self):
        state, finish = self.build(claude="ok")
        text = finish() + finish()
        self.assertNotIn("sk-never-print-me", text)
        self.assertNotIn("sk-abc", text)

    def test_reports_several_problems_at_once(self):
        state, finish = self.build(node=False, claude="", account=False,
                                   settings=False)
        text = finish()
        self.assertIn("Node", text)
        self.assertIn("claude", text)
        self.assertIn("账号", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
