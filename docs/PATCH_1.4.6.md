# PATCH 1.4.6 — 相关新闻选用、查找按名去重、只重出封面

版本：`1.4.5` → `1.4.6`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **热点相关新闻可选用**：新闻补充卡片有勾选按钮。点「生成文章」时，已选热点条目与勾选的相关新闻一并进入 LLM 提示（`related_items` / `related_news`）。
2. **Skill 查找按名字去重**：点击「查找」收包时，检索到的 skill 按名字（忽略大小写）只留一条，热度更高的留下。手动粘贴的 skill 不因重名被删。
3. **只重新生成封面海报**：出图完成后可改标题框，再点「只重新生成封面海报」。只重画封面、不跑榜单改写、不删已有内容卡。

## 未改（诊断）

Skill 出图报 `榜单改写超过 25 秒未返回` **不是**因为没配模型。该句来自 `LEADERBOARD_ENRICH_SECONDS = 25` 的 `llm_timeout`。未配置模型会立刻返回 `LLM_ENRICHMENT_FAILED: model not configured`。当前 8080 的榜单模型是已配置的 `qwen3.8-max-0902`。根因是一次把全部 skill 丢进 enrich，25 秒内没有返回。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 633 通过，1 跳过。

源码快照：`archives/autoposting-patch-1.4.6-update-20260918.tar.gz`（同名 `.sha256`）。
