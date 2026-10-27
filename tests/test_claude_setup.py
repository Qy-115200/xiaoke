# -*- coding: utf-8 -*-
"""claude CLI 的安装参数：不提权、不继承用户的 npm 配置、失败要能说清原因。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import claude_setup  # noqa: E402
from server.cli import EXIT_NPM, CliContext, CliError  # noqa: E402


class NpmEnvTests(unittest.TestCase):
    BASE = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/u",
        "npm_config_registry": "https://registry.npmjs.org/",
        "npm_config_ignore_scripts": "true",      # ★ 就是它会把原生二进制省掉
        "NPM_CONFIG_OMIT": "optional",            # 大写也要摘
        "npm_config_prefix": "/usr/local",
        "SHELL": "/bin/bash",
    }

    def test_inherited_npm_config_is_replaced(self):
        env = claude_setup.npm_env(self.BASE, Path("/home/u/.local"))
        # 用户那几条不能漏进来
        self.assertEqual(env["npm_config_registry"], claude_setup.NPM_REGISTRY)
        self.assertEqual(env["npm_config_ignore_scripts"], "false")
        self.assertNotIn("NPM_CONFIG_OMIT", env)
        self.assertNotEqual(env["npm_config_prefix"], "/usr/local")

    def test_required_values_are_set(self):
        env = claude_setup.npm_env(self.BASE, Path("/home/u/.local"))
        self.assertEqual(env["npm_config_prefix"], str(Path("/home/u/.local")))
        self.assertEqual(env["npm_config_registry"], claude_setup.NPM_REGISTRY)
        self.assertEqual(env["npm_config_ignore_scripts"], "false")
        self.assertIn(".npm-cache", env["npm_config_cache"])

    def test_other_vars_survive(self):
        env = claude_setup.npm_env(self.BASE, Path("/home/u/.local"))
        self.assertEqual(env["PATH"], self.BASE["PATH"])
        self.assertEqual(env["SHELL"], "/bin/bash")


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / ".local"

    def test_install_argv_is_exact(self):
        argv = claude_setup.install_argv({"npm": "/opt/node/bin/npm"}, self.prefix)
        self.assertEqual(argv, [
            "/opt/node/bin/npm", "install", "-g",
            "--prefix", str(self.prefix),
            "@anthropic-ai/claude-code",
            "--registry", "https://registry.npmmirror.com",
            "--include=optional",
            "--no-audit", "--no-fund",
        ])

    def test_pinned_version(self):
        argv = claude_setup.install_argv({"npm": "npm"}, self.prefix, version="2.1.310")
        self.assertIn("@anthropic-ai/claude-code@2.1.310", argv)

    def test_never_prefixes_sudo(self):
        argv = claude_setup.install_argv({"npm": "npm"}, self.prefix)
        self.assertNotIn("sudo", argv)

    def test_prefers_node_plus_npm_cli_js(self):
        node_bin = Path(self.tmp.name) / "node" / "bin"
        cli_js = (Path(self.tmp.name) / "node" / "lib" / "node_modules" / "npm"
                  / "bin" / "npm-cli.js")
        cli_js.parent.mkdir(parents=True)
        cli_js.write_text("// stub", encoding="utf-8")
        argv = claude_setup.npm_argv_prefix({"node": str(node_bin / "node"),
                                             "npm": str(node_bin / "npm")})
        self.assertEqual(argv, [str(node_bin / "node"), str(cli_js)])

    def test_falls_back_to_npm_script(self):
        argv = claude_setup.npm_argv_prefix({"node": "/n/node", "npm": "/n/npm"})
        self.assertEqual(argv, ["/n/npm"])


class CandidatesTests(unittest.TestCase):
    def test_order_and_names(self):
        prefix = Path("/home/u/.local")
        cands = [Path(p) for p in claude_setup.claude_candidates(prefix)]
        self.assertEqual(cands[0], prefix / "bin" / "claude")
        self.assertTrue(any(p.name == "claude.exe" for p in cands))


class NativePackageTests(unittest.TestCase):
    """主包只是个壳，真二进制在平台子包里 —— 名字搞错就白装一遍。"""

    def test_arch_tags(self):
        self.assertEqual(claude_setup.fallback_arch_tag("x86_64", musl=False), "x64")
        self.assertEqual(claude_setup.fallback_arch_tag("AMD64", musl=False), "x64")
        self.assertEqual(claude_setup.fallback_arch_tag("aarch64", musl=False), "arm64")
        self.assertEqual(claude_setup.fallback_arch_tag("x86_64", musl=True), "x64-musl")
        self.assertEqual(claude_setup.fallback_arch_tag("riscv64", musl=False), "")

    def test_package_name(self):
        self.assertEqual(claude_setup.native_package_name("x64"),
                         "@anthropic-ai/claude-code-linux-x64")
        self.assertEqual(claude_setup.native_package_name("arm64-musl"),
                         "@anthropic-ai/claude-code-linux-arm64-musl")
        self.assertEqual(claude_setup.native_package_name(""), "")

    def test_native_install_argv_is_not_optional(self):
        argv = claude_setup.native_install_argv(
            {"npm": "npm"}, Path("/home/u/.local"),
            "@anthropic-ai/claude-code-linux-x64", version="2.1.311")
        self.assertIn("@anthropic-ai/claude-code-linux-x64@2.1.311", argv)
        self.assertIn("--prefix", argv)
        self.assertEqual(argv[1:3], ["install", "-g"])
        # ★ 不能再用 optional 那种"失败也不报错"的方式装
        self.assertNotIn("--include=optional", argv)

    def test_npm_env_gives_the_big_download_some_retries(self):
        env = claude_setup.npm_env({}, Path("/home/u/.local"))
        self.assertEqual(env["npm_config_fetch_retries"], "5")
        self.assertIn("npm_config_fetch_timeout", env)


class RepairNativeTests(unittest.TestCase):
    """壳装了、原生包没下来时，得能单独补上，而且失败要说人话。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.pkg = (self.home / ".local" / "lib" / "node_modules"
                    / "@anthropic-ai" / "claude-code")
        (self.pkg / "bin").mkdir(parents=True)
        (self.pkg / "package.json").write_text('{"version": "2.1.311"}',
                                               encoding="utf-8")
        (self.pkg / "install.cjs").write_text("// stub", encoding="utf-8")

    def _ctx(self, runner):
        return CliContext(home=self.home, env={"PATH": "", "HOME": str(self.home)},
                          runner=runner, quiet=True)

    def test_installs_platform_package_then_runs_postinstall(self):
        runner = FakeRunner([])
        ctx = self._ctx(runner)
        target = str(self.pkg / "bin" / "claude.exe")
        with mock.patch.object(claude_setup, "platform_arch_tag", return_value="x64"), \
             mock.patch.object(claude_setup, "find_installed_claude", return_value=target):
            got = claude_setup._repair_native(
                ctx, {"node": "/n/node", "npm": "/n/npm"}, "")
        self.assertEqual(got, target)
        self.assertEqual(len(runner.calls), 2)
        # 版本跟壳走，别装出两个不同版本的主/子包
        self.assertIn("@anthropic-ai/claude-code-linux-x64@2.1.311", runner.calls[0])
        self.assertEqual(runner.calls[1][0], "/n/node")
        self.assertTrue(runner.calls[1][1].endswith("install.cjs"))

    def test_failure_is_loud_and_keeps_the_first_output(self):
        from server.cli import CommandResult
        runner = FakeRunner([CommandResult(["npm"], 1, "", "ECONNRESET")])
        ctx = self._ctx(runner)
        with mock.patch.object(claude_setup, "platform_arch_tag", return_value="x64"):
            with self.assertRaises(CliError) as cm:
                claude_setup._repair_native(
                    ctx, {"node": "/n/node", "npm": "/n/npm"}, "第一次 npm 的输出")
        self.assertEqual(cm.exception.exit_code, EXIT_NPM)
        self.assertIn("ECONNRESET", cm.exception.message)
        self.assertIn("第一次 npm 的输出", cm.exception.hint)

    def test_nothing_to_repair_when_the_wrapper_is_missing(self):
        import shutil
        shutil.rmtree(self.pkg)
        runner = FakeRunner([])
        ctx = self._ctx(runner)
        self.assertEqual(claude_setup._repair_native(
            ctx, {"node": "/n/node", "npm": "/n/npm"}, ""), "")
        self.assertEqual(runner.calls, [])


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, timeout=None, stdin=None):
        from server.cli import CommandResult
        self.calls.append(list(argv))
        if self.results:
            return self.results.pop(0)
        return CommandResult(argv, 0, "2.1.310 (Claude Code)", "")


class EnsureClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _ctx(self, runner=None, dry_run=False):
        return CliContext(home=self.home, env={"PATH": "", "HOME": str(self.home)},
                          runner=runner or FakeRunner([]), quiet=True, dry_run=dry_run)

    def test_already_on_path_is_not_reinstalled(self):
        runner = FakeRunner([])
        ctx = self._ctx(runner)
        with mock.patch.object(claude_setup, "claude_on_path", return_value="/usr/bin/claude"), \
             mock.patch.object(claude_setup, "claude_version", return_value="2.1.310"):
            info = claude_setup.ensure_claude(ctx, {"node": "/n", "npm": "/n/npm"})
        self.assertFalse(info["installed"])
        self.assertEqual(runner.calls, [])

    def test_install_runs_npm_without_sudo(self):
        runner = FakeRunner([])
        ctx = self._ctx(runner)
        pkg = self.home / ".local" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "bin" / "claude.exe").write_text("#!/bin/sh\n", encoding="utf-8")

        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "find_installed_claude",
                               side_effect=["", str(pkg / "bin" / "claude.exe")]), \
             mock.patch.object(claude_setup, "claude_version", return_value="2.1.310"):
            info = claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})

        self.assertTrue(info["installed"])
        self.assertEqual(len(runner.calls), 1)
        argv = runner.calls[0]
        self.assertIn("install", argv)
        self.assertIn("--include=optional", argv)
        self.assertIn("--prefix", argv)
        self.assertNotIn("sudo", argv)

    def test_npm_failure_maps_to_exit_6(self):
        from server.cli import CommandResult
        runner = FakeRunner([CommandResult(["npm"], 1, "", "EACCES: permission denied")])
        ctx = self._ctx(runner)
        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "find_installed_claude", return_value=""):
            with self.assertRaises(CliError) as cm:
                claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})
        self.assertEqual(cm.exception.exit_code, EXIT_NPM)
        self.assertIn("EACCES", cm.exception.message)

    def test_missing_binary_after_npm_says_why(self):
        ctx = self._ctx(FakeRunner([]))
        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "find_installed_claude", return_value=""):
            with self.assertRaises(CliError) as cm:
                claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})
        self.assertEqual(cm.exception.exit_code, EXIT_NPM)
        self.assertIn("ignore-scripts", cm.exception.hint)

    def test_missing_binary_triggers_the_native_repair(self):
        """npm 退出码 0 不代表装好了：optional 依赖失败了它不报错。

        没有壳目录时补装也无从下手，所以这里把壳摆出来。"""
        runner = FakeRunner([])
        ctx = self._ctx(runner)
        pkg = self.home / ".local" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "package.json").write_text('{"version": "2.1.311"}', encoding="utf-8")
        (pkg / "install.cjs").write_text("// stub", encoding="utf-8")
        target = str(pkg / "bin" / "claude.exe")

        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "platform_arch_tag", return_value="x64"), \
             mock.patch.object(claude_setup, "claude_version", return_value="2.1.311"), \
             mock.patch.object(claude_setup, "find_installed_claude",
                               side_effect=["", "", target]):
            info = claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})

        self.assertTrue(info["installed"])
        specs = [a for call in runner.calls for a in call
                 if a.startswith("@anthropic-ai/claude-code-linux-x64")]
        self.assertEqual(specs, ["@anthropic-ai/claude-code-linux-x64@2.1.311"])

    def test_dry_run_does_not_run_npm(self):
        runner = FakeRunner([])
        ctx = self._ctx(runner, dry_run=True)
        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "find_installed_claude", return_value=""):
            info = claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})
        self.assertTrue(info["planned"])
        self.assertEqual(runner.calls, [])

    def test_timeout_is_reported(self):
        from server.cli import CommandResult
        runner = FakeRunner([CommandResult(["npm"], -1, "", "", timed_out=True)])
        ctx = self._ctx(runner)
        with mock.patch.object(claude_setup, "claude_on_path", return_value=""), \
             mock.patch.object(claude_setup, "find_installed_claude", return_value=""):
            with self.assertRaises(CliError) as cm:
                claude_setup.ensure_claude(ctx, {"node": "/n/node", "npm": "/n/npm"})
        self.assertIn("超时", cm.exception.message)


class HintTests(unittest.TestCase):
    def test_env_hint_mentions_local_bin(self):
        prefix = Path("/home/u/.local")
        hint = claude_setup.claude_env_hint(prefix)
        self.assertIn(str(prefix / "bin"), hint)
        self.assertTrue(hint.startswith("export PATH="))


if __name__ == "__main__":
    unittest.main(verbosity=2)
