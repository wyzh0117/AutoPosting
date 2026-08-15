# Patch 0.1.2 Report

Captured: 2026-08-12 CST
Package version after patch: 0.1.2
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## What Was Implemented

### Local workbench

- `autoposting web` serves a local-only single-page workbench on `127.0.0.1`.
- RSS collection is shown as **热点候选**, not as a statistical hotspot: selected windows are 6, 24, 72, 168, or 720 hours; candidates are forced-refresh RSS items, ordered by observed time with source-count tiebreaking and capped at 10.
- Each candidate keeps the original title/summary and shows a separately labeled AI Chinese translation.
- A selected candidate can generate a Chinese WeChat or Xiaohongshu article.
- The interface supports empty, loading, error, article, approval, configuration, dry-run, and manual-publication states.
- Desktop uses three columns; viewports at or below 900px use one column.

### LLM integration

- Production calls the local `hermes` binary with DeepSeek V4 Flash:
  `hermes -z PROMPT -m deepseek-v4-flash --provider deepseek --reasoning none --ignore-rules -t todo`.
- English candidate translation is one batched JSON call; Chinese candidates are copied locally.
- WeChat and Xiaohongshu use different Chinese article prompts and retain source IDs plus an `AIGC` disclosure.
- Strict JSON parsing rejects malformed, non-zero, timed-out, oversized, or structurally invalid model output.
- `llm_cache` stores translation/article results by task, platform, prompt version, content, and source set. Cache hits skip the model; cache entries are revalidated before use.
- Model failures degrade collection to the original text; article failures return stable `article_generation_failed` without writing a draft.

### Evidence and approval workflow

- Registered-source and S4-only evidence gates run before article generation.
- Human approval remains required before packaging or publishing.
- WeChat live publishing is explicit and uses the official token -> draft -> submit sequence only after in-memory configuration.
- Xiaohongshu always produces a local package and official creator-center link for manual submission.
- Credentials stay in process memory and are not written to SQLite, packages, responses, or logs.

## Correctness Fixes Completed

- Re-saving identical facts no longer downgrades an approved or rejected event.
- Re-generating identical content preserves the current approval state.
- Source-set changes invalidate article cache reuse.
- Translation prompt output shape now explicitly includes `item_id`, matching validation.
- Historical processing-version drafts cannot be approved, rejected, packaged, or published after a newer platform draft exists.
- Event approval counts only the active processing version.
- Repeated WeChat live requests reuse a recorded `SUBMITTED` reference instead of submitting twice.
- All fixes have regression tests.

## Verification Evidence

- Unit/integration/page-contract tests: **100 passed**.
- Offline self-check: **10/10 checks passed**.
- SQLite `PRAGMA integrity_check`: **ok**.
- `compileall`: passed.
- Ruff: passed.
- Real DeepSeek V4 Flash path: translation returned Chinese; article returned Chinese title/body containing the current source ID and `AIGC`; identical second request hit cache and returned the same result.
- Local Web API health check: version `0.1.2`, model `deepseek-v4-flash`, WeChat unconfigured, Xiaohongshu manual mode.
- Browser acceptance with ego-browser:
  - desktop 1470x919: three columns, no horizontal overflow;
  - collection rendered two deterministic candidates, including a two-source candidate and bilingual content;
  - selection and Xiaohongshu article generation displayed the Chinese draft and source IDs;
  - approval generated a local Xiaohongshu package and showed the official manual-publication boundary;
  - mobile 390x844: one column, 366px sections, no horizontal overflow, no overlap, and no clipped buttons.
- No request was sent to a real WeChat or Xiaohongshu account during verification.

## Archive Contents

The 0.1.2 archive contains source, tests, configuration examples, README, and project documentation. It excludes `.env`, local databases, generated packages, caches, virtual environments, compiled bytecode, and all existing archives.

Archive: `archives/autoposting-patch-0.1.2-update-20260812.tar.gz`

## Explicitly Not Implemented

- No real Xiaohongshu API or automated browser submission.
- No automatic approval, fact invention, image acquisition, or live publishing without explicit platform configuration and request.
- No X collector or private platform API.
- No third-party runtime dependency.
