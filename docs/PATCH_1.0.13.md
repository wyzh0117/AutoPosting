# AutoPosting 1.0.13

## What changed

周榜海报画布改为竖版 **1080×1440（3:4）**。内容组件不变：一句话看懂、做什么？、如何实现？、适合哪些人群、使用场景。

排版：上方 masthead（排名、标题、一句话看懂、logo），中间大面积「做什么？」，底部三列次要信息。榜单封面改回单列。画廊缩略图 3:4。小红书手写纸仍是 9:16。

## Boundaries

- 抓取和出图仍是分开的 API。
- 图像模型出图比例未改。

## Verification

- unittest 291 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.13.
- Example PNGs are 1080×1440.

Source snapshot: `archives/autoposting-patch-1.0.13-update-20260905.tar.gz`.
