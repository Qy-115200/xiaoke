# -*- coding: utf-8 -*-
"""GUI：把七个 mixin 拼成完整的界面类。

原来的 GUI 是一个 830 行的类，现在按职责切成几块，每个 mixin 只管自己那一摊：
  WindowMixin    主窗口外壳（左侧导航栏、对话页、底部询问条、日志、余额回流）
  OptionsMixin   ask_user 的选项面板
  RechargeMixin  充值弹窗
  UsageMixin     用量信息页（余额 / 各模型 token 柱状图 / 充值入口）
  LoginMixin     换设备登录弹窗（账户码 + 账户密钥）
  AccountMixin   账户信息页（账户码 / 账户密钥：打码、显示、复制、防泄露警告）
  GateMixin      检测到别家中转时拦住用户（换成我们的 / 退出）

几个 mixin 的方法名互不重复，也没有一个调 super()，所以 MRO 顺序不影响行为。
"""

from ui.account import AccountMixin
from ui.gate import GateMixin
from ui.login import LoginMixin
from ui.options import OptionsMixin
from ui.recharge import RechargeMixin
from ui.usage import UsageMixin
from ui.window import WindowMixin


class GUI(WindowMixin, OptionsMixin, RechargeMixin, UsageMixin, LoginMixin,
          AccountMixin, GateMixin):
    """界面类：管理窗口、组件、事件。

    线程约束：所有组件操作必须在主线程，其它线程通过 msg_queue 发消息，
    poll_queue 每 100ms 取一次并在这里更新界面。
    """
