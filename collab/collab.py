#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collab.py — 两个 Agent 拿 git 当总线：管对话、管任务、管交接。

为什么是这个形状（完整规范见同目录 PROTOCOL.md）：

  · **只增不改**。一条消息 = 一个新文件，而且只写进 `log/<自己>/`。
    两个 Agent 永远不碰同一个文件，git 合并不可能冲突。
  · **任务状态是算出来的**。认领 / 完成本身也是消息，没有任何"共享可写文件"
    需要谁去改。`status` 每次现算一遍。
  · **git push 就是那把锁**。推送成功后再复查一遍有没有比自己更早的认领，
    有就自动撤回。所以不需要锁服务、不需要常驻进程、不需要谁先谁后商量。
  · **离线不阻塞**。先本地提交；推不上去就挂账，下次 `sync` 自动补推。

常用命令（`python collab.py <cmd> -h` 看细节）：

    python collab.py init --as local           # 一次性：建/接 协作分支
    python collab.py status                    # 每次开工先看这个
    python collab.py poll                      # 有没有新消息
    python collab.py task new --title "改 X"    # 提个任务
    python collab.py claim T-0001              # 认领
    python collab.py send --to remote -m "..." # 说句话
    python collab.py ask  --to remote -m "..." # 问一句（等答复）
    python collab.py answer 7f3a -m "..."      # 答复
    python collab.py done T-0001 -m "已改完"    # 收工
    python collab.py export                    # 导出人看的完整对话

只用标准库，Python 3.8+，Windows / Linux 都能跑。
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- 常量

BRANCH = "collab"          # 协作数据住在这个分支上，跟代码分支彻底分开
DEFAULT_AGENTS = ("local", "remote")
KINDS = ("note", "ask", "answer", "task", "claim", "release", "done", "cancel", "handoff")

GITATTR = "* -text\n"      # 关掉换行转换：Windows 写 LF 就是 LF，Linux 拉下来不会被改
CFG_TMPL = {
    "agents": list(DEFAULT_AGENTS),
    "areas": {                      # 地盘划分，只是提示，不强制
        "ui/": "local",
        "main.py": "local",
        "config.py": "local",
        "server/": "remote",
        "prompts.py": "remote",
    },
    "note": "areas 是建议分工：认领别人地盘上的任务时 collab.py 会提醒一句，不会拦",
}
DATA_README = """# 这个分支是干什么的

**给两个 Agent 当对话和任务总线用的，不要在这里放代码。**

- 代码请走各自的代码分支；合并到 `main` 由人来做。
- 这里的文件：`log/`（每人的发言，只增不改）、`tasks/`（任务说明）、`cfg.json`（分工）。
- 想看发生了什么：`python collab.py status` 或 `python collab.py export`。
- 规则写在 `PROTOCOL.md`（在代码分支的 `collab/` 目录里）。
"""

MIRROR = None       # 协作镜像仓库的工作目录，main() 里定
ME = None           # 本机 Agent 的名字


class GitError(RuntimeError):
    pass


# ---------------------------------------------------------------- git 小工具

def git_env():
    """禁用交互式要密码：宁可报错，也不要让 Agent 卡在那里等输入。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "echo")
    return env


def run_git(*args, cwd=None, timeout=180):
    """跑一条 git 命令，原样返回 CompletedProcess（不抛异常）。"""
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false", *args]
    try:
        return subprocess.run(
            cmd, cwd=str(cwd or MIRROR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, env=git_env(),
        )
    except subprocess.TimeoutExpired:
        class _T:
            returncode, stdout, stderr = 124, "", "git 超时（%ss）" % timeout
        return _T()
    except FileNotFoundError:
        class _F:
            returncode, stdout, stderr = 127, "", "找不到 git，请先安装 git 并放进 PATH"
        return _F()


def git(*args, cwd=None):
    p = run_git(*args, cwd=cwd)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise GitError(msg or "git %s 失败" % " ".join(args))
    return p.stdout or ""


def git_ok(*args, cwd=None):
    return run_git(*args, cwd=cwd).returncode == 0


def write_text(path, text):
    """永远写 LF。Windows 上默认写 CRLF，那样 Linux 拉下来会整天显示"整个文件都改了"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def collab_home():
    """镜像仓库和"我读到哪儿了"的游标都放这儿。一台机器一个身份。"""
    env = os.environ.get("COLLAB_HOME")
    return Path(env) if env else Path.home() / ".claude-collab"


def self_path():
    return collab_home() / "self.json"


def load_self():
    try:
        return json.loads(self_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_self(d):
    cur = load_self()
    cur.update(d)
    write_text(self_path(), json.dumps(cur, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------- 消息读写

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_id():
    return "%s-%s-%s" % (stamp(), ME, secrets.token_hex(2))


def handle(mid):
    """短句柄：id 的最后一段（4 个 hex）。给人抄的，find_ev 认它。"""
    return (mid or "").rsplit("-", 1)[-1]


def dump_msg(meta, body):
    head = ["---"]
    for k in ("id", "from", "to", "kind", "re", "area", "subject", "at"):
        head.append("%s: %s" % (k, meta.get(k) or ""))
    head.append("---")
    text = "\n".join(head) + "\n\n"
    body = (body or "").strip()
    return text + (body + "\n" if body else "")


def load_msg(path):
    """读一个 .md：--- 包起来的是头部，剩下的全算正文。故意不用 yaml，少一个依赖。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.match(r"^\ufeff?---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", text, re.S)
    if not m:
        return None
    meta = {"body": (m.group(2) or "").strip(), "path": path}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    if not meta.get("id") or not meta.get("kind"):
        return None
    return meta


def all_events():
    """协作分支上的所有东西：log/ 里的发言 + tasks/ 里的任务说明，按时间排好。"""
    out = []
    for pat in ("log/*/*.md", "tasks/*.md"):
        for p in sorted(MIRROR.glob(pat)):
            ev = load_msg(p)
            if ev:
                out.append(ev)
    # 排序键：先时间，再 (from, id)。两台机器时钟对不齐时也保证两边算出来一样
    out.sort(key=lambda e: (e.get("at") or "", e.get("from") or "", e.get("id") or ""))
    return out


def find_ev(pat):
    """认全 id、前缀、短句柄。返回 (事件, 候选列表)。"""
    cands = []
    for ev in all_events():
        if ev["id"] == pat:
            return ev, [ev]
        if ev["id"].startswith(pat) or handle(ev["id"]) == pat.lower() or ev["id"].endswith(pat):
            cands.append(ev)
    return (cands[0] if len(cands) == 1 else None), cands


# ---------------------------------------------------------------- 状态推导

def derive(evs=None):
    """把"只增不改"的流水算成当前状态。没有任何一个字段是被谁手改出来的。"""
    evs = evs if evs is not None else all_events()
    specs, msgs, answers, shadowed = {}, [], set(), []

    for ev in evs:
        if ev["kind"] == "task":
            tid = ev["id"]
            if tid not in specs:
                specs[tid] = ev
            else:
                # 两边在同一时刻开了同一个号：先到的那份算数，后到的那份只记一笔。
                # 谁先到由 (at, from) 定，两边算出来一定一样。
                first, other = specs[tid], ev
                if (other["at"], other["from"]) < (first["at"], first["from"]):
                    specs[tid], other = other, first
                shadowed.append({"id": tid, "ignored": other["path"],
                                 "kept": specs[tid]["path"]})
        else:
            msgs.append(ev)
            if ev["kind"] == "answer" and ev.get("re"):
                answers.add(ev["re"])

    tasks = {}
    for tid, ev in specs.items():
        tasks[tid] = {
            "id": tid, "title": ev.get("subject") or "(无标题)",
            "by": ev.get("from"), "at": ev.get("at"), "area": ev.get("area"),
            "owner": None, "done_by": None, "done_at": None, "cancelled": False,
            "body": ev.get("body", ""), "spec": ev,
        }
    for m in msgs:                                  # 消息已经按时间排好，顺序过一遍即可
        t = tasks.get(m.get("re"))
        if not t:
            continue
        if m["kind"] == "claim":
            if not t["owner"]:
                t["owner"] = m.get("from")          # 最早那次认领说了算
        elif m["kind"] == "release":
            if t["owner"] == m.get("from"):
                t["owner"] = None
        elif m["kind"] == "done":
            t["done_by"], t["done_at"] = m.get("from"), m.get("at")
        elif m["kind"] == "cancel":
            t["cancelled"] = True

    for t in tasks.values():
        t["state"] = ("已取消" if t["cancelled"] else
                      "已完成" if t["done_by"] else
                      "进行中" if t["owner"] else "待认领")
    return {"tasks": tasks, "msgs": msgs, "answered": answers, "specs": specs,
            "shadowed": shadowed}


def inbox(evs):
    """写给我的、还没答的提问。"""
    return [m for m in evs["msgs"]
            if m["kind"] == "ask" and m.get("to") in (ME, "all", "")
            and m.get("from") != ME and m["id"] not in evs["answered"]]


def todo(evs):
    return [t for t in evs["tasks"].values() if t["state"] == "待认领"]


def mine(evs):
    return [t for t in evs["tasks"].values() if t["owner"] == ME and t["state"] == "进行中"]


def area_owner(cfg, area):
    if not area:
        return None
    for k, v in (cfg or {}).get("areas", {}).items():
        if area == k or area.rstrip("/") == k.rstrip("/") or area.startswith(k.rstrip("/") + "/"):
            return v
    return None


# ---------------------------------------------------------------- 推送 / 同步

def pending_count():
    p = run_git("rev-list", "--count", "origin/%s..%s" % (BRANCH, BRANCH))
    try:
        return int((p.stdout or "0").strip() or 0)
    except ValueError:
        return 0                                   # 远端还没有这个分支


def commit(msg):
    git("add", "-A")
    if git_ok("diff", "--cached", "--quiet"):
        return False                               # 没有实际变化
    git("commit", "-q", "-m", msg)
    return True


def push(quiet=False):
    """推送。成功返回 (True, "")；快进不了的先 rebase 再试，最多 5 轮。

    这是整套协议里唯一的原子操作：git 服务端对同一个 ref 只接受一次更新，
    所以"谁先推上去"天然互斥，不需要锁、不需要谁先报名。
    """
    for _ in range(5):
        p = run_git("push", "origin", "HEAD:refs/heads/%s" % BRANCH)
        if p.returncode == 0:
            return True, ""
        err = (p.stderr or p.stdout or "").strip()
        low = err.lower()
        if ("non-fast-forward" in low or "rejected" in low or "fetch first" in low
                or "behind" in low):
            if not fetch_and_rebase(quiet=quiet):
                return False, err
            continue
        return False, err                           # 网络 / 权限 / 没登录，重试没用
    return False, "推送反复被拒，已试 5 次"


def fetch_and_rebase(quiet=False):
    if run_git("fetch", "origin").returncode != 0:
        return False
    if pending_count():
        p = run_git("rebase", "origin/%s" % BRANCH)
        if p.returncode != 0:
            run_git("rebase", "--abort")
            raise GitError("rebase 冲突。按协议不该发生（只增不改），"
                           "多半是有人手工改过协作分支上的文件：\n" + (p.stderr or ""))
        if not quiet:
            print("  （对端有新提交，已 rebase 叠上去）")
    else:
        # 没有本地提交时也必须走一步：fetch 只更新 ref，**不动工作区里的文件**。
        # 少了这一步，只会读不会写的那一方会一直盯着自己那份旧的看。
        p = run_git("merge", "--ff-only", "origin/%s" % BRANCH)
        if p.returncode != 0:
            raise GitError("工作区有没提交的改动，挡着快进了。"
                           "协作分支是消息仓库，不要手工编辑里面的文件：\n"
                           + (p.stderr or ""))
    return True


def withdraw_lost_claims(quiet=True):
    """认领输了就自己撤：删掉自己那条 claim 再推一次。

    什么时候会输：两台机器几乎同时认领了同一个任务，对方先推成功。
    自己撤掉，任务就干净地归对方，不用人介入。
    """
    evs = derive()
    killed = []
    for m in evs["msgs"]:
        if m["kind"] == "claim" and m.get("from") == ME:
            t = evs["tasks"].get(m.get("re"))
            if t and t["owner"] != ME:
                try:
                    m["path"].unlink()
                    killed.append((t["id"], t.get("owner")))
                except OSError:
                    pass
    if killed:
        commit("collab(%s): 撤回落选的认领" % ME)
        push(quiet=True)
        if not quiet:
            for tid, who in killed:
                print("  ⚠ %s 已被 %s 抢先，我的认领已撤回" % (tid, who))
    return killed


def sync(quiet=False, withdraw=True):
    """拉最新的、把自己的提交叠上去、必要时撤回认领。False = 离线。"""
    if not (MIRROR / ".git").exists():
        raise GitError("还没 init 过。先跑：python collab.py init --as %s" % ME)
    online = fetch_and_rebase(quiet=quiet)
    if online and withdraw:
        withdraw_lost_claims(quiet=quiet)
    return online


def commit_and_push(subject, quiet=False):
    """提交 + 推送。推不上去不算失败：挂账，下次 sync 补。"""
    if not commit(subject):
        return True
    ok, why = push(quiet=quiet)
    if not ok:
        print("  ⚠ 已本地提交，但推不出去：%s" % (why.splitlines()[0][:120] or "原因不明"))
        print("    离线也能继续干活；下次 `python collab.py sync` 会自动补推。")
    return ok


# ---------------------------------------------------------------- 输出小工具

def hhmm(at):
    m = re.match(r"\d{4}-\d\d-\d\dT(\d\d:\d\d)", at or "")
    return m.group(1) if m else (at or "?")


def arrow(to):
    return "→全员" if to in ("all", "") else "→%s" % to


def re_short(re_):
    """re 可能指向一个任务号，也可能指向某条消息的完整 id。给人看的都缩成短句柄。"""
    if not re_:
        return ""
    return re_ if re_.startswith("T-") else handle(re_)


def fmt_msg(m, maxlen=64):
    body = re.sub(r"\s+", " ", m.get("body") or "").strip()
    if len(body) > maxlen:
        body = body[:maxlen - 1] + "…"
    tail = re_short(m.get("re"))
    return "%s  %-9s %-7s %s  %s%s" % (
        hhmm(m.get("at")), "%s%s" % (m.get("from"), arrow(m.get("to"))),
        m["kind"], handle(m["id"]), ("re %s  " % tail) if tail else "", body)


def need_init():
    print("✖ 还没初始化。先跑一次：")
    print("    python collab.py init --as <你的名字>")
    return 2


def load_cfg():
    try:
        return json.loads((MIRROR / "cfg.json").read_text(encoding="utf-8"))
    except Exception:
        return dict(CFG_TMPL)


def require_online():
    """同步一次，顺便告诉调用方现在到底通不通网。"""
    if sync(quiet=True):
        return True
    print("⚠ 连不上远端：这次改动会先存在本地，下次 sync 补推。")
    return False


# ---------------------------------------------------------------- 命令

def cmd_init(a):
    global ME, MIRROR
    home = collab_home()
    home.mkdir(parents=True, exist_ok=True)
    ME = a.as_ or ME
    if not ME:
        return need_init()
    MIRROR = home / "mirror"

    if not a.repo:                                  # 没说就跟着当前仓库的 origin 走
        p = run_git("remote", "get-url", "origin", cwd=Path.cwd())
        if p.returncode != 0:
            print("✖ 当前目录不是 git 仓库或没配 origin。请加 --repo <git 地址>")
            return 2
        a.repo = (p.stdout or "").strip()
    print("协作仓库：%s" % a.repo)
    print("镜像目录：%s" % MIRROR)
    print("我的身份：%s" % ME)

    if not (MIRROR / ".git").exists():
        git("init", "-q", str(MIRROR), cwd=home)
    if not git_ok("remote", "get-url", "origin"):
        git("remote", "add", "origin", a.repo)
    else:
        git("remote", "set-url", "origin", a.repo)
    git("config", "user.name", "collab-%s" % ME)
    git("config", "user.email", "%s@collab.local" % ME)
    git("config", "core.autocrlf", "false")
    # 只拉 collab 一个分支：拉全库会把代码历史也拽下来，没必要
    git("config", "remote.origin.fetch",
        "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH))

    # ls-remote 对空仓库也不会报错，用它判断"远端到底有没有 collab 分支"最稳。
    # 直接用 fetch 判断会踩坑：远端还没有这个 ref 时，配置里的 refspec 一匹配不上，
    # git 会直接 fatal 退出（远端是空仓库时尤其如此）。
    ls = run_git("ls-remote", "--heads", "origin", BRANCH)
    if ls.returncode != 0:
        print("✖ 连不上远端。检查网络，以及这台机器有没有这个仓库的读权限。")
        print("  %s" % (ls.stderr or "").strip().splitlines()[-1:][:1])
        return 2

    if (ls.stdout or "").strip():
        git("fetch", "origin")
        git("checkout", "-q", "-B", BRANCH, "origin/%s" % BRANCH)
        print("接上已有的协作分支 %s" % BRANCH)
        ensure_data_files()
    else:
        git("checkout", "-q", "--orphan", BRANCH)   # 孤儿分支：跟代码历史完全不沾
        ensure_data_files()
        commit("collab: 初始化协作分支")
        p = run_git("push", "-u", "origin", "HEAD:refs/heads/%s" % BRANCH)
        if p.returncode != 0:
            print("✖ 推不上去：%s" % (p.stderr or "").strip().splitlines()[-1][:200])
            print("  这个仓库需要写权限。第一次可以先手工登录一次：")
            print("    cd \"%s\" && git push -u origin %s" % (MIRROR, BRANCH))
            return 2
        print("建好了协作分支 %s（孤儿分支，只放对话和任务，不放代码）" % BRANCH)
    if git_ok("rev-parse", "--verify", "-q", "refs/remotes/origin/%s" % BRANCH):
        git("branch", "--set-upstream-to=origin/%s" % BRANCH)
    save_self({"name": ME, "repo": a.repo, "home": str(home)})
    print("✔ 就绪。下一个命令：python collab.py status")
    return 0


def ensure_data_files():
    changed = []
    if not (MIRROR / ".gitattributes").exists():
        write_text(MIRROR / ".gitattributes", GITATTR)
        changed.append(".gitattributes")
    if not (MIRROR / "cfg.json").exists():
        write_text(MIRROR / "cfg.json", json.dumps(CFG_TMPL, ensure_ascii=False, indent=2) + "\n")
        changed.append("cfg.json")
    if not (MIRROR / "README.md").exists():
        write_text(MIRROR / "README.md", DATA_README)
        changed.append("README.md")
    if changed:
        commit("collab: 补上 %s" % "/".join(changed))
        push(quiet=True)
        print("  补建了 %s" % "、".join(changed))


def cmd_status(a):
    if not sync(quiet=True):
        print("⚠ 连不上远端，下面显示的是本地最后一次同步到的状态。")
    evs = derive()
    cfg = load_cfg()
    pend = pending_count()

    if a.json:
        print(json.dumps({
            "me": ME, "pending_push": pend,
            "inbox": [m["id"] for m in inbox(evs)],
            "todo": [t["id"] for t in todo(evs)],
            "mine": [t["id"] for t in mine(evs)],
            "tasks": {k: {kk: vv for kk, vv in v.items() if kk != "spec"}
                      for k, v in evs["tasks"].items()},
        }, ensure_ascii=False, indent=2))
        return 0

    print("── collab " + "─" * 52)
    print("  我 = %-8s  分支 %s  对端 = %s" % (
        ME, BRANCH, "、".join(x for x in cfg.get("agents", []) if x != ME) or "?"))
    print("  待推送 %d 条   待我回复 %d 条   待认领 %d 个   我手上 %d 个" % (
        pend, len(inbox(evs)), len(todo(evs)), len(mine(evs))))

    rows = inbox(evs)
    if rows:
        print("\n【待我回复】")
        for m in rows:
            print("  %s  %s %s 问：" % (handle(m["id"]), hhmm(m["at"]), m["from"]))
            if m.get("subject"):
                print("      %s" % m["subject"])
            if m.get("body"):
                print("      %s" % re.sub(r"\s+", " ", m["body"])[:140])
            print("      回答：python collab.py answer %s -m \"...\"" % handle(m["id"]))
    rows = todo(evs)
    if rows:
        print("\n【待认领】")
        for t in rows:
            own = area_owner(cfg, t.get("area"))
            print("  %s  %s  %s%s" % (t["id"], hhmm(t["at"]), t["title"],
                                      ("  ←%s 的地盘" % own) if own and own != ME else ""))
    rows = mine(evs)
    if rows:
        print("\n【我手上】")
        for t in rows:
            print("  %s  %s" % (t["id"], t["title"]))
    others = [t for t in evs["tasks"].values() if t["state"] == "进行中" and t["owner"] != ME]
    if others:
        print("\n【对端在做】")
        for t in others:
            print("  %s  %s  (%s)" % (t["id"], t["title"], t["owner"]))
    if a.all:
        print("\n【全部任务】")
        for t in sorted(evs["tasks"].values(), key=lambda x: x["id"]):
            print("  %s  %-4s %-8s %s" % (t["id"], t["state"], t["owner"] or "-", t["title"]))

    if evs["shadowed"]:
        print("\n【撞号的副本】(任务号重复，以下这些已被忽略，可以删掉)")
        for s in evs["shadowed"]:
            print("  忽略 %s（保留 %s）" % (s["ignored"].name, s["kept"].name))
    if evs["msgs"]:
        n = len(evs["msgs"]) if a.all else 8
        print("\n【最近对话】(最后 %d 条，全文用 export)" % n)
        for m in evs["msgs"][-n:]:
            print("  " + fmt_msg(m))
    if not evs["msgs"] and not evs["tasks"]:
        print("\n  （还是空的。提个任务：python collab.py task new --title \"...\"）")
    return 0


def cmd_poll(a):
    if not sync(quiet=True):
        print("⚠ 连不上远端。")
    try:
        cursor = tuple(load_self().get("cursor") or ())
    except TypeError:
        cursor = ()
    evs = all_events()
    fresh = [e for e in evs
             if (e.get("at") or "", e.get("from") or "", e.get("id") or "") > cursor
             and e.get("from") != ME]
    if not fresh:
        print("没有新东西。")
    else:
        print("新消息 %d 条：\n" % len(fresh))
        for e in fresh:
            if e["kind"] == "task":
                print("  %s  %s 开出任务 %s：%s" % (hhmm(e["at"]), e["from"], e["id"], e.get("subject")))
                if e.get("body"):
                    print("     " + e["body"].replace("\n", "\n     "))
                print("     认领：python collab.py claim %s\n" % e["id"])
            else:
                print("  %s  %s %s [%s]%s   id=%s" % (
                    hhmm(e["at"]), e["from"], arrow(e.get("to")), e["kind"],
                    (" re %s" % re_short(e["re"])) if e.get("re") else "", handle(e["id"])))
                if e.get("body"):
                    print("     " + e["body"].replace("\n", "\n     "))
                print("")
    if evs:
        save_self({"cursor": (evs[-1].get("at") or "", evs[-1].get("from") or "",
                              evs[-1].get("id") or "")})
    d = derive(evs)
    if inbox(d):
        print("⚠ 有 %d 条在等我回复：python collab.py status" % len(inbox(d)))
    return 0


def read_body(a):
    if getattr(a, "file", None):
        return Path(a.file).read_text(encoding="utf-8", errors="replace")
    return " ".join(getattr(a, "msg", None) or [])


def cmd_send(a, kind=None):
    require_online()
    kind = kind or a.kind
    body = read_body(a)
    mid = new_id()
    meta = {"id": mid, "from": ME, "to": a.to, "kind": kind, "re": a.re or "",
            "area": getattr(a, "area", "") or "",
            "subject": a.subject or (body.splitlines()[0][:60] if body else ""),
            "at": now_iso()}
    write_text(MIRROR / "log" / ME / (mid + ".md"), dump_msg(meta, body))
    print("发出 %s [%s] %s" % (mid, kind, arrow(a.to)))
    commit_and_push("collab(%s): %s %s" % (ME, kind, mid))
    return 0


def next_task_id():
    mx = 0
    for p in (MIRROR / "tasks").glob("T-*.md"):
        m = re.match(r"T-(\d+)", p.stem)
        if m:
            mx = max(mx, int(m.group(1)))
    return "T-%04d" % (mx + 1)


def cmd_task_new(a):
    require_online()
    body = read_body(a)
    for _ in range(3):
        tid = next_task_id()
        meta = {"id": tid, "from": ME, "to": "all", "kind": "task", "re": "",
                "area": a.area or "", "subject": a.title, "at": now_iso()}
        # 文件名带作者名：万一两边同时开了同一个号，路径也不撞，git 不会冲突
        mine_path = MIRROR / "tasks" / ("%s-%s.md" % (tid, ME))
        write_text(mine_path, dump_msg(meta, body))
        commit("collab(%s): 开出任务 %s" % (ME, tid))
        ok, why = push(quiet=True)
        if not ok:
            print("  ⚠ 已本地提交但推不出去：%s" % (why.splitlines()[0][:100]))
            print("开出任务 %s：%s（下次 sync 补推）" % (tid, a.title))
            return 0
        sync(quiet=True, withdraw=False)
        rivals = [load_msg(p) for p in (MIRROR / "tasks").glob("%s-*.md" % tid) if p != mine_path]
        rivals = [e for e in rivals if e]
        # 撞号了就让位重开。谁赢的规矩跟 derive() 完全一致：(at, from) 小的算数，
        # 这样两边各自算出来的结果一定一样，不会互相以为是对方该让。
        if rivals and min(rivals, key=lambda e: (e["at"], e["from"]))["from"] != ME:
            winner = min(rivals, key=lambda e: (e["at"], e["from"]))
            mine_path.unlink()
            commit("collab(%s): 任务号 %s 撞了，让给 %s" % (ME, tid, winner.get("from")))
            push(quiet=True)
            print("  （%s 被 %s 同时开了出来，我换一个号）" % (tid, winner.get("from")))
            continue
        print("开出任务 %s：%s" % (tid, a.title))
        other = area_owner(load_cfg(), a.area)
        if other and other != ME:
            print("  提醒：%s 是 %s 的地盘，动手前先 ask 一句。" % (a.area, other))
        return 0
    print("✖ 连撞三次号，先 sync 一下再试。")
    return 2


def _task_event(task, kind, extra=""):
    # 先同步再看：不然后果是"对端刚开出来的任务，我这边说没有这个任务"
    require_online()
    evs = derive()
    t = evs["tasks"].get(task)
    if not t:
        print("✖ 没有这个任务：%s（python collab.py status 看有哪些）" % task)
        return 2
    if kind == "claim":
        if t["state"] == "已完成":
            print("✖ %s 已经完成了。" % task)
            return 2
        if t["state"] == "已取消":
            print("✖ %s 已经取消了。" % task)
            return 2
        if t["owner"] and t["owner"] != ME:
            print("✖ %s 已经被 %s 认领了。" % (task, t["owner"]))
            return 2
        if t["owner"] == ME:
            print("✔ %s 本来就是我的。" % task)
            return 0
    if kind in ("release", "done", "cancel") and t["owner"] != ME:
        verb = {"release": "放掉", "done": "标成完成", "cancel": "取消"}[kind]
        print("✖ %s 不在我手上，不能%s。" % (task, verb))
        return 2

    mid = new_id()
    meta = {"id": mid, "from": ME, "to": "all", "kind": kind, "re": task,
            "subject": "", "at": now_iso()}
    write_text(MIRROR / "log" / ME / (mid + ".md"), dump_msg(meta, extra))
    commit("collab(%s): %s %s" % (ME, kind, task))
    ok, why = push(quiet=True)
    if not ok:
        # 推不上去就别报喜：本地那份还不算数，联网后可能发现自己其实抢输了
        print("  ⚠ 推不出去（%s），这条只是先记在本地。" % (why.splitlines()[0][:100]))
        print("    联网后 `python collab.py sync` 才生效；要是那时发现被抢先，会自动撤回。")
        return 0

    sync(quiet=True, withdraw=False)
    t = derive()["tasks"].get(task) or t
    if kind == "claim":
        if t["owner"] == ME:
            print("✔ 认领到 %s：%s" % (task, t["title"]))
            print("  代码做在自己的分支上，比如 %s/%s-<简短描述>" % (ME, task.lower()))
            print("  做完：python collab.py done %s -m \"改了什么\"" % task)
            return 0
        print("✖ 没抢到：%s 被 %s 抢先了（我的认领会自动撤回）。" % (task, t["owner"]))
        return 1
    print("✔ %s 已标记 %s" % (task, {"release": "放回待认领", "done": "完成",
                                     "cancel": "取消"}.get(kind, kind)))
    return 0


def cmd_claim(a):
    return _task_event(a.task, "claim")


def cmd_release(a):
    return _task_event(a.task, "release")


def cmd_done(a):
    return _task_event(a.task, "done", " ".join(a.msg or []))


def cmd_cancel(a):
    return _task_event(a.task, "cancel", " ".join(a.msg or []))


def cmd_answer(a):
    require_online()
    target, cands = find_ev(a.target)
    if not target:
        if cands:
            print("✖ %s 有歧义，匹配到 %d 条：%s" % (
                a.target, len(cands), "、".join(handle(c["id"]) for c in cands[:6])))
        else:
            print("✖ 找不到消息 %s" % a.target)
        return 2
    body = read_body(a) or "(收到)"
    mid = new_id()
    meta = {"id": mid, "from": ME, "to": target.get("from"), "kind": "answer",
            "re": target["id"],
            "subject": "答复 " + (target.get("subject") or target["id"])[:50],
            "at": now_iso()}
    write_text(MIRROR / "log" / ME / (mid + ".md"), dump_msg(meta, body))
    commit_and_push("collab(%s): answer %s" % (ME, handle(target["id"])))
    print("已答复 %s → %s" % (handle(target["id"]), target.get("from")))
    return 0


def cmd_log(a):
    sync(quiet=True)
    rows = derive()["msgs"]
    if a.kind:
        rows = [m for m in rows if m["kind"] == a.kind]
    if a.peer:
        rows = [m for m in rows if m.get("from") == a.peer or m.get("to") in (a.peer, "all")]
    for m in (rows[-a.n:] if a.n else rows):
        print(fmt_msg(m, maxlen=200))
        if m.get("body") and a.verbose and m["body"] != m.get("subject"):
            print("    " + m["body"].replace("\n", "\n    "))
    return 0


def cmd_show(a):
    sync(quiet=True)
    ev, cands = find_ev(a.id)
    if not ev:
        print("✖ 找不到 %s%s" % (a.id, "（有多条匹配）" if cands else ""))
        return 2
    print(dump_msg(ev, ev.get("body", "")))
    return 0


def cmd_export(a):
    sync(quiet=True)
    evs = derive()
    out = ["# 两个 Agent 的对话记录", "",
           "由 `collab.py export` 生成；源在 git 分支 `%s`。" % BRANCH, "", "## 任务", ""]
    for t in sorted(evs["tasks"].values(), key=lambda x: (x["at"] or "", x["id"])):
        out += ["### %s %s" % (t["id"], t["title"]), "",
                "- 谁提的：%s　%s" % (t["by"], hhmm(t["at"])),
                "- 状态：%s%s" % (t["state"], ("（%s）" % t["owner"]) if t["owner"] else ""),
                "- 地盘：%s" % (t["area"] or "不限"), ""]
        if t["body"]:
            out += [t["body"], ""]
    out += ["## 对话流", ""]
    for m in evs["msgs"]:
        out += ["**%s**　%s　%s　`%s`%s" % (
            hhmm(m["at"]), m["from"], m["kind"], handle(m["id"]),
            ("　re %s" % re_short(m["re"])) if m.get("re") else ""),
            "", (m.get("body") or "_(空)_"), ""]
    dest = Path(a.out) if a.out else collab_home() / "transcript.md"
    write_text(dest, "\n".join(out) + "\n")
    print("已导出 %d 条消息、%d 个任务 → %s" % (len(evs["msgs"]), len(evs["tasks"]), dest))
    return 0


def cmd_sync(a):
    if not sync(quiet=False):
        print("✖ 连不上远端，本次没同步。本地提交都还在，没丢。")
        return 1
    print("✔ 同步完成。")
    if pending_count():
        ok, why = push()
        print("✔ 补推完成。" if ok else "⚠ 还是推不出去：%s" % why.splitlines()[0][:120])
    return 0


# ---------------------------------------------------------------- 入口

def build_parser():
    p = argparse.ArgumentParser(
        prog="collab.py", description="两个 Agent 用 git 管对话和任务",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--as", dest="as_", default=os.environ.get("COLLAB_AGENT"),
                   help="我是谁（也可用环境变量 COLLAB_AGENT）")
    sub = p.add_subparsers(dest="cmd")

    def add_body(s, to=True):
        if to:
            s.add_argument("--to", default="all", help="发给谁：对方名字或 all")
        s.add_argument("-m", "--msg", nargs="*", help="正文")
        s.add_argument("--file", help="正文从文件读")
        s.add_argument("--subject", help="一句话标题")
        s.add_argument("--area", help="涉及哪块（ui/ server/ …）")

    s = sub.add_parser("init", help="一次性：建或接上协作分支")
    s.add_argument("--repo", help="协作仓库地址；不写就用当前目录的 origin")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("status", help="当前态势（每次开工先跑这个）")
    s.add_argument("--json", action="store_true", help="机器可读")
    s.add_argument("--all", action="store_true", help="全量列出")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("poll", help="只看新消息")
    s.set_defaults(fn=cmd_poll)

    s = sub.add_parser("sync", help="拉取 + 补推，并收拾掉赢不了/没意义的认领")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("send", help="说一句")
    add_body(s)
    s.add_argument("--re", help="挂在哪个任务上")
    s.add_argument("--kind", default="note", choices=KINDS)
    s.set_defaults(fn=lambda a: cmd_send(a))

    s = sub.add_parser("ask", help="问对方一句（等答复）")
    add_body(s)
    s.add_argument("--re", help="挂在哪个任务上")
    s.set_defaults(fn=lambda a: cmd_send(a, "ask"))

    s = sub.add_parser("handoff", help="把活交接给对方")
    add_body(s)
    s.add_argument("--re", help="挂在哪个任务上")
    s.set_defaults(fn=lambda a: cmd_send(a, "handoff"))

    s = sub.add_parser("answer", help="答复某条 ask")
    s.add_argument("target", help="被答复的消息（id / 前缀 / 短句柄）")
    add_body(s, to=False)
    s.set_defaults(fn=cmd_answer)

    s = sub.add_parser("task", help="任务")
    tsub = s.add_subparsers(dest="sub")
    st = tsub.add_parser("new", help="提一个新任务")
    st.add_argument("--title", required=True)
    add_body(st, to=False)
    st.set_defaults(fn=cmd_task_new)

    for name, fn, helptext in (("claim", cmd_claim, "认领任务"),
                               ("release", cmd_release, "放掉任务"),
                               ("cancel", cmd_cancel, "取消任务")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("task")
        s.set_defaults(fn=fn)

    s = sub.add_parser("done", help="收工")
    s.add_argument("task")
    s.add_argument("-m", "--msg", nargs="*")
    s.set_defaults(fn=cmd_done)

    s = sub.add_parser("log", help="看对话")
    s.add_argument("-n", type=int, default=20)
    s.add_argument("--kind")
    s.add_argument("--peer", help="只看跟某人的往来")
    s.add_argument("-v", "--verbose", action="store_true", help="连正文一起打")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("show", help="看某条消息/任务的全文")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("export", help="导出人看的完整对话 markdown")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)
    return p


def main(argv=None):
    global ME, MIRROR
    try:
        # 老 Windows 控制台是 GBK，"⚠" 这种符号会直接让 print 抛异常。
        # 分两种情形：① 输出被重定向 / 进了管道（Claude Code、> 文件）—— 一律按 UTF-8 写，
        # 这是绝大多数情况，管道另一头都是按 UTF-8 读的；
        # ② 直接怼着一个老控制台 —— 保留它自己的编码，只把编不出来的字符降级成 ?。
        enc = (sys.stdout.encoding or "").lower()
        if "utf" not in enc and not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        else:
            sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0
    ME = a.as_ or load_self().get("name")
    if not ME:
        print("✖ 不知道我是谁。用 `--as <名字>` 或环境变量 COLLAB_AGENT 指定（例如 local / remote）。")
        return 2
    MIRROR = collab_home() / "mirror"
    fn = getattr(a, "fn", None)
    if fn is None:
        print("✖ `%s` 还缺一个子命令，试试 -h" % a.cmd)
        return 2
    try:
        return fn(a) or 0
    except GitError as e:
        print("✖ git 出错：%s" % e)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
