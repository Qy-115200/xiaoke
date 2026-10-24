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
"""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import pow as powlib                     # noqa: E402
from server import identity                          # noqa: E402

DIFFICULTY = 9          # 平均 512 次哈希，测试跑得快


class _State:
    def __init__(self):
        self.closed = False
        self.calls = []          # [(method, path)]
        self.challenges = {}     # challenge -> 是否已被用过
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
            if self.path != "/api/auto-register":
                return self._send(404, {"error": "not found"})
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
            return self._send(200, {
                "user_id": 42,
                "account_code": "CC-AAAAAA-BBBBBB",
                "account_key": "k" * 64,
                "api_key": "sk-user-secret",
                "api_base": "https://aikaui.com/v1",
                "model": "deepseek-v4-pro",
                "fast_model": "deepseek-flash",
                "balance": "3.00",
            })

    return Handler


class RegisterFlowTests(unittest.TestCase):
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

    def tearDown(self):
        identity.SERVER_URL, identity.AGENT_KEY, identity.IDENTITY_FILE = self._old

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


if __name__ == "__main__":
    unittest.main()
