# Patch 0.1.4 Report

Captured: 2026-08-12 CST
Package version after patch: 0.1.4
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Added an AIHOT-style aggregate page collector using the existing `web` source type. It extracts item IDs, titles, summaries, source labels, and canonical item URLs from server-rendered pages.
- Added `https://aihot.virxact.com/` to the example source registry while retaining direct RSS sources and provenance deduplication.
- Removed generated and manually edited article title-length limits from prompts, backend validation, and browser inputs.
- Replaced the ineffective provider selector with OpenAI-compatible Base URL, API Key, and model settings. Configured requests now go directly to `/chat/completions`.
- API keys remain in process memory, are sent only in the Bearer header, and are never returned by status APIs or written to SQLite.
- Added Model, Writing Style, and Account settings pages. Writing style accepts direct text or local `.txt`/`.md` import.
- Saved style text is injected into every article-generation prompt and included in the article cache digest.

## Boundaries

- Model, key, account, and style settings remain process-memory only and reset when the service restarts.
- Plain HTTP model endpoints are accepted only for localhost; remote endpoints must use HTTPS.
- The aggregate collector depends on the public server-rendered semantic classes used by the reference page. Parser tests cover the expected contract.