# Patch 0.2.7 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.7
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Click a preview thumbnail to open a full-size lightbox. Previous/next, backdrop, the large image, Close, and Escape all dismiss or step through candidates. The image is still fetched through `/api/image-proxy`.
- Default collection copies AIHOT’s *method*, not its aggregate: official and specialist RSS feeds are first-party sources; heat is still independent-source confirmation. `aihot_aggregate` stays in the registry but is disabled so existing databases stop pulling second-hand summaries.
- Mixed feeds (IT之家, Hacker News) use `content_filter: "ai"` so phone/gadget items do not flood the hotspot list. Official AI blogs are unfiltered.
- RSS fetch uses the same browser User-Agent as page/image fetch.

## Assessment of AIHOT

AIHOT scores and clusters items from official blogs, media RSS, X accounts, and WeChat accounts. Its public page/API/RSS are already edited second-hand copies (`links.aihot` + generated summaries). Using that stream would lock later ranking, filtering, and source expansion to their choices.

Not copied in this patch: X (identity gate unchanged), WeChat official accounts (no public RSS), AIHOT’s LLM scoring, or buzzing.cc translations.

## Boundaries

- Lightbox needs the local image proxy; hotlink/login-walled originals still fail.
- Keyword filter is conservative word-boundary matching, not AIHOT’s LLM scorer.
- Anthropic / xAI / Cursor news pages have no public RSS and are not scraped.
- Old AIHOT items already in SQLite remain until they age out of the window.

## Verification

- Unit/integration/page-contract tests after this patch, including lightbox HTML and first-party registry checks.
- Extracted workbench `node --check`.
- Live service restarted; `/api/status` reports `0.2.7`.

Source snapshot: `archives/autoposting-patch-0.2.7-update-20260813.tar.gz`.
