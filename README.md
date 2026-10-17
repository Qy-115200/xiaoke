# Claude Code 安装助手

一个 Windows / macOS / Linux 桌面小程序：把「装 Claude Code」这件折腾半天的事，变成跟 AI 说一句话。

窗口左边一条导航栏，右边两个页面：**💬 对话** 和 **📊 用量信息**。对话页背后是一个能调工具的
Agent —— 它会自己检测环境、装 Node、装 Claude Code、写好配置、问清楚工作目录、在桌面放个图标，
然后把 Claude Code 打开。它也是个能用的 AI 管家：能陪你聊天、答疑、查余额、扫码充值。

## 功能

**装 Claude Code（一键完成）**

底部一个「一键安装 Claude Code」按钮，或者直接说"帮我装 Claude Code"。Agent 按固定顺序
调五个工具：领 token → 绕过地区限制 → 写配置 → 建桌面快捷方式 → 启动。
中途每一步都会用大白话跟你解释一句，要装东西之前会先问过你。

**先定工作目录，再启动**

Claude Code 只看得到你给它的**一个**文件夹。所以启动之前 Agent 必须先问清楚"以后在哪个
文件夹里干活"（`ask_user` 给三个选项：桌面新建一个 / 用户主目录 / 自己指定路径），
再把这个目录传给 `launch_claude(directory=…)` 当 `cwd`。目录不存在会建出来，越界（跑到
用户主目录外面）会被退回主目录并在日志里说明原因 —— 跟 `read_file` / `write_file` 是同一条规则。

启动完还会把这件事再讲一遍：一个项目一个文件夹，换项目就在那个文件夹里重新启动。

**推荐 VSCode + Claude Code 的工作方式**

装完的收尾里会推荐一条编号的用法：装 VSCode（免费）→ 用 VSCode 打开项目文件夹 →
`Ctrl+\`` 打开终端 → 输入 `claude`。好处是 Claude 改哪个文件、改了什么，在编辑器里直接看到差别，
还能一键撤销。**只是推荐**：提示词里写死了不许自作主张去装 VSCode，用户说要装才走"问过再装"。

**对话式 AI 管家**

不装东西的时候它就是个普通助手，问它什么都行。系统提示词里写死了：用户只是在打招呼或
提问时，**不要**自作主张去装东西。

**📊 用量信息页**

跟钱有关的东西**只有这一页**：余额（大数字，低于 ¥2 变橙、低于 ¥1 变红）、今日/本月消耗、
**两张按时间分布**的 token 用量柱状图（上 flash、下 pro）、右上角的「充值」按钮。数据每 30 秒
自动刷一次，这一页也有「刷新」按钮。

两张图各画在 tk.Canvas 上，上下叠着：今日按小时、本月按天（服务端按北京时间分桶），
**单色**（刻度写在轴线下面，颜色不承担区分职责，也就不需要图例）、柱顶直接标数值、
鼠标悬停出提示框。时间桶多时只每隔几个画一个刻度（从最后一根往回数，保证当前小时 /
今天一定有标签）。卡片标题就是模型名，右上角写这一期间的合计，所以不靠悬停也能
知道每个模型花了多少。模型按名字里的 `flash` / `pro` 挑，某个模型当期没用量也留着
那张空图（"暂无数据"），不会两张图变成同一个模型。色板跑过对比度校验
（`#3987e5` 在卡片底色 `#1f1f1f` 上 ≥3:1）。

> 后端 `/api/usage` 的 `today` / `month` 里各带一个 `series_by_model`
> （`{模型: [按小时 / 按天的序列]}`）。两张图吃 `series_by_model`；老服务端只给
> `series` / `by_model` 时，客户端会退化成每个模型一根总量柱子，并注明
> "服务器暂未按模型拆分时间"，不会白屏 —— 详见下面「服务端接口」一节。
>
> 余额不足时点服务器给的 `aikaui://recharge` 链接，助手**先显示这一页**（让人看清钱花在
> 哪了），数据回来后再自动弹充值窗；充值窗停在主窗右边，不把图表整个盖住。

**余额见底只问一句，不主动弹充值页**

余额跌破 ¥1 时，程序**不会**自己打开充值窗口，只在窗口底部冒一条暗琥珀色的
"⚠️ 余额只剩 ¥x.xx，是否立即充值？"（服务器返回 402 余额不足时同理）。点「立即充值」
才弹充值窗，点「暂不」就收起来，并提示"需要充值随时点左边「用量信息」→「充值」"。
一次会话只问一次（402 那种情况除外）。

**ask_user 选项交互**

Agent 需要你拿主意时，不是把 "1. xxx 2. xxx" 打在日志里，而是渲染成可点的选项行：
**点一下就直接当答案提交**（没有"先选、再点确定"那一步），也可以 ↑↓ 移动选中、回车提交，
或者直接打序号。Esc 跳到自由输入。最后一行永远是「✏️ 输入点别的」。

**关闭窗口 = 退出程序**

三个平台都**不做托盘**。点右上角 X 就是真的退出，不会留一个找不到的常驻进程。
（以前 Windows 上"关窗缩到托盘"的行为已删除。）

**字体是一处定义的**

界面字体按平台挑：Windows 用 **Microsoft YaHei UI**（设置面板用的那一款，比普通雅黑字距更紧）、
macOS 用 **PingFang SC**、Linux 用 **Noto Sans CJK SC**；数字和订单号走等宽，
这样 token 数、金额、日志列与列之间是对齐的。字体是运行时按优先级问 Tk 要的，
一个都找不到才退到系统兜底字体。字号全在 `ui/fonts.py` 一张表里，改一处整个界面跟着变。

## 快速开始

需要 Python 3（开发环境用的是 3.12.4），Windows / macOS / Linux 都行。

```bash
pip install -r requirements.txt
python main.py
```

首次运行会自动向服务器领一份设备凭证，存到 `~/.claude-agent/identity.json`。

离线测试（不需要联网，也不需要 GUI）：

```bash
python -m unittest discover -s tests
```

## 打包

三平台共用一份 [`agent.spec`](agent.spec)（文件名用 ASCII，CI 和命令行都能直接引用）：

```bash
python -m PyInstaller agent.spec --noconfirm --clean
```

| 平台 | 产物 | 说明 |
|---|---|---|
| Windows | `dist/ClaudeCode安装助手.exe` | 单文件、无控制台；先跑 `python build_tools/make_version_info.py` 才有文件版本信息 |
| macOS | `dist/ClaudeCodeAssistant.app` | 未签名；Info.plist 里声明了 `aikaui://` |
| Linux | `dist/ClaudeCodeAssistant` | 单文件；发行时和 `packaging/claude-code-assistant.desktop`、`agent.png` 一起打 tar.gz |

`.spec` 里的入口只写了 `main.py`，PyInstaller 会顺着 import 图自己把 `ui/`、`server/`、
`platform_support/` 收进去，不用手工维护模块清单。要手工列的数据文件只有图标
（Windows 的 `agent.ico`、macOS/Linux 的 `agent.png`）和 macOS 的 `agent.icns`。

**CI**：打 `v*` tag（或手动触发）时 [`.github/workflows/build.yml`](.github/workflows/build.yml)
在四个 runner 上并行构建（windows-x64 / macos-arm64 / macos-x64 / linux-x64），产物传到
Actions artifact。macOS 两个包都**没有签名**：首次打开要右键 →「打开」，
或者终端跑 `xattr -dr com.apple.quarantine /Applications/ClaudeCodeAssistant.app`。

## 自动更新

程序**不弹窗、不打断**：后台静默检查 → 静默下载 → 下次启动时换上新版本。
用量页底部有一行 `v1.0.0`，就是当前跑着的版本。

**客户端怎么工作**（[`updater.py`](updater.py)）

1. 启动 25 秒后起一个 daemon 线程，之后每 6 小时拉一次
   `https://aikaui.com/downloads/latest.json`（网络失败就退回上次缓存的清单）。
2. 只接受 `schema_version: 1`、`https` 地址、64 位十六进制 SHA-256、
   `size <= 300 MB`，而且版本必须比当前新、当前版本不低于 `min_version`。
3. 流式下载到 `~/.claude-agent/updates/staged/<版本>/`，边写边算哈希；
   对不上就删掉 `.part`，绝不替换。
4. 下好只写一行日志「新版本 vX 已下载，下次启动自动更新。」，然后**什么都不做**。
5. 下一次启动、创建 Tk 窗口**之前**（`main.py` 最前面）：
   拿文件锁 → 把旧文件改名为 `.old-<pid>` → 放入新文件 → 失败就换回去 →
   用原来的参数重新拉起自己 → `sys.exit(0)`。新进程打一行「已自动更新到 vX」。
6. 同一个版本连续 3 次替换失败（只读目录、杀毒软件锁文件）就写 `failed.json`、
   删掉暂存，不再拿它骚扰用户。更新失败永远只是"这次没更新成"，不会让程序起不来。

**状态文件**都在 `~/.claude-agent/updates/`：`manifest.json`（清单缓存）、`pending.json`
（待安装）、`applied.json`（更新成功的提示，只提示一次）、`failed.json`、`update.log`。

**macOS**：从"下载"目录直接打开的 app 会被 App Translocation 放到只读的随机路径，
替换会失败（只记日志）。把 app 拖进 `/Applications` 再运行即可正常更新。

**发布新版本**

1. 改 `config.py` 里的 `APP_VERSION`，提交。
2. 打 tag（必须与 `APP_VERSION` 一致，CI 会校验）：`git tag v1.0.1 && git push origin v1.0.1`。
3. CI 构建四个平台产物，`publish` job 调 [`build_tools/deploy_downloads.sh`](build_tools/deploy_downloads.sh)
   把包传到 `releases/<版本>/`，最后以"先写 `.tmp` 再 `mv`"的方式换掉 `latest.json` ——
   客户端永远读不到半个清单。
4. 手工发布也行：四个产物放一个目录，用
   [`build_tools/make_manifest.py`](build_tools/make_manifest.py) 生成 `latest.json`，
   再跑部署脚本。

> **老用户注意**：`v1.0.0` 是第一个带 updater 的版本。更早安装的版本里面没有更新代码，
> 必须**手动**装一次 `v1.0.0`，之后才能自动更新。

## 目录结构

```
agent_demo/
│
├── main.py                       134   程序入口：先换新版本 → 建窗口 → 起线程 → 事件循环
│
├── config.py                      98   唯一的常量源：版本、服务器地址、凭证、路径、配色、更新参数、MAX_STEPS
├── updater.py                    660   自动更新：清单校验 / 静默下载 / 下次启动替换自己（不 import tkinter）
├── deps.py                        19   可选依赖探测：Pillow 在不在
├── helpers.py                    217   数字金额格式化 + 用量取数（按模型 / 时间序列）+ 刻度 + 文字宽度估算 + 选项清洗
├── images.py                     138   窗口图标（打包资源优先，缺了现画）+ 付款二维码下载
├── notify.py                      26   桌面通知薄封装（各平台实现在 platform_support）
├── bus.py                         79   线程间队列 + poll_queue（每 100ms 把消息翻译成界面操作）
├── llm.py                        117   大模型客户端（走 aikaui.com 代理，带 X-Device-Id）
├── agent_core.py                 221   Agent 主循环：run_one_task / agent_worker / balance_worker
├── prompts.py                    415   系统提示词（按平台给安装指令 + 工作目录 + VSCode 推荐 + 余额分寸）
│
├── platform_support/                  三平台差异（调用方只 import 这个包）
│   ├── __init__.py                93   按 sys.platform 延迟加载并转发
│   ├── common.py                  59   safe_cwd（HOME 越界检查）、find_claude
│   ├── windows.py                208   注册表协议 / .lnk / cmd / WinRT toast
│   ├── macos.py                  182   Info.plist 协议 + Apple Event / 符号链接 / Terminal.app
│   └── linux.py                  222   .desktop + xdg-mime / 终端探测 / notify-send
│
├── server/                            服务端与工具层（不 import 界面）
│   ├── ui_bridge.py               31   GUI 注入桥：set_ui / get_ui
│   ├── identity.py               332   设备指纹、本地凭证、领 token、_ensure_account_code / _ensure_user_id
│   ├── api.py                    504   余额 / 用量 / 充值 HTTP 层（永远不抛异常）
│   ├── claude_config.py          124   把身份写进 ~/.claude.json 和 ~/.claude/settings.json
│   ├── tools_fixed.py            141   五个固定工具 + aikaui:// 协议注册（转发 platform_support）
│   ├── tools_general.py          156   bash / read_file / write_file / ask_user
│   └── tool_specs.py             286   工具说明书：tools[] + tool_map（13 个工具）
│
├── ui/                                界面层
│   ├── fonts.py                  179   全界面字体表：按平台选字体 + 字号常量 + 画布缩放
│   ├── gui.py                     26   class GUI(WindowMixin, OptionsMixin, RechargeMixin, UsageMixin, LoginMixin)
│   ├── window.py                 479   WindowMixin：窗口壳、左侧导航、对话页、底部询问条、余额回流
│   ├── usage.py                  625   UsageMixin：用量信息页（余额卡 + 两张按模型的时间分布图 + 充值入口）
│   ├── options.py                219   OptionsMixin：ask_user 选项面板（点击 / ↑↓ / 回车 / Esc）
│   ├── recharge.py               627   RechargeMixin：充值弹窗两页 + 创单 + 付款后刷新 + 只办充值时的靠右定位
│   └── login.py                  230   LoginMixin：换设备登录弹窗（账户码）
│
├── build_tools/
│   ├── make_version_info.py       80   生成 Windows 文件版本信息（build/version_info.txt）
│   ├── make_manifest.py          113   四个产物 → latest.json（校验版本、算 sha256 / size）
│   └── deploy_downloads.sh            上传产物与清单到 aikaui.com（先传包，最后原子换清单）
├── tests/                            离线单元测试（49 个，stdlib unittest）
│   ├── test_version.py                版本解析 / 比较 / 清单校验
│   ├── test_download.py               本地假服务器：下载、哈希、404、缓存回退
│   ├── test_apply.py                  备份、回滚、失败上限、锁、更新提示
│   ├── test_platform_support.py       三平台抽象层
│   ├── test_prompts.py                提示词按平台切换
│   └── test_imports.py                工具层导入不依赖 Windows
├── packaging/
│   └── claude-code-assistant.desktop   Linux 启动器（tar.gz 里一起发）
├── .github/workflows/build.yml        四平台 CI 构建 + tag 时发布
│
├── agent.spec                          PyInstaller 打包配置（三平台共用）
├── requirements.txt                    第三方依赖（按平台带 marker）
├── agent.ico / agent.png / agent.icns  窗口 / 应用图标
├── 文件执行或调用顺序.md                一次对话里各模块的出场顺序
└── README.md
```

## 架构

### 分层依赖

```
main.py ─→ ui/ ─→ agent_core ─→ llm / server/ ─→ config
```

依赖是**单向**的，全图无环：

- `config.py` 不依赖任何本地模块，是所有常量的唯一来源。
- 界面层（`ui/`）按职责拆成四个 mixin，拼成 `GUI`；四个 mixin 方法名互不重复，
  也没有一个调 `super()`，所以 MRO 顺序不影响行为。
- 工具层（`server/`）**不 import 界面**。

### 线程模型：4 条线程，全靠队列说话

| 线程 | 起点 | 周期 | 干什么 |
|---|---|---|---|
| 主线程 | `mainloop()` | `poll_queue` 每 100ms | **唯一**能碰 tkinter 的地方 |
| Agent 线程 | `agent_core.py` | 阻塞在 `user_input_queue.get()` | 跑模型、调工具 |
| 余额线程 | `agent_core.py` | 每 30 秒 | 查余额 / 用量 → 丢进 `msg_queue` |
| 协议线程 | `main.py` | 一次性 | 注册 `aikaui://`，注册完就结束 |

线程之间没有任何直接调用，全靠 `bus.py` 里那四个东西连着：`msg_queue`（工作线程 → 界面）、
`user_input_queue`（界面 → Agent）、`input_event` + `input_holder`（`ask_user` 专用的
"闹钟 + 信箱"）。

**铁律：非主线程绝不碰控件，一律往 `msg_queue` 丢消息，由主线程在 `poll_queue` 里更新界面。**

`poll_queue` 会分发的消息种类：`log`、`balance`、`busy`、`recharge`、`recharge_result`、
`recharge_paid`、`ask`、`progress_start`、`progress_stop`。其中 `recharge`（服务器说余额不足）
**不弹窗**，只调 `offer_recharge()` 在底部问一句。

一次对话里文件和线程的完整流转，见 [文件执行或调用顺序.md](文件执行或调用顺序.md)。

### 五个容易踩的坑

**1. `_ui` 的晚绑定。**

工具层拿界面对象必须走 `server/ui_bridge.py` 的 `get_ui()`：

```python
u = get_ui()
if u:
    u.ui_log("...")
```

不能 `from server.ui_bridge import _ui` —— 那样会在 **import 的那一刻**把值（通常是 `None`）
拷进来，之后 `set_ui(gui)` 改的是 `ui_bridge` 里的 `_ui`，你那份永远还是 `None`。

**2. 两个同名的"刷新余额"。**
`ui/recharge.py` 和 `ui/usage.py` 都用到了，别搞混：

```python
from agent_core import refresh_balance          # 查完丢进队列，刷用量页
from server.api import refresh_balance as fetch_balance   # 查完返回 dict，判断到没到账
```

`ui/usage.py` 的「刷新」按钮也是起一条 daemon 线程跑 `agent_core.refresh_balance` ——
网络请求不能落在主线程上，不然界面会卡住。

**3. 四个 mixin 的方法名不能撞。**
`GUI` 靠 MRO 把四个 mixin 拼起来，谁都不调 `super()`，所以一旦两个 mixin 定义了同名方法，
排在后面的那个会被静默吃掉。加方法前先在全目录搜一下同名。

**4. `_ensure_account_code` / `_ensure_user_id` 必须留在 `server/identity.py`。**
`server/api.py` 要用它们，而它们内部会回调 `register_device()`。挪进 `api.py` 就变成
`api ↔ identity` 双向依赖，直接循环导入。

**5. `tk.Canvas` 上的东西不会被 CTk 缩放。**
字体全在 `ui/fonts.py` 一处定义：控件用 `font(S_BODY)`，画布用 `spec(S_CANVAS)`，
**别在别处写 `ctk.CTkFont(size=11)` 或 `font=("微软雅黑", 11)` 这种字面量**。特别是画布——
`ctk.CTkFont` 的 size 是**像素**（还会按屏幕 DPI 再放大一遍，这台机器 150% 就是 ×1.5），
而 `create_text` 老实按 Tk 的规矩解释：正数是磅、负数是像素，1 磅只有 1.33 像素。
所以 `spec()` 传的是负数；同时画布上的**长度**（留白、柱宽、提示框）也得乘 `canvas_scale()`，
不然图上的字永远比旁边的控件小一圈。文字宽度用 `helpers._text_width(text, fonts.csize(S_AXIS))`
估算 —— 字号口径必须和 `spec()` 一致，否则提示框会量错、把文字切掉。

## 数据与凭证

| 文件 | 内容 |
|---|---|
| `~/.claude-agent/identity.json` | 本机凭证：`device_id`、`api_key`、`api_base`、`model`、`user_id`、`account_code`、`balance`，以及最近一笔充值订单 `last_order` |
| `~/.claude.json` | Claude Code 的配置，`set_model_config` 往它的 `env` 里写中转地址、token 和各档位模型映射 |
| `~/.claude/settings.json` | Claude Code 的用户级设置，`set_model_config` 只改其中的 `model` 一个键（钉默认模型），其它键原样保留 |

### 模型档位：默认只走 flash，pro 要自己主动选

`set_model_config` 写进去的映射分两层：

| 档位 | 环境变量 | 指向 |
|---|---|---|
| 默认 | `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` / `ANTHROPIC_DEFAULT_MODEL` | flash |
| 默认 | `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` | flash |
| 显式 | `ANTHROPIC_DEFAULT_OPUS_MODEL` | pro |

外加 `~/.claude/settings.json` 的 `"model"` 也钉成 flash（写**完整模型名**而不是
`haiku` 这类别名，可以直接绕开别名解析）。

**`ANTHROPIC_DEFAULT_{SONNET,OPUS,HAIKU}_MODEL` 不是"优先级较低的默认值"，而是给
`sonnet` / `opus` / `haiku` 这几个别名做重定向的**。Claude Code 在用户没明确指定模型时
走的是 *sonnet 档*，所以 `ANTHROPIC_DEFAULT_SONNET_MODEL` 一旦指向强模型，用户什么
都没选也会打到贵的那个上 —— 这就是"指定了 flash 却偷偷调用 pro"的原因。
同理，只要 `ANTHROPIC_DEFAULT_OPUS_MODEL` 指向 pro，任何请求到 opus 档的地方
（子 agent、plan 模式等）都会落到 pro；这是留给用户自己 `/model` 选 opus 的通道，
不想要可以把它也改成 flash。

`device_id` 是主机名 + MAC + 架构 的 sha256 前 16 位，同一台机器每次算出来都一样。
服务器靠它记账，所以一旦注册过就固定复用文件里那个 —— 每次重新生成的话，服务器会当成
新设备再发一份 token，用户余额也看不到。

同一个 `account_code`（形如 `CC-F43D-5152`）可以换设备找回账号，**「用量信息」页的余额卡
下面一直显示着它**，方便用户截图保存。

## 服务端接口

统一走 `https://aikaui.com`，统一带 `X-Agent-Key` 请求头（值在 `config.py` 里，
是个"半公开"的凭证，服务器端会限流）。

| 接口 | 用途 |
|---|---|
| `POST /api/issue-token` | 拿 `device_id` 换一份设备专属 token |
| `GET /api/balance` | 查余额（按 `account_code` 查） |
| `GET /api/usage` | 查 token 消耗 |
| `POST /api/create-order` | 创建充值订单，返回付款二维码 |
| `aikaui://recharge?user_id=N` | 自定义协议：支付页付完款靠它把本程序唤起来 |

`server/api.py` 里的所有函数**永远不抛异常** —— 失败就返回 `(None, 错误说明)` 或
`{"error": "..."}`，错误原文会被翻译成大白话带到界面上。余额查不到、支付平台挂了，
都不会把程序搞崩。

### `/api/usage` 的响应形状（后端已按这个返回）

`today` / `month` 两块里各带一个 `by_model`、一个 `series`（总量序列）和一个
`series_by_model`（**按模型拆开的时间序列**，两张图读这个）。
序列按**北京时间**分桶：今日是 0 点到当前小时，本月是 1 号到今天。

```json
{"today": {"tokens": 11785016, "yuan": "2.22",
           "by_model": {"deepseek-flash": {"tokens": 11785016, "yuan": "2.22"}},
           "series": [{"label": "00:00", "tokens": 0, "yuan": "0"},
                      {"label": "01:00", "tokens": 1234, "yuan": "0.01"}],
           "series_by_model": {
               "deepseek-flash": [{"label": "00:00", "tokens": 0, "yuan": "0"},
                                  {"label": "01:00", "tokens": 1234, "yuan": "0.01"}],
               "deepseek-v4-pro": [{"label": "00:00", "tokens": 0, "yuan": "0"}]}},
 "month": {"tokens": 48647024, "yuan": "13.44",
           "by_model": {"deepseek-flash": {"tokens": 44580889, "yuan": "9.81"},
                        "deepseek-v4-pro": {"tokens": 4066135, "yuan": "3.64"}},
           "series_by_model": {
               "deepseek-flash": [{"label": "10-01", "tokens": 100, "yuan": "0"}],
               "deepseek-v4-pro": [{"label": "10-01", "tokens": 0, "yuan": "0"}]}}}
```

**改动这个接口时守四条**：`by_model` 里各模型的 `tokens` 加起来等于同级的 `tokens`；
`series` 里各桶的 `tokens` 加起来也等于同级的 `tokens`；`series_by_model` 里每个模型的
桶加起来等于该模型在 `by_model` 里的 `tokens`；老的 `tokens` / `yuan` 字段保留原样
（余额卡和两个消耗小格读的是它们）。

客户端解析做得很松：`helpers._norm_by_model` 认 `by_model` / `models` / `by_model_tokens`，
值可以是 `{模型: token数}`、`{模型: {"tokens":…, "yuan":…}}`，也可以是
`[{"model": …, "tokens": …}, …]`；`helpers._norm_series` 认 `series` / `by_hour` / `by_day`
（`series_by_model` 里的每一段也走它）。**按模型序列缺失时那两张图退回每个模型一根
总量柱子并注明"服务器暂未按模型拆分时间"；连模型数据都没有时退回总量的单根柱子 +
"暂无数据"，不会白屏**。

## 已知限制

- **三平台功能一致，但细节依赖系统**。Windows 的快捷方式是 `.lnk`、协议写注册表；
  macOS 的协议写在 Info.plist 里、链接靠 pyobjc 收 Apple Event、快捷方式是桌面符号链接；
  Linux 的协议靠 `.desktop` + `xdg-mime`、快捷方式也是 `.desktop`。桌面图标和终端启动
  各平台的实现见 `platform_support/`；Linux 没装终端模拟器时会明确报失败，不会假装成功。
- **macOS 包没有签名**。首次打开要右键 →「打开」，或 `xattr -dr com.apple.quarantine`；
  从 Downloads 直接运行时 App Translocation 会让"覆盖自身"失败（更新会在下次启动时跳过）。
- **文件操作限制在用户主目录内**。`read_file` / `write_file` / `launch_claude` 的 `directory`
  都会做路径越界检查，越界直接拒绝或退回主目录。
- **余额是个"测试大数"**。当前这个测试账号服务器给的余额是 8 位数的量级，看起来不像真钱；
  界面对它没有任何问题（不溢出、不上色），只是读起来怪。
- **`create-order` 的响应字段名（`img` / `qrcode` / `pay_url`）是按接口文档实现的。**
  代码对字段缺失做了容错（只给 `qrcode` 不给 `pay_url` 也能跑），但没有跟真实响应核对过。
  如果扫码页显示「二维码没加载出来」，先去确认这几个字段名。
- **`httpx2` / `httpcore2` 不是笔误，是这一套 `openai` 真的要的。** 本机装的是 openai 3.15.0，
  它内部写的是 `import httpx2`（标准版 openai 才是 `httpx`），而这两个包跟同名去尾数的
  版本是两套不同的东西。所以 `.spec` 里那两行 `collect_all` 必须留着 —— 换成别的 openai
  版本时要跟着换成 `httpx` / `httpcore`，否则打出来的 exe 一联网就崩。
