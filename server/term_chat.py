# -*- coding: utf-8 -*-
"""服务器版的"对话界面"：一个终端里的聊天框。

用户要的交互在这儿落地：
  · Agent 在干活（BUSY）时**不接受打字**，只有 Ctrl-C 有效；
  · 干完了（IDLE）才能打字，回车发出去；
  · Agent 提问时（ASKING）选项带高亮，上下键移动、回车确认，
    Esc 跳到"自己打字"。

分工很硬：**主线程独占终端**（读键、写屏、改终端模式），工作线程只往
`self._events` 里塞事件，一个字节都不直接写屏幕。`ui_ask` 在工作线程里阻塞，
但它阻塞的是工作线程，主线程照样在泵按键。

`termios` / `tty` / `select` 都在 `start()` 里才 import：开发机和 CI 是 Windows，
`import server.term_chat` 必须能过（真正跑起来时才需要 POSIX）。
"""

import os
import queue
import shutil
import sys
import threading
import time

from agent_bus import user_input_queue
from server import term_render as R
from server.console_ui import ConsoleUI
from server.term_keys import KeyDecoder

BUSY_LABEL = "正在处理"
ABORT_ARMED = "再按一次 Ctrl-C 中断（已经开始的下载/安装在后台会继续跑完）"
ABORTED = "已中断。"


class Outcome:
    """一次会话怎么结束的。

    aborted  用户按了两次 Ctrl-C / Ctrl-D
    error    收场时要说给用户听的一句话（成功是 None）
    exit_code 带出去给 `main()` 用的退出码
    """

    __slots__ = ("aborted", "error", "exit_code")

    def __init__(self, aborted=False, error=None, exit_code=None):
        self.aborted = bool(aborted)
        self.error = error
        self.exit_code = exit_code

    def __repr__(self):
        return (f"Outcome(aborted={self.aborted}, error={self.error!r}, "
                f"exit_code={self.exit_code!r})")


# ================= 纯状态机（离线可测）=================

class ChatModel:
    IDLE, BUSY, ASKING = "IDLE", "BUSY", "ASKING"

    def __init__(self, auto=False, history_limit=50):
        self.state = self.IDLE
        self.session_busy = False      # 工作线程还在干活（和"正在问问题"是两件事）
        self.buffer = ""
        self.cursor = 0
        self.options = []
        self.selected = 0
        self.auto = bool(auto)
        self.history = []
        self.history_limit = history_limit
        self.history_pos = None
        self._draft = ""
        self._ctrl_c_at = None          # ★ None 表示"还没按过"，别拿 0.0 当哨兵

    # ---------- 状态切换 ----------
    def set_busy(self, flag):
        self.session_busy = bool(flag)
        if self.state != self.ASKING:
            self.state = self.BUSY if self.session_busy else self.IDLE

    def begin_ask(self, options):
        """开始提问。返回一个动作（`--yes` 时会立刻自答），否则 None。"""
        self.options = R.norm_options(options)
        self.selected = 0
        self.buffer = ""
        self.cursor = 0
        self.history_pos = None
        self.state = self.ASKING
        if self.auto:
            text = self.options[0] if self.options else ""
            return self._answer(text)
        return None

    def _done_with_input(self):
        self.buffer = ""
        self.cursor = 0
        if self.state == self.ASKING:
            self.options = []
        self.history_pos = None
        self.state = self.BUSY if self.session_busy else self.IDLE

    def _submit(self, text):
        self.history.append(text)
        if len(self.history) > self.history_limit:
            self.history = self.history[-self.history_limit:]
        self._done_with_input()
        return ("submit", text)

    def _answer(self, text):
        self._done_with_input()
        return ("answer", text)

    # ---------- 编辑 ----------
    def _insert(self, text):
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)

    def _backspace(self):
        if self.cursor > 0:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1

    def _delete(self):
        if self.cursor < len(self.buffer):
            self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]

    def _history_move(self, delta):
        if not self.history:
            return
        total = len(self.history)
        if self.history_pos is None:
            self._draft = self.buffer
            pos = total - 1 if delta < 0 else total
        else:
            pos = self.history_pos + delta
        if pos < 0:
            pos = 0
        if pos >= total:
            self.history_pos = None
            self.buffer = self._draft
            self.cursor = len(self.buffer)
            return
        self.history_pos = pos
        self.buffer = self.history[pos]
        self.cursor = len(self.buffer)

    def _move_selection(self, delta):
        total = len(self.options) + 1          # 最后一行是"自己打字"
        self.selected = (self.selected + delta) % total

    # ---------- 按键 ----------
    def handle_key(self, kind, value="", now=None):
        now = time.monotonic() if now is None else float(now)

        if kind == "ctrl" and value == "c":
            if self._ctrl_c_at is not None and now - self._ctrl_c_at <= 2.0:
                return ("abort",)
            self._ctrl_c_at = now
            return ("arm_abort",)

        # ★ BUSY 时"不允许对话"：除了 Ctrl-C，什么都丢掉
        if self.state == self.BUSY:
            return ("none",)

        if kind == "ctrl":
            if value == "d" and not self.buffer:
                return ("eof",)
            if value == "a":
                self.cursor = 0
            elif value == "e":
                self.cursor = len(self.buffer)
            elif value == "u":
                self.buffer = ""
                self.cursor = 0
            elif value == "w":
                self._kill_word()
            return ("redraw",) if value == "l" else ("none",)

        if kind == "esc":
            if self.state == self.ASKING:
                self.selected = len(self.options)   # 跳到"自己打字"
            else:
                self.buffer = ""
                self.cursor = 0
            return ("none",)

        if kind in ("up", "down"):
            if self.state == self.ASKING:
                self._move_selection(-1 if kind == "up" else 1)
            else:
                self._history_move(-1 if kind == "up" else 1)
            return ("none",)

        if kind == "enter":
            text = self.buffer.strip()
            if self.state == self.ASKING:
                if text:
                    return self._answer(R.resolve_option(text, self.options))
                if self.selected < len(self.options):
                    return self._answer(self.options[self.selected])
                return ("none",)               # 停在"自己打字"上但没打字
            if not text:
                return ("none",)
            return self._submit(text)

        if kind == "paste":
            self._insert(str(value).replace("\r", " ").replace("\n", " "))
            return ("none",)
        if kind == "char":
            self._insert(str(value))
            return ("none",)
        if kind == "backspace":
            self._backspace()
            return ("none",)
        if kind == "delete":
            self._delete()
            return ("none",)
        if kind == "left":
            self.cursor = max(0, self.cursor - 1)
            return ("none",)
        if kind == "right":
            self.cursor = min(len(self.buffer), self.cursor + 1)
            return ("none",)
        if kind == "home":
            self.cursor = 0
            return ("none",)
        if kind == "end":
            self.cursor = len(self.buffer)
            return ("none",)
        return ("none",)

    def _kill_word(self):
        s, i = self.buffer, self.cursor
        while i > 0 and s[i - 1].isspace():
            i -= 1
        while i > 0 and not s[i - 1].isspace():
            i -= 1
        self.buffer = s[:i] + s[self.cursor:]
        self.cursor = i


# ================= 真正碰终端的那一层 =================

def _is_tty(stream) -> bool:
    try:
        return bool(stream and stream.isatty())
    except Exception:
        return False


def is_interactive() -> bool:
    return (os.name == "posix" and _is_tty(sys.stdin) and _is_tty(sys.stdout))


class TerminalChat:
    """实现 ui_bridge 那几个钩子（ui_log / ui_ask / ui_progress_* / …）。"""

    def __init__(self, auto=False, stream=None, fd_in=0, fd_out=1, color=None):
        self.stream = stream or sys.stdout
        self.model = ChatModel(auto=auto)
        self.color = _color_ok() if color is None else bool(color)
        self._fd_in = fd_in
        self._fd_out = fd_out
        self._events = queue.Queue()
        self._decoder = KeyDecoder()
        self._pending = None
        self._done = None
        self._live_rows = 0
        self._reserved = 0
        self._saved = None
        self._started = False
        self._closed = False
        self._busy_text = BUSY_LABEL
        self._busy_since = 0.0
        self._shown_secs = -1
        self._idle_since = 0.0
        self._resize = False
        self._stop = False
        self._old_handlers = {}

    # ---------- ui_bridge 的钩子（可能在工作线程里被调用）----------
    def push(self, kind, data=None):
        self._events.put((kind, data))

    def emit(self, event):
        """`run_one_task(emit=…)` 的出口：收**一个** (kind, data) 元组，
        和 `msg_queue.put` 同签名。别写成 `emit = push` —— 那是两个参数。"""
        try:
            kind, data = event
        except (TypeError, ValueError):
            return
        self.push(kind, data)

    def ui_log(self, text):
        self.push("log", str(text))

    def ui_progress_start(self, text=""):
        self.push("progress", ("start", str(text or "")))

    def ui_progress_stop(self):
        self.push("progress", ("stop", None))

    def ui_pow_progress(self, text="", pct=None):
        # ★ identity.py 传的 pct 是 0~1，不是 0~100
        self.push("pow", (str(text or ""), pct))

    def ui_recovered(self):
        self.push("recovered", None)

    def ui_busy(self, flag):
        """工作线程开始/结束一轮活：切 BUSY / IDLE。"""
        self.push("busy", bool(flag))

    def on_token_acquired(self):
        pass

    def ui_ask(self, question, options=None):
        """阻塞到用户回答为止。★ 这是在工作线程里跑的，主线程还在泵按键。"""
        options = list(options or [])
        self.push("log", f"\n[Agent] {question}")
        event = threading.Event()
        holder = {"event": event, "value": None}
        self._pending = holder                   # ★ 先摆好状态再让主线程去画
        self.push("ask", {"options": options})
        event.wait()
        return f"用户回答：{holder['value']}"

    def finish(self, outcome=None):
        self.push("done", outcome or Outcome())

    # ---------- 终端模式 ----------
    def start(self):
        if self._started:
            return
        import termios
        import tty
        self._termios = termios
        try:
            self.stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        try:
            self._saved = termios.tcgetattr(self._fd_in)
            tty.setcbreak(self._fd_in)            # 关掉行缓冲和回显
            # ★ setcbreak **不动 ISIG**：开着的话 Ctrl-C 会变成真 SIGINT 直接打断
            #   run()，用户永远看不到「再按一次中断」。关掉它，让 \x03 变成一个普通
            #   按键，交给 ChatModel 去做两次确认；SIGTERM / SIGHUP 仍然照常收场。
            now = termios.tcgetattr(self._fd_in)
            now[3] &= ~termios.ISIG               # [3] 是 lflag
            termios.tcsetattr(self._fd_in, termios.TCSADRAIN, now)
        except Exception:
            self._saved = None
        self._started = True
        self._install_signals()
        self._write("\x1b[?2004h")                # 括号粘贴：多行粘贴不会当成多次回车
        self._commit(_banner())

    def _install_signals(self):
        try:
            import signal
            for sig, flag in ((signal.SIGWINCH, "_resize"),
                              (signal.SIGTERM, "_stop"),
                              (signal.SIGHUP, "_stop")):
                try:
                    self._old_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, self._on_signal)
                except Exception:
                    pass
        except Exception:
            pass

    def _on_signal(self, signum, _frame):
        import signal
        if signum == getattr(signal, "SIGWINCH", None):
            self._resize = True
        else:
            self._stop = True

    def _restore(self):
        """幂等。每一条退出路径都要走一遍 —— 尤其是异常和中断。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._started:
                self._write(R.erase_live(self._live_rows))
                self._write("\x1b[r\x1b[?2004l\x1b[?25h\x1b[0m")
                self._live_rows = 0
        except Exception:
            pass
        try:
            if self._saved is not None:
                self._termios.tcsetattr(self._fd_in,
                                        self._termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        try:
            import signal
            for sig, handler in self._old_handlers.items():
                try:
                    signal.signal(sig, handler)
                except Exception:
                    pass
        except Exception:
            pass

    close = _restore

    # ---------- 输出 ----------
    def _write(self, text):
        if not text:
            return
        try:
            os.write(self._fd_out, text.encode("utf-8", "replace"))
        except Exception:
            try:
                self.stream.write(text)
                self.stream.flush()
            except Exception:
                pass

    def _size(self):
        try:
            size = os.get_terminal_size(self._fd_out)
            return max(20, size.columns), max(6, size.lines)
        except Exception:
            size = shutil.get_terminal_size((80, 24))
            return max(20, size.columns), max(6, size.lines)

    def _commit(self, text):
        """写一行"历史"。写完活区就不在原处了，所以要重画。"""
        out = R.erase_live(self._live_rows)
        self._live_rows = 0
        self._reserved = 0
        body = str(text).replace("\r\n", "\n").replace("\n", "\r\n").rstrip("\r\n")
        out += (body + "\r\n") if body else "\r\n"
        self._write(out)

    def _live_lines(self, cols, rows):
        model = self.model
        if model.state == ChatModel.BUSY:
            elapsed = time.monotonic() - self._busy_since if self._busy_since else 0
            return [R.busy_line(self._busy_text, elapsed, cols)]
        if model.state == ChatModel.ASKING:
            return R.ask_block(model.options, model.selected, model.buffer,
                               model.cursor, cols, rows, self.color)
        if model.state == ChatModel.IDLE and not self._closed:
            return [R.idle_line(model.buffer, model.cursor, cols, self.color)]
        return []

    def _redraw(self):
        if self._closed:
            return
        cols, rows = self._size()
        lines = self._live_lines(cols, rows)
        if not self.color:
            lines = [line.replace(R.REVERSE, "").replace(R.RESET, "")
                     .replace(R.DIM, "") for line in lines]
        out = R.erase_live(self._live_rows)
        if lines:
            n = len(lines)
            if n > self._reserved:
                out += R.reserve(n - self._reserved)
                self._reserved = n
            out += R.draw_lines(lines)
            self._live_rows = n
        else:
            self._live_rows = 0
        self._write(out)

    # ---------- 输入 ----------
    def _read(self, timeout):
        """返回 bytes = 按键；b"" = 超时没按键；None = 终端没了（必须收场）。"""
        import select
        try:
            ready, _, _ = select.select([self._fd_in], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return b""
        try:
            data = os.read(self._fd_in, 1024)
        except OSError:
            return None
        return data or None                      # b"" 是 EOF，不是"没按键"

    def _drain_events(self):
        while True:
            try:
                kind, data = self._events.get_nowait()
            except queue.Empty:
                return
            self._on_event(kind, data)
            if self._done is not None:
                return

    def _on_event(self, kind, data):
        model = self.model
        if kind == "log":
            self._commit(data)
        elif kind == "busy":
            model.set_busy(bool(data))
            if data:
                self._busy_since = time.monotonic()
                self._busy_text = BUSY_LABEL
                self._shown_secs = -1
            else:
                self._busy_since = 0.0
        elif kind == "ask":
            action = model.begin_ask((data or {}).get("options") or [])
            if action:
                self._dispatch(action)
        elif kind == "progress":
            what, text = data
            if what == "start":
                self._busy_text = text or BUSY_LABEL
            else:
                self._busy_text = BUSY_LABEL
        elif kind == "pow":
            text, pct = data
            label = text or BUSY_LABEL
            try:
                if pct is not None:
                    label = f"{label} {int(float(pct) * 100)}%"
            except Exception:
                pass
            self._busy_text = label
        elif kind == "recovered":
            self._commit("    服务器认出了你原来的账号，余额已经还回来了。")
        elif kind == "done":
            self._done = data if isinstance(data, Outcome) else Outcome()
            return
        # install_done / balance 之类在服务器版没有对应界面，忽略
        self._redraw()

    def _deliver(self, text):
        holder = self._pending
        if not holder:
            return
        self._pending = None
        holder["value"] = text
        holder["event"].set()

    def _dispatch(self, action):
        kind = action[0]
        if kind == "submit":
            user_input_queue.put(action[1])
        elif kind == "answer":
            self._deliver(action[1])
        elif kind == "arm_abort":
            self._commit(R.dim_line(ABORT_ARMED, self._size()[0]))
        elif kind == "abort":
            self._deliver("（用户中断）")
            self._wake_worker()
            self._done = Outcome(aborted=True)
        elif kind == "eof":
            self._deliver("（用户退出）")
            self._wake_worker()
            self._done = Outcome(aborted=True)
        self._redraw()

    def _wake_worker(self):
        """万一工作线程正停在"等用户打字"上，把它叫醒，别留一个挂死的线程。"""
        try:
            user_input_queue.put(None)
        except Exception:
            pass

    def _handle_keys(self, data):
        for kind, value in self._decoder.feed(data):
            self._dispatch(self.model.handle_key(kind, value))
            if self._done is not None:
                return

    def _tick(self):
        if self.model.state != ChatModel.BUSY or not self._busy_since:
            return
        secs = int(time.monotonic() - self._busy_since)
        if secs != self._shown_secs:
            self._shown_secs = secs
            self._redraw()

    def _flush_escape(self):
        if not self._decoder.pending:
            return
        for kind, value in self._decoder.flush():
            self._dispatch(self.model.handle_key(kind, value))

    # ---------- 主循环 ----------
    def run(self):
        outcome = Outcome()
        self._redraw()
        try:
            while self._done is None:
                if self._stop:
                    outcome = Outcome(aborted=True)
                    break
                if self._resize:
                    self._resize = False
                    self._on_resize()
                self._drain_events()
                if self._done is not None:
                    break

                data = self._read(0.05)
                if data is None:                 # 终端没了
                    outcome = Outcome(aborted=True)
                    break
                if data == b"":
                    now = time.monotonic()
                    if self._idle_since == 0.0:
                        self._idle_since = now
                    elif now - self._idle_since >= 0.15:
                        self._flush_escape()
                        self._idle_since = 0.0
                    self._tick()
                    continue
                self._idle_since = 0.0
                self._handle_keys(data)
        finally:
            self._restore()
        if self._done is not None:
            return self._done
        return outcome

    def _on_resize(self):
        # 尺寸变了，之前的"上移 N 行"就不再可靠：把活区留在原地，重新起一行
        self._write("\r\n")
        self._live_rows = 0
        self._reserved = 0
        self._redraw()


def _color_ok() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if (os.environ.get("TERM") or "").lower() in ("dumb", ""):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _banner() -> str:
    return ("小克 · 服务器版\n"
            "需要你拿主意的时候，用上下键选、回车确认；没在干活时可以直接打字。")


def make_ui(auto=False, stream=None, quiet=False):
    """真终端用对话界面，否则退回逐行打印的 ConsoleUI（`--yes` 时会自动选第一项）。"""
    if is_interactive():
        return TerminalChat(auto=auto, stream=stream)
    return ConsoleUI(stream=stream, verbose=not quiet, auto=auto)
