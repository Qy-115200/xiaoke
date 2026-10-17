# -*- coding: utf-8 -*-
"""下载与清单拉取的离线测试：本地起一个假服务器，不碰外网。"""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

BINARY = b"ClaudeCodeAssistant fake build " * 100
SHA = hashlib.sha256(BINARY).hexdigest()

_FILES = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in _FILES:
            self.send_response(404)
            self.end_headers()
            return
        body = _FILES[self.path]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {}
        for name in ("UPDATES_DIR", "MANIFEST_CACHE", "PENDING_FILE", "APPLIED_FILE",
                     "FAILED_FILE", "LOCK_FILE", "LOG_FILE", "STAGED_DIR"):
            self._saved[name] = getattr(updater, name)
        updater.UPDATES_DIR = self.root
        updater.MANIFEST_CACHE = self.root / "manifest.json"
        updater.PENDING_FILE = self.root / "pending.json"
        updater.APPLIED_FILE = self.root / "applied.json"
        updater.FAILED_FILE = self.root / "failed.json"
        updater.LOCK_FILE = self.root / ".lock"
        updater.LOG_FILE = self.root / "update.log"
        updater.STAGED_DIR = self.root / "staged"
        _FILES.clear()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(updater, name, value)
        self.tmp.cleanup()

    # ---------- 清单 ----------

    def test_fetch_manifest_and_cache_fallback(self):
        manifest = {"schema_version": 1, "latest": {}}
        _FILES["/latest.json"] = json.dumps(manifest).encode()
        self.assertEqual(updater.fetch_manifest(self.base + "/latest.json")["schema_version"], 1)
        self.assertTrue(updater.MANIFEST_CACHE.exists())

        # 服务器挂了也要能退回缓存
        _FILES.clear()
        got = updater.fetch_manifest(self.base + "/latest.json", timeout=(1, 2))
        self.assertIsNotNone(got)
        self.assertEqual(got["schema_version"], 1)

    def test_fetch_manifest_garbage(self):
        _FILES["/latest.json"] = b"not json"
        self.assertIsNone(updater.fetch_manifest(self.base + "/latest.json"))
        self.assertFalse(updater.MANIFEST_CACHE.exists())

    # ---------- 下载 ----------

    def _entry(self, **over):
        entry = {
            "version": "1.0.1",
            "url": self.base + "/app.bin",
            "sha256": SHA,
            "size": len(BINARY),
            "min_version": "1.0.0",
            "notes": "",
        }
        entry.update(over)
        return entry

    def test_download_ok(self):
        _FILES["/app.bin"] = BINARY
        path = updater.download_update(self._entry())
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), BINARY)
        self.assertEqual(list(updater.STAGED_DIR.rglob("*.part")), [])

    def test_hash_mismatch_cleans_part(self):
        _FILES["/app.bin"] = BINARY
        path = updater.download_update(self._entry(sha256="b" * 64))
        self.assertIsNone(path)
        self.assertEqual(list(updater.STAGED_DIR.rglob("*.part")), [])
        self.assertFalse((updater.STAGED_DIR / "1.0.1" / "app.bin").exists())

    def test_size_mismatch(self):
        _FILES["/app.bin"] = BINARY
        self.assertIsNone(updater.download_update(self._entry(size=len(BINARY) + 1)))

    def test_404(self):
        self.assertIsNone(updater.download_update(self._entry()))

    def test_prepare_update_writes_pending(self):
        # 本地的假服务器只有 http，而 select_release 只放行 https，
        # 所以这里直接给它一个已经选好的条目，专测下载 → 暂存 → 写标记。
        entry = self._entry(url=self.base + "/app.bin")
        _FILES["/app.bin"] = BINARY
        with mock.patch.object(updater, "select_release", return_value=entry), \
                mock.patch.object(updater.platform_support, "platform_key", return_value="linux-x64"), \
                mock.patch.object(updater.platform_support, "is_macos", return_value=False), \
                mock.patch.object(updater.platform_support, "is_linux", return_value=False):
            ready = updater.prepare_update({"schema_version": 1, "latest": {}})
        self.assertIsNotNone(ready)
        self.assertTrue(updater.PENDING_FILE.exists())
        pending = updater.read_pending()
        self.assertEqual(pending["version"], "1.0.1")
        self.assertTrue(Path(pending["staged"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
