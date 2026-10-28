# -*- coding: utf-8 -*-
"""run_one_task 的五个注入缝：给服务器版用，桌面版的默认行为一点都不能变。

服务器版复用这一套循环，但：
  - 事件不能进 bus.msg_queue（那边没有 GUI 在取）；
  - 工具表是服务器自己那套；
  - "装完了"的判定不能靠 install_done（复用本机账号时它永远不触发）；
  - 失败原因要说清楚（连不上模型 / 余额不足 / 跑满了步数）。

★ 第一组测试专门钉住"不传参数 = 老行为"：桌面版和现有测试都靠 patch
  `agent_core.tool_map` / `agent_core.tools` / `agent_core.get_client` 工作。
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_core                                              # noqa: E402
from agent_bus import msg_queue                                # noqa: E402
from llm import NoIdentity                                     # noqa: E402
from tests.test_agent_core import (                            # noqa: E402
    _FakeClient, _drain, _text_response, _tool_response, _FakeToolCall,
)


def _run(responses, tool_map=None, **kwargs):
    """跑一轮任务，模型和工具全打桩。返回 (ok, status, events)。"""
    events = []
    status = {}
    kwargs.setdefault("emit", events.append)
    with mock.patch.object(agent_core, "get_client",
                           return_value=(_FakeClient(responses), "fake-model")), \
         mock.patch.object(agent_core, "tools", []), \
         mock.patch.object(agent_core, "tool_map", tool_map or {}):
        ok = agent_core.run_one_task(
            [{"role": "system", "content": "s"}], "帮我装",
            status=status, **kwargs)
    return ok, status, events


def _kinds(events):
    return [kind for kind, _data in events]


class DefaultBehaviourTests(unittest.TestCase):
    """一个参数都不传时，仍然走模块全局 + bus.msg_queue。"""

    def setUp(self):
        _drain()
        self.addCleanup(_drain)

    def test_default_still_uses_the_module_globals_and_bus_queue(self):
        with mock.patch.object(agent_core, "get_client",
                               return_value=(_FakeClient([_text_response("你好")]),
                                             "fake-model")), \
             mock.patch.object(agent_core, "tools", []), \
             mock.patch.object(agent_core, "tool_map", {}):
            ok = agent_core.run_one_task(
                [{"role": "system", "content": "s"}], "hi")
        self.assertTrue(ok)
        # 事件进的是 bus 那只队列（桌面版主线程就往这儿取）
        got = []
        while True:
            try:
                got.append(msg_queue.get_nowait()[0])
            except Exception:
                break
        self.assertIn("log", got)

    def test_default_tool_map_is_the_desktop_one(self):
        # 模块级的 tool_map 还在（测试要 patch 它，服务器不 import 它）
        self.assertIn("open_vscode", agent_core.tool_map)
        self.assertIn("ask_user", agent_core.tool_map)


class EmitSeamTests(unittest.TestCase):
    def setUp(self):
        _drain()
        self.addCleanup(_drain)

    def test_injected_emit_captures_everything_and_leaves_the_queue_empty(self):
        ok, _status, events = _run([_text_response("好了")])
        self.assertTrue(ok)
        self.assertTrue(events)
        self.assertTrue(all(isinstance(e, tuple) and len(e) == 2 for e in events))
        self.assertIn("log", _kinds(events))
        # ★ 这是服务器版的前提：没有 GUI 在取队列，事件一条都不能漏进去
        with self.assertRaises(Exception):
            msg_queue.get_nowait()

    def test_injected_tool_map_is_used(self):
        called = []
        tool_map = {"ping": lambda **kw: called.append(kw) or "pong"}
        ok, _status, events = _run(
            [_tool_response(_FakeToolCall("ping", '{"x": 1}')), _text_response("好")],
            tool_map=tool_map)
        self.assertTrue(ok)
        self.assertEqual(called, [{"x": 1}])
        # 结果回填进了日志
        self.assertTrue(any("[结果] pong" in str(d) for _k, d in events))


class MarksCompleteSeamTests(unittest.TestCase):
    def test_injected_predicate_decides_install_done(self):
        tool_map = {"register_device": lambda **kw: json.dumps({"note": "领取成功"}),
                    "finish": lambda **kw: "装好了"}
        ok, _status, events = _run(
            [_tool_response(_FakeToolCall("register_device"),
                            _FakeToolCall("finish")),
             _text_response("搞定")],
            tool_map=tool_map,
            marks_complete=lambda name, result: name == "finish")
        self.assertTrue(ok)
        self.assertIn("install_done", _kinds(events))

    def test_server_style_predicate_never_fires(self):
        """服务器版传 lambda *_: False —— 收尾由它自己的 state.finished 决定。"""
        tool_map = {"finish": lambda **kw: "装好了"}
        ok, _status, events = _run(
            [_tool_response(_FakeToolCall("finish")), _text_response("搞定")],
            tool_map=tool_map,
            marks_complete=lambda *_: False)
        self.assertTrue(ok)
        self.assertNotIn("install_done", _kinds(events))


class StatusSeamTests(unittest.TestCase):
    def test_no_identity(self):
        status = {}
        with mock.patch.object(agent_core, "get_client",
                               side_effect=NoIdentity("还没有账号凭证")):
            ok = agent_core.run_one_task([{"role": "system", "content": "s"}], "hi",
                                         emit=lambda _e: None, status=status)
        self.assertFalse(ok)
        self.assertEqual(status["error"], "no_identity")

    def test_model_unreachable(self):
        # 用一个 create 直接抛异常的 client
        class _Boom:
            class chat:
                class completions:
                    @staticmethod
                    def create(**_kw):
                        raise RuntimeError("connection refused")

        status = {}
        with mock.patch.object(agent_core, "get_client",
                               return_value=(_Boom(), "fake-model")):
            ok = agent_core.run_one_task([{"role": "system", "content": "s"}], "hi",
                                         emit=lambda _e: None, status=status)
        self.assertFalse(ok)
        self.assertEqual(status["error"], "model")
        self.assertIn("connection refused", status["detail"])

    def test_balance(self):
        class _Paywall(Exception):
            status_code = 402

        class _Boom:
            class chat:
                class completions:
                    @staticmethod
                    def create(**_kw):
                        raise _Paywall("余额已用完")

        status = {}
        events = []
        with mock.patch.object(agent_core, "get_client",
                               return_value=(_Boom(), "fake-model")):
            ok = agent_core.run_one_task([{"role": "system", "content": "s"}], "hi",
                                         emit=events.append, status=status)
        self.assertFalse(ok)
        self.assertEqual(status["error"], "balance")
        # 界面才知道该露出"立即充值"
        self.assertIn("recharge", _kinds(events))

    def test_max_steps(self):
        tool_map = {"ping": lambda **kw: "pong"}
        with mock.patch.object(agent_core, "MAX_STEPS", 2):
            status = {}
            events = []
            with mock.patch.object(agent_core, "get_client",
                                   return_value=(_FakeClient(
                                       [_tool_response(_FakeToolCall("ping"))] * 5),
                                       "fake-model")), \
                 mock.patch.object(agent_core, "tools", []), \
                 mock.patch.object(agent_core, "tool_map", tool_map):
                ok = agent_core.run_one_task(
                    [{"role": "system", "content": "s"}], "hi",
                    emit=events.append, status=status)
        self.assertFalse(ok)
        self.assertEqual(status["error"], "max_steps")

    def test_success_leaves_no_error(self):
        ok, status, _events = _run([_text_response("好了")])
        self.assertTrue(ok)
        self.assertNotIn("error", status)


class SecretRedactionTests(unittest.TestCase):
    """工具参数里带密钥时，日志里一个字都不能出现。"""

    def test_account_key_is_masked(self):
        tool_map = {"login_with_account_code":
                    lambda **kw: json.dumps({"ok": True})}
        _ok, _status, events = _run(
            [_tool_response(_FakeToolCall(
                "login_with_account_code",
                '{"account_code": "CC-1", "account_key": "SUPERSECRETKEY123"}')),
             _text_response("好")],
            tool_map=tool_map)
        blob = "\n".join(str(d) for _k, d in events)
        self.assertIn("login_with_account_code", blob)
        self.assertNotIn("SUPERSECRETKEY123", blob)
        self.assertIn("***", blob)

    def test_write_file_content_is_not_logged(self):
        tool_map = {"write_file": lambda **kw: "已写入"}
        _ok, _status, events = _run(
            [_tool_response(_FakeToolCall(
                "write_file", '{"path": "/home/u/.claude/settings.json",'
                              ' "content": "TOKEN-IN-CONTENT"}')),
             _text_response("好")],
            tool_map=tool_map)
        blob = "\n".join(str(d) for _k, d in events)
        self.assertIn("/home/u/.claude/settings.json", blob)
        self.assertNotIn("TOKEN-IN-CONTENT", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
