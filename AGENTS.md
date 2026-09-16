# AutoPosting Agent Rules

This directory is an independent project. Do not mix work with sibling MiniWork folders.

## Runtime

- Python 3.11 standard library and SQLite only. No third-party runtime dependencies.
- Tests import sibling modules: `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`.
- Offline self-check: `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`.
- Web service binds `127.0.0.1:8080`. Restart with `ServiceController` in `service_control.py`. Restart drops writing style. Model endpoints, WeChat/X/Xiaohongshu credentials, and model API keys survive in SQLite and the macOS keychain.

## Version bumps

When the package version changes, update all of: `pyproject.toml`, `src/autoposting/__init__.py`, `src/autoposting/web.py` (`APP_VERSION` and the status badge), `src/autoposting/llm.py` (`HTTP_USER_AGENT`), and version assertions in tests.

## Article prompting

Article-model instructions live in `docs/ARTICLE_BASE_PROMPT.md` and `docs/article_styles/` named files (`AI专家.md`, `经验分享.md`). Option values are `AI专家` and `经验分享`. The base file owns shared facts, output, priority, and platform rules; the style files own the selectable writing types. Do not put new 调教 copy in `llm.py`. Settings → 写作风格 is the highest-priority prompt overlay.

## Image prompting

GitHub weekly **model-image** prompts live in `docs/image_prompts/`: `STYLE.md` (asset style bible), `cover.md`, `poster.md`. Placeholders: `{style}`, `{ranking}`, `{name}`, `{rank}`, `{rank02}`, `{summary}`, `{does}`, `{scene}`, `{kind}`. Jobs are built from these files before any image API call. Do not put new 出图调教 copy in `image_gen.py`.

The HTML visual publishing system (ranking-overview / project-detail) also lives under `docs/image_prompts/` (design-system, components, page-schemas, content-schemas, rendering) and `src/autoposting/visual/`. Workbench GitHub weekly: fetch is `POST /api/github-weekly`, render is `POST /api/github-weekly/render` with `theme` and `palette` (main/secondary/accent). Edit tokens and schemas there; do not stuff page layout back into `STYLE.md`. Original STYLE text is `docs/image_prompts/legacy/STYLE.original.md`.

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
- Xiaohongshu stays manual-package only. WeChat live (`live: true`) writes the official draft box; it does not call freepublish. Account credentials persist in the keychain until deleted.

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
