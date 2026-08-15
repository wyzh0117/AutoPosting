# Patch 0.2.8 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.8
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Every generated article is framed in code: header `【信息来源 N 条 · 原文 M 篇】`, footer original links. Community/frontline items also get a 讨论提示 and the model is told to write 「某某称」.
- S4 is no longer a publish block. Forum or frontline items can become a post. HN comment/score and X public metrics can promote that *item* from S4 to S3 attention; the source’s base grade stays S4.
- Heat ranking now uses independent source count, then public discussion weight, then recency.
- X: official user-timeline collector. Bearer token lives in process memory (settings → 账号 → X). S4 watch accounts can be enabled without identity verification; S1/S2 still need it. No HTML scrape.

## Thresholds

- HN: score ≥ 50 or comments ≥ 30
- X: likes+reposts+replies ≥ 200, or reposts ≥ 50
- Reddit shape is reserved (score ≥ 100 or comments ≥ 50) but no Reddit source is shipped yet

## Boundaries

- Promotion is per item, not a permanent upgrade of the whole HN/X source.
- Without an X Bearer token, enabled X sources fail with `x_bearer_token_required`.
- Header/footer are added after the model returns, so counts and links do not depend on the model remembering them.

## Follow-up fix

- Homepage was empty until「刷新全部」finished a full 13-feed + translation round (~60s). A failed refresh also wiped the list.
- `GET /api/hotspots` now lists the current window from SQLite on page load and when the hour range changes. Refresh no longer clears existing cards on error.
- Live collection of independent feeds runs in parallel.

## Verification

- Unit/integration/page-contract tests after this patch.
- Extracted workbench `node --check`.
- Live service restarted; `/api/status` reports `0.2.8`.

Source snapshot: `archives/autoposting-patch-0.2.8-update-20260813.tar.gz`.
