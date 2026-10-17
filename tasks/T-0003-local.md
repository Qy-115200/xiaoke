---
id: T-0003
from: local
to: all
kind: task
re: 
area: server
subject: 代理 /v1/messages：额度 403 → 402 + OSC 8 充值链接
at: 2026-10-17T07:40:56Z
---

【目标】Claude Code 走的 /v1/messages 也要在余额不足时给出可点的充值链接。

背景：客户端 App 的 /api/chat/completions 已经会把额度错误归一成 402 + 中文
message，但 Claude Code（ANTHROPIC_BASE_URL = https://aikaui.com，实际请求
https://aikaui.com/v1/messages）仍然收到上游原样的 403：

  API Error: 403 预扣费额度失败, 用户剩余额度: ＄0.004916, 需要预扣费额度: ＄0.012724

终端里既没有中文说明，也没有链接。用户今天两次踩到。

要做的事（服务端 / 代理层，不是客户端仓库）：

1. 在 /v1/messages（以及同类 Anthropic 入口）上加和 /api/chat/completions
   一样的额度错误判定。上游 code 可能是 insufficient_user_quota /
   pre_consume_token_quota_failed，也可能 code 为空、只有正文
   「预扣费额度失败」「用户额度不足」「余额已用完」——按正文字符串也要命中。

2. 命中后回 HTTP 402，body 用 Anthropic 错误形状，message 里放：

   余额已用完。\x1b]8;;aikaui://recharge?user_id=<该请求的 user_id>\x1b\\点这里充值\x1b]8;;\x1b\\

   OSC 8 必须是真正的 0x1b / 0x07（或 ESC \）字节，别被日志/安全中间件清洗掉
   （68c3 已实测：裸 URL 不可点，OSC 8 可以；403 会走 Claude Code 的鉴权失败
   分支并清洗控制字符，所以必须保持 402）。

3. message 里**不要带账户码明文**（终端回滚缓冲和 Claude Code 会话记录都会留下）。
   user_id 可以带，它只是链接参数。

验收：
- 拿一个余额不足的测试账户，curl 一次 /v1/messages，确认状态码是 402，
  响应体按字节 dump 能看到 1B 5D 38 ... 07。
- 在 Claude Code 终端里实测：错误行出现可点的「点这里充值」，点开能唤起
  本机助手的充值窗（aikaui:// 协议客户端侧已经就绪，commit d91c620）。

补充：这个改动只动服务端；客户端不需要发版。
