# -*- coding: utf-8 -*-
"""把 identity.json 里的凭证同步进 Claude Code 自己的配置。

★ 必须**两个文件都写**，少写一个就会出现「界面上是新账号、终端里还在烧旧账号」：

    ~/.claude.json            的 env   ← 一键安装一直写的是这里
    ~/.claude/settings.json   的 env   ← Claude Code 也会读，而且优先级更高

2026-10-17 的 bug 就是这么来的：换账户码登录只改了 identity.json，
settings.json 里的 ANTHROPIC_AUTH_TOKEN 还是旧的。用户看到新账号的余额，
终端里 Claude Code 却把用量记在旧账号上。

★ 模型分档规则（别改错，改错了用户会莫名其妙被计费到贵模型上）：
  · 默认层 = fast_model（便宜快）。Claude Code 没被明确指定模型时走的是
    "sonnet" 档，所以 ANTHROPIC_DEFAULT_SONNET_MODEL 必须映射到 fast_model。
  · 显式层 = model（强模型）。只有用户自己在 /model 里选 opus 时才走 pro。
"""

import json
from pathlib import Path

from config import CLAUDE_CONFIG, CLAUDE_SETTINGS


def normalize_base(base: str) -> str:
    """规范化 base URL：Claude Code 会自己拼 /v1/messages，
    如果 base 以 /v1 结尾会拼成 /v1/v1/messages → 404，所以去掉尾部 /v1。"""
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def env_from_identity(identity: dict) -> dict:
    """identity.json → ANTHROPIC_* 环境变量（只算，不写盘）。"""
    identity = identity or {}
    api_base = normalize_base(str(identity.get("api_base") or ""))
    api_key = str(identity.get("api_key") or "")
    model = identity.get("model") or "deepseek-v4-pro"
    fast_model = identity.get("fast_model") or model

    env = {
        "ANTHROPIC_BASE_URL": api_base,
        "ANTHROPIC_AUTH_TOKEN": api_key,       # ★ 是 AUTH_TOKEN，不是 API_KEY
        # --- 默认层：全部指向快模型 ---
        "ANTHROPIC_MODEL": fast_model,
        "ANTHROPIC_SMALL_FAST_MODEL": fast_model,
        "ANTHROPIC_DEFAULT_MODEL": fast_model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": fast_model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": fast_model,
        # --- 显式层：用户自己 /model 选 opus 时才落到强模型 ---
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
    }
    return {k: v for k, v in env.items() if v}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}                              # 文件不存在 / 坏了都当空的，后面重建


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def apply(identity: dict):
    """把凭证写进两处 Claude Code 配置。返回 (ok, 给用户看的一句话)。

    ★ 合并写，不整体覆盖：用户自己设的其他 env / 字段（modelSettings 等）原样保留。
    ★ 不抛异常：写不进去只影响"终端用哪个账号"，不该把登录本身判成失败。"""
    env = env_from_identity(identity)
    if not env.get("ANTHROPIC_AUTH_TOKEN") or not env.get("ANTHROPIC_BASE_URL"):
        return False, "身份文件里缺 api_key / api_base，命令行配置没改"

    written = []
    failed = []
    fast_model = env.get("ANTHROPIC_MODEL")

    # ---------- ~/.claude.json ----------
    try:
        data = _read_json(CLAUDE_CONFIG)
        merged = dict(data.get("env") or {})
        merged.update(env)
        data["env"] = merged
        data["hasCompletedOnboarding"] = True   # 顺便再保证一次绕过地区
        _write_json(CLAUDE_CONFIG, data)
        written.append(str(CLAUDE_CONFIG))
    except Exception as e:
        failed.append(f"{CLAUDE_CONFIG.name}: {e}")

    # ---------- ~/.claude/settings.json ----------
    try:
        s = _read_json(CLAUDE_SETTINGS)
        merged = dict(s.get("env") or {})
        merged.update(env)                      # ★ 这里就是这次 bug 的修复点
        s["env"] = merged
        if fast_model:
            s["model"] = fast_model              # 默认模型钉成快模型（/model 显式选择仍能覆盖）
        _write_json(CLAUDE_SETTINGS, s)
        written.append(str(CLAUDE_SETTINGS))
    except Exception as e:
        failed.append(f"{CLAUDE_SETTINGS.name}: {e}")

    if not written:
        return False, "命令行配置没写进去：" + "；".join(failed)
    if failed:
        return True, "已更新 " + "、".join(written) + "；没写成的：" + "；".join(failed)
    return True, "已更新 " + "、".join(written)


def sync_from_identity_file():
    """从 identity.json 读凭证再 apply（登录/换账号之后调）。不抛异常。"""
    try:
        from config import IDENTITY_FILE
        identity = _read_json(IDENTITY_FILE)
    except Exception as e:
        return False, f"读身份文件失败：{e}"
    if not identity:
        return False, "还没领取 token，命令行配置没改"
    return apply(identity)
