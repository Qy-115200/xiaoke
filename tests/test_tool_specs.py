# -*- coding: utf-8 -*-
"""tools（给模型看的 schema）和 tool_map（真正调的函数）必须一一对上。

agent_core 的分发是 `tool_map[name](**args)`，args 直接来自模型按 schema 填的
properties。所以只要 schema 里写了函数不接受的参数名，运行时就是 TypeError；
反过来，函数接受了参数却没写进 schema，模型永远不会传它。

这类错在真机上表现为"某个工具突然报错"，很难从聊天记录里看出来，所以在这里拦。
"""

import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.tool_specs import tool_map, tools                  # noqa: E402


def _schemas():
    return {t["function"]["name"]: t["function"] for t in tools}


class ToolSpecTests(unittest.TestCase):
    def setUp(self):
        self.schemas = _schemas()

    def test_names_match_exactly(self):
        self.assertEqual(set(self.schemas), set(tool_map))

    def test_no_duplicate_schema(self):
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_has_description(self):
        for name, f in self.schemas.items():
            with self.subTest(tool=name):
                self.assertTrue(f.get("description", "").strip())

    def test_schema_properties_are_accepted_by_function(self):
        for name, f in self.schemas.items():
            func = tool_map[name]
            params = inspect.signature(func).parameters
            accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())
            props = (f.get("parameters") or {}).get("properties") or {}
            for prop in props:
                with self.subTest(tool=name, prop=prop):
                    self.assertTrue(
                        accepts_kwargs or prop in params,
                        f"{name} 的 schema 写了 {prop}，但函数签名里没有",
                    )

    def test_required_properties_exist(self):
        for name, f in self.schemas.items():
            schema = f.get("parameters") or {}
            props = schema.get("properties") or {}
            for req in schema.get("required") or []:
                with self.subTest(tool=name, required=req):
                    self.assertIn(req, props)

    def test_callable_without_required_args(self):
        """只有 fully-optional 的工具才能被无条件调用（模型看不到必填项时会漏填）。"""
        for name, f in self.schemas.items():
            schema = f.get("parameters") or {}
            if schema.get("required"):
                continue
            func = tool_map[name]
            missing = [
                p.name for p in inspect.signature(func).parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                               inspect.Parameter.POSITIONAL_ONLY)
            ]
            with self.subTest(tool=name):
                self.assertEqual(missing, [],
                                 f"{name} 的 schema 没标必填，但函数有必填参数 {missing}")


class VscodeToolSpecTests(unittest.TestCase):
    def test_four_vscode_tools_registered(self):
        for name in ("check_vscode", "install_vscode",
                     "install_claude_extension", "open_vscode"):
            with self.subTest(tool=name):
                self.assertIn(name, tool_map)
                self.assertIn(name, _schemas())

    def test_open_vscode_takes_directory(self):
        props = _schemas()["open_vscode"]["parameters"]["properties"]
        self.assertIn("directory", props)

    def test_no_progress_param_on_install_tools(self):
        # 装 VSCode / 插件自己会显示进度条，模型不用（也不该）传 show_progress
        for name in ("install_vscode", "install_claude_extension"):
            with self.subTest(tool=name):
                props = _schemas()[name]["parameters"]["properties"]
                self.assertNotIn("show_progress", props)

    def test_launch_claude_is_documented_as_fallback(self):
        desc = _schemas()["launch_claude"]["description"]
        self.assertIn("open_vscode", desc)
        self.assertIn("只能开一个", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
