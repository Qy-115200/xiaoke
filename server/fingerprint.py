# -*- coding: utf-8 -*-
"""设备指纹（跨平台）。

做一件事：把"这台机器是谁"压成 32 位十六进制，注册时随请求发给服务器。
服务器的用法（2026-10-31 起）：同一个指纹再注册，直接返回它原来的账号 ——
于是"删掉 identity.json 再注册一次"不再能刷出一个新号。

取值按顺序，第一个拿到的赢：

    1. Windows  主板序列号 + CPU ProcessorId     ← 老公式，一个字节都别改
                                                  （线上已经入库的指纹还认它）
    2. Windows  MachineGuid（主板是占位串 / 虚拟机时用这个）
    3. macOS    IOPlatformUUID（读不到就退到硬件序列号）
    4. Linux    machine-id + DMI product_uuid + 主板序列号（容器里再加 hostname）
    5. 兜底     device_id（主机名 + MAC + 架构）
    6. 全失败   ""（服务器按"不知道"处理）

★ 指纹**仍然不是凭据**，别拿它当密码用：
  1. 它不保密 —— 本机任何程序都读得到同样的值，随便谁也能直接往接口里传任意字符串；
  2. 唯一性只有前几个来源才靠谱。ProcessorId 对同型号 CPU 是常量（它是 CPUID 特性位 +
     型号签名，不是序列号）；不少 OEM 主板和虚拟机的主板序列号是空 /
     "To be filled by O.E.M." / "Default string"，那些机器算出来是同一个值；
  3. 所以服务器就算"按指纹找回账号"，也只发 api_key + account_code，
     **绝不发 account_key** —— 那才是这个产品里唯一的"主人凭证"。撞了指纹的人
     最多花掉余额，锁不死账号主人。

★ 三条实现纪律：
  1. **绝不抛异常**。取不到就退到下一个来源，最后一个都失败才返回 ""。
  2. **绝不能卡界面**。Windows 上要起 PowerShell（半秒到两秒），所以一个进程只算一次
     （_memo）；调用方仍应放在后台线程里（注册本来就在后台线程）。
  3. 平台相关的读取全在 platform_support/ 里，本模块只决定"选哪个、怎么哈希"，
     所以它是纯逻辑 —— 测试不用碰系统。
"""

import hashlib

from platform_support import fingerprint_parts

# 这些值是"厂商没填"的占位符，不是真序列号。拿它们算出来的指纹在成百上千台
# 机器上是一样的 —— 那不是指纹，是个常量，宁可当它不存在。
_PLACEHOLDERS = {
    "", "none", "0", "n/a", "na", "null", "unknown", "invalid", "not specified",
    "not applicable", "not available", "oem", "default string", "to be filled by oem",
    "to be filled by o.e.m.", "system serial number", "system manufacturer",
    "chassis serial number", "base board serial number", "serial number",
    "123456789", "1234567890", "0123456789",
}

# 一个进程只算一次：注册成功后就不再需要，但重复调用别重复起进程。
_memo = None        # {"fingerprint": ..., "source": ...}
_failed = False


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


def _usable(value) -> str:
    """能用的原始值 → 原样（去空白）；占位符 / 空 → ""。"""
    text = str(value or "").strip()
    return "" if _is_placeholder(text) else text


def compute(board: str, cpu: str) -> str:
    """把两个原始值算成指纹。纯函数，测试用这个，不碰系统。

    主板序列号是空的 / 占位符 → 返回 ""。**不能**拿 "|" + CPU 去算：
    那会得到一个全网同型号机器共用的常量，比返回空危险得多。

    ★ 这是**老公式**，线上已经入库的指纹全是它算的 —— 一个字节都别改，
      改了等于把那些账号的"按指纹找回"全部作废。
    """
    board = (board or "").strip()
    cpu = (cpu or "").strip()
    if _is_placeholder(board):
        return ""
    raw = f"{board}|{cpu}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def _hash(source: str, value: str) -> str:
    """新来源的哈希：带来源前缀，避免两个来源碰巧算出同一个值。"""
    raw = f"{source}|{value}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def derive(parts: dict) -> tuple[str, str]:
    """(指纹, 来源)。纯函数：parts 是原始标识，不碰系统。

    来源的取值：board+cpu / machine-guid / platform-uuid / platform-serial /
    machine-id+dmi / device-id，全失败是 ""。服务器会把来源记进日志 ——
    两个不同的人"撞"到同一个账号时，得能看出是哪个来源造成的。
    """
    # 1. Windows 主板 + CPU（老公式）
    value = compute(str(parts.get("board") or ""), str(parts.get("cpu") or ""))
    if value:
        return value, "board+cpu"

    # 2. Windows MachineGuid：主板序列号是占位串时的真正唯一值
    guid = _usable(parts.get("machine_guid"))
    if guid:
        return _hash("machine-guid", guid), "machine-guid"

    # 3. macOS：IOPlatformUUID 每台 Mac 唯一，重装系统也不变
    uuid = _usable(parts.get("platform_uuid"))
    if uuid:
        return _hash("platform-uuid", uuid), "platform-uuid"
    serial = _usable(parts.get("serial"))
    if serial:
        return _hash("platform-serial", serial), "platform-serial"

    # 4. Linux：machine-id + DMI。三者拼起来，缺哪个就用剩下的。
    linux = [x for x in (_usable(parts.get("machine_id")),
                         _usable(parts.get("product_uuid")),
                         _usable(parts.get("board_serial"))) if x]
    if linux:
        container = _usable(parts.get("container"))
        if container:
            # ★ 容器里必须带上：同宿主机同镜像的两个容器，上面三项完全一样
            linux.append("container:" + container)
        return _hash("machine-id+dmi", "|".join(linux)), "machine-id+dmi"

    # 5. 兜底：device_id（主机名 + MAC + 架构）。弱，但绝不会空。
    device = _usable(parts.get("device_id"))
    if device:
        return _hash("device-id", device), "device-id"

    return "", ""


def _device_id_part() -> str:
    """兜底来源的值。延迟 import：server.identity 也 import 本模块，模块级会成环。"""
    try:
        from server.identity import _device_id
        return _device_id()
    except Exception:
        return ""


def raw_ids() -> dict:
    """调试 / 支持用：这个平台读到的原始标识（测试就 patch 这里）。

    ★ 别把它写进日志或弹给用户 —— 主板序列号能拿去查保修、也能被用来冒充。
      界面上要显示就显示指纹，别显示原始值。"""
    try:
        return dict(fingerprint_parts() or {})
    except Exception:
        return {}                   # 平台层自己已经兜住了，这里是第二道


def _resolve(force: bool = False) -> dict:
    """算一次、记住：{"fingerprint": 32 位 hex 或 "", "source": 来源}。"""
    global _memo, _failed
    if _memo is not None and not force:
        return _memo
    if _failed and not force:
        return {"fingerprint": "", "source": ""}
    try:
        parts = raw_ids()
        parts["device_id"] = _device_id_part()
        fp, source = derive(parts)
    except Exception:
        fp, source = "", ""          # 取指纹失败绝不能把注册搞挂
    _memo = {"fingerprint": fp, "source": source}
    if not fp:
        _failed = True               # 这个进程里别再试了，省得每次注册都起一次进程
    return _memo


def get_device_fingerprint(force: bool = False) -> str:
    """本机指纹（32 位十六进制）。极端情况下才为 ""（连 device_id 都拿不到）。

    ★ 只在后台线程调用；同一次运行里只有第一次会真的去读系统。"""
    return _resolve(force)["fingerprint"]


def get_device_fingerprint_info(force: bool = False) -> dict:
    """指纹 + 它是从哪个来源算出来的（排查"指纹为什么撞了"要看这个）。"""
    return dict(_resolve(force))


def _reset_cache() -> None:
    """测试用：清掉记忆。"""
    global _memo, _failed
    _memo = None
    _failed = False
