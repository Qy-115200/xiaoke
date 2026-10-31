# -*- coding: utf-8 -*-
"""凭证相关的纯逻辑：打码、硬件指纹、保险箱（server/vault.py）。

三件事各测一层：

  _mask_secret      打码后不能露全文，但得留够让人认得出"是哪一条"
  fingerprint       同输入同输出、占位符算空、取不到一律 ""、绝不抛异常
  vault             存/读/清除的往返；**测试永远不许碰真实的注册表**

★ vault 的测试必须先把 `_force_file` 打开、`_file_override` 指到临时目录。
  默认（Windows）它写的是 HKCU 注册表 —— 测试往用户真实注册表里塞一条
  假账户码，是那种"跑一次测试就污染一台机器"的坑。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from helpers import _mask_secret                       # noqa: E402
from server import fingerprint as fplib                # noqa: E402
from server import vault                               # noqa: E402


class MaskTests(unittest.TestCase):
    def test_account_code_keeps_first_four(self):
        masked = _mask_secret("CC-AAAAAA-BBBBBB", keep=4)
        self.assertTrue(masked.startswith("CC-A"))
        self.assertIn("*", masked)
        self.assertNotIn("BBBBBB", masked)          # 后半截不能露出来
        self.assertNotEqual(masked, "CC-AAAAAA-BBBBBB")

    def test_account_key_masks_whole(self):
        masked = _mask_secret("k" * 64, keep=0)
        self.assertEqual(masked, "*" * 24)          # 任务书里的样子
        self.assertNotIn("k", masked)

    def test_empty_is_empty(self):
        self.assertEqual(_mask_secret(""), "")
        self.assertEqual(_mask_secret(None), "")
        self.assertEqual(_mask_secret("   "), "")

    def test_short_value_fully_masked(self):
        # 比 keep 还短的串不能原样漏出来
        self.assertEqual(_mask_secret("abc", keep=4), "***")

    def test_no_plaintext_survives(self):
        for raw in ("CC-123456-789ABC", "deadbeef" * 8):
            self.assertNotIn(raw[6:], _mask_secret(raw, keep=4))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        fplib._reset_cache()
        self.addCleanup(fplib._reset_cache)

    def test_compute_is_stable_and_32_hex(self):
        one = fplib.compute("BOARD-123", "BFEBFBFF000906A3")
        two = fplib.compute("BOARD-123", "BFEBFBFF000906A3")
        self.assertEqual(one, two)
        self.assertEqual(len(one), 32)
        int(one, 16)                                # 必须是合法十六进制
        self.assertNotEqual(one, fplib.compute("BOARD-124", "BFEBFBFF000906A3"))

    def test_whitespace_is_trimmed(self):
        self.assertEqual(fplib.compute("  BOARD-123 ", " cpu "),
                         fplib.compute("BOARD-123", "cpu"))

    def test_placeholder_board_yields_empty(self):
        # 厂商没填序列号时，"|" + CPU 会算出一个全网同型号共用的常量，
        # 那不是指纹。宁可返回空，让服务器按"不知道"处理。
        for bad in ("", "   ", "To be filled by O.E.M.", "Default string",
                    "None", "unknown", "System Serial Number"):
            self.assertEqual(fplib.compute(bad, "BFEBFBFF000906A3"), "",
                             f"{bad!r} 应该算作占位符")

    def test_cpu_placeholder_still_hashes(self):
        # CPU 缺失不致命：主板序列号是真的时候还能区分机器
        value = fplib.compute("BOARD-123", "")
        self.assertEqual(len(value), 32)

    def test_board_cpu_formula_is_frozen(self):
        # 线上已经入库的指纹全是老公式 sha256("board|cpu")[:32] 算的 —— 换了公式
        # 等于把那些账号的"按指纹找回"全部作废。这个值钉死，改公式这条就红。
        self.assertEqual(fplib.compute("BOARD-123", "BFEBFBFF000906A3"),
                         "ef803a4cc5d0773e46411edc6ea9b3e7")
        value, source = fplib.derive(
            {"board": "BOARD-123", "cpu": "BFEBFBFF000906A3",
             "machine_guid": "SOMETHING-ELSE"})
        self.assertEqual(source, "board+cpu")       # 主板可用时 MachineGuid 不参与
        self.assertEqual(value, "ef803a4cc5d0773e46411edc6ea9b3e7")

    def test_machine_guid_when_board_is_placeholder(self):
        value, source = fplib.derive(
            {"board": "Default string", "cpu": "BFEBFBFF000906A3",
             "machine_guid": "4f2a1c3e-1111-2222-3333-444455556666"})
        self.assertEqual(source, "machine-guid")
        self.assertEqual(len(value), 32)
        int(value, 16)

    def test_placeholder_guid_does_not_become_a_constant(self):
        # MachineGuid 理论上不该是占位串，但"读成 unknown"这种烂值不能当指纹用
        value, source = fplib.derive({"board": "", "cpu": "", "machine_guid": "unknown"})
        self.assertEqual((value, source), ("", ""))

    def test_macos_platform_uuid_beats_serial(self):
        parts = {"platform_uuid": "UUID-1", "serial": "SERIAL-1"}
        value, source = fplib.derive(parts)
        self.assertEqual(source, "platform-uuid")
        serial_only, src2 = fplib.derive({"serial": "SERIAL-1"})
        self.assertEqual(src2, "platform-serial")
        self.assertNotEqual(value, serial_only)

    def test_linux_machine_id_and_dmi(self):
        parts = {"machine_id": "abc123", "product_uuid": "UUID-2",
                 "board_serial": "BRD-9"}
        value, source = fplib.derive(parts)
        self.assertEqual(source, "machine-id+dmi")
        self.assertEqual(len(value), 32)
        # 缺一部分也不能变成空（服务器要求注册带上它）
        value2, source2 = fplib.derive({"machine_id": "abc123"})
        self.assertEqual(source2, "machine-id+dmi")
        self.assertNotEqual(value, value2)

    def test_container_hostname_changes_the_fingerprint(self):
        # 同一宿主机、同一镜像的两个容器 machine-id/DMI 完全一样 —— 只有 hostname
        # 能分开它们。分不开就等于"谁的指纹撞上来谁登进别人的号"。
        base = {"machine_id": "abc123", "product_uuid": "UUID-2"}
        a, _ = fplib.derive(dict(base, container="c1"))
        b, _ = fplib.derive(dict(base, container="c2"))
        self.assertNotEqual(a, b)

    def test_device_id_is_the_last_resort(self):
        value, source = fplib.derive({"device_id": "dev-abc"})
        self.assertEqual(source, "device-id")
        self.assertEqual(len(value), 32)

    def test_nothing_available_is_empty(self):
        self.assertEqual(fplib.derive({}), ("", ""))

    def test_never_empty_when_device_id_exists(self):
        # "永不空"：只读得到 device_id 也要给出指纹（服务器靠它防清配置刷号）
        with mock.patch.object(fplib, "raw_ids", return_value={}), \
                mock.patch.object(fplib, "_device_id_part", return_value="dev-abc"):
            info = fplib.get_device_fingerprint_info()
        self.assertEqual(len(info["fingerprint"]), 32)
        self.assertEqual(info["source"], "device-id")

    def test_empty_only_when_everything_fails(self):
        with mock.patch.object(fplib, "raw_ids", return_value={}), \
                mock.patch.object(fplib, "_device_id_part", return_value=""):
            self.assertEqual(fplib.get_device_fingerprint(), "")

    def test_never_raises(self):
        with mock.patch.object(fplib, "raw_ids", side_effect=RuntimeError("boom")):
            self.assertEqual(fplib.get_device_fingerprint(), "")

    def test_memoized_within_process(self):
        calls = []

        def fake():
            calls.append(1)
            return {"board": "BOARD-123", "cpu": "CPU-1"}

        with mock.patch.object(fplib, "raw_ids", side_effect=fake):
            first = fplib.get_device_fingerprint()
            second = fplib.get_device_fingerprint()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)             # 一个进程只起一次 PowerShell


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = (vault._force_file, vault._file_override)
        vault._force_file = True                    # ★ 绝不写真实注册表
        vault._file_override = Path(self.tmp.name) / "aikaui" / "credential.json"
        self.addCleanup(self._restore)

    def _restore(self):
        vault._force_file, vault._file_override = self._old

    def test_round_trip(self):
        self.assertTrue(vault.save("CC-AAAAAA-BBBBBB", "k" * 64, 42))
        got = vault.load()
        self.assertEqual(got["account_code"], "CC-AAAAAA-BBBBBB")
        self.assertEqual(got["account_key"], "k" * 64)
        self.assertEqual(got["user_id"], 42)

    def test_empty_code_is_not_saved(self):
        self.assertFalse(vault.save(""))
        self.assertFalse(vault.save(None))
        self.assertIsNone(vault.load())

    def test_missing_file_is_none(self):
        self.assertIsNone(vault.load())

    def test_corrupt_file_is_none(self):
        path = vault._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not json at all")
        self.assertIsNone(vault.load())

    def test_unknown_format_is_none(self):
        path = vault._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"aikv9:whatever")
        self.assertIsNone(vault.load())

    def test_clear_removes_it(self):
        vault.save("CC-AAAAAA-BBBBBB")
        self.assertTrue(vault.clear())
        self.assertIsNone(vault.load())

    def test_payload_never_keeps_empty_fields(self):
        vault.save("CC-AAAAAA-BBBBBB", "", None)
        self.assertEqual(vault.load(), {"account_code": "CC-AAAAAA-BBBBBB"})

    @unittest.skipIf(sys.platform == "win32", "Windows 上走注册表，没有文件权限这回事")
    def test_file_is_private(self):
        vault.save("CC-AAAAAA-BBBBBB")
        mode = vault._file_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_decrypt_rejects_unknown_magic(self):
        self.assertIsNone(vault._decrypt(b"aikv9:x"))
        self.assertEqual(vault._decrypt(b"aikv1:u:hello"), b"hello")


if __name__ == "__main__":
    unittest.main()
