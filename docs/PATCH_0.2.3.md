# Patch 0.2.3 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.3
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Clicking 发布 now auto-saves unsaved title/body, then approves and publishes the saved draft.
- Publish responses include `archive_path`. The final edited article is written atomically to `output/archives/{draft_id}-{platform}-{content_hash}.json` (`article-archive-v1`) on Xiaohongshu, dry-run, and WeChat live paths.
- Same-row workbench controls share a 40px height, `inline-flex` centering, and zero vertical padding so text and icons share a centerline.
- Settings fieldset rows use `align-items:flex-end` and `flex-wrap:nowrap` so model/effort/action controls stay on one line.
- Input height rules come after the general text-field padding rule so 40px fields keep vertically centered text.

## Boundaries

- WeChat live publish still requires in-memory credentials and explicit `live: true`. Restarting the 8080 service drops WeChat credentials and writing style; model endpoints in SQLite and the keychain remain.
- Xiaohongshu still returns `manual_required` and a local package. The article archive is not a substitute for the official creator-center submit.
- Patch snapshots belong in `archives/`. Published article snapshots belong in `output/archives/`.

## Verification

- Unit/integration/page-contract tests: 126 passed.
- Offline self-check: previously 10/10; code path unchanged after the CSS cascade/nowrap follow-up.
- `python3 -m compileall -q src tests` and extracted `<script>` `node --check` passed.
- Live service restarted; `GET /api/status` reports `version=0.2.3`.
- Browser: desktop same-row control center delta 0px; settings fieldset rows 0px after nowrap; chat input computed padding-top/bottom `0px` and height `40px`; no page console errors. Mobile keeps existing wrap of header/actions; controls that remain on one row stay aligned.

Source snapshot: `archives/autoposting-patch-0.2.3-update-20260813.tar.gz`.
