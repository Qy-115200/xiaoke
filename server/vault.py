# -*- coding: utf-8 -*-
"""凭证的"第二份"：账号丢了还能找回的那条命。

identity.json 只有一份，用户重装客户端、清理工具、手滑删掉 ~/.claude-agent，
它连同账户码一起没了。再把注册流程走一遍 = 开一个新账号 = 原来的余额和用量
全部留在旧账号里，用户根本拿不回来。

所以注册成功 / 换设备登录成功时，把**账户码**（还有备查用的 account_key）
另外存一份到"不会跟程序目录一起被删"的地方：

  Windows   HKCU\\Software\\aikaui\\ClaudeCodeAssistant，值用 DPAPI 加密
            （DPAPI 是"同一个 Windows 用户"才能解，换用户/换机器都解不开）
  macOS     ~/Library/Application Support/aikaui/credential.json（0600）
  其它      $XDG_CONFIG_HOME/aikaui/credential.json（默认 ~/.config，0600）

★ 为什么不是"把 api_key 也存一份，直接拿来用"：api_key 是服务器按设备发的，
  重新绑定才是正道；而且把能直接调模型的密钥多存一份，出事时损失更大。
  这里只存"能证明我是这个账号主人"的账户码，恢复时走 /api/bind-account。

★ 这个文件/注册表值是**秘密**。任何时候都不要打印它、不要拼进日志、
  不要塞进给模型看的工具结果。读出来只做两件事：发给服务器、填进界面。

★ 本机密钥（server/device_key.py 那串）也存在这里，但走**另一个槽位**：
  Windows 同一个注册表键下的另一个值 `m`，其它平台是凭证文件旁边的 `.state`
  （同样 0600）。分开存是刻意的 —— `clear()` 是"换账号 / 忘掉这台机器"时调的，
  清账户码不该顺手把"这台机器以前来过没有"一起忘掉，那正是防刷号用的东西。
  要彻底重置用 `clear_device_key()`。

★ 所有的失败都只是"这次没存上"，绝不抛异常。存不下凭证不该影响注册本身。
"""

import json
import os
import sys
from pathlib import Path

# Windows 注册表位置。HKCU 是当前用户，不需要管理员权限，也不会影响别的用户。
_REG_PATH = r"Software\aikaui\ClaudeCodeAssistant"
_REG_VALUE = "cred"
# 本机密钥（device key）的槽位。值名故意起得没意义：它不该被谁顺手抄走。
_REG_DEVICE_VALUE = "m"
# 文件后端下本机密钥的文件名（和 credential.json 放在同一个目录）。
_DEVICE_FILE_NAME = ".state"

# 存进去的是一个 dict：{"account_code": …, "account_key": …, "user_id": …}
# 我们只关心 account_code，其它字段是给客服排查用的。
_KEYS = ("account_code", "account_key", "user_id")

# 带前缀存盘：u 表示原样（明文 JSON），e 表示 DPAPI 加密过。
# 留这个前缀是为了将来换加密方式时能看懂旧数据。
_MAGIC_PLAIN = b"aikv1:u:"
_MAGIC_DPAPI = b"aikv1:e:"

# 测试用钩子：设成 "file" 就强制走文件后端（绝不能在跑测试时写用户真实注册表）。
_force_file = False
_file_override = None


def _use_registry() -> bool:
    return sys.platform == "win32" and not _force_file


# ================= 加密 =================

def _encrypt(raw: bytes):
    """尽量用 DPAPI 加密。返回 (blob, protected)。没有 pywin32 就原样返回。"""
    try:
        import win32crypt                    # pywin32，Windows 才有
        blob = win32crypt.CryptProtectData(
            raw, "aikaui credential", None, None, None, 0)
        return _MAGIC_DPAPI + bytes(blob), True
    except Exception:
        return _MAGIC_PLAIN + raw, False


def _decrypt(blob: bytes):
    """把 _encrypt 存下来的东西还原成 bytes；认不出来返回 None。"""
    if blob.startswith(_MAGIC_PLAIN):
        return blob[len(_MAGIC_PLAIN):]
    if blob.startswith(_MAGIC_DPAPI):
        try:
            import win32crypt
            return win32crypt.CryptUnprotectData(
                blob[len(_MAGIC_DPAPI):], None, None, None, 0)[1]
        except Exception:
            return None                      # 换用户了 / 换了机器，解不开
    return None


# ================= Windows：注册表 =================

def _registry_read(name=_REG_VALUE):
    try:
        import winreg
    except Exception:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0,
                            winreg.KEY_READ) as key:
            blob, kind = winreg.QueryValueEx(key, name)
    except Exception:
        return None
    if kind != winreg.REG_BINARY or not isinstance(blob, bytes):
        return None
    return _decrypt(blob)


def _registry_write(raw: bytes, name=_REG_VALUE) -> bool:
    try:
        import winreg
        blob, _protected = _encrypt(raw)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_PATH, 0,
                                winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, blob)
        return True
    except Exception:
        return False


def _registry_clear(name=_REG_VALUE) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATH, 0,
                            winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, name)
        return True
    except Exception:
        return False


# ================= 其它平台：另一个目录下的文件 =================

def _file_path() -> Path:
    if _file_override is not None:
        return Path(_file_override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "aikaui" / "credential.json"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "aikaui" / "credential.json"


def _file_read(path: Path = None):
    path = path or _file_path()
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    return _decrypt(raw) if raw.startswith(b"aikv1:") else raw


def _file_write(raw: bytes, path: Path = None) -> bool:
    try:
        path = path or _file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        blob, _protected = _encrypt(raw)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(blob)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _file_clear(path: Path = None) -> bool:
    try:
        (path or _file_path()).unlink()
        return True
    except Exception:
        return False


def _device_path() -> Path:
    """本机密钥落在**凭证文件旁边**（不是凭证文件里面，见文件头）。"""
    return _file_path().with_name(_DEVICE_FILE_NAME)


# ================= 对外接口 =================

def backend() -> str:
    """现在是哪种存法（界面上显示在哪、排查问题时看）。"""
    if _use_registry():
        try:
            import win32crypt          # noqa: F401
            return "Windows 注册表（DPAPI 加密）"
        except Exception:
            return "Windows 注册表"
    if sys.platform == "darwin":
        return "~/Library/Application Support/aikaui"
    return "~/.config/aikaui"


def save(account_code, account_key="", user_id=None) -> bool:
    """存一份账户码。成功返回 True；失败只是没存上，不影响调用方。"""
    code = str(account_code or "").strip()
    if not code:
        return False                       # 没有账户码，存了也没用（别存半份垃圾）
    payload = {"v": 1, "account_code": code}
    if account_key:
        payload["account_key"] = str(account_key)
    if user_id is not None:
        payload["user_id"] = user_id
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _registry_write(raw) if _use_registry() else _file_write(raw)


def load():
    """读回账户码。没有 / 解不开 / 格式不对 → None。

    返回 {"account_code": …, "account_key": …, "user_id": …}（后两个可能缺）。"""
    raw = _registry_read() if _use_registry() else _file_read()
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {k: data[k] for k in _KEYS if data.get(k) not in (None, "")}
    return out if out.get("account_code") else None


def clear() -> bool:
    """换账号 / 用户明确要求"忘掉这台机器"时清掉。

    ★ **不动**本机密钥（clear_device_key 才动）：清账户码是换账号，不是换机器。
      那串密钥正是"这台机器以前来过没有"的凭据，留着才能挡住删配置刷号。"""
    return _registry_clear() if _use_registry() else _file_clear()


# ================= 本机密钥（device key）=================
# 只放存储；生成和"只发给注册"的规矩在 server/device_key.py。
# 这里和其它凭证一样：失败只是没存上，绝不抛异常。

def save_device_key(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    raw = text.encode("utf-8")
    if _use_registry():
        return _registry_write(raw, _REG_DEVICE_VALUE)
    return _file_write(raw, _device_path())


def load_device_key():
    """读回本机密钥；没有 / 解不开 → None。"""
    if _use_registry():
        raw = _registry_read(_REG_DEVICE_VALUE)
    else:
        raw = _file_read(_device_path())
    if not raw:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except Exception:
        return None
    return text or None


def clear_device_key() -> bool:
    """彻底重置这台机器的身份（客服排查 / 用户要求"当作新机器"）。"""
    if _use_registry():
        return _registry_clear(_REG_DEVICE_VALUE)
    return _file_clear(_device_path())
