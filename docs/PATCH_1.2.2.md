# AutoPosting 1.2.2

## 变更

Skill 查找器按七步工作流重梳，并修查找按钮与等待条观感。

1. 流水线：理解需求 → 还原生产链 → 分解工作流 → 按模块生成关键词 → 用关键词查找 skill → 按模块分组 → 组合成 end-to-end workflow。快照带 `goal` / `chain` / `groups` / `progress`（第 n/7 步）。网络预算改为最多 80 次 / 240 秒（软上限，不凑满 5 分钟，也不在 45 秒砍掉整次查找）。
2. 查找中持续心跳：步骤名 + `正在检索：模块 · 关键词` + 已用时间，用来区分干活和卡死。停止按钮仍中断。
3. 「查找」两字同一行、40px、不裁切。
4. 全局等待条去掉滑动 `.wait-meter`，改卡片呼吸光；停止/AbortController 行为不变，且不挡其它按钮。

## 边界

- 重启仍会丢掉写作风格。
- 拆覆盖地图和写介绍文仍走文章模型，可能各占数十秒。
- 微信 live 仍停在 draft/add。

## 验证

- unittest 356 passed。
- 重启后 `GET /api/status` version 1.2.2。
- 浏览器：徽章 `v1.2.2 · 本地服务`；「查找」40×76px、`white-space:nowrap`；等待卡无 `.wait-meter`，文案 `第 1/7 步 · 理解需求 · 正在把输入收成可执行目标`；点停止变为「已停止」。

Source snapshot: `archives/autoposting-patch-1.2.2-update-20260914.tar.gz`.
