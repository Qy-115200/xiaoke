# -*- coding: utf-8 -*-
"""platform_support 的离线测试（不联网、不真的开终端）。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_support  # noqa: E402
from config import HOME  # noqa: E402
from platform_support.common import find_claude, safe_cwd  # noqa: E402
from server.tools_fixed import parse_protocol_url  # noqa: E402


class ProtocolUrlTests(unittest.TestCase):
    def test_recharge(self):
        self.assertEqual(
            parse_protocol_url("aikaui://recharge?user_id=7"),
            {"action": "recharge", "user_id": "7"},
        )

    def test_trailing_slash_and_case(self):
        self.assertEqual(parse_protocol_url("AIKAUI://Recharge/")["action"], "recharge")

    def test_not_ours(self):
        self.assertEqual(parse_protocol_url("https://aikaui.com"), {})
        self.assertEqual(parse_protocol_url(""), {})


class SafeCwdTests(unittest.TestCase):
    def test_empty_means_home(self):
        cwd, note = safe_cwd("")
        self.assertEqual(cwd, HOME)
        self.assertEqual(note, "")

    def test_inside_home(self):
        with tempfile.TemporaryDirectory(dir=HOME) as d:
            target = Path(d) / "projects" / "demo"
            cwd, note = safe_cwd(str(target))
            self.assertEqual(cwd.resolve(), target.resolve())
            self.assertEqual(note, "")
            self.assertTrue(target.is_dir())

    def test_outside_home_is_refused(self):
        outside = "C:\\Windows" if platform_support.is_windows() else "/etc"
        cwd, note = safe_cwd(outside)
        self.assertEqual(cwd, HOME)
        self.assertNotEqual(note, "")


class FindClaudeTests(unittest.TestCase):
    def test_no_candidates_does_not_crash(self):
        # 没有候选路径时会退回 shutil.which("claude")：这台机器可能真装了，
        # 所以只要求"返回 None 或路径字符串"，不假设一定没有。
        self.assertIn(type(find_claude([])), (str, type(None)))

    def test_existing_path_is_returned(self):
        with tempfile.NamedTemporaryFile(dir=HOME, suffix="-claude") as f:
            self.assertEqual(find_claude([Path(f.name)]), f.name)


class PlatformApiTests(unittest.TestCase):
    def test_platform_key_has_known_prefix(self):
        key = platform_support.platform_key()
        self.assertTrue(
            key.startswith(("windows-", "macos-", "linux-")), key
        )

    def test_shell_command_shape(self):
        argv, use_shell = platform_support.shell_command("echo hi")
        if platform_support.is_windows():
            self.assertIsInstance(argv, str)
            self.assertTrue(use_shell)
            self.assertIn("chcp 65001", argv)
        else:
            self.assertIsInstance(argv, list)
            self.assertFalse(use_shell)
            self.assertEqual(argv[-1], "echo hi")

    def test_desktop_dir_is_path(self):
        self.assertIsInstance(platform_support.desktop_dir(), Path)


class FingerprintPartsTests(unittest.TestCase):
    """原始标识的读取层：只验"读到什么键"，不验哈希（那在 test_credentials）。

    ★ 不能真去读这台机器的硬件 —— 全部把底层读法 mock 掉。"""

    def test_windows_parts(self):
        from platform_support import windows

        with mock.patch.object(windows, "_cim_ids",
                               return_value={"board": " BRD-1 ", "cpu": "CPU-1"}), \
                mock.patch.object(windows, "_machine_guid", return_value="GUID-1"):
            parts = windows.fingerprint_parts()
        self.assertEqual(parts, {"board": "BRD-1", "cpu": "CPU-1",
                                 "machine_guid": "GUID-1"})

    def test_windows_never_raises(self):
        from platform_support import windows

        with mock.patch.object(windows, "_cim_ids", side_effect=RuntimeError("boom")), \
                mock.patch.object(windows, "_machine_guid", side_effect=RuntimeError("boom")):
            # 读硬件失败绝不能把注册搞挂：返回空键，上层还有别的来源可退
            self.assertEqual(windows.fingerprint_parts(),
                             {"board": "", "cpu": "", "machine_guid": ""})

    def test_macos_parses_ioreg(self):
        from platform_support import macos

        stdout = (
            '    "IOPlatformSerialNumber" = "SER-1"\n'
            '    "IOPlatformUUID" = "UUID-1"\n'
            '    "IOPlatformProduct" = "MacBookPro18,3"\n'
        )

        class _Proc:
            returncode = 0
            stdout = ""

        proc = _Proc()
        proc.stdout = stdout

        with mock.patch.object(macos.subprocess, "run", return_value=proc) as run:
            parts = macos.fingerprint_parts()
        self.assertEqual(parts, {"platform_uuid": "UUID-1", "serial": "SER-1"})
        self.assertEqual(run.call_args[0][0][0], "ioreg")

    def test_linux_reads_files_and_container(self):
        from platform_support import linux

        values = {
            "/etc/machine-id": "abc123\n",
            "/sys/class/dmi/id/product_uuid": "UUID-2\n",
            "/sys/class/dmi/id/board_serial": "BRD-2\n",
        }
        with mock.patch.object(linux, "_read_first",
                               side_effect=lambda paths: next(
                                   (values[p] for p in paths if p in values), "").strip()), \
                mock.patch.object(linux, "_in_container", return_value=True), \
                mock.patch.object(linux.socket, "gethostname", return_value="c1"):
            parts = linux.fingerprint_parts()
        self.assertEqual(parts, {"machine_id": "abc123", "product_uuid": "UUID-2",
                                 "board_serial": "BRD-2", "container": "c1"})

    def test_backend_without_impl_returns_empty(self):
        # 平台后端没实现 → {}，调用方（server/fingerprint.py）必须能接受
        with mock.patch.object(platform_support, "_impl",
                               return_value=object()):
            self.assertEqual(platform_support.fingerprint_parts(), {})


class WindowsClaudePathTests(unittest.TestCase):
    """便携版 Node 那次的真机故障（2026-11-02）。

    msiexec 装不上 → 助手把 Node 解到 %USERPROFILE%\\nodejs、用临时的 `set PATH=`
    跑 npm install -g ⇒ claude.cmd 落在 %USERPROFILE%\\nodejs 里，既不在标准目录
    也不在 PATH。原来 find_claude_path() 三处都落空，装好的 claude 被判"没装好"。
    """

    def setUp(self):
        from platform_support import windows
        self.windows = windows
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _claude_in(self, *parts) -> Path:
        p = self.home.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("@echo off\n", encoding="utf-8")
        return p

    def test_portable_node_dir_is_found(self):
        want = self._claude_in("nodejs", "claude.cmd")
        with mock.patch.object(self.windows, "HOME", self.home), \
                mock.patch("platform_support.common.shutil.which", return_value=None):
            self.assertEqual(self.windows.find_claude_path(), str(want))

    def test_portable_node_dir_is_on_the_child_path(self):
        # 不补进 PATH 的话，装完 Node 之后模型每条命令都得自己 set PATH=
        with mock.patch.object(self.windows, "HOME", self.home):
            self.assertIn(self.home / "nodejs", self.windows.path_dirs())

    def test_npm_prefix_is_the_last_resort(self):
        npm = self._claude_in("nodejs", "npm.cmd")
        prefix = self.home / "elsewhere"
        want = self._claude_in("elsewhere", "claude.cmd")

        def fake_run_capture(argv, **kwargs):
            self.assertEqual(argv[0], str(npm))
            self.assertEqual(argv[1:], ["prefix", "-g"])
            return str(prefix).encode() + b"\r\n", b"", False, 0

        with mock.patch.object(self.windows, "HOME", self.home), \
                mock.patch.object(self.windows, "run_capture", side_effect=fake_run_capture), \
                mock.patch("platform_support.common.shutil.which", return_value=None):
            self.assertEqual(self.windows.find_claude_path(), str(want))

    def test_npm_prefix_failure_is_silent(self):
        self._claude_in("nodejs", "npm.cmd")
        with mock.patch.object(self.windows, "HOME", self.home), \
                mock.patch.object(self.windows, "run_capture",
                                  side_effect=OSError("no npm")), \
                mock.patch("platform_support.common.shutil.which", return_value=None):
            self.assertIsNone(self.windows.find_claude_path())


if __name__ == "__main__":
    unittest.main(verbosity=2)
