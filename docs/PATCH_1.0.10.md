# AutoPosting 1.0.10

## What changed

Regenerating GitHub weekly posters no longer keeps the previous layout on screen, and English GitHub descriptions no longer occupy the 「做什么？」 panel.

Root cause was not a missing page template. `project_detail` already assembled the 1.0.9 blocks (一句话看懂 / 做什么？ / 如何实现？ / 适合哪些人群 / 使用场景). Two later links were not joined:

1. Gallery images reuse the same `/api/generated/github-weekly/{id}.png` URL. The handler sent `Cache-Control: private, max-age=300`, so the browser kept showing the previous PNG after overwrite.
2. `fallback_copy` / `project_from_repo` treated English `does` as valid bullets, so KIND_DOES Chinese fallback never ran. 生成图片 only re-renders the snapshot; it does not re-fetch or re-enrich copy.

## Boundaries

- Fetch weekly ranking and render images remain separate APIs.
- Leaderboard LLM is still the only path for per-repo rewritten copy. When it is not configured, posters use vernacular KIND_* fallbacks instead of English fragments.
- Xiaohongshu stays manual-package. WeChat live still requires `live: true`.
- Stdlib + SQLite only.

## Verification

- unittest 287 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.10.
- Regenerated `01-archify` after restart: API URL includes `?v=`, `Cache-Control: no-store`, 「做什么？」 is Chinese KIND_DOES bullets. Browser lightbox loaded that URL and showed the new five-block layout.

Source snapshot: `archives/autoposting-patch-1.0.10-update-20260904.tar.gz`.
