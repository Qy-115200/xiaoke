# -*- coding: utf-8 -*-
"""Node 自动安装：选版本、拼地址、校验、解压、写 PATH。全程离线。"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import node_setup  # noqa: E402
from server.cli import EXIT_NODE, CliContext, CliError  # noqa: E402

INDEX = [
    {"version": "v26.3.0", "lts": False, "files": ["linux-x64", "linux-arm64"]},
    {"version": "v24.22.1", "lts": "Krypton", "files": ["linux-x64", "linux-arm64"]},
    {"version": "v22.11.0", "lts": "Jod", "files": ["linux-x64"]},
    {"version": "v20.19.0", "lts": "Iron", "files": ["linux-x64"]},   # 太老
    {"version": "v24.9.0", "lts": "Krypton", "files": ["linux-arm64"]},  # 没有 x64 包
]


class SemverTests(unittest.TestCase):
    def test_parse(self):
        cases = {
            "v24.22.1": (24, 22, 1),
            "24.22.1": (24, 22, 1),
            "v22.11.0-pre": (22, 11, 0),
            "v18": (18,),
            "": (),
            "abc": (),
            "v.1.2": (),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(node_setup.parse_semver(text), want)

    def test_version_ok(self):
        self.assertTrue(node_setup.node_version_ok("v22.0.0"))
        self.assertTrue(node_setup.node_version_ok("v24.22.1"))
        self.assertFalse(node_setup.node_version_ok("v20.19.0"))
        self.assertFalse(node_setup.node_version_ok(""))

    def test_detect_arch(self):
        self.assertEqual(node_setup.detect_node_arch("x86_64"), "x64")
        self.assertEqual(node_setup.detect_node_arch("AMD64"), "x64")
        self.assertEqual(node_setup.detect_node_arch("aarch64"), "arm64")
        self.assertIsNone(node_setup.detect_node_arch("riscv64"))


class SelectTests(unittest.TestCase):
    def test_picks_highest_lts_with_that_arch(self):
        entry = node_setup.select_latest_lts(INDEX, "x64")
        self.assertEqual(entry["version"], "v24.22.1")   # 26 是 current，20 太老

    def test_arm64_can_pick_a_different_version(self):
        entry = node_setup.select_latest_lts(INDEX, "arm64")
        self.assertEqual(entry["version"], "v24.22.1")

    def test_order_does_not_matter(self):
        shuffled = list(reversed(INDEX))
        self.assertEqual(node_setup.select_latest_lts(shuffled, "x64")["version"],
                         "v24.22.1")

    def test_skips_current_releases(self):
        index = [{"version": "v26.3.0", "lts": False, "files": ["linux-x64"]}]
        self.assertIsNone(node_setup.select_latest_lts(index, "x64"))

    def test_no_match(self):
        self.assertIsNone(node_setup.select_latest_lts(INDEX, "riscv64"))
        self.assertIsNone(node_setup.select_latest_lts([], "x64"))
        self.assertIsNone(node_setup.select_latest_lts("nope", "x64"))

    def test_pinned(self):
        self.assertEqual(node_setup.pick_pinned(INDEX, "22.11.0")["version"], "v22.11.0")
        self.assertEqual(node_setup.pick_pinned(INDEX, "v22.11.0")["version"], "v22.11.0")
        self.assertIsNone(node_setup.pick_pinned(INDEX, "99.0.0"))


class UrlsTests(unittest.TestCase):
    def test_asset_name_and_urls(self):
        self.assertEqual(node_setup.asset_name("v24.22.1", "x64"),
                         "node-v24.22.1-linux-x64.tar.xz")
        self.assertEqual(node_setup.asset_url("v24.22.1", "arm64"),
                         "https://cdn.npmmirror.com/binaries/node/v24.22.1/"
                         "node-v24.22.1-linux-arm64.tar.xz")
        self.assertEqual(node_setup.shasums_url("24.22.1"),
                         "https://cdn.npmmirror.com/binaries/node/v24.22.1/"
                         "SHASUMS256.txt")

    def test_parse_shasums(self):
        text = (
            "0" * 64 + "  node-v24.22.1-linux-x64.tar.xz\n"
            + "A" * 64 + " *node-v24.22.1-linux-arm64.tar.xz\n"
            "坏行\n"
            "1234 太短.txt\n"
        )
        table = node_setup.parse_shasums(text)
        self.assertEqual(table["node-v24.22.1-linux-x64.tar.xz"], "0" * 64)
        self.assertEqual(table["node-v24.22.1-linux-arm64.tar.xz"], "a" * 64)
        self.assertEqual(len(table), 2)


# ================= 集成：假 HTTP，真解压 =================

def _make_tarball(version: str, arch: str) -> bytes:
    """造一个和官方包同形状的 .tar.gz（bin/node 换成一句话脚本）。"""
    buf = io.BytesIO()
    root = f"node-v{version}-linux-{arch}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in (("node", "#!/bin/sh\necho v24.22.1\n"),
                           ("npm", "#!/bin/sh\necho 10.9.0\n")):
            data = body.encode()
            info = tarfile.TarInfo(f"{root}/bin/{name}")
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeHttp:
    def __init__(self, index, tarball, shasums):
        self.index = index
        self.tarball = tarball
        self.shasums = shasums
        self.downloads = []

    def get_json(self, url, timeout=None):
        return self.index

    def get_text(self, url, timeout=None):
        return self.shasums

    def download(self, url, dest, timeout=None, expect_sha256="", expect_size=None):
        Path(dest).write_bytes(self.tarball)
        self.downloads.append(url)
        return str(dest)


class EnsureNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _ctx(self, http, env=None):
        return CliContext(home=self.home,
                          env=env if env is not None else {"PATH": "", "HOME": str(self.home)},
                          http=http, quiet=True)

    def test_uses_existing_node_and_downloads_nothing(self):
        http = FakeHttp([], b"", "")
        ctx = self._ctx(http)
        with mock.patch.object(node_setup, "find_system_node",
                               return_value=("/usr/bin/node", "v24.0.0")):
            with mock.patch("shutil.which", return_value="/usr/bin/npm"):
                info = node_setup.ensure_node(ctx, arch="x64")
        self.assertFalse(info["installed"])
        self.assertEqual(info["node"], "/usr/bin/node")
        self.assertEqual(http.downloads, [])

    def test_installs_into_local_when_missing(self):
        tarball = _make_tarball("24.22.1", "x64")
        digest = hashlib.sha256(tarball).hexdigest()
        shasums = f"{digest}  node-v24.22.1-linux-x64.tar.xz\n"
        http = FakeHttp(INDEX, tarball, shasums)
        ctx = self._ctx(http)

        with mock.patch.object(node_setup, "find_system_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "find_private_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "_node_version", return_value="v24.22.1"):
            info = node_setup.ensure_node(ctx, arch="x64")

        self.assertTrue(info["installed"])
        self.assertEqual(info["version"], "v24.22.1")
        installed = self.home / ".local" / "node-v24.22.1" / "bin" / "node"
        self.assertTrue(installed.is_file(), info)
        # ~/.local/bin 里要有 node / npm
        self.assertTrue((self.home / ".local" / "bin" / "node").exists())
        self.assertTrue((self.home / ".local" / "bin" / "npm").exists())
        # 当前进程的 PATH 立刻生效
        self.assertIn(str(installed.parent), ctx.env["PATH"])
        # shell 配置里写了 PATH 块
        bashrc = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertIn(node_setup.MARK_BEGIN, bashrc)
        self.assertIn(str(installed.parent), bashrc)
        self.assertEqual(http.downloads[0].rsplit("/", 1)[-1],
                         "node-v24.22.1-linux-x64.tar.xz")

    def test_sha_mismatch_is_fatal(self):
        tarball = _make_tarball("24.22.1", "x64")
        http = FakeHttp(INDEX, tarball,
                        "f" * 64 + "  node-v24.22.1-linux-x64.tar.xz\n")
        ctx = self._ctx(http)
        with mock.patch.object(node_setup, "find_system_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "find_private_node", return_value=(None, "")):
            with self.assertRaises(CliError) as cm:
                node_setup.ensure_node(ctx, arch="x64")
        self.assertEqual(cm.exception.exit_code, EXIT_NODE)
        self.assertIn("校验失败", cm.exception.message)

    def test_mirror_down_gives_actionable_error(self):
        class DeadHttp:
            def get_json(self, url, timeout=None):
                raise OSError("连接被拒绝")
        ctx = self._ctx(DeadHttp())
        with mock.patch.object(node_setup, "find_system_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "find_private_node", return_value=(None, "")):
            with self.assertRaises(CliError) as cm:
                node_setup.ensure_node(ctx, arch="x64")
        self.assertEqual(cm.exception.exit_code, EXIT_NODE)
        self.assertIn("nodejs.org", cm.exception.hint)

    def test_dry_run_touches_nothing(self):
        http = FakeHttp(INDEX, b"", "")
        ctx = CliContext(home=self.home,
                         env={"PATH": ""}, http=http, quiet=True, dry_run=True)
        with mock.patch.object(node_setup, "find_system_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "find_private_node", return_value=(None, "")):
            info = node_setup.ensure_node(ctx, arch="x64")
        self.assertTrue(info["planned"])
        self.assertEqual(http.downloads, [])
        self.assertFalse((self.home / ".local").exists())

    def test_reuses_previously_installed_node(self):
        http = FakeHttp(INDEX, b"", "")
        ctx = self._ctx(http)
        with mock.patch.object(node_setup, "find_system_node", return_value=(None, "")), \
             mock.patch.object(node_setup, "find_private_node",
                               return_value=("/home/u/.local/node/bin/node", "v24.22.1")):
            info = node_setup.ensure_node(ctx, arch="x64")
        self.assertFalse(info["installed"])
        self.assertEqual(http.downloads, [])

    def test_unknown_arch_fails_fast(self):
        ctx = self._ctx(FakeHttp([], b"", ""))
        with mock.patch.object(node_setup, "detect_node_arch", return_value=None):
            with self.assertRaises(CliError) as cm:
                node_setup.ensure_node(ctx, arch=None)
        self.assertIn("架构", cm.exception.message)


class ShellPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_block_written_once(self):
        from server.cli import ensure_shell_path
        first = ensure_shell_path(self.home, self.home / ".local",
                                  extra_dirs=[self.home / ".local" / "node-v24" / "bin"])
        second = ensure_shell_path(self.home, self.home / ".local",
                                   extra_dirs=[self.home / ".local" / "node-v24" / "bin"])
        self.assertTrue(first)
        self.assertEqual(second, [])            # 第二次一个字都不动
        text = (self.home / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual(text.count(node_setup.MARK_BEGIN), 1)

    def test_existing_user_block_untouched(self):
        from server.cli import ensure_shell_path
        rc = self.home / ".bashrc"
        rc.write_text("# 我的 PATH\nexport PATH=$HOME/bin:$PATH\n", encoding="utf-8")
        ensure_shell_path(self.home, self.home / ".local")
        self.assertTrue(rc.read_text(encoding="utf-8").startswith("# 我的 PATH"))

    def test_zsh_gets_zshrc(self):
        from server.cli import ensure_shell_path
        ensure_shell_path(self.home, self.home / ".local", shell="/bin/zsh")
        self.assertTrue((self.home / ".zshrc").exists())


class NoSudoTests(unittest.TestCase):
    def test_no_privilege_escalation_in_source(self):
        text = (ROOT / "server" / "node_setup.py").read_text(encoding="utf-8")
        # 只检查"调用了 sudo"这种写法；注释里出现 sudo 不算
        for bad in ('"sudo"', "'sudo'", '["sudo"'):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
