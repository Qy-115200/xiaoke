# -*- coding: utf-8 -*-
"""设备硬件指纹（Windows）。

读两样东西拼起来做 SHA-256，取前 32 位十六进制：

    Win32_BaseBoard.SerialNumber   主板序列号
    Win32_Processor.ProcessorId    CPU ID

★ 这个指纹**不是凭据**，别拿它当密码用。原因写在 server/vault.py 和 README 里：
  1. 它不保密 —— 本机任何程序都能读到同样的值，随便谁也能直接往接口里传任意字符串；
  2. ProcessorId 对同型号的 CPU 是常量（它是 CPUID 特性位 + 型号签名，不是序列号），
     唯一性完全靠主板序列号那十几个字符；
  3. 不少 OEM 主板和虚拟机的序列号是空 / "To be filled by O.E.M." / "Default string"，
     那些机器会算出同一个指纹。

  所以它只当"反滥用线索 + 本机标识"用。丢了凭证要找回账号，靠的是
  server/vault.py 里那张 DPAPI 加密的账户码，不是这个指纹。

★ 三条实现纪律：
  1. **绝不抛异常**。取不到就返回 ""（服务器按"不知道"处理）。
  2. **绝不能卡界面**。PowerShell 起来要半秒到两秒，只能在后台线程里调，
     而且一个进程只算一次（_memo）。
  3. **不能弹出黑窗口**。这个 exe 是无控制台的，直接 subprocess 会闪一下 cmd，
     所以必须 CREATE_NO_WINDOW + SW_HIDE。
"""

import hashlib
import json
import subprocess
import sys

# PowerShell 起来慢，给足时间；超时就当取不到（注册不能因为这个失败）
FINGERPRINT_TIMEOUT = 20

# 直接拼一条命令，一次进程问两样东西。$ErrorActionPreference 设成静默：
# 老机器上可能没有某个 CIM 类，让它返回空而不是抛错。
_PS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$b=(Get-CimInstance -ClassName Win32_BaseBoard | "
    "Select-Object -First 1).SerialNumber;"
    "$c=(Get-CimInstance -ClassName Win32_Processor | "
    "Select-Object -First 1).ProcessorId;"
    "@{board=$b;cpu=$c} | ConvertTo-Json -Compress"
)

# 这些值是"厂商没填"的占位符，不是真序列号。拿它们算出来的指纹在成百上千台
# 机器上是一样的 —— 那不是指纹，是个常量，宁可返回空。
_PLACEHOLDERS = {
    "", "none", "0", "n/a", "na", "null", "unknown", "invalid", "not specified",
    "not applicable", "not available", "oem", "default string", "to be filled by oem",
    "to be filled by o.e.m.", "system serial number", "system manufacturer",
    "chassis serial number", "base board serial number", "serial number",
    "123456789", "1234567890", "0123456789",
}

# 一个进程只算一次：注册成功后就不再需要，但重复调用别重复起进程。
_memo = None
_failed = False


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in _PLACEHOLDERS


def compute(board: str, cpu: str) -> str:
    """把两个原始值算成指纹。纯函数，测试用这个，不碰系统。

    主板序列号是空的 / 占位符 → 返回 ""。**不能**拿 "|" + CPU 去算：
    那会得到一个全网同型号机器共用的常量，比返回空危险得多。
    """
    board = (board or "").strip()
    cpu = (cpu or "").strip()
    if _is_placeholder(board):
        return ""
    raw = f"{board}|{cpu}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def _run_powershell() -> dict:
    """起一次 PowerShell 问硬件信息。失败返回 {}（绝不抛）。"""
    if sys.platform != "win32":
        return {}
    try:
        kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
        except Exception:
            pass
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT],
            capture_output=True, text=True, timeout=FINGERPRINT_TIMEOUT,
            encoding="utf-8", errors="replace", **kwargs,
        )
    except Exception:
        return {}
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return {}
    try:
        data = json.loads(proc.stdout.strip())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def raw_ids() -> dict:
    """调试 / 支持用：返回 {'board': ..., 'cpu': ...}，取不到的键是空串。

    ★ 别把它写进日志或弹给用户 —— 主板序列号能拿去查保修、也能被用来冒充。
      界面上要显示就显示指纹，别显示原始值。"""
    data = _run_powershell()
    return {
        "board": str(data.get("board") or "").strip(),
        "cpu": str(data.get("cpu") or "").strip(),
    }


def get_device_fingerprint(force: bool = False) -> str:
    """本机指纹（32 位十六进制）。非 Windows / 取不到 / 全是占位符 → ""。

    ★ 只在后台线程调用；同一次运行里只有第一次会真的起 PowerShell。"""
    global _memo, _failed
    if _memo is not None and not force:
        return _memo
    if _failed and not force:
        return ""
    try:
        ids = raw_ids()
        value = compute(ids.get("board", ""), ids.get("cpu", ""))
    except Exception:
        value = ""                  # 取指纹失败绝不能把注册搞挂
    _memo = value
    if not value:
        _failed = True              # 这个进程里别再试了，省得每次注册都起一次进程
    return value


def _reset_cache() -> None:
    """测试用：清掉记忆。"""
    global _memo, _failed
    _memo = None
    _failed = False
