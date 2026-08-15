# AutoPosting Agent Rules

This directory is an independent project. Do not mix work with sibling MiniWork folders.

## Runtime

- Python 3.11 standard library and SQLite only. No third-party runtime dependencies.
- Tests import sibling modules: `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`.
- Offline self-check: `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`.
- Web service binds `127.0.0.1:8080`. Restart with `ServiceController` in `service_control.py`. Restart drops in-process WeChat credentials and writing style; model endpoints in SQLite and keychain survive.

## Version bumps

When the package version changes, update all of: `pyproject.toml`, `src/autoposting/__init__.py`, `src/autoposting/web.py` (`APP_VERSION` and the status badge), `src/autoposting/llm.py` (`HTTP_USER_AGENT`), and version assertions in tests.

## Article prompting

All article-model instructions live in `docs/ARTICLE_PROMPT.md`. Edit that file to change how drafts are written. Do not put new 调教 copy in `llm.py`. Settings → 写作风格 is an extra overlay only.

## Functional acceptance

Surface checks are not enough. After any task — this one and every later one — keep going until the requested behavior actually happens.

- A button that can be clicked, a CSS class that exists, or a unit assertion that only looks at markup is not acceptance.
- Prove the function: collect really inserts the official source; a longer article really lengthens the panel; a refresh really leaves candidates visible.
- Use the closest live path (browser, local service, real feed or a faithful fixture). If a step fails the *behavior*, fix it and re-check. Do not stop at “the control is there.”

## After every patch

Record and archive the update in this folder before declaring the patch done. Do this even if a previous note said 0.2.x skips `docs/PATCH_*.md`.

1. Write `docs/PATCH_{version}.md` with what changed, boundaries, and verification (tests, self-check, service version, browser checks).
2. Create `archives/autoposting-patch-{version}-update-{YYYYMMDD}.tar.gz` plus a sibling `.sha256`.
3. Include only `src/`, `tests/`, `docs/`, `config/`, `README.md`, `AGENTS.md`, `pyproject.toml`, `service_control.py`, and `AutoPosting服务控制.command`.
4. Exclude `data/`, `output/`, `archives/` (do not nest old tarballs), `.git/`, caches, `__pycache__`, `.venv/`, `.env`, and secrets.

Published article snapshots belong in `output/archives/` and are not a substitute for the patch archive.

## Safety

- Never log or return API keys, AppSecret, or access tokens.
- Xiaohongshu stays manual-package only. WeChat live publish requires explicit `live: true` and in-memory credentials.
