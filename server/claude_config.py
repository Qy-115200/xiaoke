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

★ 除了"装的时候写一次"，还要能**事后对账**（`check_and_repair`）：
  重装 Claude Code、上次写盘中断、服务端轮换 token，都会让这两份配置和
  identity.json 对不上。对账的三条底线：
    1. 只修自己的：磁盘上的 ANTHROPIC_BASE_URL 与 identity 一致（或者受管键
       一个都没有）才算"我们的"，base 指向别处一律不碰；
    2. 坏文件绝不覆盖：解析不了就当没看见，报一声，绝不重建；
    3. 没有 diff 就不写：不落盘、不备份、不打日志。
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from config import CLAUDE_CONFIG, CLAUDE_SETTINGS

# 受管键 = env_from_identity() 会算出来的那些。文件里别的 ANTHROPIC_* 不动。
MANAGED_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
)

BACKUP_SUFFIX = ".aikaui-bak"


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


# ================= 读 / 写 =================
#
# 这几个状态是分开的，不能都退化成 `{}`：
#   文件不存在   → 安装器该去建
#   文件坏了     → 绝不覆盖（重建会把用户自己的配置抹掉）
#   不是软链     → 原子替换会把软链本身换掉，指向的真文件反而不更新

def read_strict(path: Path):
    """读一份 JSON 配置。返回 (data, status)。

    status ∈ ok / missing / bad-json / not-dict / bad-env / symlink / unreadable
    """
    path = Path(path)
    if path.is_symlink():
        return None, "symlink"
    if not path.exists():
        return None, "missing"
    try:
        # utf-8-sig：有些编辑器（和 Windows 记事本）会带 BOM
        raw = path.read_text(encoding="utf-8-sig")
    except Exception:
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except Exception:
        return None, "bad-json"
    if not isinstance(data, dict):
        return None, "not-dict"
    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        return None, "bad-env"
    return data, "ok"


def _read_json(path: Path) -> dict:
    """宽松读：读不出来就当空的。**只给 identity.json 用**（那边坏了等于没领过）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    """同目录临时文件 + os.replace。中途挂掉不会留下半份配置。

    权限钉 0600：里面有 token，不管 umask 是什么都不该被别人读。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, data: dict) -> None:
    _write_json_atomic(path, data)


def _backup(path: Path, keep_existing: bool = False) -> Path:
    """写之前存一份原样。只留一份（旧的不停堆着，token 也到处是）。

    keep_existing=True：已经有备份就不动它。apply() 用这个 —— 那份备份代表
    「装我们之前的样子」，重装 / 换账号再跑一次时不能拿我们自己的配置把它顶掉，
    否则用户原来那套中转就真的找不回来了。"""
    path = Path(path)
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if keep_existing and bak.exists():
        return bak
    raw = path.read_bytes()
    bak.write_bytes(raw)
    try:
        os.chmod(bak, 0o600)
    except OSError:
        pass
    return bak


# ================= 安装时：写一次 =================

def apply(identity: dict):
    """把凭证写进两处 Claude Code 配置。返回 (ok, 给用户看的一句话)。

    ★ 合并写，不整体覆盖：用户自己设的其他 env / 字段（modelSettings 等）原样保留。
    ★ 不抛异常：写不进去只影响"终端用哪个账号"，不该把登录本身判成失败。
    ★ 文件存在但读不出来时**不写**：宁可这一份没配上，也不能把用户的配置重建掉。"""
    env = env_from_identity(identity)
    if not env.get("ANTHROPIC_AUTH_TOKEN") or not env.get("ANTHROPIC_BASE_URL"):
        return False, "身份文件里缺 api_key / api_base，命令行配置没改"

    written = []
    failed = []
    backed = []
    fast_model = env.get("ANTHROPIC_MODEL")

    # ---------- ~/.claude.json ----------
    try:
        data, status = read_strict(CLAUDE_CONFIG)
        if status not in ("ok", "missing"):
            raise ValueError(f"文件读不出来（{status}），没有覆盖")
        data = data or {}
        # ★ 覆盖前先留底：这里可能是用户原来那套别家中转（含它自己的 token）。
        #   备份不成就不写 —— 说好「能换回来」就得真能换回来。
        if status == "ok":
            backed.append(_backup(CLAUDE_CONFIG, keep_existing=True))
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
        s, status = read_strict(CLAUDE_SETTINGS)
        if status not in ("ok", "missing"):
            raise ValueError(f"文件读不出来（{status}），没有覆盖")
        s = s or {}
        if status == "ok":
            backed.append(_backup(CLAUDE_SETTINGS, keep_existing=True))
        merged = dict(s.get("env") or {})
        merged.update(env)                      # ★ 这里就是这次 bug 的修复点
        s["env"] = merged
        if fast_model:
            s["model"] = fast_model              # 默认模型钉成快模型（/model 显式选择仍能覆盖）
        _write_json(CLAUDE_SETTINGS, s)
        written.append(str(CLAUDE_SETTINGS))
    except Exception as e:
        failed.append(f"{CLAUDE_SETTINGS.name}: {e}")

    # 备份说给用户听：他原来那套配置还在，想换回去拷一下就回来
    note = ("；原来的配置已备份到 " + "、".join(Path(p).name for p in backed)) if backed else ""
    if not written:
        return False, "命令行配置没写进去：" + "；".join(failed)
    if failed:
        return True, "已更新 " + "、".join(written) + "；没写成的：" + "；".join(failed) + note
    return True, "已更新 " + "、".join(written) + note


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


# ================= 事后对账 =================

@dataclass
class Change:
    """一份文件要改什么。keys 是受管键里不一致的那些（外加个别标记位）。"""
    path: Path
    data: dict                                   # 写完之后的完整内容
    keys: list = field(default_factory=list)     # 给用户看的键名列表（不含值）
    reason: str = ""                             # fill / drift / create


@dataclass
class Plan:
    changes: list = field(default_factory=list)          # [Change]
    skipped: list = field(default_factory=list)          # [(path, 原因)]
    healthy: list = field(default_factory=list)          # 已经一致的文件
    problem: str = ""                                    # 整个对账没法做的原因

    def change_for(self, path):
        for c in self.changes:
            if Path(c.path) == Path(path):
                return c
        return None


@dataclass
class Result:
    ok: bool = False
    repaired: bool = False
    planned: bool = False
    problem: str = ""                            # no-identity / bad-identity / ...
    changes: list = field(default_factory=list)  # [Change] 实际（或将要）写入的
    healthy: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # [(path, 原因)]

    @property
    def hard_problems(self):
        """真正没法修的（坏文件/软链/写失败）。指向别家中转的那种不算。"""
        return [(p, why) for p, why in self.skipped
                if why in HARD_SKIP_REASONS or why.startswith("write")]

    @property
    def summary(self) -> str:
        """给人看的一句话。★ 只报文件名和键名，绝不带值（值是 token）。"""
        if self.problem == "no-identity":
            return "还没有领取账号，配置没动"
        if self.problem == "bad-identity":
            return "身份文件里缺 api_base，配置没动"
        if not self.changes:
            return "命令行配置和账号一致，没动"
        names = "、".join(Path(c.path).name for c in self.changes)
        verb = "需要补齐" if self.planned else "已补齐"
        return f"{verb} {names}"

    def lines(self):
        """CLI 用的逐文件结论。"""
        out = []
        for c in self.changes:
            verb = "将补齐" if self.planned else "已补齐"
            keys = "、".join(c.keys) if c.keys else "字段"
            out.append(f"  {verb} {c.path}（{keys}）")
        for path in self.healthy:
            out.append(f"  一致   {path}")
        for path, why in self.skipped:
            out.append(f"  跳过   {path}（{SKIP_REASONS.get(why, why)}）")
        return out


# 这些"跳过"是真的没法修（不是"这份是别人的，故意不动"），CLI 要报成失败
HARD_SKIP_REASONS = ("symlink", "bad-json", "not-dict", "bad-env", "unreadable")


SKIP_REASONS = {
    "symlink": "是软链接，原子替换会把它本身换掉，不敢动",
    "bad-json": "JSON 解析不了，怕覆盖掉你的配置",
    "not-dict": "内容不是 JSON 对象，怕覆盖掉你的配置",
    "bad-env": "env 不是对象，怕覆盖掉你的配置",
    "unreadable": "文件读不了（权限？编码？）",
    "foreign": "指向的不是本服务的地址，估计是你自己配的中转，不动",
}


def _is_ours(env: dict, expected: dict) -> bool:
    """磁盘上这份 env 是不是我们写的。

    · 受管键一个都没有（空 env / 别人只写了别的键）→ 算我们的，可以填；
    · ANTHROPIC_BASE_URL 与 identity 规范化后相同 → 我们的；
    · base 是别的地址 → 别人的，不动。"""
    present = {k: env.get(k) for k in MANAGED_KEYS if k in env}
    if not present:
        return True
    base = present.get("ANTHROPIC_BASE_URL")
    if not base:
        return True                    # 只有 token 没有 base：半成品，补上 base
    return normalize_base(str(base)) == normalize_base(str(expected.get("ANTHROPIC_BASE_URL") or ""))


def _plan_one(path: Path, expected: dict, force: bool, is_config: bool):
    """返回 (Change | None, status)。status 是 ok/healthy/skip 之类的内部标记。"""
    data, status = read_strict(path)
    if status == "missing":
        return None, "missing"
    if status != "ok":
        # ★ 哪怕 --force 也不覆盖坏文件：那是用户别处的配置，重建等于删数据
        return None, status

    env = dict(data.get("env") or {})
    if not _is_ours(env, expected) and not force:
        return None, "foreign"

    keys = [k for k in MANAGED_KEYS if k in expected and env.get(k) != expected[k]]
    new_data = dict(data)
    if keys:
        merged = dict(env)
        merged.update({k: expected[k] for k in keys})
        new_data["env"] = merged
    if is_config and data.get("hasCompletedOnboarding") is not True:
        new_data["hasCompletedOnboarding"] = True
        keys.append("hasCompletedOnboarding")
    if not keys:
        return None, "healthy"
    return Change(path=path, data=new_data, keys=keys,
                  reason="fill" if not env else "drift"), "change"


def plan_repair(identity, config_path=None, settings_path=None, force=False,
                create_missing=False):
    """只算不写：对账 identity 与两份配置。

    force=True          连"指向别人家中转"的也覆盖（--repair --force）
    create_missing=True 两份都不在时也按 identity 建出来（--repair 用；自动修复不用，
                        那种情况交给安装器）
    """
    config_path = Path(config_path or CLAUDE_CONFIG)
    settings_path = Path(settings_path or CLAUDE_SETTINGS)
    plan = Plan()

    expected = env_from_identity(identity)
    if not expected.get("ANTHROPIC_AUTH_TOKEN"):
        plan.problem = "no-identity"
        return plan
    if not expected.get("ANTHROPIC_BASE_URL"):
        plan.problem = "bad-identity"
        return plan

    targets = ((config_path, True), (settings_path, False))
    states = {}
    for path, _is_config in targets:
        change, status = _plan_one(path, expected, force, _is_config)
        states[path] = status
        if change is not None:
            plan.changes.append(change)
        elif status == "healthy":
            plan.healthy.append(path)
        elif status == "missing":
            pass                       # 先不报；下面看另一份决定要不要建
        else:
            plan.skipped.append((path, status))

    # 两份都不在：自动修复不主动创建（安装器负责），--repair 才建
    if all(s == "missing" for s in states.values()) and not create_missing:
        return plan

    # 只有一份在：另一份是我们的 → 补建；另一份也不是我们的 → 交给安装器，不吭声。
    # --repair（create_missing）时两份都不在 → 都建；但另一份是别人的 → 还是不动，
    # 否则会一边保留用户自己的中转地址、一边把更高优先级的 settings.json 写成我们的。
    for path, is_config in targets:
        if states[path] != "missing":
            continue
        other = next(p for p, _ in targets if p != path)
        other_ours = states[other] in ("healthy", "change")
        both_missing = create_missing and states[other] == "missing"
        if not (other_ours or both_missing):
            continue
        data = {"env": dict(expected)}
        if is_config:
            data["hasCompletedOnboarding"] = True
        plan.changes.append(Change(path=path, data=data,
                                   keys=list(expected.keys()) +
                                         (["hasCompletedOnboarding"] if is_config else []),
                                   reason="create"))
    return plan


def check_and_repair(identity=None, force=False, dry_run=False,
                     config_path=None, settings_path=None, create_missing=False,
                     _writer=_write_json_atomic):
    """对账 + 需要就修。返回 Result；不抛异常（异常都折成 ok=False）。

    dry_run=True：只算，不备份、不写盘。
    """
    try:
        if identity is None:
            from server.identity import load_identity
            identity = load_identity()
    except Exception:
        identity = None

    if not identity:
        return Result(ok=True, problem="no-identity")

    try:
        plan = plan_repair(identity, config_path, settings_path, force=force,
                           create_missing=create_missing)
        if plan.problem:
            return Result(ok=False, problem=plan.problem)
        result = Result(ok=True, planned=dry_run, changes=list(plan.changes),
                        healthy=list(plan.healthy), skipped=list(plan.skipped))
        if not plan.changes:
            return result
        if dry_run:
            return result

        failed = []
        for change in plan.changes:
            path = Path(change.path)
            try:
                if path.exists():
                    # ★ 也走 keep_existing：`*.aikaui-bak` 的定义是"装我们之前的样子"，
                    #   不能被后来任何一次自动修复拿我们自己的配置顶掉。
                    _backup(path, keep_existing=True)
                _writer(path, change.data)
                # 写完重读：落盘失败（磁盘满、权限、被别的进程抢写）不能报成功
                back, status = read_strict(path)
                if status != "ok":
                    raise ValueError(f"写完读不回来（{status}）")
                env = back.get("env") or {}
                expected_env = change.data.get("env") or {}
                missing = [
                    k for k in change.keys
                    if (k in MANAGED_KEYS and env.get(k) != expected_env.get(k))
                    or (k == "hasCompletedOnboarding" and back.get(k) is not True)
                ]
                if missing:
                    raise ValueError("写完核对不一致：" + "、".join(missing))
            except Exception as e:
                failed.append((path, f"write: {e}"))

        if failed:
            kept = [c for c in result.changes if Path(c.path) not in {p for p, _ in failed}]
            return Result(ok=False, repaired=bool(kept), changes=kept,
                          healthy=result.healthy,
                          skipped=result.skipped + failed)
        result.repaired = True
        return result
    except Exception as e:                       # 兜底：自愈永远不该把启动拖垮
        return Result(ok=False, problem=f"error: {e}")
