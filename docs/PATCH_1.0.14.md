# AutoPosting 1.0.14

## What changed

- 「做什么？」高度约为原来一半。其下「如何实现？」与「适合哪些人群」并排，再下是通栏「使用场景」。
- 没有独立副标题时不再显示副标题，避免和「一句话看懂」重复。
- 「一句话看懂」改为「身份 + 情景 + 功能」，不再用「它可以帮你…」套话。榜单 prompt 在 `docs/image_prompts/copy.md`。

## Boundaries

- 小红书文章卡片（`xhs_cards.py` 的 9:16 手写纸）未改。
- 抓取周榜后才会用新的一句话句式；只生成图片时，旧 snapshot 里的「它…」会被 KIND 模板替换。

## Verification

- unittest 291 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.14.
- Example project-detail PNG 1080×1440 with the new stack.

Source snapshot: `archives/autoposting-patch-1.0.14-update-20260905.tar.gz`.
