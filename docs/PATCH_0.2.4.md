# Patch 0.2.4 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.4
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Source-image attach: RSS enclosure/media/HTML plus optional article-page `og:image`/`img` extraction. Selects up to 4 originals (prefer 3:4), never AI-generated. Credits are appended to the draft body; `assets` persist in SQLite.
- WeChat live publish uploads those originals via `media/uploadimg` and injects 3:4 `object-fit:cover` figures with source captions. Xiaohongshu/dry-run packages already carry `assets`.
- Hotspot window default is now 4 hours. Presets are 4/6/24/72/168/720 plus a custom integer in 1–720.
- Scheduled publish: datetime-local + 定时发布. Jobs live in SQLite `scheduled_jobs` and fire from the running web process every 15 seconds.
- Model settings follow a Hermes-style flow: one Base URL + API Key, connect and list models, then 用于文章 / 用于对话. Saved endpoints appear as cards with 选用/删除.

## Boundaries

- Page fetch for extra images only happens when the feed yields fewer than 3 URLs and a page fetcher is available. Failed probes are skipped, not fatal.
- WeChat still needs `thumb_media_id` for the official cover; body images are additional fills.
- Scheduled jobs survive restart in SQLite, but the 8080 process must be running to fire them. Restart still drops in-memory WeChat credentials.
- Xiaohongshu remains manual package submit.

## Verification

- Unit/integration/page-contract tests after this patch.
- Offline self-check and `node --check` on the extracted workbench script.
- Live service restarted; `/api/status` reports `0.2.4`.

Source snapshot: `archives/autoposting-patch-0.2.4-update-20260813.tar.gz`.
