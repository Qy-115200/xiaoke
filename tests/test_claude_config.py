# -*- coding: utf-8 -*-
"""配置自愈：identity.json 和两份 Claude Code 配置对账。

这里全是"改错了会伤到用户"的地方，所以测的是行为不是实现：
  · 没有 diff 时一个字节都不动（不写、不备份、不打日志）；
  · 坏文件绝不覆盖；指向别人家中转的也绝不覆盖；
  · 只补受管的那 8 个键，用户别的 env / 顶层 model 原样保留；
  · 日志和返回值里绝不出现 token。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import claude_config as cc  # noqa: E402

TOKEN = "sk-test-DO-NOT-LEAK-0123456789"
IDENT = {
    "api_key": TOKEN,
    "api_base": "https://aikaui.com/v1",
    "model": "pro-x",
    "fast_model": "fast-x",
    "account_code": "A1",
}
ENV = cc.env_from_identity(IDENT)


def _dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.config = self.root / ".claude.json"
        self.settings = self.root / ".claude" / "settings.json"

    def write_both(self, config_data, settings_data):
        _dump(self.config, config_data)
        _dump(self.settings, settings_data)

    def repair(self, **kw):
        return cc.check_and_repair(identity=IDENT, config_path=self.config,
                                   settings_path=self.settings, **kw)

    def plan(self, **kw):
        return cc.plan_repair(IDENT, self.config, self.settings, **kw)

    def read(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def backup(self, path):
        return Path(path).with_name(Path(path).name + cc.BACKUP_SUFFIX)


class NoDiffTests(Base):
    def test_healthy_files_are_not_touched(self):
        self.write_both({"env": dict(ENV), "hasCompletedOnboarding": True},
                        {"env": dict(ENV), "model": "fast-x"})
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in (self.config, self.settings)}

        result = self.repair()

        self.assertTrue(result.ok)
        self.assertFalse(result.repaired)
        self.assertEqual(result.changes, [])
        for path, (raw, mtime) in before.items():
            self.assertEqual(path.read_bytes(), raw, f"{path} 被重写了")
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertFalse(self.backup(path).exists(), "没有 diff 还生成了备份")

    def test_health_is_reported(self):
        self.write_both({"env": dict(ENV), "hasCompletedOnboarding": True},
                        {"env": dict(ENV)})
        self.assertEqual(len(self.plan().healthy), 2)


class FillTests(Base):
    def test_empty_files_get_all_managed_keys(self):
        self.write_both({}, {})
        result = self.repair()
        self.assertTrue(result.ok and result.repaired)

        config = self.read(self.config)
        settings = self.read(self.settings)
        self.assertEqual(config["env"], ENV)
        self.assertIs(config["hasCompletedOnboarding"], True)
        self.assertEqual(settings["env"], ENV)

    def test_user_fields_are_preserved(self):
        self.write_both({"env": {"FOO": "1"}, "theme": "dark"},
                        {"env": {"ANTHROPIC_CUSTOM_HEADERS": "X-A: 1"},
                         "model": "opus", "permissions": {"allow": ["Bash"]}})
        self.repair()

        config = self.read(self.config)
        settings = self.read(self.settings)
        self.assertEqual(config["theme"], "dark")
        self.assertEqual(config["env"]["FOO"], "1")
        self.assertEqual(settings["model"], "opus", "顶层 model 是用户在 /model 里的选择，不能动")
        self.assertEqual(settings["permissions"], {"allow": ["Bash"]})
        self.assertEqual(settings["env"]["ANTHROPIC_CUSTOM_HEADERS"], "X-A: 1")

    def test_onboarding_flag_alone_is_worth_a_write(self):
        # env 已经全对，只差 hasCompletedOnboarding —— 也得补（不然还会被地区检查拦住）
        self.write_both({"env": dict(ENV)}, {"env": dict(ENV)})
        result = self.repair()
        self.assertTrue(result.repaired)
        self.assertIs(self.read(self.config)["hasCompletedOnboarding"], True)
        self.assertEqual(self.read(self.config)["env"], ENV)

    def test_only_the_one_missing_file_is_created(self):
        _dump(self.settings, {"env": dict(ENV)})
        result = self.repair()
        self.assertTrue(result.repaired)
        self.assertTrue(self.config.exists())
        self.assertEqual(self.read(self.config)["env"], ENV)

    def test_both_missing_is_left_to_the_installer(self):
        plan = self.plan()
        self.assertEqual(plan.changes, [])
        self.assertFalse(self.config.exists())
        self.assertFalse(self.settings.exists())

    def test_repair_flag_may_create_both(self):
        # --repair 是显式的"按账号把配置修好"，两份都不在时该建出来
        result = self.repair(create_missing=True)
        self.assertTrue(result.repaired)
        self.assertEqual(self.read(self.config)["env"], ENV)
        self.assertEqual(self.read(self.settings)["env"], ENV)

    def test_missing_file_is_not_created_next_to_a_foreign_one(self):
        _dump(self.settings, {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid"}})
        result = self.repair(create_missing=True)
        self.assertFalse(self.config.exists(),
                         "另一份是别人的，就不能再补一份更高优先级的配置出来")
        self.assertFalse(result.repaired)


class DriftTests(Base):
    def setUp(self):
        super().setUp()
        self.write_both({"env": dict(ENV), "hasCompletedOnboarding": True},
                        {"env": dict(ENV)})

    def test_token_rotation_only_touches_the_token(self):
        settings = self.read(self.settings)
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-old"
        _dump(self.settings, settings)

        plan = self.plan()
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(plan.changes[0].keys, ["ANTHROPIC_AUTH_TOKEN"])

        self.repair()
        self.assertEqual(self.read(self.settings)["env"]["ANTHROPIC_AUTH_TOKEN"], TOKEN)
        # 没变的文件不重写
        self.assertFalse(self.backup(self.config).exists())

    def test_base_with_trailing_v1_counts_as_ours(self):
        settings = self.read(self.settings)
        settings["env"]["ANTHROPIC_BASE_URL"] = "https://aikaui.com/v1/"
        _dump(self.settings, settings)
        plan = self.plan()
        self.assertEqual([c.path for c in plan.changes], [self.settings])

    def test_foreign_provider_is_never_overwritten(self):
        foreign = {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid",
                           "ANTHROPIC_AUTH_TOKEN": "sk-mine"}}
        _dump(self.settings, dict(foreign))
        raw = self.settings.read_bytes()

        result = self.repair()
        self.assertTrue(result.ok)
        self.assertFalse(result.repaired)
        self.assertEqual(self.settings.read_bytes(), raw)
        self.assertTrue(any(p == self.settings for p, _ in result.skipped))

    def test_force_overwrites_foreign_provider(self):
        _dump(self.settings, {"env": {"ANTHROPIC_BASE_URL": "https://example.invalid",
                                      "ANTHROPIC_AUTH_TOKEN": "sk-mine"}})
        result = self.repair(force=True)
        self.assertTrue(result.repaired)
        self.assertEqual(self.read(self.settings)["env"], ENV)

    def test_dry_run_writes_nothing(self):
        settings = self.read(self.settings)
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-old"
        _dump(self.settings, settings)
        raw = self.settings.read_bytes()

        result = self.repair(dry_run=True)
        self.assertTrue(result.planned)
        self.assertEqual(self.settings.read_bytes(), raw)
        self.assertFalse(self.backup(self.settings).exists())


class BadFileTests(Base):
    def test_broken_json_is_never_overwritten(self):
        self.write_both("{ this is not json", {"env": dict(ENV)})
        raw = self.config.read_bytes()

        result = self.repair()

        self.assertEqual(self.config.read_bytes(), raw)
        self.assertFalse(result.repaired)
        self.assertTrue(any(p == self.config for p, _ in result.skipped))
        # 另一份是好的：不该因为这份坏了就把它也标成失败
        self.assertTrue(result.ok)

    def test_env_that_is_not_an_object(self):
        self.write_both({"env": ["nope"]}, {"env": dict(ENV)})
        raw = self.config.read_bytes()
        self.repair()
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertEqual(self.plan().skipped[0][1], "bad-env")
        self.assertIn("env", cc.SKIP_REASONS["bad-env"])

    def test_symlinked_config_is_skipped(self):
        real = self.root / "dotfiles" / "claude.json"
        _dump(real, {"env": dict(ENV)})
        try:
            self.config.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("这个平台/账户不允许建符号链接")
        _dump(self.settings, {"env": dict(ENV)})
        raw = real.read_bytes()

        result = self.repair()

        self.assertEqual(real.read_bytes(), raw)
        self.assertTrue(self.config.is_symlink(), "软链被原子替换换掉了")
        self.assertEqual(self.plan().skipped[0][1], "symlink")

    def test_write_failure_is_reported_not_hidden(self):
        self.write_both({}, {})
        result = self.repair(_writer=lambda path, data: None)   # 假装写了，其实没写
        self.assertFalse(result.ok)
        self.assertFalse(result.repaired)


class BackupTests(Base):
    def test_one_backup_with_the_original_bytes(self):
        self.write_both({"env": dict(ENV)}, {"env": dict(ENV)})
        settings = self.read(self.settings)
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-old"
        _dump(self.settings, settings)
        raw = self.settings.read_bytes()

        self.repair()

        bak = self.backup(self.settings)
        self.assertTrue(bak.is_file())
        self.assertEqual(bak.read_bytes(), raw)
        self.assertEqual([p.name for p in self.settings.parent.iterdir()
                          if p.name.endswith(cc.BACKUP_SUFFIX)],
                         ["settings.json" + cc.BACKUP_SUFFIX])


class SecretTests(Base):
    def test_summary_and_lines_never_contain_the_token(self):
        self.write_both({}, {})
        result = self.repair()
        text = result.summary + "\n" + "\n".join(result.lines()) + "\n" + result.problem
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("sk-test", text)
        self.assertIn("settings.json", result.summary)

    def test_plan_reasons_are_key_names_only(self):
        self.write_both({"env": dict(ENV), "hasCompletedOnboarding": True},
                        {"env": dict(ENV)})
        settings = self.read(self.settings)
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-old"
        _dump(self.settings, settings)
        for change in self.plan().changes:
            self.assertEqual(change.keys, ["ANTHROPIC_AUTH_TOKEN"])


class IdentityTests(Base):
    def test_no_identity_is_silent(self):
        self.write_both({}, {})
        result = cc.check_and_repair(identity={}, config_path=self.config,
                                     settings_path=self.settings)
        self.assertTrue(result.ok)
        self.assertFalse(result.repaired)
        self.assertEqual(result.problem, "no-identity")

    def test_missing_base_is_a_problem_not_a_guess(self):
        bad = {"api_key": TOKEN}
        result = cc.check_and_repair(identity=bad, config_path=self.config,
                                     settings_path=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.problem, "bad-identity")


class ApplyTests(Base):
    """安装/换账号时的那次写：坏文件同样不能覆盖。"""

    def setUp(self):
        super().setUp()
        self._saved = (cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS)
        cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS = self.config, self.settings
        self.addCleanup(self._restore)

    def _restore(self):
        cc.CLAUDE_CONFIG, cc.CLAUDE_SETTINGS = self._saved

    def test_apply_writes_both(self):
        ok, note = cc.apply(IDENT)
        self.assertTrue(ok, note)
        self.assertEqual(self.read(self.config)["env"], ENV)
        self.assertEqual(self.read(self.settings)["env"], ENV)
        self.assertEqual(self.read(self.settings)["model"], "fast-x")

    def test_apply_does_not_rebuild_a_broken_file(self):
        _dump(self.config, "{ broken")
        raw = self.config.read_bytes()
        ok, note = cc.apply(IDENT)
        self.assertEqual(self.config.read_bytes(), raw, "坏配置被重建了")
        self.assertIn("读不出来", note)
        # 另一份照样写好，不能因为一份坏了就整体放弃
        self.assertTrue(ok)
        self.assertEqual(self.read(self.settings)["env"], ENV)


if __name__ == "__main__":
    unittest.main(verbosity=2)
