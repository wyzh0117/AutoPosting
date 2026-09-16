# AutoPosting 1.0.17

## What changed

海报四大板块里，「适合哪些人群」和「使用场景」不再把多条挤在同一行用空格分隔；每句单独一行。入库时保留换行，不再把换行收成空格后再截断。

这两栏标题前各放一个图标（人群、地点），框内条目不再跟图标。做什么 / 如何实现 仍保留条目图标。

海报文案不再自己加省略号。已经截成「……」的残句在出图时丢掉，改走完整回退句。榜单 prompt 明确：一条一行，禁止用空格拼条，禁止用省略号截断。

## Boundaries

- 小红书 9:16 手写纸卡片未改。
- 已入库、被空格拼在一起的旧文案，点「生成图片」会按汉字空格拆成多行；被省略号截断的残句会丢掉。要让模型按新规则重写，仍需先「抓取周榜」。

## Verification

- unittest 300 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.17.
- Example project-detail PNG：适合哪些人群 / 使用场景标题有图标、条目无图标、一句一行、无省略号。

Source snapshot: `archives/autoposting-patch-1.0.17-update-20260905.tar.gz`.
