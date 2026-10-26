# -*- coding: utf-8 -*-
"""终端版的"界面"。

`server/ui_bridge.get_ui()` 拿到的是这个对象 —— 身份层（`server/identity.py`）在
注册 / 算力校验时会回调几个钩子（`ui_progress_start` / `ui_pow_progress` …），
桌面版把这些钩子转成界面动作，服务器版只需要把字打到终端上。

★ 这里只实现**不阻塞**的钩子。桌面版的 `ui_ask` 会卡在 `input_event.wait()` 上，
  服务器版不用它：CLI 该问什么直接在 `server_main.py` 里 `input()`。
  留着 `ask()` 只是为了防止以后有人误把工具层搬过来时 AttributeError。
"""

import os
import sys
import time


def _is_tty(stream) -> bool:
    try:
        return bool(stream and stream.isatty())
    except Exception:
        return False


def _width(text: str) -> int:
    """按"终端列"算宽度：中文占两格。只用来擦掉上一行进度。"""
    n = 0
    for ch in text:
        n += 2 if ord(ch) > 0x2E80 else 1
    return n


class ConsoleUI:
    """最小可用的 ui_bridge 替身。"""

    def __init__(self, stream=None, verbose: bool = True):
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self._last_len = 0          # 上一次进度行占了几个字符，擦行要用
        self._last_pct = -1
        self._last_at = 0.0

    # ---------- 基础输出 ----------
    def _write(self, text: str = "", end: str = "\n"):
        try:
            self.stream.write(text + end)
            self.stream.flush()
        except Exception:
            pass

    def _clear_line(self):
        if not _is_tty(self.stream) or not self._last_len:
            return
        self.stream.write("\r" + " " * self._last_len + "\r")
        self._last_len = 0

    # ---------- identity.py 用到的钩子 ----------
    def ui_log(self, text: str):
        if not self.verbose:
            return
        self._clear_line()
        self._write(str(text))

    def ui_progress_start(self, text: str = ""):
        if not self.verbose:
            return
        self._clear_line()
        self._write(f"    {text}" if text else "    …")

    def ui_progress_stop(self):
        self._clear_line()

    def ui_pow_progress(self, text: str = "", pct=None):
        """算力校验的进度。真终端里就地刷新一行，管道里每 10% 打一行。"""
        if not self.verbose:
            return
        tty = _is_tty(self.stream)
        if pct is None:
            self.ui_progress_start(text)
            return
        try:
            pct = max(0, min(100, int(pct)))
        except Exception:
            pct = 0

        if tty:
            bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
            line = f"    {text} [{bar}] {pct:3d}%"
            pad = max(0, self._last_len - _width(line))
            self.stream.write("\r" + line + " " * pad)
            self.stream.flush()
            self._last_len = _width(line)
            return

        # 不是终端（日志文件 / CI）：别刷屏，每 10% 一行
        now = time.time()
        if pct >= 100:
            self._write(f"    {text} {pct}%")
            self._last_pct = pct
        elif pct >= self._last_pct + 10 and now - self._last_at >= 1.0:
            self._write(f"    {text} {pct}%")
            self._last_pct = pct
            self._last_at = now

    def ui_recovered(self):
        self._clear_line()
        self._write("    服务器认出了你原来的账号，余额已经还回来了。")

    def on_token_acquired(self):
        """桌面版这里会重置 LLM 客户端；服务器版不用模型，什么都不做。"""
        pass

    # ---------- 兜底：有人真调 ui_ask 时不至于炸 ----------
    def ask(self, question: str, options=None) -> str:
        if options:
            for i, opt in enumerate(options, 1):
                self._write(f"  {i}. {opt}")
        try:
            answer = input(f"{question} > ") if _is_tty(sys.stdin) else ""
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return f"用户回答：{answer}"


def make_console_ui(stream=None) -> ConsoleUI:
    """按环境变量决定要不要啰嗦：`AIKAUI_QUIET=1` 只打错误。"""
    quiet = os.environ.get("AIKAUI_QUIET", "").strip() not in ("", "0", "false")
    return ConsoleUI(stream=stream, verbose=not quiet)
