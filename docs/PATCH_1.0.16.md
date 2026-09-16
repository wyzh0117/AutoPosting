# AutoPosting 1.0.16

## What changed

四大板块（做什么 / 如何实现 / 适合哪些人群 / 使用场景）不再用「它……」排比。KIND 回退文案改成动作起笔；榜单 prompt `docs/image_prompts/copy.md` 禁止以「它」或「这个工具」作主语。旧 snapshot 里仍以「它」开头的 does 会在出图时丢掉，改走 KIND 模板。

每条 bullet 前的图标扩成约 20 个手绘 SVG（终端、网页、芯片、检索、交接、边界等），按关键词挑选；对不上词的条目用 seed 轮换，避免整列同一个方块。

## Boundaries

- 小红书 9:16 手写纸卡片未改。
- 新的模型文案要重新「抓取周榜」；只「生成图片」会替换旧的「它……」回退句，但不会重新调用榜单模型。

## Verification

- unittest 295 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.16.
- Example project-detail PNG：正文无「它」起句；11 个 bullet 图标 8 种不重复。

Source snapshot: `archives/autoposting-patch-1.0.16-update-20260905.tar.gz`.
