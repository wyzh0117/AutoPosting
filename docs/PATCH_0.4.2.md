# Patch 0.4.2 Report

Date: 2026-08-24  
Package version after patch: 0.4.2

## Changes

- Added a generation-style selector immediately before “找切口”, with options 1–7.
- Added seven independently replaceable placeholder prompt fragments in `docs/article_styles/`.
- Passed the selected style through `/api/articles` into the shared article prompt and included its file content in the LLM cache digest.
- Added “打开原文” to the right of “时间优先”. It opens an in-page reader for the selected candidate, renders stored source text with `textContent`, and exposes allowlisted HTTP(S) source links.
- Preserved the selected candidate across ranking redraws, cleared it when absent, and made closing during an in-flight original request immediately restore the button.
- Bumped package, service badge, HTTP user-agent, and version assertions to 0.4.2.

## Boundaries

- `docs/ARTICLE_PROMPT.md` remains the shared article instruction source and was not changed by this patch; numbered files are style overlays only.
- The numbered prompt files currently contain only their placeholder digits. Replace their contents when the real style prompts are ready.
- The original reader uses the text already collected into SQLite. It does not re-fetch arbitrary feed URLs, avoiding a new SSRF surface; an external original-site link remains available.
- Selecting another style regenerates the same item/platform draft rather than retaining seven parallel drafts.
- Existing Settings → 写作风格 remains an additional free-text overlay.

## Verification

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`: 202 tests passed.
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`: all checks passed.
- `git diff --check` and Python bytecode compilation passed.
- Restarted through `ServiceController`; `/api/status` reports healthy service version 0.4.2 with `qwen3.8-max` and effort `none`.
- Real Chromium check with `ego-browser` confirmed:
  - style 7 remains selected and is positioned left of “找切口”;
  - “打开原文” is positioned right of “时间优先” and is enabled only after choosing a candidate;
  - the reader opened with the selected title, a 496-character stored source text, and a safe external source link;
  - ranking switches preserve exactly one visible selection;
  - closing during a delayed original request immediately restores “打开原文” and it remains usable after the request settles.
- API regression checks confirm `javascript:`, `data:`, and other non-HTTP(S) source links are excluded and stored HTML-like text is returned for safe `textContent` rendering.

## Archive

Source snapshot: `archives/autoposting-patch-0.4.2-update-20260824.tar.gz`.
