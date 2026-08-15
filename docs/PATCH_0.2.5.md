# Patch 0.2.5 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.5
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Source health: each configured source shows status (`ok` / `error` / `stale` / `never` / `disabled`), last fetch time or last error, and a per-source 刷新 button.
- Force refresh: 刷新全部 still collects every enabled source with `force=true`. A source-row button sends `source_id` so only that feed is fetched.
- Dual ranking in the hotspot panel: 热度优先 (independent source count, then recency) and 时间优先 (recency, then source count). The 4-hour window still applies to both. Collect responses include both lists plus `collection.inserted` so the UI can say how many items were newly stored.

## Boundaries

- Heat is still source-count, not engagement. It is not `hotspot-check` statistical significance.
- Stale means last success is older than twice the source interval.
- Single-source refresh still re-ranks the whole window from SQLite; it does not hide other sources' existing items.
- Scheduled publish and article images are unchanged from 0.2.4.

## Verification

- Unit/integration/page-contract tests after this patch.
- Offline self-check and extracted workbench `node --check`.
- Live service restarted; `/api/status` reports `0.2.5` and includes `sources`.

Source snapshot: `archives/autoposting-patch-0.2.5-update-20260813.tar.gz`.
