# -*- coding: utf-8 -*-
"""把某个 tag 的 GitHub Release 产物下载到本地目录。

    python build_tools/fetch_release.py v1.4.8 --dir release

为什么不直接写 `gh release download`：**这台机器上没有 gh CLI**，而发布流程
的下一步（build_tools/deploy_downloads.sh）就在这台机器上跑。装了 gh 也能用，
两条路等价；没装的时候这条能通。

令牌按仓库惯例从 `github_token.txt` 读，或者从环境变量 GITHUB_TOKEN 读。
**令牌值不会被打印，也不会出现在命令行参数里** —— 命令文本是要进对话记录的。

下载走 `Accept: application/octet-stream` 拿 302，再**不带认证头**去取 CDN 地址：
GitHub 对带认证头的 CDN 请求一律 401。这个坑见 memory 里的 github-ops。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
UA = "agent_demo-fetch-release"


def parse_repo(remote: str) -> str:
    """从 remote URL 里取 `owner/name`。支持 https 和 ssh 两种写法。"""
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", remote.strip())
    if not m:
        raise SystemExit(f"看不懂这个 remote：{remote}")
    return f"{m.group(1)}/{m.group(2)}"


def read_token(token_file: Path) -> str:
    """令牌只读进来，不打印。没有也能跑（公开仓库），只是容易撞限流。"""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"].strip()
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def _request(url: str, token: str, accept: str) -> urllib.request.Request:
    headers = {"Accept": accept, "User-Agent": UA,
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _get_json(url: str, token: str):
    with urllib.request.urlopen(_request(url, token, "application/vnd.github+json"),
                                timeout=60) as r:
        return json.load(r)


def download(url: str, token: str, dest: Path, timeout: int = 120) -> None:
    """下载一个 asset。先问 API 要 302，再不带认证头去 CDN 取正文。"""
    req = _request(url, token, "application/octet-stream")

    # 不跟随重定向，先看 Location。跟随的话 urllib 会把 Authorization 一起带过去。
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=timeout)
        location = None
        resp.close()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers.get("Location")
        e.close()

    if location:
        # ★ 这里**故意**不建带 Authorization 的 Request。
        with urllib.request.urlopen(
                urllib.request.Request(location, headers={"User-Agent": UA}),
                timeout=timeout) as r, dest.open("wb") as fh:
            _copy(r, fh)
        return

    # 没重定向（少数情况直接给正文），那就已经带着认证拿到句柄了。
    with urllib.request.urlopen(req, timeout=timeout) as r, dest.open("wb") as fh:
        _copy(r, fh)


def _copy(resp, fh, chunk: int = 1 << 20) -> None:
    while True:
        block = resp.read(chunk)
        if not block:
            return
        fh.write(block)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 302 变成 HTTPError 抛出来，好让我们自己拿 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，不重挂就 UnicodeEncodeError（--help 里也有中文）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="下载 GitHub Release 里的产物")
    ap.add_argument("tag", help="例如 v1.4.8")
    ap.add_argument("--dir", default="release", help="存到哪个目录（默认 release/）")
    ap.add_argument("--repo", default="", help="owner/name；不给就从 origin 推出来")
    ap.add_argument("--token-file", default=str(ROOT / "github_token.txt"))
    ap.add_argument("--overwrite", action="store_true", help="本地已有也重下")
    args = ap.parse_args(argv)

    repo = args.repo or parse_repo(
        subprocess.run(["git", "config", "--get", "remote.origin.url"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace").stdout)
    token = read_token(Path(args.token_file))
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        rel = _get_json(f"{API}/repos/{repo}/releases/tags/{args.tag}", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"✖ {repo} 上没有 Release {args.tag} —— CI 构建完了吗？", file=sys.stderr)
            return 1
        raise

    assets = rel.get("assets") or []
    if not assets:
        print(f"✖ Release {args.tag} 里没有资产", file=sys.stderr)
        return 1

    print(f"▶ {repo} 的 {args.tag}：{len(assets)} 个资产 → {out}")
    for a in assets:
        dest = out / a["name"]
        want = int(a["size"])
        if dest.is_file() and dest.stat().st_size == want and not args.overwrite:
            print(f"  · {a['name']} 已存在（{want} 字节），跳过")
            continue
        print(f"  ↓ {a['name']}（{want} 字节）")
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            download(a["url"], token, tmp)
            got = tmp.stat().st_size
            if got != want:
                raise SystemExit(f"✖ {a['name']} 大小不对：期望 {want}，拿到 {got}")
            tmp.replace(dest)          # 下完才改名，中途断掉不会留下半个包
        finally:
            if tmp.exists():
                tmp.unlink()
        print(f"  ✔ {a['name']}  sha256 {sha256_of(dest)}")

    missing = [n for n in ("ClaudeCodeAssistant-win-x64.exe",
                           "ClaudeCodeAssistant-macos-arm64.zip",
                           "ClaudeCodeAssistant-macos-x64.zip",
                           "ClaudeCodeAssistant-linux-x64.tar.gz")
               if not (out / n).is_file()]
    if missing:
        print("✖ 四个平台的产物不全，deploy_downloads.sh 会拒绝发：" + "、".join(missing),
              file=sys.stderr)
        return 1
    print(f"✔ 齐了。下一步：DEPLOY_HOST=… DEPLOY_USER=root "
          f"bash build_tools/deploy_downloads.sh {args.tag.lstrip('v')} {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
