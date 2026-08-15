---
name: autoposting
description: Operational rules and workflow for the AutoPosting project — a local-first Python 3.11 + SQLite AI news-automation MVP. Covers test/self-check commands, service control, the version-bump checklist, where article-prompting lives, functional-acceptance and patch-archive requirements, and safety boundaries. Load this skill before changing code, running the service, bumping the version, or recording a patch in this repository.
whenToUse: When working inside the AutoPosting repository — running tests or self-check, starting/restarting the web service, changing the package version, editing article prompts, or writing patch notes/archives.
---

# AutoPosting workflow

Authoritative details live in `README.md` and `AGENTS.md`; this skill is the distilled operational checklist.

## Runtime

- Python 3.11 standard library + SQLite only. **No third-party runtime dependencies.**
- Run tests: `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`
- Offline self-check: `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
- Web service binds `127.0.0.1:8080` (or `localhost`). Start it directly with:
  `PYTHONPATH=src python3 -m autoposting.cli web --db data/autoposting.db --sources config/sources.example.json --port 8080`
- Restart with `ServiceController` in `service_control.py` (macOS Tk GUI, one start/stop button).
- **Restart drops in-process WeChat credentials and writing style.** Model endpoints in SQLite and the macOS keychain survive.

## Version bumps (all five places, in lockstep)

When the package version changes, update ALL of:
1. `pyproject.toml`
2. `src/autoposting/__init__.py`
3. `src/autoposting/web.py` — `APP_VERSION` and the status badge
4. `src/autoposting/llm.py` — `HTTP_USER_AGENT`
5. version assertions in `tests/`

## Article prompting

All article-model instructions live in `docs/ARTICLE_PROMPT.md`. Edit **only that file** to change how drafts are written. Do not put new 调教 copy in `llm.py`. Settings → 写作风格 is an extra overlay only.

## Functional acceptance

Surface checks are not enough — a clickable button or a CSS class is not acceptance.
- Prove the behavior: collect really inserts the official source; a longer article really lengthens the panel; a refresh really leaves candidates visible.
- Use the closest live path (real browser, local service at `http://127.0.0.1:8080/`, real feed, or a faithful fixture).
- For browser verification, prefer the `ego-browser` skill.

## After every patch (always, even if a note says otherwise)

1. Write `docs/PATCH_{version}.md` — what changed, boundaries, verification (tests, self-check, service version, browser checks).
2. Create `archives/autoposting-patch-{version}-update-{YYYYMMDD}.tar.gz` plus a sibling `.sha256`.
3. Archive ONLY: `src/`, `tests/`, `docs/`, `config/`, `README.md`, `AGENTS.md`, `pyproject.toml`, `service_control.py`, `AutoPosting服务控制.command`.
4. Exclude `data/`, `output/`, `archives/` (no nested old tarballs), `.git/`, caches, `__pycache__`, `.venv/`, `.env`, and secrets.
5. Published article snapshots go in `output/archives/` — they are NOT a substitute for the patch archive.

## Safety

- Never log or return API keys, AppSecret, or access tokens.
- Xiaohongshu stays manual-package only. WeChat live publish requires explicit `live: true` plus in-memory credentials.
