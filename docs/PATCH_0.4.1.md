# Patch 0.4.1 Report

Captured: 2026-08-24 CST  
Package version after patch: 0.4.1  
Runtime: Python 3.11 standard library and SQLite (no new runtime dependencies)

## Implemented

- Initialized Graft 0.10.1 for this repository only with `--agents agents --no-global`; the regenerable `graft/` cache remains ignored.
- Fixed the model-switch timeout at its source. The home-page switch no longer carries a previous model's hidden reasoning effort into the new model, and DashScope Qwen3.8 receives an explicit `reasoning_effort: none` instead of falling back to its xhigh default.
- Kept explicitly configured reasoning levels unchanged; non-DashScope OpenAI-compatible endpoints retain the previous request shape.
- Shortened the article and chat model selectors to the model ID only. Endpoint URLs remain visible in Settings.
- Replaced the one-line chat input with a three-row textarea. It grows with content up to 40% of the viewport, then scrolls internally; Enter sends, Shift+Enter inserts a newline, and IME composition is preserved.
- Bumped all package, service badge, HTTP user-agent, and version assertions to 0.4.1.

## Boundaries

- The 90-second timeout was not increased: the observed configuration was `qwen3.8-max + effort=max`, which DashScope maps to xhigh. Waiting longer would preserve the excessive hidden reasoning work rather than fix the inherited setting.
- Streaming support was not added. It remains a future option for users who deliberately select deep reasoning for long generations.
- The pre-existing user edit to `docs/ARTICLE_PROMPT.md` (article-v6) was preserved unchanged by this task; only its stale version/text assertions were aligned so the repository test suite reflects the live prompt.
- No article content was sent to an external model during acceptance. The full article path was verified with the project's faithful local model fixture because approval to send a concrete hotspot to DashScope was not granted.
- Xiaohongshu remains manual-package only. WeChat live publishing still requires explicit `live: true` and in-memory credentials.

## Verification

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`: 197 tests passed.
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`: `ok: true` with all ten checks true.
- Live service restarted through `ServiceController`; `/api/status` and the page badge report 0.4.1.
- Browser acceptance with ego-browser:
  - article/chat selectors showed `qwen3.8-max` with no URL;
  - an actual home-page model change returned `模型已切换` and persisted article effort as `none`;
  - the chat textarea started at about 86 px, grew to about 366 px for 30 lines with a 40vh cap and internal scrolling, then returned to about 84 px after clearing.
- Faithful HTTP/model fixtures covered article creation, cache/idempotency, request error mapping, model settings, and persistence without external content disclosure.
- Independent subagent review completed; hostname boundary and textarea border-box findings were corrected.
- `graft check .`: 744 nodes, 1,949 edges, 37 cards; graph in sync.
- `git diff --check`: clean.

Source snapshot: `archives/autoposting-patch-0.4.1-update-20260824.tar.gz`.
