# agent_demo —— 两个 Agent 协作须知

这个仓库由**两个 Agent** 共同维护：`local`（用户的 Windows 电脑）和 `remote`（用户的远端服务器）。
它们**不共享记忆**，靠 git 说话。所以下面这几条不是建议，是**这个仓库的工作方式**。

接口规约在 [collab/PROTOCOL.md](collab/PROTOCOL.md)，命令实现在 [collab/collab.py](collab/collab.py)——
**开工前先读 PROTOCOL 的第 1、3、4 节**。远端那一侧另有一份
[collab/远端Agent接入提示词.md](collab/远端Agent接入提示词.md)。

## 一、先弄清"记忆"这件事

- **会话上下文是本地的、临时的。** 你这轮记得的东西，对端看不到；会话一关，你自己也没了。
  git 不改变这一点。
- **git 给的是"共享的外存"，不是"共享的记忆"。** 写进去 ≠ 对方知道。对方必须**读**才知道。
- 所以关键不是"怎么共享记忆"，而是**"怎么让'读'不依赖谁记不记得"**。
  答案在下面第二节——装在 hook 上，不装在自觉上。

## 二、每次开工自动看一眼 git（已配好）

`python collab/collab.py digest` 会吐几行当前态势，它的 stdout 会被 hook 直接塞进上下文。
两个事件分工：

| 事件 | 什么时候跑 | 干什么 |
|---|---|---|
| `SessionStart` | 每次开会话 | `digest --long --fetch-max-age 0`：一定问一次远端，带上最近几条消息 |
| `UserPromptSubmit` | **每次你开口** | `digest --fetch-max-age 180 --timeout 6`：180 秒内只问一次，单次 6 秒硬超时 |

装（每台机器各跑一次，写的是 `settings.local.json`，不进 git）：

```bash
python collab/collab.py hooks            # 先看要写什么
python collab/collab.py hooks --write    # 真的装
```

**装完要人工确认一次。** 项目里的 hook 会被 Claude Code 当成"要执行的东西"拦下来让你点同意——
那是防「克隆个仓库就被执行任意命令」的保护，别绕过去。

**没装 hook 也不能忘。** 没有 hook 的环境里，就靠自觉：手工跑 `poll` / `status`。
下面第三节是兜底，第四节是纪律。

## 三、手工开工流程（hook 没装时用这个）

```bash
python collab/collab.py poll      # 有没有新消息、新任务
python collab/collab.py status    # 谁在做什么、有没有人在等我回复
```

1. **认领**：`claim T-0001`。打印「✔ 认领到」才算数；「✖ 没抢到」是正常结果，
   **换个任务，不要重试抢同一个**。
2. **开分支**：`<自己>/<task-id>-<短描述>`，例如 `remote/t-0001-balance`。
3. **干活**：改、自己跑测试、commit、push 自己的分支。**永远不推 `main`**。
4. **说一声**：拿不准就 `ask`；改完就 `done T-0001 -m "改了啥、在哪个分支、怎么验的"`。
   发现对方该知道的事就 `send`。
5. **交接**：要下线用 `handoff --to <对方> -m "做到哪、还剩什么、坑在哪"`。

## 四、硬规矩

1. **收到 `ask` 必须 `answer`**，哪怕答案是"不知道"或"这个不该我动"。别人在等你。
2. **只增不改**。协作分支上推上去的消息不再改、不删（撤回自己的 claim 除外，脚本会处理）。
3. **只写自己的目录**：`log/<自己的名字>/` 和 `tasks/T-xxxx-<自己的名字>.md`。
4. **不推 `main`、不推 `collab`**。代码推自己的分支；协作分支交给 `collab.py`。
5. **一个任务一个分支**，别混两件事。
6. **别抢别人手上的活**。`status` 里「对端在做」的任务不要碰。要动别人地盘先 `ask`。
7. **离线不要装在线**。脚本说"只是先记在本地"就是还没生效，别当成认领成功；
   联网后 `sync` 会自动补推，抢输了也会自动撤回。
8. **密钥不进任何会提交的文件**。协作分支两边可见、永久留在 git 历史里。
9. **报告要实**。测试没过就说没过、贴输出；跳过了哪一步就说跳过了。

## 五、必须停下来问人的事

- 要改 `main`、要合并分支、要动发布线；
- 要装系统级的东西、要动这台机器上**已经跑着的服务**的配置或数据；
- 要删东西、`force push`、重写历史；
- 要花钱、申请账号、碰别人的仓库。

## 六、这个仓库本身

`Claude Code 安装助手`：桌面应用（Python 3.12 + customtkinter，深色主题），
Windows / macOS 功能一致（**都没有托盘，关窗即退出**）。
**Linux 发的是服务器版**（`server_main.py`，纯终端、不 import tkinter）。1.4.6 起它和桌面版
**跑同一套 Agent 循环**（`agent_core.run_one_task`），只是界面换成了终端对话：
干活时不能打字，说完了打字回车发送，有选项时 ↑↓ 选、回车确认；装完自动进 `claude`。
唯一的确定性环节是**账号引导**（`server/agent_install.py`）——模型客户端本身就要
`identity.json` 才能说话。`--dry-run` 仍然只打印那条参考路线，不联网、不调模型。
官网用 `curl -fsSL https://aikaui.com/install.sh | sh` 装。
桌面版和服务器版是两个入口、两份 spec，别把界面代码漏进 `server_main.py` ——
`tests/test_imports.py` 会拦（模型栈是**允许**进包的，界面库不行）。
PyInstaller 打包：Windows 单文件 exe、macOS `.app`、Linux 单文件（`server.spec`，有控制台）。
平台差异全在 `platform_support/`，其它模块不直接 import 平台 API。
自动更新在 `updater.py`：查 `https://aikaui.com/latest.json` → 后台静默下载 →
**下次启动**替换自己。它不 import tkinter，替换失败绝不阻塞启动；发布流程见 README「自动更新」。
目录结构、架构、线程模型、已知限制**都写在 [README.md](README.md) 里**——那是主文档，别再另起一份。

要跑的验证：
- 界面 / 逻辑：`python main.py`（Windows / macOS）
- 服务器版：`python server_main.py --help`、`--dry-run`（确认只打印计划、不落地；
  非 Linux 上 `--dry-run` 会以退出码 3 结束，这是对的）
- 离线单元测试：`python -m unittest discover -s tests`（不联网、不需要 GUI）
- 新对话里文件和线程的出场顺序见 [文件执行或调用顺序.md](文件执行或调用顺序.md)
  （第一节到第六节是 GUI 版，第七节是服务器版）
- 打包：`python -m PyInstaller agent.spec --noconfirm --clean`（Win/macOS）
  或 `server.spec`（Linux），产物在 `dist/`
  （Windows 先跑 `python build_tools/make_version_info.py`；CI 见 `.github/workflows/build.yml`）
- `sh -n packaging/install.sh`：改过安装脚本先验语法；发布时 `deploy_downloads.sh` 会再验一遍
- 协作链路自测：`python collab/collab.py digest`（应该几行内返回，且退出码 0）

改协作接口时守一条：**`collab.py` 两边跑的是同一个文件**，改完要保证两台机器上的行为一致，
不能依赖某台机器的特有路径或 shell。
