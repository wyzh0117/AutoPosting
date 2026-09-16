# AutoPosting 1.0.15

## What changed

四个说明板块（做什么 / 如何实现 / 适合哪些人群 / 使用场景）收成同一个 `bullet_panel`：同一套字体和卡片样式。去掉 featured 大字号和固定 36% 高度。

框高按内容撑开：字少则矮，字多则高。主体 `c-project-shell` 垂直居中，空出来的高度分到上下，避免头重脚轻。并排的两列各自按内容高度，不再被拉成等高空框。

## Boundaries

- 四个板块仍是四个标题 + 四段文案；只精简组件和占位，不合并文案。
- 小红书 9:16 手写纸卡片未改。

## Verification

- unittest 291 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.15.
- Example project-detail PNG hugs content and sits mid-canvas.

Source snapshot: `archives/autoposting-patch-1.0.15-update-20260905.tar.gz`.
