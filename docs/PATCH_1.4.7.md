# PATCH 1.4.7 — 榜单改写超时改为 3 分钟

版本：`1.4.6` → `1.4.7`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

Skill / 周榜海报 enrich 的截止时间从 25 秒改为 **3 分钟**（`LEADERBOARD_ENRICH_SECONDS = 180`）。超时文案为「榜单改写超过 3 分钟未返回」。HTTP 调用在 enrich 期间也会等到同一时限，避免仍被文章通道的 90 秒截断。

## 边界

- 文章生成、翻译仍使用 90 秒 `LLM_TIMEOUT_SECONDS`。
- 未配置模型仍立即 `LLM_ENRICHMENT_FAILED: model not configured`。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 634 通过，1 跳过。

源码快照：`archives/autoposting-patch-1.4.7-update-20260918.tar.gz`（同名 `.sha256`）。
