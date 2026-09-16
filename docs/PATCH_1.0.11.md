# AutoPosting 1.0.11

## What changed

- 「做什么？」文案从 `llm.py` 抽到 `docs/image_prompts/copy.md`。榜单模型按完整句、递进逻辑改写 does；未配置模型时 KIND_DOES 回退同样用完整句。
- 周榜海报画布改为 **1920×1080（16:9）**。详情页左侧是一句话看懂 / 如何实现 / 人群 / 场景，右侧大面积「做什么？」。榜单封面改为两列。
- 小红书手写纸卡片仍是 9:16，未改。

## Boundaries

- 抓取周榜和生成图片仍是分开的 API。新 does 规则要在「抓取周榜」且配置了榜单模型时才会进 snapshot；只点生成图片会用已有文案或中文 KIND_DOES 回退。
- 图像模型出图比例仍按原 9:16 设置，与 HTML 海报画布无关。

## Verification

- unittest 288 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.11.
- Example and live posters are 1920×1080; 「做什么？」uses complete Chinese sentences.

Source snapshot: `archives/autoposting-patch-1.0.11-update-20260904.tar.gz`.
