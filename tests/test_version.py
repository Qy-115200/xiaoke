# -*- coding: utf-8 -*-
"""版本比较与清单校验。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import updater  # noqa: E402

SHA = "a" * 64
GOOD = {
    "version": "1.0.1",
    "url": "https://aikaui.com/releases/1.0.1/app.exe",
    "sha256": SHA,
    "size": 123,
    "min_version": "1.0.0",
}


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version(" 10.0.20 "), (10, 0, 20))

    def test_parse_rejects(self):
        for bad in ("1.2", "1.2.3.4", "v1.2.3", "1.2.beta", "", None, 123):
            self.assertIsNone(updater.parse_version(bad), bad)

    def test_newer(self):
        self.assertTrue(updater.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(updater.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("0.9.9", "1.0.0"))
        self.assertFalse(updater.is_newer("bad", "1.0.0"))


class ManifestTests(unittest.TestCase):
    def test_schema(self):
        self.assertIsNotNone(updater.validate_manifest({"schema_version": 1, "latest": {}}))
        self.assertIsNone(updater.validate_manifest({"schema_version": 2, "latest": {}}))
        self.assertIsNone(updater.validate_manifest({"latest": {}}))
        self.assertIsNone(updater.validate_manifest([]))

    def test_select_good_entry(self):
        m = {"schema_version": 1, "latest": {"linux-x64": dict(GOOD)}}
        got = updater.select_release(m, key="linux-x64", current="1.0.0")
        self.assertEqual(got["version"], "1.0.1")
        self.assertEqual(got["sha256"], SHA)

    def test_unknown_platform(self):
        m = {"schema_version": 1, "latest": {"solaris-x64": dict(GOOD)}}
        self.assertIsNone(updater.select_release(m, key="linux-x64"))

    def test_rejects_http(self):
        bad = dict(GOOD, url="http://aikaui.com/app.exe")
        m = {"schema_version": 1, "latest": {"linux-x64": bad}}
        self.assertIsNone(updater.select_release(m, key="linux-x64"))

    def test_rejects_bad_sha(self):
        for sha in ("abc", "Z" * 64, None):
            bad = dict(GOOD, sha256=sha)
            m = {"schema_version": 1, "latest": {"linux-x64": bad}}
            self.assertIsNone(updater.select_release(m, key="linux-x64"), sha)

    def test_rejects_bad_size(self):
        for size in (0, -1, "123", 10 ** 12, True):
            bad = dict(GOOD, size=size)
            m = {"schema_version": 1, "latest": {"linux-x64": bad}}
            self.assertIsNone(updater.select_release(m, key="linux-x64"), size)

    def test_rejects_not_newer(self):
        m = {"schema_version": 1, "latest": {"linux-x64": dict(GOOD, version="1.0.0")}}
        self.assertIsNone(updater.select_release(m, key="linux-x64", current="1.0.0"))

    def test_min_version(self):
        m = {"schema_version": 1, "latest": {"linux-x64": dict(GOOD)}}
        self.assertIsNone(updater.select_release(m, key="linux-x64", current="0.9.9"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
