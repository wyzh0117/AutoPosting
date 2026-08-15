# Patch 0.3.1 Report

Captured: 2026-08-14 CST
Package version after patch: 0.3.1
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Why phone ads showed up

The 24-hour window after the last refresh was dominated by `ithome` (37 of 80 rows). Examples that should never have been AI candidates:

- OPPO Enco Clip2 耳夹耳机限时优惠价
- 小米米家扫拖机器人京东 1510 元 / 以旧换新
- 三星 Galaxy 头戴耳机、传音 TECNO 手机、大疆 AI 超级夜景 3299 元起
- 五菱缤果 / 小米汽车电池 / 比亚迪秦 MAX 等车稿
- 「IT早报」把一天杂讯捆在一条里

`content_filter: "ai"` 以前在**整篇正文**里找 `AI`。IT之家页脚、配件功能（AI 降噪、AI 夜景）和早报里提到的 DeepSeek/OpenAI 都会误伤。这是混合科技站 + 过宽词表的问题，不是广告源被单独订阅。

## Filter change

- Title must itself be an AI topic.
- Drop 早报/晚报, 开售/优惠价/元起, 耳机手机汽车零售稿, and “AI 夜景/一键成片” feature-speak.
- The same title filter is applied when listing candidates, so already-ingested junk leaves the workbench immediately.

## Article context

Before drafting, the workbench:

1. Reads the item and, if possible, the original page.
2. Pulls related recent rows from SQLite (e.g. DeepSeek Harness on HN plus IT之家公测稿).
3. Searches DuckDuckGo HTML for recent coverage.

The model is told to write for a reader who missed yesterday: 前因与脉络, what actually happened, 「某某称」, and one-line glosses for niche jargon only. Prompt version is `article-v3`.

## Verification

- 172 unit/integration tests (filter, DDG/Wikipedia parse, local related search, list-window hiding, article-v3 prompt).
- Live `/api/hotspots?hours=24` after restart: no 耳机优惠、扫拖京东价、IT早报、汽车万元起. Remaining rows are model/lab/funding AI stories.
- HN item titled only `DeepSeek Harness` with summary `Comments` now gathers local related copy, including IT之家「Harness 公测」and Decoder 的 V4 Pro / agent 报道, which is the missing context for yesterday’s empty draft.

Source snapshot: `archives/autoposting-patch-0.3.1-update-20260814.tar.gz`.
