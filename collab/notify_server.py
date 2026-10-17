#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify_server.py —— 一条极小的 HTTPS 通知通道。

它**不传消息**，只回答一个问题：「远端变了吗」。消息照旧走 git。

为什么这么设计，三条硬规矩：

  1. **它只回一个版本号，永远回不了自由文本。**
     `digest` 的输出会被原样塞进 agent 的上下文，所以这条通道如果能回任意字符串，
     它就变成了一个提示词注入入口 —— 谁能连上这个端口，谁就能往 agent 脑子里写字。
     版本号被限死成 `^[0-9A-Za-z._:-]{1,80}$`，装不下任何句子。

  2. **它挂了不影响协作。** 它只是"快路"：读到相同的版本号就省掉一次 git fetch。
     正确性由 git 那条路兜底（`--fetch-max-age` 到点一定真去 fetch）。

  3. **读不需要鉴权，写需要。** 读走的只是"有没有变"，泄露了也不值钱；
     写要密钥，免得有人乱按喇叭。

就两个接口：

    GET  /v?ch=<频道>                → {"v": "...", "at": "..."}
    POST /v?ch=<频道>   Bearer 密钥   → 更新版本号（body: {"v": "..."}）

跑起来：

    COLLAB_NOTIFY_SECRET=$(openssl rand -hex 16) python3 notify_server.py --port 8787

生产上别裸奔：默认只绑 127.0.0.1，要对外就用 nginx/caddy 在前面收 TLS。
只用标准库，Python 3.8+。
"""

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CH_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")        # 频道名
V_RE = re.compile(r"^[0-9A-Za-z._:-]{1,80}$")       # 版本号 —— 装不下句子，故意的
MAX_BODY = 4096
LOCK = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """一个频道一个文件。没有数据库 —— 要存的就是一个字符串。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, ch):
        return self.root / ("%s.json" % ch)

    def get(self, ch):
        try:
            return json.loads(self._p(ch).read_text(encoding="utf-8"))
        except Exception:
            return {"v": "", "at": ""}

    def set(self, ch, v):
        rec = {"v": v, "at": now_iso()}
        tmp = self._p(ch).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        tmp.replace(self._p(ch))            # 原子替换：读到一半崩了也不会给出半个文件
        return rec


class Handler(BaseHTTPRequestHandler):
    server_version = "collab-notify/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        # 默认会把每个请求打到 stderr，journalctl 会被刷满。只留一行要点。
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    # ---------------------------------------------------------------- 工具

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _channel(self):
        q = parse_qs(urlparse(self.path).query)
        ch = (q.get("ch") or ["default"])[0]
        return ch if CH_RE.match(ch) else None

    def _authorized(self):
        want = self.server.secret
        if not want:
            return False
        got = (self.headers.get("Authorization") or "")
        if not got.startswith("Bearer "):
            return False
        return hmac.compare_digest(got[7:].strip(), want)

    # ---------------------------------------------------------------- 路由

    def do_GET(self):
        if urlparse(self.path).path != "/v":
            return self._send(404, {"error": "只有 /v"})
        ch = self._channel()
        if not ch:
            return self._send(400, {"error": "频道名不合法"})
        with LOCK:
            rec = self.server.store.get(ch)
        return self._send(200, {"v": rec.get("v", ""), "at": rec.get("at", "")})

    def do_POST(self):
        if urlparse(self.path).path != "/v":
            return self._send(404, {"error": "只有 /v"})
        ch = self._channel()
        if not ch:
            return self._send(400, {"error": "频道名不合法"})
        if not self._authorized():
            return self._send(401, {"error": "密钥不对"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "Content-Length 不合法"})
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "body 太大或为空"})
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "body 不是合法 JSON"})
        v = str((data or {}).get("v") or "")
        if not V_RE.match(v):
            # 这条是防注入的硬闸门：版本号里装不下句子，
            # 所以这条通道永远没法往 agent 的上下文里塞文字。
            return self._send(400, {"error": "版本号不合法（只许 [0-9A-Za-z._:-]，≤80 字符）"})
        with LOCK:
            old = self.server.store.get(ch)
            if old.get("v") == v:
                return self._send(200, {"v": v, "at": old.get("at", ""), "noop": True})
            rec = self.server.store.set(ch, v)
        sys.stderr.write("ch=%s %s -> %s\n" % (ch, (old.get("v") or "-")[:12], v[:12]))
        return self._send(200, {"v": rec["v"], "at": rec["at"]})

    def do_DELETE(self):
        return self._send(405, {"error": "不支持"})

    def do_PUT(self):
        return self._send(405, {"error": "不支持"})


def main(argv=None):
    p = argparse.ArgumentParser(description="collab 的通知通道（只回版本号）")
    p.add_argument("--bind", default="127.0.0.1", help="监听地址（默认只有本机能连）")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--dir", default=os.environ.get("COLLAB_NOTIFY_DIR", "/var/lib/collab-notify"),
                   help="版本号存哪")
    p.add_argument("--secret", default=os.environ.get("COLLAB_NOTIFY_SECRET", ""),
                   help="写操作要的密钥（默认读环境变量）")
    a = p.parse_args(argv)

    if not a.secret:
        generated = secrets.token_hex(16)
        print("⚠ 没给密钥，临时生成了一个（重启就变，只能用来试）：")
        print("    COLLAB_NOTIFY_SECRET=%s" % generated)
        print("  正式跑请把它固定到环境变量里，并且别写进仓库。")
        a.secret = generated

    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.secret = a.secret
    srv.store = Store(a.dir)
    srv.daemon_threads = True
    print("通知通道起来了：http://%s:%d/v?ch=default" % (a.bind, a.port))
    print("版本号存在 %s" % a.dir)
    if a.bind not in ("127.0.0.1", "localhost"):
        print("⚠ 绑在 %s 上：这个服务本身说 plain HTTP，别直接对着公网。" % a.bind)
        print("  要对外请在前面放 nginx/caddy 收 TLS，并且只转发 /v。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
