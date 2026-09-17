# PATCH 1.4.2 — 原文配图和周榜 / Skill 海报改成上一张下一张

版本：`1.4.1` → `1.4.2`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **热点候选原文配图**：`#image-picker` 不再上下滚动铺开全部缩略图，一次只显示一张；用「上一张 / 下一张」切换，计数为 `n / 总数`。封面 / 正文勾选和正文图上移下移仍作用在当前这张。
2. **GitHub 周榜生成图**：`#github-gallery` 从三列网格改成同一套轮播；生成过程中自动切到正在出的那张。
3. **Skill 查找器生成图**：`#skills-gallery` 同样改成上一张 / 下一张；出图轮询时跟到最新有图或正在生成的一张。
4. 点击图片仍打开原来的放大预览（lightbox 上一张 / 下一张保留）。

## 边界

- 只有一张图时导航按钮隐藏。
- 全选 / 导出仍针对全部图片，不只当前这一张。
- 小红书九宫格卡片仍是三列网格，未改。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 606 通过，1 跳过。
- 合同测试 `test_patch_142_image_carousels` 覆盖三处独立的 prev / next id、共享 `clampCarouselIndex` / `paintCarouselNav`，以及画廊不再用三列网格。
- 本地服务 `GET /api/status` → `version=1.4.2`。
- 浏览器：GitHub 周榜 `1 / 11` 点下一张到 `2 / 11`（cover → 01-i-have-adhd），再上一张回到封面，始终只显示 1 张；Skill 查找器 `1 / 9` → `2 / 9`（cover → 01-absurdity）同样只显示 1 张；热点原文配图注入 3 张后 `1 / 3` → `2 / 3` → `1 / 3`。

源码快照：`archives/autoposting-patch-1.4.2-update-20260917.tar.gz`（同名 `.sha256`）。
