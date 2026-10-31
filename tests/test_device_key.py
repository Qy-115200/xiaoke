# -*- coding: utf-8 -*-
"""本机密钥（server/device_key.py）的存储与规矩。

只验证三件事：
  1. 生成一次、之后一直用同一串（换一串就等于换了一台机器，防刷号就失效了）；
  2. 存在**比 identity.json 更深**的地方 —— vault 的另一个槽位，且 vault.clear()
     （换账号）不能把它一起清掉；
  3. 任何失败都只是"这次没发"，绝不抛异常、绝不写出半串。

★ 和 test_credentials.py 一样：vault 强制文件后端、落在临时目录，
  绝不碰这台机器真实的 HKCU 注册表。
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import device_key                          # noqa: E402
from server import vault                               # noqa: E402


class DeviceKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old = (vault._force_file, vault._file_override)
        vault._force_file = True                        # ★ 绝不写真实注册表
        vault._file_override = Path(self.tmp.name) / "aikaui" / "credential.json"
        self.addCleanup(self._restore)

    def _restore(self):
        vault._force_file, vault._file_override = self._old

    def test_first_call_generates_64_hex(self):
        self.assertEqual(device_key.get(), "")
        key = device_key.get_or_create()
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(device_key.get(), key)

    def test_same_value_on_every_later_call(self):
        first = device_key.get_or_create()
        for _ in range(3):
            self.assertEqual(device_key.get_or_create(), first)

    def test_stored_beside_the_credential_not_inside_it(self):
        key = device_key.get_or_create()
        vault.save("CC-AAAAAA-BBBBBB", "k" * 64, 42)
        # 凭证文件里绝不能出现本机密钥（Windows 上它是 DPAPI 密文，用 replace 解码）
        cred = Path(vault._file_override).read_bytes().decode("utf-8", "replace")
        self.assertNotIn(key, cred)
        self.assertNotIn("device_key", cred)
        # 它在同目录的另一个文件里（加密与否看平台，读回来必须还是同一串）
        state = Path(vault._file_override).with_name(".state")
        self.assertTrue(state.exists(), "本机密钥没有落到 .state")
        self.assertEqual(vault.load_device_key(), key)

    @unittest.skipIf(os.name == "nt", "Windows 上 chmod 不表示权限")
    def test_file_is_owner_only(self):
        device_key.get_or_create()
        state = Path(vault._file_override).with_name(".state")
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_vault_clear_keeps_the_device_key(self):
        # vault.clear() 是"换账号"用的：清账户码，但不该把"这台机器来过"忘掉，
        # 否则换个账号就等于换了一台机器，防刷号直接失效。
        key = device_key.get_or_create()
        vault.save("CC-AAAAAA-BBBBBB", "k" * 64, 42)
        vault.clear()
        self.assertIsNone(vault.load())
        self.assertEqual(device_key.get(), key)

    def test_corrupted_value_is_replaced(self):
        vault.save_device_key("not-a-key")
        self.assertEqual(device_key.get(), "")
        new = device_key.get_or_create()
        self.assertRegex(new, r"^[0-9a-f]{64}$")
        self.assertNotEqual(new, "not-a-key")

    def test_cannot_persist_returns_empty(self):
        # 存不下就别发：绑一个会丢的值没有意义（服务器认不出这台机器）
        with mock.patch.object(vault, "save_device_key", return_value=False):
            self.assertEqual(device_key.get_or_create(), "")

    def test_forget_clears_it(self):
        device_key.get_or_create()
        self.assertTrue(device_key.forget())
        self.assertEqual(device_key.get(), "")
        self.assertFalse(Path(vault._file_override).with_name(".state").exists())

    def test_load_tolerates_garbage(self):
        path = Path(vault._file_override).with_name(".state")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8 \x00")
        self.assertEqual(device_key.get(), "")

    def test_nothing_is_printed_or_json_serialised(self):
        # 本机密钥不该出现在任何给用户 / 模型看的文本里：这里钉住"它不在凭证 JSON"。
        key = device_key.get_or_create()
        vault.save("CC-AAAAAA-BBBBBB", "k" * 64, 42)
        blob = json.dumps(vault.load(), ensure_ascii=False)
        self.assertNotIn(key, blob)


if __name__ == "__main__":
    unittest.main()
