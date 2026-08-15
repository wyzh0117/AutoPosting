# Patch 0.2.10 Report

Captured: 2026-08-14 CST
Package version after patch: 0.2.10
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Both workbench panels keep a 0.2.9-sized floor (`main` min-height `calc(100dvh - 8.1rem)`). They no longer clip to the viewport. Extra hotspot cards, an expanded source list, a long article, or chat output lengthen the page; the two panels stay the same height as whichever side is taller.
- Article body grows with its text instead of staying in a 260px inner scroller.
- New first-party source `deepseek_official` (S1). DeepSeek has no public RSS; the collector reads the official docs sitemap and the static `/news/news*` pages. Live pull returned eight official posts, newest `DeepSeek-V4-Pro GA Release`.
- Existing databases are migrated so `sources.type` accepts `sitemap`.
- `AGENTS.md` now requires functional acceptance on every later task, not “the button clicked.”

## Boundaries

- Official DeepSeek dates come from the news slug (`news260813` → 2026-08-13). Older official posts will not sit in a 4-hour window.
- At most eight official news pages are fetched per collect.
- WeChat live publish and X API were not part of this patch.

## Verification

- 165+ unit/integration/page-contract tests, including sitemap fixtures and a legacy-DB migration.
- Live `SitemapNewsCollector` against `https://api-docs.deepseek.com/sitemap.xml` inserted 8 official items, source status `ok`.
- Browser: badge `v0.2.10`; DeepSeek Official in the source list as 抓取成功; empty-enough panels stayed above the 0.2.9 floor; a long article grew both panels from 4356px to 6009px.

Source snapshot: `archives/autoposting-patch-0.2.10-update-20260814.tar.gz`.
