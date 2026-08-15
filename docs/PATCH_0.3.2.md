# Patch 0.3.2 Report

Captured: 2026-08-14 CST
Package version after patch: 0.3.2
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Article-model 调教 is a single document: `docs/ARTICLE_PROMPT.md`.
- `src/autoposting/article_prompt.py` loads that file on each generate. Edit the markdown, then generate again; do not edit prompt strings in `llm.py`.
- Settings → 写作风格 remains an overlay via `{style_block}`.

## How to change the voice later

1. Open `docs/ARTICLE_PROMPT.md`.
2. Change `## 共用`, `## 微信`, or `## 小红书`.
3. If the contract changes, bump `version:` so old cache keys do not reuse stale drafts.
4. Restart is not required; the next 「生成文章」 rereads the file.

## Verification

- Tests load the real repo document and assert generate_article’s prompt contains its sections.
- Editing a stand-in document changes the built prompt.
- Existing article-v3 wording is unchanged.

Source snapshot: `archives/autoposting-patch-0.3.2-update-20260814.tar.gz`.
