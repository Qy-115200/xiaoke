# -*- coding: utf-8 -*-
"""生成自动更新清单 latest.json。

用法（四个产物都齐了之后）：

    python build_tools/make_manifest.py \
        --version 1.0.1 \
        --base-url https://aikaui.com/downloads/releases/1.0.1 \
        --file windows-x64=release/ClaudeCodeAssistant-win-x64.exe \
        --file macos-arm64=release/ClaudeCodeAssistant-macos-arm64.zip \
        --file macos-x64=release/ClaudeCodeAssistant-macos-x64.zip \
        --file linux-x64=release/ClaudeCodeAssistant-linux-x64.tar.gz \
        --out release/latest.json

规则（客户端 updater.py 会逐条校验，这里先自己把关）：
  · 版本必须是 X.Y.Z
  · 每个平台必须正好一条，文件必须存在
  · url 用 https + 文件名（不写本地路径）
  · sha256 是文件真实哈希，size 是真实字节数
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import APP_MIN_UPDATE_FROM, APP_VERSION  # noqa: E402

PLATFORMS = ("windows-x64", "macos-arm64", "macos-x64", "linux-x64")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_files(items) -> dict:
    mapping = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--file 要写成 平台=路径，收到：{item}")
        key, path = item.split("=", 1)
        key = key.strip()
        if key not in PLATFORMS:
            raise SystemExit(f"未知平台键：{key}（可选：{', '.join(PLATFORMS)}）")
        if key in mapping:
            raise SystemExit(f"平台重复：{key}")
        mapping[key] = Path(path).expanduser()
    return mapping


def build_manifest(version: str, base_url: str, files: dict,
                   min_version=None, notes="") -> dict:
    if version.count(".") != 2 or not all(p.isdigit() for p in version.split(".")):
        raise SystemExit(f"版本号必须形如 X.Y.Z，收到：{version}")

    missing = [k for k in PLATFORMS if k not in files]
    if missing:
        raise SystemExit(f"缺少平台产物：{', '.join(missing)}")

    latest = {}
    for key in PLATFORMS:
        path = files[key]
        if not path.is_file():
            raise SystemExit(f"{key} 的文件不存在：{path}")
        latest[key] = {
            "version": version,
            "url": f"{base_url.rstrip('/')}/{path.name}",
            "sha256": sha256_of(path),
            "size": path.stat().st_size,
            "min_version": min_version or APP_MIN_UPDATE_FROM,
            "notes": notes,
        }

    return {"schema_version": 1, "latest": latest}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 latest.json")
    ap.add_argument("--version", default=APP_VERSION)
    ap.add_argument("--base-url", required=True,
                    help="产物所在的 https 目录，例如 .../releases/1.0.1")
    ap.add_argument("--file", action="append", metavar="平台=路径")
    ap.add_argument("--min-version", default=None,
                    help=f"低于这个版本的用户需要手动安装（默认 {APP_MIN_UPDATE_FROM}）")
    ap.add_argument("--notes", default="")
    ap.add_argument("--out", default="latest.json")
    args = ap.parse_args(argv)

    files = parse_files(args.file)
    manifest = build_manifest(args.version, args.base_url, files,
                              args.min_version, args.notes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(out)
    print(f"已生成 {out}（v{args.version}，{len(manifest['latest'])} 个平台）")
    for key, entry in manifest["latest"].items():
        print(f"  {key:12s} {entry['size']:>12,d}  {entry['sha256'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
