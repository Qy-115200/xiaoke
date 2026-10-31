# -*- coding: utf-8 -*-
"""终端版的"界面"。

`server/ui_bridge.get_ui()` 拿到的是这个对象 —— 身份层（`server/identity.py`）在
注册 / 算力校验时会回调几个钩子（`ui_progress_start` / `ui_pow_progress` …），
桌面版把这些钩子转成界面动作，服务器版只需要把字打到终端上。

★ 这里只实现**不阻塞**的钩子。真终端里走的是 `server/term_chat.py`（`make_ui()`
  会挑），这个类只在没有 tty 的地方兜底：管道、CI、Dockerfile 里的 `--yes`。
  所以 `ui_ask` 在这里必须**不能等**：`--yes` 自动选第一项，没 tty 就直接回
  「没有终端可以回答」，绝不挂在输入上。
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

    def __init__(self, stream=None, verbose: bool = True, auto: bool = False):
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self.auto = bool(auto)      # --yes：提问时自动选第一项
        self._last_len = 0          # 上一次进度行占了几个字符，擦行要用
        self._last_pct = -1
        self._last_at = 0.0
        self.outcome = None         # 会话结束的结论（TerminalChat 里是 run() 的返回值）

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
        """算力校验的进度。真终端里就地刷新一行，管道里每 10% 打一行。

        ★ pct 是 **0~1**（`server/identity.py` 传的就是 `min(0.99, …)`），
          不是 0~100。以前这里按百分数算，屏幕上从头到尾都是 0%。"""
        if not self.verbose:
            return
        tty = _is_tty(self.stream)
        if pct is None:
            self.ui_progress_start(text)
            return
        try:
            pct = max(0, min(100, int(round(float(pct) * 100))))
        except (TypeError, ValueError):
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
        self._write("    服务器认出了这台机器原来的账号，已经直接登回去了。")

    def on_token_acquired(self):
        """桌面版这里会重置 LLM 客户端；服务器版不用模型，什么都不做。"""
        pass

    # ---------- 提问 ----------
    def ui_ask(self, question: str, options=None) -> str:
        """`tools_general.ask_user` 调的钩子（真终端里由 term_chat 实现）。

        这里是兜底路径，**三条出路都不许卡住**：
          · `--yes` 且给了选项 → 自动选第一项，并打一行 `[自动]` 留痕；
          · 没有 tty（管道 / CI / Dockerfile）→ 直说没法回答；
          · 有 tty → 读一行（这才是唯一会等人的情况）。
        """
        options = [str(o) for o in (options or [])]
        self._clear_line()
        self._write(str(question))
        for i, opt in enumerate(options, 1):
            self._write(f"  {i}. {opt}")

        if options and self.auto:
            self._write(f"[自动] 选了：{options[0]}")
            return f"用户回答：{options[0]}"
        if not _is_tty(sys.stdin):
            self._write("[跳过] 没有终端可以回答，按默认继续")
            return "用户回答：（没有终端可以回答，请按默认方式处理）"
        try:
            answer = input("你的选择 > " if options else "> ")
        except (EOFError, KeyboardInterrupt):
            return "用户回答：（没有回答，请按默认方式处理）"
        return f"用户回答：{answer.strip()}"

    ask = ui_ask          # 老名字，别再直接调

    # ---------- 会话驱动：和 term_chat.TerminalChat 同一套五个方法 ----------
    # `server_main._run_agent` 两边一样地调 start/emit/ui_busy/finish，
    # 差别只在"谁来跑主循环"：真终端是 TerminalChat.run()（泵按键），
    # 这里是 `_run_agent` 直接 join 工作线程。

    def emit(self, event):
        """`run_one_task(emit=…)` 的出口。这里只认 "log"，别的没人看。"""
        try:
            kind, data = event
        except (TypeError, ValueError):
            return
        if kind == "log":
            self.ui_log(str(data))

    def ui_busy(self, flag):
        """管道里没有"底下一行"，BUSY 只是个语义标记，不用画。"""

    def finish(self, outcome=None):
        self.outcome = outcome

    def start(self):
        """没有终端可切（不切 raw mode，也不装信号处理器）。"""

    def run(self):
        """没有 tty 就没有键可读：直接返回工作线程给的结论。"""
        return self.outcome

    def close(self):
        """没有需要恢复的终端状态。"""


def make_console_ui(stream=None, auto=False) -> ConsoleUI:
    """按环境变量决定要不要啰嗦：`AIKAUI_QUIET=1` 只打错误。"""
    quiet = os.environ.get("AIKAUI_QUIET", "").strip() not in ("", "0", "false")
    return ConsoleUI(stream=stream, verbose=not quiet, auto=auto)
