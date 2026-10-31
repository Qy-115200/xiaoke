# -*- coding: utf-8 -*-
"""agent_core 的"安装完成"判定：什么时候该自动退出，什么时候绝对不能退。

背景：`install_done` 会让界面 10 秒后关掉助手。误判的后果是用户还没看完收尾说明
窗口就没了；漏判的后果是装完不退出。所以这里把两条触发路径都钉死：

  装完一轮 = 这一轮调过 register_device（真在走安装流程）
             **并且** 最后用 open_vscode / launch_claude 真的把人领进门了

★ 用户单独说"再帮我打开一次"时这一轮没有 register_device，绝不能退出。
★ 打开失败（找不到 VSCode / 找不到 claude）也不算完成。

全程打桩模型和工具，不联网。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_bus                                               # noqa: E402
import agent_core                                              # noqa: E402
from bus import msg_queue                                      # noqa: E402


# ================= 打桩：OpenAI SDK 的返回形状 =================

class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name, arguments="{}", call_id=None):
        self.id = call_id or f"call_{name}"
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeResponse:
    def __init__(self, message):
        self.choices = [type("_Choice", (), {"message": message})()]


def _tool_response(*calls):
    return _FakeResponse(_FakeMessage(tool_calls=list(calls)))


def _text_response(text="好了"):
    return _FakeResponse(_FakeMessage(content=text))


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **_kwargs):
        if not self._responses:
            return _text_response()
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(responses)})()


def _drain():
    while True:
        try:
            msg_queue.get_nowait()
        except Exception:
            return


# ================= 纯函数真值表 =================

class MarksInstallCompleteTests(unittest.TestCase):
    def test_open_vscode_success(self):
        self.assertTrue(agent_core._marks_install_complete(
            "open_vscode", "已打开 VSCode（D:\\vs code\\Code.exe），工作目录：C:\\Users\\x"))

    def test_open_vscode_failure(self):
        for text in ("找不到 VSCode，无法打开",
                     "打开 VSCode 失败：拒绝访问",
                     ""):
            self.assertFalse(agent_core._marks_install_complete("open_vscode", text), text)

    def test_launch_claude_success(self):
        self.assertTrue(agent_core._marks_install_complete(
            "launch_claude", "已打开新终端启动 Claude Code（x），工作目录：y"))

    def test_launch_claude_failure(self):
        for text in ("找不到 claude 命令，无法启动", "启动失败：x"):
            self.assertFalse(agent_core._marks_install_complete("launch_claude", text), text)

    def test_other_tools_never_count(self):
        # 别的工具返回同样的文字也不算 —— 只认工具名
        self.assertFalse(agent_core._marks_install_complete(
            "bash", "已打开 VSCode（x）"))
        self.assertFalse(agent_core._marks_install_complete(
            "install_vscode", "已安装 VSCode（x）"))
        self.assertFalse(agent_core._marks_install_complete(
            "register_device", '{"note": "领取成功"}'))


# ================= run_one_task 的端到端判定 =================

class InstallDoneTests(unittest.TestCase):
    def setUp(self):
        _drain()
        self.addCleanup(_drain)

    def _run(self, responses, tool_map):
        with mock.patch.object(agent_core, "get_client",
                               return_value=(_FakeClient(responses), "fake-model")), \
             mock.patch.object(agent_core, "tool_map", tool_map), \
             mock.patch.object(agent_core, "tools", []):
            ok = agent_core.run_one_task([{"role": "system", "content": "s"}], "帮我装")
        return ok

    def _kinds(self):
        out = []
        while True:
            try:
                out.append(msg_queue.get_nowait()[0])
            except Exception:
                return out

    def test_install_with_vscode_fires_install_done(self):
        tool_map = {
            "register_device": lambda: json.dumps({"note": "领取成功"}),
            "open_vscode": lambda directory="": "已打开 VSCode（x），工作目录：y",
        }
        responses = [
            _tool_response(_FakeToolCall("register_device")),
            _tool_response(_FakeToolCall("open_vscode", '{"directory": "y"}')),
        ]
        self.assertTrue(self._run(responses, tool_map))
        self.assertIn("install_done", self._kinds())

    def test_install_with_terminal_still_fires(self):
        # 老路径不能坏：用户不要 VSCode 时走终端，照样算装完
        tool_map = {
            "register_device": lambda: json.dumps({"note": "领取成功"}),
            "launch_claude": lambda directory="": "已打开新终端启动 Claude Code（x），工作目录：y",
        }
        responses = [
            _tool_response(_FakeToolCall("register_device")),
            _tool_response(_FakeToolCall("launch_claude")),
        ]
        self._run(responses, tool_map)
        self.assertIn("install_done", self._kinds())

    def test_open_vscode_alone_does_not_fire(self):
        # 回归：用户说"再帮我打开一次"→ 没走安装流程 → 绝不能把助手关掉
        tool_map = {"open_vscode": lambda directory="": "已打开 VSCode（x），工作目录：y"}
        responses = [_tool_response(_FakeToolCall("open_vscode"))]
        self._run(responses, tool_map)
        self.assertNotIn("install_done", self._kinds())

    def test_launch_claude_alone_does_not_fire(self):
        tool_map = {"launch_claude": lambda directory="": "已打开新终端启动 Claude Code（x）"}
        responses = [_tool_response(_FakeToolCall("launch_claude"))]
        self._run(responses, tool_map)
        self.assertNotIn("install_done", self._kinds())

    def test_failed_open_does_not_fire(self):
        # 安装流程走了一半（VSCode 没装成、下载页打开了），不能假装成功退出
        tool_map = {
            "register_device": lambda: json.dumps({"note": "领取成功"}),
            "open_vscode": lambda directory="": "找不到 VSCode，无法打开",
        }
        responses = [
            _tool_response(_FakeToolCall("register_device")),
            _tool_response(_FakeToolCall("open_vscode")),
        ]
        self._run(responses, tool_map)
        self.assertNotIn("install_done", self._kinds())

    def test_failed_register_does_not_fire(self):
        # 连账号都没领到，这一轮就不算安装流程
        tool_map = {
            "register_device": lambda: "领取失败：网络不通",
            "open_vscode": lambda directory="": "已打开 VSCode（x），工作目录：y",
        }
        responses = [
            _tool_response(_FakeToolCall("register_device")),
            _tool_response(_FakeToolCall("open_vscode")),
        ]
        self._run(responses, tool_map)
        self.assertNotIn("install_done", self._kinds())


class CancelTests(unittest.TestCase):
    """用户点了「中断」之后，这一轮就不能再往下跑。

    ★ 牌子由调用方摘（agent_worker / 服务器版每轮开工调 clear_cancel），
      run_one_task 只负责"看见牌子就停"——它不该自己拆牌子，否则
      长下载里立的那一下会被下一步顺手抹掉。
    """

    def setUp(self):
        _drain()
        agent_bus.clear_cancel()
        self.addCleanup(agent_bus.clear_cancel)
        self.addCleanup(_drain)

    def _run(self, responses, tool_map, status=None):
        with mock.patch.object(agent_core, "get_client",
                               return_value=(_FakeClient(responses), "fake-model")), \
             mock.patch.object(agent_core, "tool_map", tool_map), \
             mock.patch.object(agent_core, "tools", []):
            return agent_core.run_one_task(
                [{"role": "system", "content": "s"}], "帮我装", status=status)

    def test_cancelled_before_starting_never_calls_the_model(self):
        status = {}
        agent_bus.request_cancel()
        with mock.patch.object(agent_core, "get_client") as get_client:
            ok = self._run([_tool_response(_FakeToolCall("bash"))], {}, status)
        self.assertFalse(ok)
        get_client.assert_not_called()
        self.assertEqual(status["error"], "cancelled")

    def test_cancelled_during_a_long_tool_stops_after_it(self):
        """下载 / 装包是在工具里点的中断：工具跑完就得停，别再调下一次模型。"""
        status = {}

        def slow_tool(**kwargs):
            agent_bus.request_cancel()          # 用「按钮」的同一套 API
            return "下到一半被中断"

        responses = [
            _tool_response(_FakeToolCall("bash", '{"command": "x"}', "call_1")),
            _tool_response(_FakeToolCall("bash", '{"command": "y"}', "call_2")),
        ]
        calls = []

        class _CountingCompletions(_FakeCompletions):
            def create(self, **kw):
                calls.append(1)
                return super().create(**kw)

        class _CountingClient:
            def __init__(self, resp):
                self.chat = type("_Chat", (), {
                    "completions": _CountingCompletions(resp)})()

        with mock.patch.object(agent_core, "get_client",
                               return_value=(_CountingClient(responses), "fake-model")), \
             mock.patch.object(agent_core, "tool_map", {"bash": slow_tool}), \
             mock.patch.object(agent_core, "tools", []):
            ok = agent_core.run_one_task(
                [{"role": "system", "content": "s"}], "帮我装", status=status)
        self.assertFalse(ok)
        self.assertEqual(status["error"], "cancelled")
        self.assertEqual(len(calls), 1, "第二次模型调用根本不该发生")

    def test_the_flag_is_not_cleared_by_run_one_task(self):
        agent_bus.request_cancel()
        self._run([_tool_response(_FakeToolCall("bash"))], {})
        self.assertTrue(agent_bus.is_cancelled(),
                        "摘牌子是下一轮开工的事，run_one_task 不能替用户做主")


if __name__ == "__main__":
    unittest.main(verbosity=2)
