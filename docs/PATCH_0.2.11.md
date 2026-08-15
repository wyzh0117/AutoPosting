# Patch 0.2.11 Report

Captured: 2026-08-14 CST
Package version after patch: 0.2.11
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Collapsed left rail is a fixed workbench-height panel. `#hotspot-list` scrolls inside `#hotspot-pane`; many cards no longer lengthen the page.
- Clicking `点开看来源` adds `sources-open` on `#hotspots-panel`. The panel then grows so every source-health row is visible. Collapsing restores the fixed list + scrollbar.
- Right rail no longer stretches to match a tall left column. With no article, chat sits just under `#article-model`. `renderArticle` inserts the draft above chat and pushes the chat block down.
- Desktop `main` grid is `1fr 2fr` (about one third / two thirds).

## Boundaries

- Mobile (≤900px) still stacks one column; the candidate list uses a 50vh inner scroll.
- Collect, ranking, DeepSeek sitemap, model APIs, publish, and X are unchanged.

## Verification

- 167 unit/integration/page-contract tests, including `test_patch_0211_fixed_hotspot_scroll_sources_open_chat_flow_and_third`.
- Live service started twice; `/api/status` reported `0.2.11` both times.
- Browser on `127.0.0.1:8080`: left column 32.8% of 1400px main; collapsed list 374px vs 3201px of cards with `overflow-y: auto`; opening sources grew the left panel 789 → 1419px and showed 18 rows; empty-state chat was 68px under the article model picker; after `renderArticle` chat moved 388 → 1337px and stayed below the article block.

Source snapshot: `archives/autoposting-patch-0.2.11-update-20260814.tar.gz`.
