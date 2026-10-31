# -*- coding: utf-8 -*-
"""本机密钥（device key）—— 注册时和设备指纹一起，证明"这台机器以前来过"。

为什么要有它（2026-11-01）：

  设备指纹（server/fingerprint.py）大致唯一，但**不保密**：本机任何进程都读得到，
  接口也接受任意 32 位 hex。光靠它，"同一台机器换个指纹再注册一次"就能再领一份
  赠送额度，服务器分辨不出这是不是同一个客户端。

  所以客户端自己生一串 256 位的随机数，**只在注册那一刻**（/api/auto-register）
  和 device_id、fingerprint 一起发上去；服务器只存它的 sha256。下次同一台机器再
  注册时把同一串带上，服务器就能认出"这台机器来过"，把原来的账号直接还回去，
  而不是再开一个新号。指纹可以伪造，随机数伪造不了。

三条规矩，改的时候别丢：

  1. **只发给注册**。不发给 bind-account、不发给 chat、不进 identity.json、
     不进界面、不进日志、不进任何给模型看的文本。它只在这里被读出来一次。
  2. **存在比身份文件更深的地方**：Windows 是 HKCU 注册表里 DPAPI 加密的值，
     其它平台是凭证目录下一个 0600 的 `.state`。删掉 `~/.claude-agent/identity.json`
     不会把它一起删掉 —— 这正是它能挡住"删配置刷号"的原因。
  3. **失败不阻断注册**：存不下就返回空串，注册照常走（服务器那边的开关默认关）。

它**不是**账号凭证：拿到它不能调用模型、不能取余额、不能换设备登录。
账号凭证只有账户码 + 账户密钥（见 server/identity.py 的文件头）。
"""

import re
import secrets

from server import vault

# 32 字节 = 64 位小写 hex。和服务端 `/^[0-9a-f]{64}$/` 对齐。
_SHAPE = re.compile(r"\A[0-9a-f]{64}\Z")


def _valid(value) -> str:
    text = str(value or "").strip().lower()
    return text if _SHAPE.match(text) else ""


def get() -> str:
    """当前本机密钥；没有（或存坏了）返回空串。**绝不打印返回值。**

    ★ 不做进程内缓存：注册一次只读一次，缓存省不了什么，却会在测试里、
      以及用户清了注册表之后继续拿旧值。
    """
    return _valid(vault.load_device_key())


def get_or_create() -> str:
    """拿本机密钥；第一次调用时生成并存下来。存不下返回空串。

    ★ 先落盘再用：先返回再存的话，注册成功后密钥却丢了，服务器那边绑的是一串
      再也拿不出来的值，下次注册就认不出这台机器了。
    """
    value = get()
    if value:
        return value
    value = secrets.token_hex(32)
    if not vault.save_device_key(value):
        return ""                    # 存不下就别发：绑一个会丢的值没有意义
    return value


def forget() -> bool:
    """清掉本机密钥（测试 / 客服排查"当作新机器"）。"""
    return vault.clear_device_key()
