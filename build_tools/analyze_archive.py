# -*- coding: utf-8 -*-
"""按顶层包归总一个已打包的产物，看谁最占地方。

    python build_tools/analyze_archive.py dist/ClaudeCode安装助手.exe
    python build_tools/analyze_archive.py dist/ClaudeCodeAssistant

`uncomp MB` 是解压后的大小（能看出"这东西本来多大"），`comp MB` 是它在文件里
实际占的字节（这才是能省下来的量）。PYZ 里那几千个模块单独算一行 ——
它们的压缩是整体的，拆不到单包。

2026-10-28 就是靠它把桌面版从 35.9MB 压到 23.6MB 的，当时排在前面的几项：
`PIL/_avif`（4.3MB 压缩）、重复收的包源码、pywin32 的 win32ui、setuptools。
"""

import sys
from collections import defaultdict

from PyInstaller.archive.readers import CArchiveReader


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    agg = defaultdict(lambda: [0, 0, 0])
    for name, ent in CArchiveReader(argv[1]).toc.items():
        dlen, ulen, typ = ent[1], ent[2], ent[4]
        # typ == "z" 是 PYZ 里所有模块合成的那一块（带包名字符串，但大小是整体）
        top = ("<<PYZ 里面的模块>>" if typ == "z"
               else name.replace("\\", "/").split("/")[0].lower())
        agg[top][0] += ulen
        agg[top][1] += dlen
        agg[top][2] += 1

    rows = sorted(agg.items(), key=lambda kv: -kv[1][0])
    print(f"{'uncomp MB':>10} {'comp MB':>9} {'n':>5}  group")
    for k, (u, d, n) in rows[:30]:
        print(f"{u / 1e6:>10.2f} {d / 1e6:>9.2f} {n:>5}  {k}")
    print()
    print("TOTAL uncomp %.1f MB / comp %.1f MB"
          % (sum(v[0] for v in agg.values()) / 1e6,
             sum(v[1] for v in agg.values()) / 1e6))
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，中文路径和 ✔ 会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv))
