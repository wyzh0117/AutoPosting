# Patch 0.3.3 Report

Captured: 2026-08-14 CST
Package version after patch: 0.3.3
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

1. Candidate photos live in one inset panel (`#image-picker`). `#image-picker-list` has a fixed max height and its own scrollbar (`overscroll-behavior: contain`). Twelve photos scroll inside the panel; the page height does not grow with the photo count.
2. The left hotspot list defaults to exactly two candidate cards (`fitHotspotList`). Extra cards stay in the list scroller. Opening 点开看来源 still grows the panel to show every source row; the list stays two cards tall.
3. Workbench restyle: warm ink / ivory / antique brass. Serif wordmark, pill controls, no emoji. One motion system (`--ease`, `--t-fast` / `--t-mid` / `--t-slow`) on buttons, cards, dialogs, and list enter. Date/number fields use the same dark surface.

## Boundaries

- Collect, ranking, models, publish, X, and `docs/ARTICLE_PROMPT.md` are unchanged.
- Mobile (≤900px) still stacks one column; the candidate list is capped at 50vh and still fits about two cards.
- Chinese originals no longer repeat a duplicate 「中文（AI翻译）」block; English cards still show the translation slot.

## Verification

- 176 unit/integration/page-contract tests, including `test_patch_033_image_panel_two_hotspots_and_motion`. Offline self-check `ok`.
- Live `/api/status` reports `0.3.3`.
- Browser on `127.0.0.1:8080` (1454×782): list locked at 398px with two full cards in view; inner list scroll left `pageTop` at 0; 12 injected photos, list `scrollHeight` 1355 vs 255 visible, page height unchanged after inner scroll; sources-open 724→1336px with 18 rows and list still 398px; settings dialog plays `rise`; no emoji. Mobile 390×844: stacked column, two cards, image panel inner scroll.

Source snapshot: `archives/autoposting-patch-0.3.3-update-20260814.tar.gz`.
