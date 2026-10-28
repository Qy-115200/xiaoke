# -*- coding: utf-8 -*-
"""终端对话的状态机：**不碰终端也能测的那一半**。

用户要的规矩全在这里：Agent 干活时不许打字、干完了才能发、有选项时 ↑↓ 选
回车确认、Esc 跳到"自己打字"。这些如果只在真 tty 里能手验，改一行就可能悄悄
坏掉，所以状态机单独拎出来测。
"""

import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.term_chat import ChatModel, Outcome, TerminalChat, make_ui  # noqa: E402


def typed(model, text):
    """把一串字符一个个喂进去（就像用户敲的）。"""
    for ch in text:
        model.handle_key("char", ch)


class BusyTests(unittest.TestCase):
    def test_busy_swallows_typing(self):
        m = ChatModel()
        m.set_busy(True)
        self.assertEqual(m.state, ChatModel.BUSY)
        typed(m, "别插嘴")
        self.assertEqual(m.buffer, "", "干活的时候不许打字")
        self.assertEqual(m.handle_key("enter"), ("none",))

    def test_busy_still_honours_ctrl_c(self):
        m = ChatModel()
        m.set_busy(True)
        self.assertEqual(m.handle_key("ctrl", "c", now=0) , ("arm_abort",))
        self.assertEqual(m.handle_key("ctrl", "c", now=1), ("abort",))

    def test_back_to_idle(self):
        m = ChatModel()
        m.set_busy(True)
        m.set_busy(False)
        self.assertEqual(m.state, ChatModel.IDLE)
        typed(m, "hi")
        self.assertEqual(m.handle_key("enter"), ("submit", "hi"))

    def test_asking_survives_a_busy_flag(self):
        # 模型提问时工作线程当然还是"忙"的，但界面得停在 ASKING，
        # 否则选项会在用户眼前消失。
        m = ChatModel()
        m.set_busy(True)
        m.begin_ask(["甲", "乙"])
        m.set_busy(True)
        self.assertEqual(m.state, ChatModel.ASKING)
        m.set_busy(False)
        self.assertEqual(m.state, ChatModel.ASKING)


class IdleTests(unittest.TestCase):
    def test_empty_enter_does_nothing(self):
        m = ChatModel()
        self.assertEqual(m.handle_key("enter"), ("none",))
        self.assertEqual(m.state, ChatModel.IDLE)

    def test_whitespace_enter_does_nothing(self):
        m = ChatModel()
        typed(m, "   ")
        self.assertEqual(m.handle_key("enter"), ("none",))

    def test_typing_then_enter_submits(self):
        m = ChatModel()
        typed(m, "你好")
        self.assertEqual(m.handle_key("enter"), ("submit", "你好"))
        self.assertEqual(m.buffer, "")
        self.assertEqual(m.state, ChatModel.IDLE)

    def test_submit_goes_back_to_busy_when_session_is_busy(self):
        m = ChatModel()
        m.set_busy(True)
        m.set_busy(False)                 # 主体已经跑完了，只剩用户说话
        typed(m, "x")
        m.session_busy = True             # 模拟"又派了一轮活"
        m.handle_key("enter")
        self.assertEqual(m.state, ChatModel.BUSY)

    def test_history_moves_with_up_down(self):
        m = ChatModel()
        typed(m, "第一句")
        m.handle_key("enter")
        typed(m, "第二句")
        m.handle_key("enter")
        m.handle_key("up")
        self.assertEqual(m.buffer, "第二句")
        m.handle_key("up")
        self.assertEqual(m.buffer, "第一句")
        m.handle_key("up")
        self.assertEqual(m.buffer, "第一句", "到头就停住，不绕圈")
        m.handle_key("down")
        self.assertEqual(m.buffer, "第二句")
        m.handle_key("down")
        self.assertEqual(m.buffer, "", "翻回最新的草稿")

    def test_ctrl_d_on_empty_buffer_means_eof(self):
        m = ChatModel()
        self.assertEqual(m.handle_key("ctrl", "d"), ("eof",))
        typed(m, "a")
        self.assertEqual(m.handle_key("ctrl", "d"), ("none",))

    def test_ctrl_u_clears(self):
        m = ChatModel()
        typed(m, "写错了")
        m.handle_key("ctrl", "u")
        self.assertEqual(m.buffer, "")


class AskingTests(unittest.TestCase):
    def test_enter_takes_the_highlighted_option(self):
        m = ChatModel()
        m.begin_ask(["保留原来的", "换成我们的"])
        self.assertEqual(m.state, ChatModel.ASKING)
        self.assertEqual(m.handle_key("enter"), ("answer", "保留原来的"))

    def test_answer_returns_to_idle_or_busy(self):
        m = ChatModel()
        m.begin_ask(["甲"])
        m.handle_key("enter")
        self.assertEqual(m.state, ChatModel.IDLE)

        m.set_busy(True)
        m.begin_ask(["甲"])
        m.handle_key("enter")
        self.assertEqual(m.state, ChatModel.BUSY)

    def test_arrows_wrap_at_both_ends(self):
        m = ChatModel()
        m.begin_ask(["甲", "乙"])          # 选项 2 个 + 自己打字 1 个 = 3 行
        self.assertEqual(m.selected, 0)
        m.handle_key("up")
        self.assertEqual(m.selected, 2, "第一项按上键绕到最后一行")
        m.handle_key("down")
        self.assertEqual(m.selected, 0, "最后一行按下键绕回第一项")
        m.handle_key("down")
        self.assertEqual(m.selected, 1)

    def test_esc_jumps_to_free_text_without_submitting(self):
        m = ChatModel()
        m.begin_ask(["甲", "乙"])
        self.assertEqual(m.handle_key("esc"), ("none",))
        self.assertEqual(m.selected, 2)
        self.assertEqual(m.state, ChatModel.ASKING, "Esc 不是回答，别把问题关掉")

    def test_enter_on_free_row_does_nothing_until_you_type(self):
        m = ChatModel()
        m.begin_ask(["甲"])
        m.handle_key("esc")
        self.assertEqual(m.handle_key("enter"), ("none",))
        typed(m, "我自己说")
        self.assertEqual(m.handle_key("enter"), ("answer", "我自己说"))

    def test_typing_a_number_picks_that_option(self):
        m = ChatModel()
        m.begin_ask(["保留原来的", "换成我们的"])
        typed(m, "2")
        self.assertEqual(m.handle_key("enter"), ("answer", "换成我们的"))

    def test_number_out_of_range_is_treated_as_free_text(self):
        m = ChatModel()
        m.begin_ask(["甲"])
        typed(m, "9")
        self.assertEqual(m.handle_key("enter"), ("answer", "9"))

    def test_free_text_can_be_typed_without_pressing_esc(self):
        m = ChatModel()
        m.begin_ask(["甲"])
        typed(m, "都行吧")
        self.assertEqual(m.handle_key("enter"), ("answer", "都行吧"))

    def test_model_side_numbering_is_stripped(self):
        m = ChatModel()
        m.begin_ask(["1. 保留原来的", "2、换成我们的"])
        self.assertEqual(m.options, ["保留原来的", "换成我们的"])

    def test_auto_mode_answers_immediately(self):
        m = ChatModel(auto=True)
        self.assertEqual(m.begin_ask(["甲", "乙"]), ("answer", "甲"))

    def test_auto_mode_without_options_answers_empty(self):
        m = ChatModel(auto=True)
        self.assertEqual(m.begin_ask([]), ("answer", ""))


class CtrlCTests(unittest.TestCase):
    def test_first_press_only_arms(self):
        m = ChatModel()
        self.assertEqual(m.handle_key("ctrl", "c", now=10.0), ("arm_abort",))
        self.assertNotEqual(m.handle_key("ctrl", "c", now=13.0), ("abort",),
                            "隔了 3 秒，算新的一次")

    def test_two_presses_abort(self):
        m = ChatModel()
        m.handle_key("ctrl", "c", now=10.0)
        self.assertEqual(m.handle_key("ctrl", "c", now=10.4), ("abort",))


class MiscTests(unittest.TestCase):
    def test_paste_flattens_newlines(self):
        m = ChatModel()
        m.handle_key("paste", "第一行\n第二行\r\n第三行")
        self.assertEqual(m.buffer, "第一行 第二行  第三行")

    def test_backspace_and_delete(self):
        m = ChatModel()
        typed(m, "abc")
        m.handle_key("left")
        m.handle_key("backspace")
        self.assertEqual(m.buffer, "ac")
        m.handle_key("delete")
        self.assertEqual(m.buffer, "a")

    def test_ctrl_w_kills_a_word(self):
        m = ChatModel()
        typed(m, "npm install claude")
        m.handle_key("ctrl", "w")
        self.assertEqual(m.buffer, "npm install ")

    def test_home_and_end(self):
        m = ChatModel()
        typed(m, "abc")
        m.handle_key("home")
        typed(m, "X")
        self.assertEqual(m.buffer, "Xabc")
        m.handle_key("end")
        typed(m, "Y")
        self.assertEqual(m.buffer, "XabcY")


class OutcomeTests(unittest.TestCase):
    def test_default_is_a_clean_exit(self):
        o = Outcome()
        self.assertFalse(o.aborted)
        self.assertIsNone(o.error)

    def test_carries_an_error(self):
        o = Outcome(error="模型连不上")
        self.assertEqual(o.error, "模型连不上")


class TerminalChatHooksTests(unittest.TestCase):
    """主线程之外的那一半：钩子只排队，不直接写屏幕。"""

    def test_hooks_only_enqueue(self):
        ui = TerminalChat(auto=False)
        ui.ui_log("一句日志")
        ui.ui_progress_start("装 Node")
        ui.ui_pow_progress("算力校验", 0.5)
        ui.ui_recovered()
        ui.ui_busy(True)
        kinds = []
        while not ui._events.empty():
            kinds.append(ui._events.get_nowait()[0])
        self.assertEqual(kinds, ["log", "progress", "pow", "recovered", "busy"])

    def test_pow_progress_keeps_the_fraction(self):
        ui = TerminalChat()
        ui.ui_pow_progress("算力校验", 0.5)
        self.assertEqual(ui._events.get_nowait(), ("pow", ("算力校验", 0.5)))

    def test_emit_takes_the_queue_tuple(self):
        """`run_one_task` 调的是 `emit(("log", 文字))` —— 一个元组，和 msg_queue.put 同签名。"""
        ui = TerminalChat()
        ui.emit(("log", "x"))
        self.assertEqual(ui._events.get_nowait(), ("log", "x"))

    def test_emit_ignores_malformed_events(self):
        ui = TerminalChat()
        ui.emit("随便一个字符串")
        self.assertTrue(ui._events.empty())

    def test_finish_enqueues_a_done_event(self):
        ui = TerminalChat()
        ui.finish(Outcome(aborted=True))
        kind, data = ui._events.get_nowait()
        self.assertEqual(kind, "done")
        self.assertTrue(data.aborted)


class RawModeTests(unittest.TestCase):
    """`start()` 切终端模式那一小段。用假 termios 跑，不需要真 tty。"""

    ISIG = 0o1
    ICANON = 0o2
    ECHO = 0o10
    TCSADRAIN = 2

    def _start(self):
        written = []
        attrs = [0, 0, 0, self.ISIG | self.ICANON | self.ECHO, 0, 0, []]  # [3] 是 lflag

        def setcbreak(_fd):
            # 和 CPython 的 tty.setcbreak 一样：只关 ICANON，不动 ISIG
            attrs[3] &= ~self.ICANON

        def tcsetattr(_fd, _when, value):
            written.append(value)
            attrs[:] = value

        fake = types.SimpleNamespace(
            ISIG=self.ISIG, TCSADRAIN=self.TCSADRAIN,
            tcgetattr=lambda _fd: list(attrs), tcsetattr=tcsetattr)
        ui = TerminalChat(stream=io.StringIO(), fd_in=-1, fd_out=-1)
        with mock.patch.dict(sys.modules, {"termios": fake,
                                           "tty": types.SimpleNamespace(
                                               setcbreak=setcbreak)}):
            ui.start()
        self.addCleanup(ui.close)
        return written

    def test_ctrl_c_is_a_byte_not_a_signal(self):
        """★ setcbreak 不动 ISIG。留着它，Ctrl-C 会变成真 SIGINT 直接打断，
        用户就永远看不到「再按一次 Ctrl-C 中断」那一步。"""
        written = self._start()
        self.assertTrue(written, "start() 没有把终端属性写回去")
        self.assertFalse(written[-1][3] & self.ISIG, "ISIG 还开着，Ctrl-C 会变成信号")

    def test_the_rest_of_the_mode_is_left_alone(self):
        """只拨 ISIG 这一位；把整组 lflag 写死会让用户原来的终端设置悄悄变掉。"""
        written = self._start()
        self.assertTrue(written[-1][3] & self.ECHO, "不该动 ECHO 以外的位")


class MakeUITests(unittest.TestCase):
    def test_no_tty_falls_back_to_console_ui(self):
        from server.console_ui import ConsoleUI
        ui = make_ui(auto=True, stream=None, quiet=True)
        self.assertIsInstance(ui, ConsoleUI)
        self.assertTrue(ui.auto)
        self.assertFalse(ui.verbose)


if __name__ == "__main__":
    unittest.main(verbosity=2)
