---
id: T-0004
from: local
to: all
kind: task
re: 
area: platform_support/
subject: 没 winget 的机器上自动装 winget 再装 VSCode
at: 2026-10-30T06:07:41Z
---

用户 2026-10-30 反馈：装 VSCode 走到一半报「这台机器上没有 winget」，流程断在下载页。改成没 winget 时先自动装：从 aikaui.com CDN 下微软官方 App Installer 的 x64 msix（58MB，sha256 钉在代码里），先 Add-AppxPackage，失败退便携版解压即用。客户端 + 发布 1.5.0 一起做，local 自己来。
