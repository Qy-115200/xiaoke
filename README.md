# 小克 · Claude Code 一键配置

一个 Windows / macOS / Linux 桌面小程序：把「装 Claude Code」这件折腾半天的事，变成跟 AI 说一句话。

窗口左边一条导航栏，右边三个页面：**💬 对话**、**📊 用量信息** 和 **🔐 账户信息**。对话页背后是一个能调工具的
Agent —— 它会自己检测环境、装 Node、装 Claude Code、写好配置、问清楚工作目录、在桌面放个图标，
然后把 Claude Code 打开。它也是个能聊的「小克」：能陪你聊天、答疑、查余额、扫码充值。

## 功能

**装 Claude Code（一键完成）**

底部一个「一键安装 Claude Code」按钮，或者直接说"帮我装 Claude Code"。Agent 按固定顺序
调前四个工具：领 token → 绕过地区限制 → 写配置 → 建桌面快捷方式，然后问清楚工作目录，
最后**二选一**收尾：装好 Claude 的 VSCode 插件并用 VSCode 打开（默认），或者开一个新终端。
中途每一步都会用大白话跟你解释一句，要装东西之前会先问过你。

**装到一半不耐烦了可以点「中断」**（2026-10-31 加）

装 winget 要下 58MB、走官方安装包那条路要下 240MB，几分钟很正常。输入框右边有一个
「中断」按钮，平时是灰的，Agent 一开工就亮起来。点了会同时做三件事：

1. 立一块「中断」牌子（`agent_bus.cancel_event`）—— Agent 循环每走一步、每个下载循环
   每 1MB 都会看一眼，看见就停；
2. 掐掉正在跑的子进程（`kill_active_children()`，Windows 上 `taskkill /F /T`），
   不然 winget / powershell / 安装包能自顾自跑几分钟，按钮就成了摆设；
3. 如果 Agent 正卡在问你问题那一步，把等待叫醒，别让线程一直挂着。

**已经装到一半的东西不会回滚**，点完的提示里也这么写。下一轮任务开工时牌子会自动摘掉
（`agent_worker` / 服务器版每轮调 `clear_cancel()`）。服务器版对应的是**连按两次 Ctrl-C**
（第一次只是「上膛」），同一个牌子、同一套收尾。

**装完不自动关窗口**（2026-10-31）。以前安装流程走完会留 10 秒，然后助手自己退出；
用户明确说不要 —— 装完正是最想回头问两句的时候（"这个面板怎么用""项目放哪"），
助手自己关掉最招人烦。`install_done` 这个事件还在（它仍然是"这一轮真把用户领进门了"
的唯一判据），只是界面拿到它什么都不做。关窗口只有两个办法：点右上角 X，或直接说再见。
顺带的好处：助手常驻之后，PyInstaller 报的
`Failed to remove temporary directory: …\_MEI…` 也不会再被"自己先退出、VSCode 还占着目录"触发。

**先定工作目录，再启动**

Claude Code 只看得到你给它的**一个**文件夹。所以启动之前 Agent 必须先问清楚"以后在哪个
文件夹里干活"（`ask_user` 给三个选项：桌面新建一个 / 用户主目录 / 自己指定路径），
再把这个目录传给 `open_vscode(directory=…)` / `launch_claude(directory=…)` 当 `cwd`。目录不存在会建出来，越界（跑到
用户主目录外面）会被退回主目录并在日志里说明原因 —— 跟 `read_file` / `write_file` 是同一条规则。

启动完还会把这件事再讲一遍：一个项目一个文件夹，换项目就在 VSCode 里打开那个文件夹（或在那个文件夹里重新启动）。

**装完接到 VSCode：检测 → 问 → 装 / 装插件 → 打开**

装完 Claude Code、定好工作目录之后，Agent 先 `check_vscode()` 拿事实，再按情况**只问一次**：

| 情况 | 问什么 | 同意之后 |
|---|---|---|
| 没有 VSCode | 先讲一句好处（改了什么在编辑器里直接看到、能一键撤销），问要不要装 | `install_vscode()` → `install_claude_extension()` → `open_vscode()` |
| 有 VSCode 没插件 | 问要不要装 Claude 插件 | `install_claude_extension()` → `open_vscode()` |
| 都有 | 不问 | 直接 `open_vscode()` |

用户说"不用"，就退回 `launch_claude()` 开终端，**之后任何一轮都不再提 VSCode**。
两个入口只能开一个，绝不都开。

**打开 VSCode 时关掉「工作区信任」门禁**（2026-10-31）。助手是替用户**新建**的目录，
VSCode 一定把它当成"未信任"，先弹一个「Do you trust the authors of the files in this
folder? … Creating a terminal process requires executing code」。在用户点「信任」之前，
扩展跑在受限模式里：Claude 面板出不来、连终端都开不了 —— 用户看到的现象是"插件装了却没开"。
所以 `open_vscode()` 一律带 `--disable-workspace-trust`（`VSCODE_NO_TRUST_FLAG`）：
**只对这一次打开的那个窗口**生效，不改任何全局设置，用户之后自己打开别的文件夹照样会被问。
带开关就得走 CLI（直接起 `Code.exe` 时参数会被转手给已运行的实例，开关不生效）。

装 VSCode 本身：Windows 走 `winget --scope user`（不弹 UAC），macOS 走 `brew install --cask`，
Linux 只打开官网下载页（apt/snap 要 sudo，助手不替你提权）。**装不上就打开下载页**并如实说明；
插件市场连不上也一样，照样用 VSCode 打开，让用户在扩展面板里手动装。

**机器上没有 winget 时先把它装上**（`ensure_winget()`，2026-10-30 加）。原因很实在：
Windows 10/11 的家庭版精简镜像、老 LTSC、服务器版都可能不带 App Installer，
走到 `install_vscode()` 就直接死在「这台机器上没有 winget」，用户被卡在装到一半。

- 包是我们自己 CDN 上的微软官方 App Installer（x64 那个 msix，58MB，sha256 钉在
  `platform_support/windows.py` 里）。**不走 GitHub**：2026-10-30 实测国内直连
  `objects.githubusercontent.com` 是 0 字节，而 winget 的 msixbundle 有 200MB+。
  这里只取 bundle 里 x64 的那一份，内容与微软发布的完全一致，没有做任何改动。
- 先装**依赖框架包**再装主包（2026-10-31 加）：App Installer 依赖
  `Microsoft.UI.Xaml.2.8` 和 `Microsoft.VCLibs.140.00.UWPDesktop`，微软把它们单独放在
  `DesktopAppInstaller_Dependencies.zip` 里。漏了这一步，主包可能装不上。
  两个包（6.7MB + 5.1MB，sha256 钉在 `WINGET_DEPS` 里）下到
  `%TEMP%\aikaui-winget-deps\`，大小和 sha256 都对得上就复用、不再走网络。
  依赖装不上不直接放弃（机器上可能本来就有），主包照样试；两个都失败才报两句。
  `Add-AppxPackage` 的 `0x80073D06 / 0x80073CFB / 0x80073D02`（更高版本已装 / 同版本
  已装 / 资源包已注册）都算成功 —— 重装依赖时天天撞见，不认它就是误报。
- **`Add-AppxPackage` 的参数名只能是 `-Path`**（2026-10-31 查清，这是那个老 bug 的真正原因）。
  它**没有** `-LiteralPath`；写上它 PowerShell 当场抛
  `ParameterBindingException` / `NamedParameterNotFound,
  Microsoft.Windows.Appx.PackageManager.Commands.AddAppxPackageCommand`，整条命令一句都没执行。
  表现就是用户日志里的「Add-AppxPackage 返回 1」，然后一路退到 240MB 的官方安装包 ——
  走得通，但慢得多。`-Path` 认通配符，所以路径里真带 `[` `]` `` ` `` 时会先
  `Set-Location -LiteralPath <目录>` 再只传文件名。
- **失败时要留下能看的错误**（2026-10-31 重写）。原来是「取最后三行」—— 取到的正好
  是 PowerShell 的 `+ ~~~~` 位置标记，真正有信息的第一行
  `Deployment failed with HRESULT: 0x8007…` 反被丢了；而且硬按 UTF-8 解码，
  中文路径 `瞿毅` 在用户日志里变成 `????`（中文 Windows 的 PowerShell 写 GBK）。
  现在 `_summarize_powershell_error()`：先去掉纯装饰行，保留第一行，再补一条带
  `0x8…` 的、一条 `CategoryInfo`、一条 `FullyQualifiedErrorId`；解码走
  `smart_decode()`（UTF-8 → 本地编码）。
- 失败后**隔 2 秒再试一次**，第二次把 msix 复制到 `%PUBLIC%\aikaui\`（纯 ASCII 路径）
  再装 —— AppX 部署对非 ASCII 路径有过 IO 报错，ASCII 副本是绕开它的最省事办法。
  `_ascii_copy()` 只在原路径含非 ASCII 时才做，装完（无论成败）删掉副本。
  两次都失败就报两段的错误。
- 先 `Add-AppxPackage`（用户级、不弹 UAC、不改系统策略）。**装不上就认，不再解压
  便携版**（2026-10-31 删掉）：便携版没注册成 AppX 包，源索引永远打不开，
  `winget install` 必然 `0x8A15004B`。以前这里会解压一份、打一行「已装好 winget
  （便携版，解压即用）」，下一句紧接着就是「读不到软件源，改用官方安装包」——
  白等十几秒，日志还自相矛盾。现在直接返回失败原因，`install_vscode()` 跳第 3 条。
- 下载边写边算 sha256，对不上就删掉、换下一个源；两个源（CDN / 回源）都失败才回到下载页。
- 58MB 的安装包用完即删；只有第一次需要，之后 `find_winget()` 直接命中。

**winget 装上了不等于能用**（2026-10-30 第二个坑）。刚装好的 winget 没有源索引，
`winget install` 会**当场**返回 `0x8A15004B`（`SourceOpenFailedSuggestion`）——
用户在真机上撞到的就是这个：包下完了、winget 也装上了，然后报
`winget 返回 2316632139`。所以 `install_vscode()` 现在是三条路依次试：

1. `winget install`。失败时**只有**看着像源的问题（`SourceOpenFailed` / `source`）才
   `winget source update`，不行再 `source reset --force` + `update`，然后重试一次。
   超时之类的失败不重试 —— 那等于把上限翻倍。
2. 没有 winget 就先 `ensure_winget()` 装上（上面那 58MB），再走第 1 条。
   `ensure_winget()` 失败（`Add-AppxPackage` 两次都没成）就**直接跳第 3 条** ——
   不再解压便携版，理由见上。
3. **都不通就绕开 winget**：直接下微软官方的用户级安装包
   （`update.code.visualstudio.com/latest/win32-x64-user/stable`，约 240MB），
   `/VERYSILENT /MERGETASKS=!runcode` 静默装，退出码 3010（要求重启）也算成功。
   这条和下载页给用户的是同一个文件，签名也是微软的。

三条都不行才打开下载页，并把每一步卡在哪如实说出来（`on_note` 会往对话里打行）。

`find_winget(verify=True)` 的顺序是 **PATH → 执行别名 → 最后问一次 AppX 注册表**，
前面那几个候选手到之后都会真跑一次 `--version`：`WindowsApps` 下的**执行别名可能是空壳**
（文件在、包没为这个用户装，`Popen` 直接 WinError 3），不验就会拿一个跑不起来的路径去装东西。
**便携版那条候选删掉了**（2026-10-31）：老的解压目录就算还在，拿它装东西也必然失败。

最后那道 `_winget_from_appx_registration()`（2026-10-31 加）是**不再靠猜路径**的兜底：
问系统 `(Get-AppxPackage Microsoft.DesktopAppInstaller).InstallLocation` 拿到包真正装在哪，
再用那里的 `winget.exe`。加它的原因很实在 —— 用户机器上 winget **明明是好的**
（`Get-AppxPackage` 报 1.29.380 / Ok，别名跑 `--version` 回 `v1.29.380`），我们却判定
「没有 winget」，于是白下 58MB、白装一遍（还失败）、再白下 240MB。上面那些候选全靠猜：
PATH 最前面那条 `WindowsApps` 可能属于**另一个账户**（本机就是，Administrator 的空壳），
`%USERPROFILE%` 也可能不是你以为的那个目录（本机 `USERNAME` 是 `瞿毅`，profile 却是
`C:\Users\QY`）。问 AppX 注册表则没有「猜」这回事。


`open_vscode()` 不只是开窗：它**把 Claude 面板也一起打开**。VSCode 命令行没有 `--command`
这种东西，所以走的是插件自己注册的 URI 入口 `vscode://anthropic.claude-code/open`
（插件 `extension.js` 里的 `registerUriHandler` 接到它就执行 `claude-vscode.primaryEditor.open`）。
两个细节：① 只在 `--list-extensions` 里确认插件存在时才发链接 —— 否则 VSCode 会弹一个
"无法打开链接"的报错框；② 本流程刚装完插件会先开一个**新窗口**（已开着的窗口是在装插件
之前加载扩展的，里面没有它），等 4 秒让插件宿主注册好 URI 处理器再发。
面板没能自动打开时，返回里会写清楚，让用户点左边活动栏的 Claude 图标 —— 不会假装成功。

检测不依赖 PATH：Windows 先查注册表（`App Paths` → `Applications\Code.exe` → `vscode` 协议），
再查常见目录，最后才是 `which`。这样装在 `D:\vs code` 这种非默认位置也找得到。
CLI 一律用安装目录里的 `bin\code.cmd`（`Code.exe --list-extensions` 是空输出），
`Code.exe` 只用来开窗；所有 VSCode 子进程都先过 `clean_env()` 摘掉
`ELECTRON_RUN_AS_NODE` / `VSCODE_*`（带着它们 `Code.exe <目录>` 会当 node 脚本跑完就退，
不报错也不弹窗）。

**对话式的「小克」**

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

余额卡下面那行账户码**只露前 4 位**（完整凭证在「🔐 账户信息」页）：这一页是用户最容易
截图发给别人的地方。

**🔐 账户信息页**

账户码和账户密钥在这里，**默认打码**（`CC-D************` / `************************`）。
每行有「👁 显示」按钮，点一下才看明文；再点变回打码，切走再回来也是打码 —— 明文状态
不做任何持久化。旁边是「复制」，只写系统剪贴板，**不写日志**（日志是用户会截图的地方）。

两串的分工：**账户码"找到哪个账号"，账户密钥"证明这个账号是你的"**。换设备登录要两样
一起填（见下面「换设备登录要两样」）。

下面一行红字是这一页存在的理由：

> ⚠️ 重要：这是您找回账户和余额的唯一凭证，请截图保存在安全的地方。切勿发给他人，否则余额将被盗用。

打码只是"别被路过的人一眼看走"，**不是加密**：明文就躺在本地凭证里。所以那句警告是必需品，
不是装饰。

**余额见底只问一句，不主动弹充值页**

余额跌破 ¥1 时，程序**不会**自己打开充值窗口，只在窗口底部冒一条暗琥珀色的
"⚠️ 余额只剩 ¥x.xx，是否立即充值？"（服务器真的说"余额已用完"时同理）。点「立即充值」
才弹充值窗，点「暂不」就收起来，并提示"需要充值随时点左边「用量信息」→「充值」"。
一次会话只问一次（服务器明确报余额用完时那次除外）。

**402 有两种，不能混成一句「余额已用完」**

new-api 在请求开始前按「上下文 + max_tokens」做**预扣费估算**，估算值超过剩余额度就拒绝，
不等实际生成完再算。所以一次很大的请求（图片、长文件、大 max_tokens）完全可能被拒，
而余额还有几毛钱 —— 2026-10-28 就因此让用户充了不必要充的钱。两种情况的处置完全不同：

| 服务器回的 `code` | 真实含义 | 界面怎么说 | 要不要劝充值 |
|---|---|---|---|
| `insufficient_user_quota` | 余额真的没了 | 「账户余额已用完」 | 要 |
| `quota_exceeded_for_request` | 余额还在，这次请求的预估花费超了 | 「这次请求的预估花费超过了剩余余额（还剩 ¥0.75）」 | **不要**，先让他把请求改小 |

`llm.insufficient_balance()` 返回 `{"kind", "text", "remaining"}`，`agent_core` 只对
`kind == "empty"` 发 `("recharge", …)` 事件。代理还会把真实剩余额度放在 `remaining`
（单位：**元**）里 —— 只要它大于 0，就算 `code` 还是老的那几个，也按第二种处理。
老代理只发一句中文、没有这些字段，那就按第一种走（和 v1.4.8 以前行为一致），
所以两边谁先上线都不会坏。

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

首次运行会自动注册一个账号（服务器要求做一次 PoW 人机校验，通常一两秒），
凭证存到 `~/.claude-agent/identity.json`。之后每次启动都直接用这份本地凭证，不再联网注册。

离线测试（不需要联网，也不需要 GUI）：

```bash
python -m unittest discover -s tests
```

**服务器上没有图形界面怎么办？** 用服务器版（见下一节），它不需要 `$DISPLAY`。

## 服务器版（Linux，无图形界面）

服务器上没有 X11 / Wayland，`python main.py` 一进来就 `import customtkinter`，
`ctk.CTk()` 会直接抛 `TclError`。所以 Linux 发的是**另一份产物**：一个纯终端程序。

**它和桌面版跑的是同一套 Agent 循环**（`agent_core.run_one_task`），只是把界面换成了
终端对话：装 Node → 装 claude → 领账号 → 写配置这几件事由模型来决定顺序、怎么补救、
什么时候该问你，不再是一条写死的流水线。唯一的确定性环节是**账号引导**——模型客户端
本身就要 `identity.json` 才能说话，所以那几步由代码问、代码办。

终端里的约定：

- **在干活时不能打字**（底部一行转圈 + 已用时），干完了才能打字、回车发送；
- 有选项时高亮整行，`↑`/`↓` 选、回车确认，Esc 跳到「自己打字」；
- `Ctrl-C` 要按两次：第一次只是「上膛」，2 秒内再按一次才中断。
  **已经开始的下载/安装不会被杀掉**（子进程是独立会话），它会自己在后台跑完；
- 真终端里装完会自动进入 `claude`；管道/容器里（`--yes`）不会，只打印怎么启动。

一条命令装好（脚本会自动下 [latest.json](https://aikaui.com/latest.json) 里 `linux-x64`
那一条，校验 sha256，不需要 root）：

```bash
curl -fsSL https://aikaui.com/install.sh | sh
```

装到 `~/.local/lib/claude-code-assistant/`，软链成 `~/.local/bin/claude-code-assistant`，
并往 `~/.bashrc` / `~/.profile` 写一段带标记的 PATH（`# >>> aikaui >>>`，重跑不会重复写）。
装完会跑一次 `--version`：**跑不起来就退 4，不会留下一个"装上了但用不了"的状态**。

装完直接启动（模型会一步步做，需要你拿主意的时候它才问）：

```bash
claude-code-assistant                 # 首次运行：注册账号 → 写 ~/.claude.json → 提示怎么用
claude-code-assistant --account-code <账户码> --account-key <账户密钥>   # 用已有账号绑这台机器
claude-code-assistant --yes           # 全默认，适合写进 Dockerfile / cloud-init（需要模型可达）
claude-code-assistant --dry-run       # 只打印要做什么，不下载、不写任何文件
claude-code-assistant --repair        # 只对账配置：跟 identity.json 不一致就补齐（见下）
claude-code-assistant --balance       # 查余额和用量
claude-code-assistant --recharge 20   # 充 20 元：在终端里打印付款二维码
```

| 参数 | 作用 |
|---|---|
| `--dir PATH` | claude 的工作目录，默认 `$HOME` |
| `--account-code` / `--account-key` | 绑已有账号（比在新机器上再注册一个更常见） |
| `--new-account` | 上次注册没走完、留下了旧账户码时，明确放弃它重新注册 |
| `--node-version` / `--claude-version` | 钉版本；默认 Node 取 LTS、claude 取 latest |
| `--yes` / `--force` / `--quiet` | 不提问（提问时自动选第一项）/ 已装也重装 / 只输出错误；`--yes` 现在**需要模型可达**，见下 |
| `--repair` | 只检查并修复 `~/.claude.json` 和 `~/.claude/settings.json`，不装 Node、不装 claude、不注册账号 |
| `--balance` | 只查余额和用量（今日 / 本月 token），不装东西 |
| `--recharge 金额` | 创建充值订单并打印二维码；`--channel wechat` 可指定微信（默认支付宝） |
| `--allow-root` | 1.4.4 起 root 默认就能装，这个参数只为兼容老脚本保留 |

`--repair` / `--balance` / `--recharge` 是"只做一件事"的模式，互相不能组合，
也不能和 `--account-code` / `--new-account` 一起用（那是安装路径的事）。

**余额和充值（服务器版）**。没有图形界面，所以付款走终端：
[`server/billing_console.py`](server/billing_console.py) 把
[`server/api.py`](server/api.py) 拿到的数据变成几行字，二维码用 `segno` 画成方块 ——
黑字白底（深色终端里不这么做，黑白是反的，有些扫码器不认）。**画不出来不影响充值**：
没装 `segno`、或者服务器没给二维码内容时，退回打印 `pay_url` 和二维码图片地址。

余额跌破 ¥1（和桌面版同一条线）时，`--balance` 和装完最后那屏都会把两条路摆出来：

1. `claude-code-assistant --recharge 20` —— 就地充，二维码直接打在终端里；
2. 绑到桌面版扫码 —— 用同一个账户码，余额记在账号上，这台服务器立刻能用
   （账户密钥在 `~/.claude-agent/identity.json` 的 `account_key`，只填进桌面版，别外传）。

账户码、金额、订单号可以打；`api_key` / `account_key` 的值**任何一行都不打印**，
提示里只出现 `<密钥>` 这样的占位符。


**它是分两层跑的：** 上面这几件"装什么、怎么装"交给模型（工具在
[`server/agent_tools.py`](server/agent_tools.py)，提示词在
[`server/prompts_server.py`](server/prompts_server.py)），模型的顺序**大概率**是这四步；
`--dry-run` 打印的就是这条确定性参考路线（不联网、不调模型，退出码照旧）。

模型能用的 12 个工具：`ensure_node` / `install_claude` / `configure_claude` /
`finish_install`（验收闸门：Node、claude、账号、两份配置都在才算装好）、
`bash` / `read_file` / `write_file` / `ask_user`、`query_balance` / `query_usage` /
`query_pay_methods` / `create_recharge`。**没有** VSCode、快捷方式、`aikaui://` 协议、
`login_with_account_code` —— 账户密钥不进模型的上下文。

`bash` 有护栏：不许 `sudo`、不许 `apt/dnf/yum/apk/snap install`、不许自己 `npm install`
（那几条坑在 `install_claude` 里已经处理好了，让模型现编就是事故重演）、
不许读写 `~/.claude-agent/`（里面有 `api_key`）；子进程一律 `stdin=/dev/null`，
否则它会抢走终端的按键，用户看到的是"打字没反应"。

**四步的细节**（也就是那四个工具的实现在做**什么**）：

1. **环境自检**：不是 Linux、架构不是 x86_64/aarch64、`~/.local` 不可写，都会在下载任何
   东西之前就失败（退出码 3）。root 不拦，只警告一句：东西全落在 `/root` 下，换个人用不了。
2. **Node**：`node` 主版本 ≥ 22 就复用；否则从 `cdn.npmmirror.com` 下
   `node-v<ver>-linux-<arch>.tar.xz`，对 `SHASUMS256.txt` 校验，装到 `~/.local/node-v<ver>`
   再软链成 `~/.local/node`。**全程不 sudo。**
3. **claude CLI**：`npm install -g --prefix ~/.local @anthropic-ai/claude-code
   --include=optional --allow-scripts=@anthropic-ai/claude-code`。
   `--include=optional` 不能省：真二进制在 optionalDependencies 的平台子包里。
   `--allow-scripts` 也不能省：**npm ≥ 11 默认不跑依赖的安装脚本**，只警告一句
   `npm warn install-scripts … not yet covered by allowScripts`，而这个包正是靠
   postinstall 把二进制拷到 `bin/`。脚本一被拦，装完就是「added 1 package」加一个
   500 字节的占位。参数写裸包名（npm 的匹配规则允许，钉版本会随发版失效）；
   npm 10 不认这个参数，但它只当普通配置忽略，不会报错。
   另外 `npm_env()` 把所有继承来的 `npm_config_*` 全部清掉再显式设一遍，
   防用户 `.npmrc` 里的 `ignore-scripts=true` / `omit=optional`。
   原生包还是没下来时（132MB 断在半路），单独 `npm install -g` 平台子包并
   **自动重试 3 次**；如果它其实已经在树里、只是没拷到位，就只跑一次
   `install.cjs`，不重复下载。
4. **账号 + 配置**：本地已有 identity 就复用（不会偷偷重注册）；`--account-code` 走
   `bind_account()`；都没有才 `register_device()`（PoW 进度在终端里画一行）。
   最后 `bypass_region_check()` + `set_model_config()` 写 `~/.claude.json` 和
   `~/.claude/settings.json`。

**模型连不上就直说连不上。** 这是用户明确要的：不假装装好了、也不含糊成"安装失败"。
[`server/agent_install.py`](server/agent_install.py) 按原因分开说：

| 原因 | 打出来的话 | 退出码 |
|---|---|---|
| 本机没有账号凭证 | 本机没有账号凭证，模型用不了 | 4 |
| 余额不足 | 账户余额不足（+ 充值命令） | 4 |
| 连不上模型服务器 | 连不上模型服务器（+ 先确认能打开 aikaui.com） | 1 |
| 模型服务器返回看不懂的内容 | 等一会儿重跑；一直这样请联系我们 | 1 |
| 跑满 40 步还没装完 | Agent 跑满了步数上限还没装完 | 1 |
| 用户按了两次 Ctrl-C | 已中断 | 130 |

**配置自愈**。两份配置里的 `ANTHROPIC_*` 是我们写的，但用户重装 / 升级 Claude Code、
上次写盘中断、服务端轮换 token，都会让它们跟 `~/.claude-agent/identity.json` 对不上。
桌面版**每次启动**、服务器版**每次 `--repair`** 都会对一次账
（[`server/claude_config.py`](server/claude_config.py) 的 `check_and_repair`）：

- **只修自己的**：磁盘上的 `ANTHROPIC_BASE_URL` 与 identity 一致（或一个受管键都没有）
  才算我们的；指向别处的不自动改（那是用户自己的东西），但也**不算配好了** ——
  桌面版会拦下来（见下面「别人的中转」），服务器版 `--repair` 报成失败（退出码 7），
  加 `--force` 才换成我们的；
- **坏文件绝不覆盖**（JSON 解析不了、`env` 不是对象、路径是软链 → 跳过并说明）；
- **没有 diff 就不写**；写之前留一份 `<文件名>.aikaui-bak`；
  `settings.json` 顶层的 `model` 不碰 —— 那是用户在 `/model` 里的选择；
- 桌面版只在**真的补了东西**时往对话区留一行日志，不弹窗。

**`<文件名>.aikaui-bak` 的定义是"装我们之前的样子"**，不是"上一次写入前的样子"：
`apply()`（安装 / 换账号时那次覆盖）和 `check_and_repair()`（自动修复）
都只在备份还不存在时才写它。这样用户原来那套别家中转（连同它自己的 token）
永远留在备份里 —— 管家给的选项写着「换成我们的（原来的会备份，随时能换回）」，
这条承诺就得对后来每一次自动修复都成立。

**别人的中转：拒绝使用，除非换成我们的。** 这个客户端只配合 aikaui 中转工作 ——
终端要是还指着别处，我们既收不到那台机器的用量，也不该继续发它对话额度。
[`server/relay_gate.py`](server/relay_gate.py) 只看两个文件的 `env.ANTHROPIC_BASE_URL`：

| 情况 | 判定 |
|---|---|
| 没写 base / 只写了 token | 不算（还没接别家） |
| 与 identity 的 `api_base` 规范化后相同（带不带 `/v1` 都算） | 我们的 |
| 是别的地址 | **拦** —— 桌面版弹窗只给「换成 aikaui 中转」和「退出」两条路 |
| 文件坏了 / 是软链 / `env` 不是对象 | 判断不了就不拦，交给 `--repair` 报 |

- 「换成 aikaui 中转」= 先备份再覆盖（`apply()`），原配置随时能换回；
- 还没有账号时它顺带注一个（首次运行就是这样），注册失败照实说，不假装切换成功；
- 用户选「退出」，程序直接关，**不会**起 Agent；
- `--recharge` 那条只付钱、不碰终端配置，不拦；
- 服务器版从来只写我们的配置，`--repair` 发现还指着别处就退出 7。

**退出码**。两个程序的码各有各的含义，脚本化部署时靠它判断，别只 grep 输出：

`install.sh`（下载和落地这一段）：

| 码 | 含义 |
|---|---|
| 0 | 装好了 |
| 1 | 拉不到 `latest.json`、下载失败，或清单里缺字段 |
| 2 | 被前置检查拒绝：root、`HOME` 为空或 `/`、缺 tar/awk/curl |
| 3 | 包不对：非 https、大小不符、sha256 不符、tar 读不了、包里有 `../`、包内没有可执行文件 |
| 4 | 装上了但 `--version` 跑不起来（glibc / 架构不兼容） |

`claude-code-assistant`（Agent 装 + 终端对话）：

| 码 | 含义 |
|---|---|
| 0 | 装好了（`--dry-run` / `--balance` / `--recharge` / `--repair` 成功也走这个码） |
| 1 | 没归类的失败：模型连不上、模型服务器返回看不懂的内容、跑满步数还没装完、装完但核验没过 |
| 2 | 参数不对（argparse） |
| 3 | 环境不支持 / `~/.local` 不可写，或没有 TTY 又没给 `--yes` |
| 4 | 账号：注册失败、绑账号失败、本地留着未完成注册的旧账户码却没给 `--new-account`（`--repair` 时是"本机没有账号"） |
| 5 | Node 装不上（镜像挂了、校验失败） |
| 6 | npm 装不上（网络、权限、包缺原生二进制） |
| 7 | 配置写不进 `~/.claude.json` / `~/.claude/settings.json`；`--repair` 遇到坏文件、写入校验失败，或者配置还指向别家中转（加 `--force` 才换成我们的） |
| 130 | 用户在终端里按了两次 Ctrl-C |

★ 5/6/7 现在是 **Agent 调工具时拿到的失败**：它自己会重试、换镜像、问你要不要继续，
这几条只在"它也解决不了、最后收场"时才成为退出码。所以脚本化部署仍然可以按码判断。

装完 `install.sh` 会**直接进向导**，并把向导的退出码原样透传出来 ——
所以 `curl … | sh` 返回 5/6/7 时，问题在向导那一步，照第二张表查。

**没有 TTY 的时候**（CI、`ssh host cmd`、被别的脚本调用）：不阻塞、不假装成功。
`DISPLAY` 在就试着用 `x-terminal-emulator`/`gnome-terminal` 之类把向导开在真终端里，
否则打一行提示 + 写 `~/.claude-agent/cli.log`，退 3。

`--yes` 可以完全绕过这个分支，但**语义变了**：它现在进的是 Agent 模式，
所以要求 ① 这台机器已经有账号（没有的话 `--account-code`/`--account-key` 一起给，
或者先在真终端里跑一次），② 模型服务连得上。两条缺一条就会照实报错退出
（4 / 1），不会退回去走那条老流水线。管道里没有主循环，所以：

- 模型提问时自动选第一项，并打一行 `[自动] 选了：…` 留痕；
- 不会自动进 `claude`（那是给人用的交互界面），只打印 `cd <工作目录> && claude`。

**`install.sh` 认这几个环境变量**（都是给测试和特殊部署用的，正常安装不用管）：
`AIKAUI_SITE`（换站点，默认 `https://aikaui.com`）、`AIKAUI_LIB`（换安装目录）、
`AIKAUI_NO_RUN`（只装不启动）、`AIKAUI_ALLOW_ROOT=1`（1.4.4 起不需要了：root 默认就装，只打一行警告；
留着只为兼容老文档。向导那边照旧传 `--allow-root`，钉老版本时才用得上）。

**root**：容器里常常只有 root，所以不拦。代价是 Node、claude 和配置全落在 `/root` 下，
换个人用不了 —— 警告里直说这一点。

**边界**：一台机器一个账号 —— 同一个 `$HOME` 下的所有用户共用一份凭证，本版不做多租户。

## 打包

Windows / macOS 用 [`agent.spec`](agent.spec)，Linux 用 [`server.spec`](server.spec)
（两份的入口不同：`main.py` / `server_main.py`）。文件名都用 ASCII，CI 和命令行都能直接引用：

```bash
python -m PyInstaller agent.spec --noconfirm --clean     # Windows / macOS
python -m PyInstaller server.spec --noconfirm --clean    # Linux（服务器版）
```

| 平台 | 产物 | 说明 |
|---|---|---|
| Windows | `dist/ClaudeCode安装助手.exe` | 单文件、无控制台；先跑 `python build_tools/make_version_info.py` 才有文件版本信息 |
| macOS | `dist/ClaudeCodeAssistant.app` | 未签名；Info.plist 里声明了 `aikaui://` |
| Linux | `dist/ClaudeCodeAssistant` | 单文件、**有控制台**（服务器版）；发行时和 `agent.png` 一起打 tar.gz |

`.spec` 里的入口只写了 `main.py` / `server_main.py`，PyInstaller 会顺着 import 图自己把
`ui/`、`server/`、`platform_support/` 收进去，不用手工维护模块清单。要手工列的数据文件只有图标
（Windows 的 `agent.ico`、macOS/Linux 的 `agent.png`）和 macOS 的 `agent.icns`。

`server.spec` 另外做了几件和 `agent.spec` 不同的事，改的时候别照抄漏掉：

- **`console=True`**。GUI 版没有控制台是有意的；服务器版没有控制台等于把所有报错吞掉。
- **`excludes` 里点名 `tkinter` / `_tkinter` / `customtkinter` / `PIL` / `Pillow` /
  `ui` / `images` / `deps` / `bus` / `prompts` / `updater` / `notify` /
  `platform_support.windows|macos`**。
  这不是省体积，是**保证界面库进不来** —— `tests/test_imports.py` 会断言
  `import server_main` 之后这些模块（连同 `openai` / `pydantic` / `agent_core`）不在
  `sys.modules` 里，所以"不小心 import 了 `ui.something`"会在测试阶段就红。
  `bus` 和 `prompts` 容易漏：`agent_core` 里**确实写着** `from prompts import …`，
  只是挪进了 `agent_worker()`（服务器版不调它）；`bus` 则是把 `tkinter` 拖进包的那根线。
  排掉它们的前提就是这两句只在函数里 —— 所以 `tests/test_updater_linux_payload.py`
  连"是不是写在函数里"一起断言。
- **模型栈现在进包了**（1.4.6 起服务器版是 Agent 装）。`openai` / `pydantic` /
  `pydantic_core` / `httpx2` / `httpcore2` / `anyio` / `jiter` / `sniffio` 的数据文件和
  子模块都要收，包从 26MB 涨到 **36.5MB**（tar.gz 35.9MB，2026-10-28 量的）。
  涨的空间基本就是模型栈：`openai` + `pydantic` + `httpx2`。注意 **openai 3.x 依赖的是
  `httpx2` / `httpcore2`**，不是 `httpx` / `httpcore` —— 写错名字会静默跳过，
  到用户机器上才 `ModuleNotFoundError`。
  ★ 收这些包用 `_collect()`，**不用 `collect_all()`**：后者默认
  `include_py_files=True`，会把每个包的 `.py` 再作为数据文件放一份（PYZ 里已经有
  字节码了）。桌面版靠这一条从 35.9MB 降到 23.6MB，服务器版同理。改这里时别贪方便
  换回 `collect_all`。
- **`helpers` 不能排除**：`server/term_render.py` 从它取 `norm_options` / `resolve_option`
  （桌面版和终端版的选项语义靠同一个来源对齐）。它只 import `math` / `re`。
- **`hiddenimports` 点名 `agent_bus` / `agent_core` / `llm` / `server.agent_install` /
  `server.agent_tools` / `server.prompts_server` / `server.term_chat` / `server.term_keys` /
  `server.term_render` / `lzma` / `_lzma` / `platform_support.linux`**。这些全是**函数里
  import** 的（`--balance` 不该把模型栈拖起来），静态分析跟不到，漏一个就是运行时崩。
  `lzma` 是解 Node 的 `.tar.xz` 用的（PyInstaller 不会自动收 C 扩展），
  `platform_support.linux` 是 `platform_support/__init__.py` 按平台延迟 import 的。

**体积**（2026-10-28 量的 Windows 单文件）：**23.6MB**，改之前 35.9MB，少了 12.3MB（−34%）。
省下来的是打包方式，功能一个没减，细节和"哪些不能动"写在 [`agent.spec`](agent.spec) 文件头：
不用 `collect_all`（源码会重复进包）、只声明真的用到的 `win32com.client`、排掉已经坏了的
`win10toast → pkg_resources → setuptools` 那条链、去掉 Pillow 用不到的 `_avif` / `_imagingcms`
解码器、`optimize=2`（等价 `python -OO`）。tcl/tk（5.9MB）、libcrypto/libssl（2.4MB）、
openai 的资源模块（5.9MB）都没动 —— 那是拿功能换体积。

想再量一遍，或者怀疑哪个包又变胖了：

```bash
python build_tools/analyze_archive.py dist/ClaudeCode安装助手.exe
```

它按顶层包归总，**`comp MB` 才是能省下来的量**（`uncomp MB` 是解压后的大小）；PYZ 里那
几千个模块压缩是整体的，单列一行，拆不到单个包。

**CI**：打 `v*` tag（或手动触发）时 [`.github/workflows/build.yml`](.github/workflows/build.yml)
在四个 runner 上并行构建（windows-x64 / macos-arm64 / macos-x64 / linux-x64）。矩阵每一行带
`spec:`，Linux 那行指向 `server.spec`；构建完还有一个 **Linux smoke test** 步骤跑
`./dist/ClaudeCodeAssistant --version` 和 `--help` —— 这两条能过就说明没有把 tkinter 拖进来、
`lzma` 也在，省得下载下来才发现跑不起来。

产物有两个去处：**GitHub Release**（打 tag 时 `release` job 挂上去，本机用
`python build_tools/fetch_release.py v1.0.1 --dir release` 取回来 —— 装了 `gh` 的话
`gh release download v1.0.1 --dir release` 等价）和 Actions artifact（保留 30 天）。
**CI 不部署到服务器** —— 见上面「发布新版本」里的分工表和原因；Actions 的摘要页也会
写清楚「线上还没变，部署在本机跑」。

macOS 两个包都**没有签名**：首次打开要右键 →「打开」，
或者终端跑 `xattr -dr com.apple.quarantine /Applications/ClaudeCodeAssistant.app`。

## 自动更新

程序**不弹窗、不打断**：后台静默检查 → 静默下载 → 下次启动时换上新版本。
用量页底部有一行 `v1.0.0`，就是当前跑着的版本。

**客户端怎么工作**（[`updater.py`](updater.py)）

1. 启动 25 秒后起一个 daemon 线程，之后每 6 小时拉一次
   `https://aikaui.com/latest.json`（网络失败就退回上次缓存的清单）。
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

1. 改 `config.py` 里的 `APP_VERSION`，跑一遍 `python -m unittest discover -s tests`，提交。
2. 打**注释** tag（必须与 `APP_VERSION` 一致，CI 会校验），正文写这版改了什么 ——
   **整个正文**会变成 GitHub Release 的说明和 `latest.json` 里的 `notes`，只维护这一份：

   ```bash
   git tag -a v1.0.1 -F - <<'EOF'
   一句话说这版改了什么
   下面几行可以写细节
   EOF
   git push origin v1.0.1
   ```

3. CI 在四个 runner 上并行构建，然后由 `release` job 把四个产物挂到 **GitHub Release** 上。
   **CI 不部署**——见下面那张分工表。Actions 摘要页会写「线上还没变」。
4. 在本机部署。先把产物取回来（本机没装 `gh` CLI，用仓库里的脚本；装了的话
   `gh release download v1.0.1 --dir release` 等价），然后：

   ```bash
   python build_tools/fetch_release.py v1.0.1 --dir release
   DEPLOY_HOST=… DEPLOY_USER=root DEPLOY_SSH_KEY=… \
     NOTES="$(git tag -l --format='%(contents)' v1.0.1)" \
     bash build_tools/deploy_downloads.sh 1.0.1 ./release
   ```

   加 `DRY_RUN=1` 只生成清单、用客户端自己的 updater 验一遍，不碰服务器。

**为什么 CI 不部署：** GitHub runner 到我们那台服务器（49.233.72.225）的上行只有
**~17KB/s** —— 2026-10-28 实测，136MB 传了十几分钟才走 11MB，按那个速度要两小时。
本机到同一台机器约 5MB/s，几十秒就完。所以分工是：

| 谁 | 干什么 | 为什么是它 |
|---|---|---|
| CI | 四个平台的**构建** + 挂 GitHub Release | macOS 的包只能在 macOS runner 上打 |
| 本机 | `deploy_downloads.sh` 推到 nginx + COS | 上行快；而且部署变成显式动作，不会"打个 tag 就改了生产" |

`deploy_downloads.sh` 的**执行顺序**是刻意的：传包到 nginx → 传包到 COS → 两边 HEAD 验大小 →
**才**原子换 `latest.json` → 更新 `install.sh` → 改两个页面的版本号 → 最后以客户端视角再确认一遍。
任何一步没验过就退出，所以清单永远不会指向一个取不到的文件。
中间被打断也安全：线上清单还是旧的，只是多了一个半截的版本目录，删掉重跑即可。

**`releases/winget/<版本>/AppInstaller_x64.msix` 不归发版管**（2026-10-30 加）。
它是客户端「没有 winget 时自动装一个」用的包，版本固定、内容永久不变，已经在
`releases/winget/1.11.430/` 下，发新版本**不用**动它。要换 winget 版本才跑一次
`build_tools/deploy_winget.sh <msix> <版本>`，然后把 `platform_support/windows.py`
里的三个常量（`WINGET_VERSION` / `WINGET_MSIX_SIZE` / `WINGET_MSIX_SHA256`）一起改掉 ——
脚本结尾会把该填的值打出来。忘了改常量的后果是客户端 sha256 校验不过，走下载页兜底。

**发布时有三个容易踩的坑，脚本已经处理好了，但你要知道它在干什么：**

- **同一份包有两个源，两个都要传。** 下载页的 HTML 用相对路径走 `aikaui.com`（nginx 的
  `/var/www/aikaui/releases/`），而 `latest.json` 里给自动更新用的 url 指向 `dl.aikaui.com`
  （腾讯云 COS + CDN）。脚本两边都传、两边都 HEAD 验大小；**任何一边取不到就直接退出，
  不换清单**。只传一边的后果不是报错，而是客户端悄悄 404 —— 只会在 `update.log` 里留一行，
  6 小时后再试一次。
- **下载页里写死了版本号。** `index.html` 和 `download/index.html` 各有 4 条
  `/releases/<版本>/…` 的链接，只换 `latest.json` 的话，新用户从下载页拿到的还是旧包。
  脚本在换完清单之后用正则把这两页的版本号改成新的（备份成 `*.bak-<时间戳>`，
  同目录 `mv` 原子替换）。用正则而不是替换某个固定字符串，所以不用每次改脚本。
- **`install.sh` 和版本号无关，但每次发版都要重传。** 官网那行
  `curl -fsSL https://aikaui.com/install.sh | sh` 拉的是服务器上的一个固定文件，
  它自己去读 `latest.json`，所以没有版本号要改 —— 也正因如此，**改了脚本却忘了传**，
  线上还是老脚本，比发错包更难发现。脚本先 `sh -n` 语法校验，再 `scp` 成 `.tmp` 后 `mv`，
  最后 `curl` 回来 `cmp` 一次字节一致，不一致就退出。

顺序是刻意的：**传包 → 两边都验 → 才换 `latest.json` → 再改下载页**。
清单永远不会指向一个取不到的文件。`DRY_RUN=1` 只生成清单并用客户端自己的
`updater.py` 验一遍（形状、sha256、size、`min_version`、已是最新时不再提示），不连服务器。


> **老用户注意**：`v1.0.0` 是第一个带 updater 的版本。更早安装的版本里面没有更新代码，
> 必须**手动**装一次 `v1.0.0`，之后才能自动更新。

## 目录结构

```
agent_demo/
│
├── main.py                       182   程序入口：先换新版本 → 建窗口 → 别家中转就拦下 → 起线程（含配置自愈）→ 事件循环
├── server_main.py                618   服务器版入口：环境自检 → Node → claude → 账号 → 配置（--repair 只对账，--balance / --recharge 只查账收钱）
│
├── config.py                      98   唯一的常量源：版本、服务器地址、凭证、路径、配色、更新参数、MAX_STEPS
├── updater.py                    660   自动更新：清单校验 / 静默下载 / 下次启动替换自己（不 import tkinter）
├── deps.py                        19   可选依赖探测：Pillow 在不在
├── helpers.py                    240   数字金额格式化 + 用量取数（按模型 / 时间序列）+ 刻度 + 文字宽度估算 + 选项清洗 + 凭证打码
├── images.py                     138   窗口图标（打包资源优先，缺了现画）+ 付款二维码下载
├── notify.py                      26   桌面通知薄封装（各平台实现在 platform_support）
├── bus.py                        112   线程间队列 + poll_queue（每 100ms 把消息翻译成界面操作）
├── llm.py                        126   大模型客户端（走 aikaui.com 代理，用本机账号的 api_key）
├── agent_core.py                 285   Agent 主循环：run_one_task / agent_worker / balance_worker
├── prompts.py                    614   系统提示词（按平台给安装指令 + 工作目录 + VSCode 询问分支 + 余额分寸）
│
├── platform_support/                  三平台差异（调用方只 import 这个包）
│   ├── __init__.py               160   按 sys.platform 延迟加载并转发
│   ├── common.py                 230   safe_cwd（HOME 越界检查）、find_claude、clean_env、跑子进程抓输出、VSCode 值解析
│   ├── windows.py               1148   注册表协议 / .lnk / cmd / WinRT toast / VSCode 探测 + winget 安装（含"没 winget 就装一个"、问 AppX 注册表定位）
│   ├── macos.py                  425   Info.plist 协议 + Apple Event / .command 快捷方式 / Terminal.app / VSCode 探测 + brew 安装
│   └── linux.py                  314   .desktop + xdg-mime / 终端探测 / notify-send / VSCode 探测（安装只开下载页）
│
├── server/                            服务端与工具层（不 import 界面）
│   ├── ui_bridge.py               31   GUI 注入桥：set_ui / get_ui
│   ├── cli.py                    335   服务器版共用的地基：退出码、子进程封装、sha256、安全解包、shell PATH 块、HttpClient
│   ├── console_ui.py             128   终端里的进度实现（实现 ui_bridge 那几个钩子，不阻塞、没 tty 也不卡住）
│   ├── node_setup.py             346   Node 检测 / 选 LTS / 从 npmmirror 下载校验 / 解到 ~/.local/node / 软链命令
│   ├── claude_setup.py           462   npm install -g --prefix ~/.local（清掉继承的 npm_config_*，显式 --include=optional + --allow-scripts；原生包没下来时单独补装，断网自己重试）
│   ├── identity.py               515   设备指纹、PoW 注册、本地凭证、换设备登录、_ensure_account_code / _ensure_user_id、pending_recovery
│   ├── fingerprint.py            147   硬件指纹：主板序列号 + CPU ID → SHA-256 前 32 位（不是凭据，只在注册时上报）
│   ├── vault.py                  213   凭证的"第二份"：账户码存注册表（DPAPI 加密）/ 配置目录
│   ├── pow.py                    128   PoW 求解（进度 / 取消 / 90 秒超时），和服务器 pow.js 逐位对齐
│   ├── api.py                    504   余额 / 用量 / 充值 HTTP 层（永远不抛异常）
│   ├── claude_config.py          495   把身份写进两份配置 + 事后对账自愈（坏文件不覆盖、外家中转不碰）
│   ├── relay_gate.py             132   别家中转的判定：有 base 且不是我们的就拦住（不 import 界面，可离线测）
│   ├── billing_console.py        226   服务器版的措辞层：余额/用量排版、终端二维码、装完能敲的命令（不联网、不 import 界面）
│   ├── tools_fixed.py            148   五个固定工具 + aikaui:// 协议注册（转发 platform_support）
│   ├── tools_general.py          125   bash / read_file / write_file / ask_user（解码和杀进程树在 platform_support.common）
│   ├── tools_vscode.py           246   check_vscode / install_vscode / install_claude_extension / open_vscode（连 Claude 面板一起开）
│   └── tool_specs.py             386   工具说明书：tools[] + tool_map（19 个工具）
│
├── ui/                                界面层
│   ├── fonts.py                  179   全界面字体表：按平台选字体 + 字号常量 + 画布缩放
│   ├── gui.py                     31   class GUI(WindowMixin, OptionsMixin, RechargeMixin, UsageMixin, LoginMixin, AccountMixin, GateMixin)
│   ├── window.py                 521   WindowMixin：窗口壳、左侧导航、对话页、底部询问条、余额回流
│   ├── usage.py                  625   UsageMixin：用量信息页（余额卡 + 两张按模型的时间分布图 + 充值入口）
│   ├── account.py                272   AccountMixin：账户信息页（账户码 / 密钥打码、眼睛、复制、防泄露红字）
│   ├── options.py                219   OptionsMixin：ask_user 选项面板（点击 / ↑↓ / 回车 / Esc）
│   ├── recharge.py               627   RechargeMixin：充值弹窗两页 + 创单 + 付款后刷新 + 只办充值时的靠右定位
│   ├── login.py                  323   LoginMixin：换设备登录弹窗（账户码 + 账户密钥；也承担"本地凭证丢了"时的自动找回）
│   └── gate.py                   156   GateMixin：检测到别家中转时的模态窗（换成我们的 / 退出）
│
├── build_tools/
│   ├── make_version_info.py       80   生成 Windows 文件版本信息（build/version_info.txt）
│   ├── make_manifest.py          113   四个产物 → latest.json（校验版本、算 sha256 / size）
│   ├── fetch_release.py               从 GitHub Release 取回四个产物（本机没有 gh CLI）
│   ├── analyze_archive.py             按顶层包归总产物，量体积时看谁最占地方
│   ├── deploy_downloads.sh            发布：传 nginx + 传 COS → 两边验大小 → 换清单 → 改下载页版本号
│   └── deploy_winget.sh               单独发那份 App Installer（换 winget 版本时才跑）
├── tests/                            离线单元测试（stdlib unittest）
│   ├── test_version.py                版本解析 / 比较 / 清单校验
│   ├── test_download.py               本地假服务器：下载、哈希、404、缓存回退
│   ├── test_apply.py                  备份、回滚、失败上限、锁、更新提示
│   ├── test_pow.py                    PoW 求解（和服务器 pow.js 的测试向量对齐）
│   ├── test_register_flow.py          本地假服务器说真契约：注册、不重注册、不泄密、指纹字段、is_recovered、保险箱
│   ├── test_credentials.py            打码、硬件指纹（占位符 / 非 Windows / 绝不抛异常）、保险箱往返
│   ├── test_ui_messages.py            poll_queue 把 recovery / recovered 送到对的界面方法
│   ├── test_bash_timeout.py           bash 工具的超时与杀干净
│   ├── test_platform_support.py       三平台抽象层
│   ├── test_prompts.py                提示词按平台切换 + VSCode 询问分支的关键句
│   ├── test_vscode.py                 VSCode 探测 / 安装 / 打开（三平台，全程打桩）
│   ├── test_winget.py                 没 winget 时自动装：sha256 校验、依赖包先装、失败报错要能看（HRESULT / 中文不乱码 / 重试一次）、不再解压便携版、问 AppX 注册表
│   ├── test_cancel.py                 「中断」：牌子语义、子进程登记/清理、下载到一半能停
│   ├── test_tools_vscode.py           四个 VSCode 工具的返回值、失败归类、Claude 面板什么时候发链接
│   ├── test_agent_core.py             install_done 什么时候触发（"这一轮真装完了"的判定）
│   ├── test_tool_specs.py             tools[] 和 tool_map 一一对上
│   ├── test_server_cli.py             服务器版：参数、退出码、--dry-run 不落地、无 TTY 不阻塞、不重复注册、密钥不进报错
│   ├── test_billing_console.py        余额/充值措辞：读不懂的余额不编成 0、二维码每行等宽、没装 segno 就退回链接、密钥不出现
│   ├── test_claude_config.py          配置自愈：没 diff 不写、坏文件不覆盖、外家中转不碰、顶层 model 不动、日志不含 token
│   ├── test_relay_gate.py             别家中转一律拦：带不带 /v1 都认得出、坏文件不拦、切换留备份、文字不含 token
│   ├── test_node_setup.py             Node 选版本 / 资产名 / SHASUMS / 校验失败 / PATH 块只写一次
│   ├── test_claude_setup.py           npm argv 精确 / --allow-scripts 带裸包名 / npm_config_* 被清掉 / 失败映射到 6 / 原生包没下来时补装、已下过的不再下一遍、断网重试
│   ├── test_console_ui.py             终端进度条：管道里不打 \r、静默模式、流坏了不抛
│   ├── test_install_sh.py             真跑 sh：假 curl + 真 tar，装到哪、校验失败退几、包里有 ../ 会不会接
│   ├── test_updater_linux_payload.py  server.spec / CI 矩阵 / updater 还认得这个包
│   └── test_imports.py                工具层导入不依赖 Windows；服务器版入口不拖界面
├── packaging/
│   ├── install.sh               322   官网那一行 `curl … | sh` 拉的就是它（POSIX sh，上线前 sh -n 校验）
│   └── claude-code-assistant.desktop   Linux 桌面启动器（服务器版不再发它，留着给手动装桌面版的人）
├── .github/workflows/build.yml        四平台 CI 构建 + tag 时发布
│
├── agent.spec                          PyInstaller 打包配置（Windows / macOS，入口 main.py）
├── server.spec                   105   PyInstaller 打包配置（Linux 服务器版，入口 server_main.py）
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
- 界面层（`ui/`）按职责拆成六个 mixin，拼成 `GUI`；六个 mixin 方法名互不重复，
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
`recharge_methods`、`recharge_qr`、`recharge_paid`、`login_result`、`recovery`、`recovered`、
`ask`、`progress_start`、`pow_progress`、`progress_stop`。其中 `recharge`（服务器说余额不足）
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

**3. 六个 mixin 的方法名不能撞。**
`GUI` 靠 MRO 把六个 mixin 拼起来，谁都不调 `super()`，所以一旦两个 mixin 定义了同名方法，
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
| `~/.claude-agent/identity.json` | 本机凭证：`device_id`、`api_key`、`account_key`、`api_base`、`model`、`fast_model`、`user_id`、`account_code`、`balance`、`fingerprint`（取到时），以及最近一笔充值订单 `last_order`。POSIX 上权限是 `0600` |
| `HKCU\Software\aikaui\ClaudeCodeAssistant` | **凭证的第二份**（只有 `account_code` / `account_key` / `user_id`），值用 DPAPI 加密 —— 只有同一个 Windows 用户、同一台机器解得开。macOS 在 `~/Library/Application Support/aikaui/credential.json`，Linux 在 `~/.config/aikaui/credential.json`（都 `0600`） |
| `~/.claude.json` | Claude Code 的配置，`set_model_config` 往它的 `env` 里写中转地址、token 和各档位模型映射 |
| `~/.claude/settings.json` | Claude Code 的用户级设置，`set_model_config` 会把同一套 `env` 也写进去（优先级更高），并把 `model` 钉成 flash，其它键原样保留 |

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
它现在只是**本机标识**（`bind-account` 用它去重），不再是身份凭证 —— 服务器只按
`api_key` / `account_code` 认人，所以同一个 `device_id` 换不出任何东西。

同一个 `account_code`（形如 `CC-XXXXXX-XXXXXX`）可以换设备找回账号，**「🔐 账户信息」页**把它和
`account_key` 都列出来（默认打码，点眼睛看明文，带复制和红字警告）；「用量信息」页只露前 4 位。
服务端还回一个 `account_key`（64 位 hex，**只回一次**），存在 `identity.json` 里，目前只用来核对
账号归属，接口并不需要它。

> 注册**不是幂等的**：新契约下"注册"= 用 PoW 开一个新账号，所以只要本地有凭证，
> 客户端**一次请求都不会再发**（重复注册会开新号、把余额丢掉）。

### 凭证丢了怎么找回来（两条路，都不靠硬件指纹）

1. **本机凭证被删 / 被清理工具带走**：注册或换设备登录成功时，账户码和账户密钥会另存一份到
   `server/vault.py`（Windows 注册表 + DPAPI）。下次启动发现 `identity.json` 不在了、
   但保险箱里还有凭证，程序**不注册新号**，而是弹出「换设备登录」窗口把两串都预填好，
   由用户点一下「登录」确认 —— 恢复走的是现成的 `POST /api/bind-account`。
   **不替用户自动确认**：共用电脑上前一个人删了凭证，后一个人一开机就被"恢复"进别人的账号，
   等于把余额送给陌生人。
2. **换了机器 / 重装了系统**：只能靠用户自己存下来的账户码 + 账户密钥（这也是「🔐 账户信息」页
   要红字提醒"截图保存"的原因）。

### 换设备登录要两样

服务端 `POST /api/bind-account` 要 `{account_code, account_key, device_id}`：

- **账户码**会露给旁人（截图、口述、页面上露前 4 位），单靠它就把整台机器的余额领走太薄；
- **账户密钥**是 32 字节随机数的 hex，注册时下发一次，服务端只存 `SHA-256`。
  比对时 **trim + 大小写不敏感**（那串 hex 大小写不携带信息，用户手抄容易错），
  用 `timingSafeEqual` 比，不按字节早退。

三种回答分得很清楚，别混着说：

| 服务端 | 含义 | 界面上 |
|---|---|---|
| `404 account_code not found` | 码查不到 | 让用户核对账户码 |
| `403 account_key_required` | 码对、但没给密钥 | 光标跳到密钥框 |
| `403 account_key_invalid` | 密钥不对 | 强调"整串复制，别只抄一段" |

**老账号（2026-10-24 之前注册的）没有密钥**，服务端对它们只认码 —— 那批账号的密钥
从来没下发过，现发一把会让"只知道码的人先登录把真用户锁在外面"。所以客户端**不**把密钥
设成必填，空着照样发，由服务端决定。

### 硬件指纹是什么、不是什么

注册时会把 `SHA-256(主板序列号 | ProcessorId)[:32]` 一起发给服务器（`server/fingerprint.py`，
Windows 上用一次 PowerShell 的 `Get-CimInstance` 读，非 Windows 或取不到就是空串、不发这个字段）。
它**只当反滥用线索**，理由写在那份文件开头：

- 它不保密 —— 本机任何程序都能读到，随手也能往接口里传任意字符串，所以**不能**用它认人；
- `ProcessorId` 对同型号 CPU 是常量（CPUID 特性位 + 型号签名，不是序列号）；
- 不少 OEM 主板 / 虚拟机的序列号是 `Default string`、`To be filled by O.E.M.` 这类占位符 ——
  这些情况一律返回空串，不拿 "`|` + CPU" 去凑一个全网同型号共用的常量。

**服务端目前对指纹只落库、不恢复**：`/api/auto-register` 收下 `fingerprint` 存进
`cc_accounts.fingerprint`（以后能统计"一个指纹建过多少号"），但**从不**据此发账号；
响应里的 `is_recovered` 恒为 `false`。

这是刻意的：指纹不保密，拿它当凭据等于"谁知道这串值谁能领走账号和余额" —— 同型号 CPU
撞号、共用电脑换人，都会把别人的余额送出去。真正能证明"这账号是我的"的，是账户码 +
账户密钥。客户端的恢复提示（`is_recovered: true` → 「检测到您之前已注册过…」）保留着，
服务端哪天改主意接上就能用，现在它不会触发。

## 服务端接口

统一走 `https://aikaui.com`。`X-Agent-Key` **只在注册链路**里发（它打在 exe 里，
是公开的"敲门砖"，不是秘密）；聊天用注册拿到的专属 `api_key`。

| 接口 | 用途 |
|---|---|
| `GET /api/challenge` | 取一个 PoW 挑战（带 `X-Agent-Key`） |
| `POST /api/auto-register` | 提交 `{challenge, nonce, fingerprint?}`，换回账号与专属 `api_key`（`is_recovered` 目前恒为 `false`） |
| `POST /api/bind-account` | 用 `{account_code, account_key, device_id}` 换设备登录（不需要 `X-Agent-Key`；老账号 `account_key` 可省） |
| `GET /api/balance` | 查余额（按 `account_code` 查） |
| `GET /api/usage` | 查 token 消耗 |
| `POST /api/create-order` | 创建充值订单，返回付款二维码 |
| `POST /api/chat/completions` | 助手自己聊天，只认 `Authorization: Bearer <用户 api_key>` |
| `aikaui://recharge?user_id=N` | 自定义协议：支付页付完款靠它把本程序唤起来 |

`POST /api/issue-token` 已经下线（恒返回 410），客户端不再调用。

### 注册：PoW 人机校验

`/api/challenge` 给一个 32 位 hex 的挑战（绑定来源 IP、120 秒过期、只能用一次），
客户端要在本地找一个 `nonce`，使 `SHA256(challenge + nonce)` 的**前 difficulty 个二进制位**
全为 0，再交给 `/api/auto-register`。难度平时是 16，同一出口注册太频繁会抬到 22。

求解在 `server/pow.py`，跑在 Agent 线程里：**能报进度（已算多少次 / 约百分之几）、能取消
（进度条上有个「取消」）、单次 90 秒硬超时**，一次注册最多重来 3 次（每次都重新取挑战）。
失败原因会照实带到界面上：注册关闭、网络太频繁、认不出 IP，三种情况用户该做的事不一样。

提交时可能多带一个 `fingerprint` 字段（见上面「硬件指纹是什么、不是什么」）。服务端还是
老版本、不认识这个字段而整条请求 400 时，客户端会**去掉字段再试一轮** —— 指纹是加分项，
不能因为它把注册搞挂。

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

- **「没 winget 就自动装」这一步，真机上还没成功过一次。**
  能确定的是：包从 CDN 下得下来（实测 58MB / 11 秒 / sha256 对得上）、
  `Add-AppxPackage` 的参数绑定不再报错、分支逻辑全有离线测试。
  用户的机器走到的是 `DeploymentError` / `IOException`（部署进程已排队，然后 IO 失败），
  **这属于 AppX 部署本机故障，不是我们能绕的**：1.5.4 那次的 `ParameterBindingException`
  已经修掉，剩下的这类失败会如实报出来、隔 2 秒换 ASCII 路径再试一次，还不行就退到
  240MB 官方安装包，不会卡住。手上没有一台"干净、没有 App Installer"的机器，
  所以这条路没跑过完整成功一遍。
- **`Add-AppxPackage 返回 1` 的原因查清了**（2026-10-31）。不是缺依赖，是**参数名写错**：
  `Add-AppxPackage` 没有 `-LiteralPath`，写上它 PowerShell 抛 `ParameterBindingException`、
  一句都不执行。10-30 那次的日志只剩「返回 1： and」，所以当时猜成了缺框架包；
  10-31 的日志留了三行，`FullyQualifiedErrorId: NamedParameterNotFound,…AddAppxPackageCommand`
  才把它点破。现在用 `-Path`（带通配符字符时先 `Set-Location -LiteralPath` 再传文件名）。
  1.5.4 之后用户那边的报错换成了 `DeploymentError` / `IOException` ——
  说明参数这关过去了，卡在 AppX 部署本身（本机 `WindowsApps\Deleted` 下有删不掉的残留、
  `ClipSVC` 停着，都是仓库受损的迹象）。
- **便携版 winget 装不了东西**（2026-10-31 确认）。它不是 AppX 包，源索引永远打不开，
  `winget install` 必然 `0x8A15004B`。所以 2026-10-31 起**不再解压便携版**（以前解压完
  打一行「已装好」、下一行就说「读不到软件源」，白等十几秒还自相矛盾）：
  `ensure_winget()` 装不上就返回失败原因，`install_vscode()` 直接走官方安装包。
- **`find_winget` 不再靠猜路径**（2026-10-31）。用户机器上 winget 是好的，我们却报了
  「没有 winget」：PATH 里第一条 `WindowsApps` 属于别的账户（空壳别名），`%USERPROFILE%`
  和实际 profile 目录也可能不是一个（本机 `USERNAME=瞿毅`，profile 是 `C:\Users\QY`）。
  现在最后一道是问 `Get-AppxPackage Microsoft.DesktopAppInstaller` 的 `InstallLocation`。
  这条**在这台机器上验证过**（能准确拿到 1.29.380 的包目录），但它依赖 PowerShell 能起得来；
  起不来就当没有，行为退回以前。
- **新装的 winget 源索引是空的**，2026-10-30 用户在真机上撞到 `0x8A15004B`。
  已经加了 `source update` / `source reset --force` 重试和「直接下微软安装包」那条兜底，
  但**没有在这台机器上复现过**（本机的 winget 本来就是装好的、源索引也在）。
  下次再遇到，日志里会多出「刷新源索引后再试一次」这些行，能直接看出走到哪一步。
- **「中断」不回滚。** 点中断只会停下正在跑的东西，装了一半的 winget / VSCode 还在，
  再跑一次会接着用（`find_winget` / `find_vscode` 都会先探测）。另外被子进程自己
  `start_new_session` 出去的孙进程（极少数）可能杀不干净，`taskkill /T` 能盖住绝大多数。

- **Linux 发的是服务器版，不是桌面版。** 从发这一版起，`linux-x64` 那个包没有图形界面：
  老的 Linux 桌面用户被自动更新换上之后，双击图标（如果他们手动装过 `.desktop`）
  或者在终端里直接跑，看到的都是一个终端向导而不是窗口。这是"一个包只服务一类机器"
  的代价，换来的是服务器上真的能用。要在 Linux 桌面上用界面版，得自己
  `python main.py`（源码）或自己用 `agent.spec` 打包。
- **服务器版一台机器一个账号。** 凭证在 `$HOME` 下，同一个 `$HOME` 的多个用户共用一份；
  不做多租户，也不支持同一台机器上切换多个账户码。
- **三平台功能一致，但细节依赖系统**。Windows 的快捷方式是 `.lnk`、协议写注册表；
  macOS 的协议写在 Info.plist 里、链接靠 pyobjc 收 Apple Event、快捷方式是桌面的
  `Claude Code.command`（双击由 Terminal 执行；脚本会自己补 PATH、出错时停住等按键，
  见 `platform_support/macos.py` 的 `_SHORTCUT_TEMPLATE`）；
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
