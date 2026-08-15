# Patch 0.1.3 Report

Captured: 2026-08-12 CST
Package version after patch: 0.1.3
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- AI failures now expose a stable top-level error, a lower-level `error_code`, and a safe `error_reason`; prompts, stderr, and credentials are not returned.
- Model, provider, and effort are configurable in Settings and may be overridden per article request. They are included in cache keys and draft audit metadata.
- The workbench is two columns on desktop and one column at 900px or below. The article title/body are editable in a scrolling region.
- Publish moved into the article panel. Save, model selection, and publish are right-aligned, with model selection immediately left of publish.
- WeChat and Xiaohongshu account settings live in one Settings dialog. Credentials remain process-memory only.
- Saving changed article content revokes prior approval; unsaved browser edits block publish. Platform drafts use independent approval events.
- Model output and manual edits enforce title limits, AIGC disclosure, and exact source-ID boundaries.
- JSON request bodies are capped at 1 MiB. WeChat live submissions are serialized in-process and reuse submitted attempts.

## Verification

- Unit/integration/page-contract tests: **110 passed**.
- Offline self-check: **10/10 checks passed**.
- `compileall`: passed.
- Embedded JavaScript syntax: `node --check` passed.
- Browser acceptance on a deterministic local fixture:
  - desktop 1280x633: two columns, no horizontal overflow;
  - Settings defaults, account tabs, scrollable content, and fixed actions verified;
  - collection, Xiaohongshu generation, edit dirty-state, save, approval, and local package generation completed;
  - article scroll region measured `overflow-y: auto` with content taller than its viewport;
  - mobile 390x844: one 366px column, no horizontal overflow or control overlap.
- No paid model or real social-platform request was made during final browser acceptance.

## Deliberate Boundaries

- Xiaohongshu remains manual publication through its official creator center.
- Settings are not persisted across service restarts, so secrets are never written to this application's database.
- No private API, simulated login, Cookie automation, or new runtime dependency was added.