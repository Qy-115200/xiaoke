# -*- coding: utf-8 -*-
"""把终端里的原始字节翻译成按键事件。

**只做翻译，不碰文件描述符、不碰终端模式** —— 这样在 Windows 上也能测
（服务器版是 Linux 专用的，但开发机和 CI 跑在 Windows 上）。

约定：
    KeyDecoder().feed(b"...")  -> [(kind, value), ...]

kind 的取值：
    "char"      普通字符（已经按 UTF-8 解好，值长度可以是 1 个字符）
    "paste"     括号粘贴的一整段文字（值里保留换行）
    "enter" / "tab" / "backspace" / "delete" / "esc"
    "up" / "down" / "left" / "right" / "home" / "end"
    "ctrl"      值是单个小写字母，例如 ("ctrl", "c")

★ 半截序列（一个 UTF-8 字符的前两个字节、或者 `ESC [` 后面还没来）必须留在
  缓冲里等下一批，不能当成乱码丢 —— SSH 和 tmux 下字节被拆开是常态。
"""

# 控制字节 → Ctrl 字母
_CTRL = {
    0x01: "a", 0x02: "b", 0x03: "c", 0x04: "d", 0x05: "e", 0x06: "f",
    0x07: "g", 0x08: None, 0x09: None, 0x0A: None, 0x0B: "k", 0x0C: "l",
    0x0D: None, 0x0E: "n", 0x0F: "o", 0x10: "p", 0x11: "q", 0x12: "r",
    0x13: "s", 0x14: "t", 0x15: "u", 0x16: "v", 0x17: "w", 0x18: "x",
    0x19: "y", 0x1A: "z",
}

# CSI 的终结字节 → 事件（不带参数的）
_CSI_FINAL = {
    b"A": "up", b"B": "down", b"C": "right", b"D": "left",
    b"H": "home", b"F": "end",
}

# `ESC [ <数字> ~` 这一族
_CSI_TILDE = {
    "1": "home", "7": "home", "4": "end", "8": "end", "3": "delete",
}

_PASTE_START = "200"
_PASTE_END = "201"


def _utf8_len(b: int) -> int:
    """这个字节打头的 UTF-8 字符一共几个字节；不是合法打头字节就返回 1。"""
    if b < 0x80:
        return 1
    if 0xC2 <= b <= 0xDF:
        return 2
    if 0xE0 <= b <= 0xEF:
        return 3
    if 0xF0 <= b <= 0xF4:
        return 4
    return 1


class KeyDecoder:
    def __init__(self):
        self._buf = b""
        self._paste = None          # 正在粘贴：攒着的文字

    @property
    def pending(self) -> bytes:
        """缓冲里还没凑齐的字节（调用方靠它判断"等 150ms 再决定是不是 Esc"）。"""
        return self._buf

    # ---------- 对外 ----------
    def feed(self, data: bytes) -> list:
        self._buf += bytes(data or b"")
        return self._parse(final=False)

    def flush(self) -> list:
        """超时了：缓冲里剩下一个孤零零的 ESC 就当 Esc 键，别一直等下去。"""
        return self._parse(final=True)

    # ---------- 内部 ----------
    def _parse(self, final: bool):
        events = []
        while self._buf:
            if self._paste is not None:
                if not self._consume_paste(events, final):
                    break
                continue
            event, used = self._one(final)
            if used == 0:
                break                       # 半截序列，等下一批
            self._buf = self._buf[used:]
            if event is not None:
                events.append(event)
        if final and self._paste is not None:
            # 粘贴没等到收尾就超时：剩下没消化的字节也算用户的东西，别丢
            self._paste += self._buf.decode("utf-8", "replace")
            self._buf = b""
            events.append(("paste", self._paste))
            self._paste = None
        return events

    def _one(self, final: bool):
        b = self._buf
        c = b[0]

        if c == 0x1B:
            return self._escape(b, final)
        if c in (0x0D, 0x0A):
            return ("enter", ""), 1
        if c in (0x7F, 0x08):
            return ("backspace", ""), 1
        if c == 0x09:
            return ("tab", ""), 1
        if c < 0x20:
            name = _CTRL.get(c)
            return (("ctrl", name) if name else None), 1

        n = _utf8_len(c)
        if len(b) < n:
            return None, 0
        try:
            text = b[:n].decode("utf-8")
        except UnicodeDecodeError:
            text = b[:1].decode("utf-8", "replace")
            n = 1
        return ("char", text), n

    def _escape(self, b: bytes, final: bool):
        if len(b) == 1:
            # 只有 ESC：可能是方向键的开头，也可能用户真按了 Esc
            if final:
                return ("esc", ""), 1
            return None, 0

        nxt = b[1:2]
        if nxt == b"[":
            return self._csi(b, final)
        if nxt == b"O":
            if len(b) < 3:
                return (None, 0) if not final else (("esc", ""), 1)
            name = _CSI_FINAL.get(b[2:3])
            if name:
                return (name, ""), 3
            return (None, 1)
        # `ESC x` 这种两字节序列（Alt+键）：认不了，整个吃掉，别让 x 跑进输入框
        return None, 2

    def _csi(self, b: bytes, final: bool):
        # ESC [ ... 终结字节在 0x40~0x7E
        i = 2
        while i < len(b) and not (0x40 <= b[i] <= 0x7E):
            i += 1
        if i >= len(b):
            if final:
                return ("esc", ""), 1        # 半截 CSI，不猜
            return None, 0
        params = b[2:i].decode("latin1")
        end = b[i:i + 1]
        used = i + 1

        if end == b"~":
            if params == _PASTE_START:
                self._paste = ""
                return None, used
            if params == _PASTE_END:
                text = self._paste
                self._paste = None
                return ("paste", text), used
            name = _CSI_TILDE.get(params)
            if name:
                return (name, ""), used
            return (None, used)
        name = _CSI_FINAL.get(end)
        if name:
            return (name, ""), used
        return (None, used)                  # 未知序列：整段吃掉，保持同步

    def _consume_paste(self, events, final: bool) -> bool:
        """在粘贴模式里找收尾标记。返回 False 表示要等下一批字节。"""
        marker = b"\x1b[201~"
        pos = self._buf.find(marker)
        if pos < 0:
            # 留一点尾巴，免得收尾标记被拆在两批字节里
            keep = len(marker) - 1
            if len(self._buf) > keep:
                self._paste += self._buf[:-keep].decode("utf-8", "replace")
                self._buf = self._buf[-keep:]
            return False
        self._paste += self._buf[:pos].decode("utf-8", "replace")
        self._buf = self._buf[pos + len(marker):]
        events.append(("paste", self._paste))
        self._paste = None
        return True
