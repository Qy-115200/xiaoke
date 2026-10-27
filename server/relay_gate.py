# -*- coding: utf-8 -*-
"""别人的中转一律不让用。

这个客户端只配合 aikaui 中转工作：终端要是还指着别处（用户自己接的中转、
同事留下的旧配置），我们既收不到那台机器的用量，也不该继续给它发我们的对话
额度。所以检测到就**拒绝继续**，直到换成我们的为止 —— 原来的文件先备份，
随时能换回，这一步是可逆的。

这里只做判断和落盘，不弹窗、不 import tkinter：
  桌面版  main.py 调 check()，拦下就交给 ui/gate.py 问用户
  服务器版  --repair 把 foreign 报成失败
判断逻辑离线可测，界面再怎么改也不会漏掉一条路径。

★ 给用户看的文字里只有文件路径和 host —— 绝不带 token 的任何片段。
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from config import CLAUDE_CONFIG, CLAUDE_SETTINGS, SERVER_URL
from server.claude_config import normalize_base, read_strict


@dataclass(frozen=True)
class Conflict:
    """一份配置文件指向了别人。只留 host：用户看一眼就知道是"谁家"。"""
    path: Path
    host: str

    def line(self) -> str:
        return f"{self.path} → {self.host or '（认不出地址）'}"


@dataclass
class Gate:
    conflicts: list = field(default_factory=list)     # [Conflict]
    expected: str = ""                                # 我们的中转地址（规范化后）
    unreadable: list = field(default_factory=list)    # [(path, status)]

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    def lines(self) -> list:
        out = [c.line() for c in self.conflicts]
        for path, status in self.unreadable:
            out.append(f"{path}（{status}，没看）")
        return out


def host_of(base: str) -> str:
    """从 base URL 里取出 host:port。取不出来就原样返回（截短，别把整串地址当名字）。"""
    base = (base or "").strip()
    try:
        parts = urlsplit(base if "//" in base else "//" + base)
        return parts.netloc or parts.path[:60]
    except Exception:
        return base[:60]


def expected_base(identity=None) -> str:
    """以哪个地址为准：有账号就用服务器下发的 api_base，没有就用官网地址。

    ★ 两处必须和 claude_config.env_from_identity 算得一样，否则会出现
      「刚写完就被判成别人的」这种自相矛盾的循环。"""
    base = (identity or {}).get("api_base")
    return normalize_base(str(base or SERVER_URL))


def check(identity=None, config_path=None, settings_path=None) -> Gate:
    """看看两份配置里有没有指向别处的 ANTHROPIC_BASE_URL。

    · 文件不在 / 是软链 / 读不出来 → 不拦（判断不了就不替用户做主，repair 会报）
    · env 里没有 ANTHROPIC_BASE_URL → 不拦（还没配，或只配了 token）
    · 有、且不是我们的 → 拦
    """
    config_path = Path(config_path or CLAUDE_CONFIG)
    settings_path = Path(settings_path or CLAUDE_SETTINGS)
    gate = Gate(expected=expected_base(identity))

    for path in (config_path, settings_path):
        data, status = read_strict(path)
        if status == "missing":
            continue
        if status != "ok":
            gate.unreadable.append((path, status))
            continue
        env = data.get("env")
        if not isinstance(env, dict):
            gate.unreadable.append((path, "bad-env"))
            continue
        base = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
        if not base:
            continue                       # 没写就是没接别家，交给安装流程
        if normalize_base(base) != gate.expected:
            gate.conflicts.append(Conflict(path=path, host=host_of(base)))
    return gate


def switch_to_ours(identity=None) -> tuple:
    """换成我们的中转。返回 (ok, 给用户看的一句话)。

    · 还没有账号 → 先领一个（首次运行就是这样；注册失败就照实说，不往下编）
    · 写盘走 claude_config.apply()：**先备份再覆盖**，用户原来那套随时能换回
    · 只报结论，不打任何值（里面是 token）"""
    from server.identity import ensure_identity, last_register_error, load_identity

    if identity is None:
        identity = load_identity()
    if not identity:
        if not ensure_identity():
            return False, "没领到账号：" + (last_register_error() or "未知原因")
        identity = load_identity()
    if not identity:
        return False, "没领到账号，配置没动"

    from server.claude_config import apply
    return apply(identity)
