# -*- coding: utf-8 -*-
"""下次启动替换自己的逻辑：备份、回滚、失败上限、不满足条件不动手。

全部在临时目录里跑，不碰真实安装。
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

CURRENT = "1.0.0"
NEW = "1.0.1"


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {}
        for name in ("UPDATES_DIR", "MANIFEST_CACHE", "PENDING_FILE", "APPLIED_FILE",
                     "FAILED_FILE", "LOCK_FILE", "LOG_FILE", "STAGED_DIR", "APP_VERSION"):
            self._saved[name] = getattr(updater, name)
        updater.UPDATES_DIR = self.root / "updates"
        updater.MANIFEST_CACHE = updater.UPDATES_DIR / "manifest.json"
        updater.PENDING_FILE = updater.UPDATES_DIR / "pending.json"
        updater.APPLIED_FILE = updater.UPDATES_DIR / "applied.json"
        updater.FAILED_FILE = updater.UPDATES_DIR / "failed.json"
        updater.LOCK_FILE = updater.UPDATES_DIR / ".lock"
        updater.LOG_FILE = updater.UPDATES_DIR / "update.log"
        updater.STAGED_DIR = updater.UPDATES_DIR / "staged"
        updater.APP_VERSION = CURRENT

        self.target = self.root / "ClaudeCodeAssistant.exe"
        self.target.write_bytes(b"old version")
        self.staged = updater.STAGED_DIR / NEW / "ClaudeCodeAssistant.exe"
        self.staged.parent.mkdir(parents=True)
        self.staged.write_bytes(b"new version")

        updater._write_json(updater.PENDING_FILE, {
            "version": NEW,
            "platform": "windows-x64",
            "staged": str(self.staged),
            "sha256": "a" * 64,
            "size": 11,
            "notes": "",
            "created_at": int(time.time()),
            "attempts": 0,
        })

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(updater, name, value)
        self.tmp.cleanup()

    def _apply(self, relaunch=False, argv=("app.exe", "--flag")):
        with mock.patch.object(updater.sys, "frozen", True, create=True), \
                mock.patch.object(updater, "_target_path", return_value=self.target), \
                mock.patch.object(updater, "_relaunch", return_value=relaunch) as rl:
            self.relaunch_mock = rl
            return updater.apply_pending_update(list(argv))

    # ---------- 正常路径 ----------

    def test_replace_and_backup(self):
        self.assertTrue(self._apply())
        self.assertEqual(self.target.read_bytes(), b"new version")
        backups = list(self.root.glob("ClaudeCodeAssistant.exe.old-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old version")
        self.assertFalse(updater.PENDING_FILE.exists())
        applied = json.loads(updater.APPLIED_FILE.read_text(encoding="utf-8"))
        self.assertEqual(applied["version"], NEW)
        self.assertFalse(applied["notified"])

    def test_exits_after_relaunch(self):
        with self.assertRaises(SystemExit) as ctx:
            self._apply(relaunch=True)
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(self.target.read_bytes(), b"new version")
        self.assertFalse(updater.PENDING_FILE.exists())

    def test_relaunch_keeps_argv(self):
        self.assertTrue(self._apply())
        self.relaunch_mock.assert_called_once()
        argv = self.relaunch_mock.call_args[0][1]
        self.assertEqual(argv[1:], ["--flag"])

    # ---------- 失败路径 ----------

    def test_rollback_keeps_old(self):
        with mock.patch.object(updater.shutil, "copy2", side_effect=OSError("locked")):
            self.assertFalse(self._apply())
        self.assertEqual(self.target.read_bytes(), b"old version")
        self.assertEqual(list(self.root.glob("*.new-*")), [])
        # 备份已经换回原名，不该留着
        self.assertEqual(list(self.root.glob("*.old-*")), [])
        pending = updater.read_pending()
        self.assertEqual(pending["attempts"], 1)

    def test_attempts_cap_three(self):
        with mock.patch.object(updater, "_replace", return_value=False):
            self.assertFalse(self._apply())
            self.assertFalse(self._apply())
            self.assertFalse(self._apply())
        self.assertFalse(updater.PENDING_FILE.exists())
        self.assertTrue(updater.FAILED_FILE.exists())
        failed = json.loads(updater.FAILED_FILE.read_text(encoding="utf-8"))
        self.assertEqual(failed["version"], NEW)
        self.assertFalse((updater.STAGED_DIR / NEW).exists())
        self.assertEqual(self.target.read_bytes(), b"old version")

    def test_not_newer_is_dropped(self):
        pending = updater.read_pending()
        pending["version"] = CURRENT
        updater._write_json(updater.PENDING_FILE, pending)
        self.assertFalse(self._apply())
        self.assertFalse(updater.PENDING_FILE.exists())
        self.assertFalse((updater.STAGED_DIR / NEW).exists())
        self.assertEqual(self.target.read_bytes(), b"old version")

    def test_missing_staged_drops_marker(self):
        self.staged.unlink()
        self.assertFalse(self._apply())
        self.assertFalse(updater.PENDING_FILE.exists())
        self.assertEqual(self.target.read_bytes(), b"old version")

    def test_lock_held_skips(self):
        updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        updater.LOCK_FILE.write_text("999", encoding="ascii")
        self.assertFalse(self._apply())
        self.assertTrue(updater.PENDING_FILE.exists())
        self.assertEqual(self.target.read_bytes(), b"old version")

    def test_stale_lock_is_taken_over(self):
        updater.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        updater.LOCK_FILE.write_text("999", encoding="ascii")
        stale = time.time() - updater.UPDATE_LOCK_STALE - 60
        os_utime(updater.LOCK_FILE, stale)
        self.assertTrue(self._apply())
        self.assertEqual(self.target.read_bytes(), b"new version")

    def test_no_pending_returns_false(self):
        updater._rm(updater.PENDING_FILE)
        self.assertFalse(self._apply())

    def test_refuses_without_frozen(self):
        # 没有 frozen、也没有 AGENT_UPDATE_TEST，开发环境不该动安装目录
        with mock.patch.dict(updater.os.environ, {}, clear=False):
            updater.os.environ.pop("AGENT_UPDATE_TEST", None)
            with mock.patch.object(updater.sys, "frozen", False, create=True):
                self.assertFalse(updater.apply_pending_update(["app.exe"]))
        self.assertEqual(self.target.read_bytes(), b"old version")
        self.assertTrue(updater.PENDING_FILE.exists())

    def test_dev_mode_never_replaces_interpreter(self):
        # AGENT_UPDATE_TEST=1 但没打包：_target_path() 指向 python.exe，必须拒绝
        with mock.patch.dict(updater.os.environ, {"AGENT_UPDATE_TEST": "1"}), \
                mock.patch.object(updater.sys, "frozen", False, create=True):
            self.assertFalse(updater.apply_pending_update(["python.exe"]))
        self.assertTrue(updater.PENDING_FILE.exists())

    # ---------- 更新提示 ----------

    def test_applied_notice_once(self):
        updater._write_json(updater.APPLIED_FILE, {
            "version": CURRENT, "from": "0.9.9",
            "at": int(time.time()), "notified": False,
        })
        first = updater.consume_applied_notice()
        self.assertIn(CURRENT, first)
        self.assertIsNone(updater.consume_applied_notice())

    def test_applied_notice_other_version(self):
        updater._write_json(updater.APPLIED_FILE, {
            "version": "9.9.9", "from": "0.9.9",
            "at": int(time.time()), "notified": False,
        })
        self.assertIsNone(updater.consume_applied_notice())


def os_utime(path, stamp):
    import os
    os.utime(path, (stamp, stamp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
