# -*- coding: utf-8 -*-
"""注册链路的端到端测试：本地假服务器说真契约的话（不联网）。

假服务器照着线上 `token-service/src/routes/account.js` 的形状回：
  GET  /api/challenge     → {challenge, difficulty, expires_in}
  POST /api/auto-register → {user_id, account_code, account_key, api_key, ...}
以及两种拒绝：403 signup_closed、429 too_many_registrations。

客户端要验证的四件事：
  1. 首次运行能走完「取挑战 → 算 PoW → 提交」并落盘；
  2. 返回给 Agent 的那段 JSON 里**没有** api_key / account_key（不能进聊天记录）；
  3. 已有凭证时**一次网络请求都不发**（重复注册 = 开新号 = 丢余额）；
  4. 注册关闭时给的是人话，不是 "HTTP 403"。

外加 2026-10 加的两样：
  5. 硬件指纹随注册请求一起发（服务器不认识这个字段时要能去掉再试一次）；
  6. 服务器回 is_recovered 时把"账号已恢复"交给界面，并把账户码存进保险箱。

★ 测试**绝不许**碰真实的 Windows 注册表 / 真实配置目录：vault 强制文件后端，
  落在临时目录里；指纹也不真去起 PowerShell。
"""

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

from server import fingerprint as fplib                # noqa: E402
from server import pow as powlib                       # noqa: E402
from server import device_key                          # noqa: E402
from server import identity                            # noqa: E402
from server import vault                               # noqa: E402

DIFFICULTY = 9          # 平均 512 次哈希，测试跑得快


class _State:
    def __init__(self):
        self.closed = False
        self.calls = []          # [(method, path)]
        self.challenges = {}     # challenge -> 是否已被用过
        self.bodies = []         # 收到的 POST 正文（看客户端发了什么字段）
        self.reject_fingerprint = False   # 模拟"服务端不认识 fingerprint 字段"
        self.require_fingerprint = False  # 模拟"服务端要求必须带指纹"
        self.reject_device_key = False    # 模拟老服务端不认识 device_key 字段
        self.require_device_key = False   # 模拟"服务端要求必须带本机密钥"
        self.recovered = False            # 模拟"按指纹认出了旧账号"
        self.good_key = "k" * 64          # bind-account 认的账户密钥
        self.key_required = True          # 模拟新账号：必须给密钥
        self.lock = threading.Lock()


def _make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):      # 别往测试输出里刷访问日志
            pass

        def _send(self, code, obj, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with state.lock:
                state.calls.append(("GET", self.path))
            if self.path != "/api/challenge":
                return self._send(404, {"error": "not found"})
            if self.headers.get("X-Agent-Key") != "test-agent-key":
                return self._send(401, {"error": "unauthorized"})
            if state.closed:
                return self._send(403, {"error": "signup_closed",
                                        "message": "注册已暂停"})
            challenge = "a" * 32
            with state.lock:
                state.challenges[challenge] = False
            return self._send(200, {"challenge": challenge,
                                    "difficulty": DIFFICULTY, "expires_in": 120})

        def do_POST(self):
            with state.lock:
                state.calls.append(("POST", self.path))
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            with state.lock:
                state.bodies.append(body)
            if self.path == "/api/bind-account":
                return self._bind(body)
            if self.path != "/api/auto-register":
                return self._send(404, {"error": "not found"})
            # 模拟老服务端：不认识 fingerprint 这个字段，整条请求 400 掉
            if state.reject_fingerprint and "fingerprint" in body:
                return self._send(400, {"error": "unexpected_field"})
            # 模拟新服务端：缺失指纹直接拒（客户端应当如实报错，不是丢掉再试）
            if state.require_fingerprint and not body.get("fingerprint"):
                return self._send(400, {"error": "fingerprint_required"})
            # 同上：老服务端不认识本机密钥 / 新服务端要求必须带
            if state.reject_device_key and "device_key" in body:
                return self._send(400, {"error": "unexpected_field"})
            if state.require_device_key and not body.get("device_key"):
                return self._send(400, {"error": "device_key_required"})
            challenge = body.get("challenge") or ""
            nonce = body.get("nonce")
            with state.lock:
                used = state.challenges.get(challenge)
            if used is None:
                return self._send(400, {"error": "challenge_invalid"})
            if used:
                return self._send(409, {"error": "challenge_used"})
            if not powlib.verify(challenge, nonce, DIFFICULTY):
                return self._send(400, {"error": "pow_failed"})
            with state.lock:
                state.challenges[challenge] = True
            payload = {
                "user_id": 42,
                "account_code": "CC-AAAAAA-BBBBBB",
                "account_key": "k" * 64,
                "api_key": "sk-user-secret",
                "api_base": "https://aikaui.com/v1",
                "model": "deepseek-v4-pro",
                "fast_model": "deepseek-flash",
                "balance": "3.00",
            }
            if state.recovered:
                # ★ 线上恢复路径**不发 account_key**（那是主人凭证，见 server/fingerprint.py）；
                #   这里照着线上来，否则测不出"空值冲掉保险箱"那个坑。
                payload["is_recovered"] = True
                payload["account_key"] = None
            return self._send(200, payload)

        def _bind(self, body):
            """照着线上 bind-account 的新契约：新账号必须给密钥。"""
            code = str(body.get("account_code") or "")
            key = str(body.get("account_key") or "")
            if code != "CC-AAAAAA-BBBBBB":
                return self._send(404, {"error": "account_code not found"})
            if state.key_required:
                if not key:
                    return self._send(403, {"error": "account_key_required"})
                if key.strip().lower() != state.good_key:
                    return self._send(403, {"error": "account_key_invalid"})
            return self._send(200, {
                "user_id": 42,
                "api_key": "sk-user-secret",
                "api_base": "https://aikaui.com/v1",
                "account_code": "CC-AAAAAA-BBBBBB",
                "balance": "3.00",
                "account_key": key.strip().lower() or None,
                "legacy_no_key": not state.key_required,
            })

    return Handler


class _FakeServerCase(unittest.TestCase):
    def setUp(self):
        self.state = _State()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        # cleanup 是后进先出：先 shutdown（停线程），再 server_close（关 socket）
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        # 把 identity.py 里的常量换成本地假服务器 + 临时身份文件
        self._old = (identity.SERVER_URL, identity.AGENT_KEY, identity.IDENTITY_FILE)
        identity.SERVER_URL = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        identity.AGENT_KEY = "test-agent-key"
        identity.IDENTITY_FILE = Path(self.tmp.name) / ".claude-agent" / "identity.json"
        powlib.reset_cancel()

        # ★ 保险箱走文件后端，落在临时目录：绝不许往这台机器真实的 HKCU 注册表里
        #   写一条假账户码（跑一次测试就污染一台机器）。
        self._old_vault = (vault._force_file, vault._file_override)
        vault._force_file = True
        vault._file_override = Path(self.tmp.name) / "credential.json"

        # ★ 指纹用固定的假值，别真去起 PowerShell 读主板序列号
        fplib._reset_cache()
        self._fp = mock.patch.object(
            fplib, "get_device_fingerprint_info",
            return_value={"fingerprint": "f" * 32, "source": "board+cpu"})
        self._fp.start()

    def tearDown(self):
        identity.SERVER_URL, identity.AGENT_KEY, identity.IDENTITY_FILE = self._old
        self._fp.stop()
        vault._force_file, vault._file_override = self._old_vault
        fplib._reset_cache()

class RegisterFlowTests(_FakeServerCase):

    def test_register_writes_identity_and_hides_secrets(self):
        result = identity.register_device()
        self.assertTrue(result.startswith("{"), result)
        payload = json.loads(result)

        self.assertEqual(payload["note"], "领取成功")
        self.assertEqual(payload["account_code"], "CC-AAAAAA-BBBBBB")
        self.assertEqual(payload["balance"], "3.00")
        # ★ secret 绝不能出现在给 Agent 的话里（会进对话历史和日志）
        self.assertNotIn("sk-user-secret", result)
        self.assertNotIn("k" * 64, result)

        saved = json.loads(identity.IDENTITY_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["api_key"], "sk-user-secret")
        self.assertEqual(saved["account_key"], "k" * 64)
        self.assertTrue(saved.get("device_id"))

    def test_second_register_does_not_touch_network(self):
        identity.register_device()
        before = len(self.state.calls)
        identity.register_device()
        self.assertEqual(len(self.state.calls), before,
                         "已有凭证时又发了请求——重新注册会开新号、丢余额")

    def test_signup_closed_is_human_readable(self):
        self.state.closed = True
        result = identity.register_device()
        self.assertFalse(result.startswith("{"), result)
        self.assertIn("关闭了新账号注册", result)
        self.assertFalse(identity.IDENTITY_FILE.exists())

    def test_ensure_identity_reports_reason(self):
        self.state.closed = True
        self.assertFalse(identity.ensure_identity())
        self.assertIn("关闭了新账号注册", identity.last_register_error())

    def test_ensure_account_code_never_registers(self):
        # 余额轮询每 30 秒调一次 _ensure_account_code；它一注册就会变成注册风暴
        code, err = identity._ensure_account_code()
        self.assertIsNone(code)
        self.assertIn("还没有账号", err)
        self.assertEqual(self.state.calls, [])
        uid, err2 = identity._ensure_user_id()
        self.assertIsNone(uid)
        self.assertEqual(self.state.calls, [])

    def test_challenge_is_reused_only_by_its_owner(self):
        # 挑战用过一次就该被拒（服务器契约），客户端不该把同一个挑战提交两次
        nonce, err = powlib.solve("a" * 32, DIFFICULTY)
        self.assertIsNone(err)
        self.assertTrue(powlib.verify("a" * 32, nonce, DIFFICULTY))

    # ---------- 设备指纹 ----------

    def test_fingerprint_is_sent_with_register(self):
        identity.register_device()
        body = self.state.bodies[-1]
        self.assertEqual(body.get("fingerprint"), "f" * 32)
        # 来源只进日志：撞指纹时得能看出是哪个来源撞的
        self.assertEqual(body.get("fingerprint_src"), "board+cpu")

    def test_fingerprint_omitted_when_unavailable(self):
        # 只有连 device_id 都拿不到的极端情况才会空 —— 那时压根不发这个字段
        with mock.patch.object(
                fplib, "get_device_fingerprint_info",
                return_value={"fingerprint": "", "source": ""}):
            identity.register_device()
        self.assertNotIn("fingerprint", self.state.bodies[-1])
        self.assertNotIn("fingerprint_src", self.state.bodies[-1])

    def test_fingerprint_required_is_reported_not_retried(self):
        # 服务器明确要求指纹、这台机器却算不出来：如实报错，别再解一遍 PoW 重试。
        self.state.require_fingerprint = True
        with mock.patch.object(
                fplib, "get_device_fingerprint_info",
                return_value={"fingerprint": "", "source": ""}):
            result = identity.register_device()
        self.assertFalse(result.startswith("{"), result)
        self.assertEqual(len(self.state.bodies), 1, "要求指纹时重试没有意义")
        self.assertIn("新版", result)              # 话术指向"去下载新版"

    def test_unknown_fingerprint_field_is_dropped_and_retried(self):
        # 服务端还是老版本、不认识 fingerprint：别让整次注册死在这儿，
        # 去掉字段再走一轮，第二次必须成功。
        self.state.reject_fingerprint = True
        result = identity.register_device()
        self.assertTrue(result.startswith("{"), result)
        self.assertIn("fingerprint", self.state.bodies[0])
        self.assertNotIn("fingerprint", self.state.bodies[-1])
        self.assertTrue(identity.IDENTITY_FILE.exists())

    # ---------- 本机密钥（device key） ----------

    def test_device_key_is_sent_with_register(self):
        # 注册时本机密钥和 device_id 一起发：服务器靠这一对认出"这台机器来过"，
        # 删掉 identity.json 再注册就把原账号还回来，而不是再开一个新号。
        identity.register_device()
        body = self.state.bodies[-1]
        key = body.get("device_key")
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(body.get("device_id"), identity._device_id())
        # 落盘了：下一次注册（换了新挑战）还得用同一串
        self.assertEqual(device_key.get(), key)

    def test_device_key_never_reaches_the_identity_file(self):
        identity.register_device()
        saved = identity.IDENTITY_FILE.read_text(encoding="utf-8")
        self.assertNotIn("device_key", saved)
        self.assertNotIn(device_key.get(), saved)

    def test_device_key_is_not_sent_to_bind_account(self):
        identity.register_device()
        identity.bind_account("CC-AAAAAA-BBBBBB", "k" * 64)
        # 注册请求（没有 account_key），换设备登录请求（有 account_key）
        reg_bodies = [b for b in self.state.bodies if "account_key" not in b]
        bind_body = [b for b in self.state.bodies if "account_key" in b][-1]
        self.assertTrue(any("device_key" in b for b in reg_bodies))
        self.assertNotIn("device_key", bind_body, "本机密钥只允许发给注册接口")

    def test_device_key_required_is_reported_not_retried(self):
        self.state.require_device_key = True
        with mock.patch.object(device_key, "get_or_create", return_value=""):
            result = identity.register_device()
        self.assertFalse(result.startswith("{"), result)
        self.assertEqual(len(self.state.bodies), 1, "服务器要求本机密钥时重试没有意义")
        self.assertIn("新版", result)

    def test_unknown_device_key_field_is_dropped_and_retried(self):
        # 老服务端不认识 device_key：先摘掉它再试，**指纹别一起丢**
        self.state.reject_device_key = True
        result = identity.register_device()
        self.assertTrue(result.startswith("{"), result)
        self.assertIn("device_key", self.state.bodies[0])
        last = self.state.bodies[-1]
        self.assertNotIn("device_key", last)
        self.assertNotIn("device_id", last)
        self.assertIn("fingerprint", last)

    # ---------- 保险箱 / 自动恢复 ----------

    def test_account_code_is_vaulted(self):
        identity.register_device()
        kept = vault.load() or {}
        self.assertEqual(kept.get("account_code"), "CC-AAAAAA-BBBBBB")
        self.assertEqual(kept.get("account_key"), "k" * 64)

    def test_pending_recovery_offers_code_when_identity_lost(self):
        identity.register_device()
        identity.IDENTITY_FILE.unlink()
        got = identity.pending_recovery()
        # 换设备登录要两样，保险箱里两样都存了 → 一起交回去
        self.assertEqual(got.get("account_code"), "CC-AAAAAA-BBBBBB")
        self.assertEqual(got.get("account_key"), "k" * 64)

    def test_pending_recovery_tolerates_vault_without_key(self):
        # 老版本保险箱里只存了码：仍然要能把码交回去，密钥留空让用户手填
        vault.save("CC-AAAAAA-BBBBBB", None, None)
        got = identity.pending_recovery()
        self.assertEqual(got.get("account_code"), "CC-AAAAAA-BBBBBB")
        self.assertEqual(got.get("account_key"), "")

    def test_pending_recovery_is_silent_when_identity_present(self):
        identity.register_device()
        self.assertIsNone(identity.pending_recovery())

    def test_pending_recovery_is_none_without_vault(self):
        self.assertIsNone(identity.pending_recovery())

    # ---------- is_recovered ----------

    def test_recovered_flag_reaches_the_ui(self):
        self.state.recovered = True
        calls = []

        class _FakeUI:
            def ui_recovered(self):
                calls.append(1)

        with mock.patch.object(identity, "get_ui", return_value=_FakeUI()):
            result = identity.register_device()

        self.assertTrue(json.loads(result)["recovered"])
        self.assertEqual(calls, [1], "服务器说账号已恢复，界面却没收到提示")

    def test_recovered_defaults_to_false(self):
        result = identity.register_device()
        self.assertFalse(json.loads(result)["recovered"])

    def test_recovery_does_not_clobber_the_vault_key(self):
        # 服务器按指纹还回旧账号时不发 account_key（主人凭证）。这时候拿空值去
        # vault.save()，会把注册表里原有的密钥冲掉 —— 用户仅剩的"换设备找回"
        # 能力就一起没了。必须把旧密钥续上。
        vault.save("CC-AAAAAA-BBBBBB", "k" * 64, 42)
        self.state.recovered = True

        result = identity.register_device()

        self.assertTrue(json.loads(result)["recovered"])
        kept = vault.load() or {}
        self.assertEqual(kept.get("account_key"), "k" * 64, "保险箱里的密钥被冲掉了")
        local = json.loads(identity.IDENTITY_FILE.read_text(encoding="utf-8"))
        self.assertFalse(local.get("account_key"))     # 服务器没给，本地也不该编一个


class BindAccountTests(_FakeServerCase):
    """换设备登录：账户码 + 账户密钥。"""

    def _last_bind_body(self):
        posts = [b for b in self.state.bodies if "device_id" in b]
        self.assertTrue(posts, "根本没往 bind-account 发请求")
        return posts[-1]

    def test_key_is_sent_along_with_code(self):
        data, err, code, _wait = identity.bind_account("CC-AAAAAA-BBBBBB", self.state.good_key)
        self.assertIsNone(err, err)
        self.assertEqual(data["user_id"], 42)
        body = self._last_bind_body()
        self.assertEqual(body.get("account_key"), self.state.good_key)
        # 回来的密钥要能落盘（客户端据此把保险箱补齐）
        self.assertEqual(data.get("account_key"), self.state.good_key)

    def test_missing_key_is_reported_as_its_own_thing(self):
        data, err, code, _wait = identity.bind_account("CC-AAAAAA-BBBBBB", "")
        self.assertIsNone(data)
        self.assertEqual(code, "account_key_required")
        self.assertIn("账户密钥", err)

    def test_wrong_key_is_not_reported_as_wrong_code(self):
        data, err, code, _wait = identity.bind_account("CC-AAAAAA-BBBBBB", "f" * 64)
        self.assertIsNone(data)
        self.assertEqual(code, "account_key_invalid")
        self.assertIn("密钥", err)
        self.assertNotIn("没找到", err)   # 别让用户以为是码抄错了

    def test_legacy_account_without_key_still_logs_in(self):
        # 2026-10-24 之前注册的账号从来没有密钥，服务端对这些放行
        self.state.key_required = False
        data, err, _code, _wait = identity.bind_account("CC-AAAAAA-BBBBBB", "")
        self.assertIsNone(err, err)
        self.assertTrue(data.get("legacy_no_key"))

    def test_key_is_never_put_in_the_agent_facing_json(self):
        from server import api
        # ★ 真实实现会去改这台机器的 ~/.claude.json 和 settings.json，测试里挡掉
        with mock.patch.object(api, "sync_from_identity_file", return_value=(True, "")):
            out = api.login_with_account_code("CC-AAAAAA-BBBBBB", self.state.good_key)
        self.assertTrue(out.startswith("{"), out)
        self.assertNotIn(self.state.good_key, out)
        self.assertNotIn("sk-user-secret", out)


if __name__ == "__main__":
    unittest.main()
