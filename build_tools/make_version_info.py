# -*- coding: utf-8 -*-
"""生成 Windows 文件版本信息（aikaui.exe 右键「属性 → 详细信息」里看到的那些）。

用法（在仓库根目录）：
    python build_tools/make_version_info.py

输出：build/version_info.txt，agent.spec 会自动带上它（如果存在）。
版本号只从 config.APP_VERSION 读，绝不在这里另写一份。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import APP_NAME, APP_VERSION  # noqa: E402

COMPANY = "aikaui"


def _four(version: str) -> tuple[int, int, int, int]:
    """'1.2.3' → (1, 2, 3, 0)；容忍多余/缺失的段，反正不合法就填 0。"""
    parts = []
    for piece in str(version).split(".")[:4]:
        try:
            parts.append(max(0, int(piece)))
        except ValueError:
            parts.append(0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])


def render(version: str = APP_VERSION, name: str = APP_NAME) -> str:
    nums = _four(version)
    return f"""# UTF-8
# 由 build_tools/make_version_info.py 生成，不要手改。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums},
    prodvers={nums},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{COMPANY}'),
          StringStruct('FileDescription', '{name}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'ClaudeCodeAssistant'),
          StringStruct('LegalCopyright', 'Copyright (c) {COMPANY}'),
          StringStruct('OriginalFilename', '{name}.exe'),
          StringStruct('ProductName', '{name}'),
          StringStruct('ProductVersion', '{version}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    out = ROOT / "build" / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8")
    print(f"已生成 {out}（version={APP_VERSION}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
