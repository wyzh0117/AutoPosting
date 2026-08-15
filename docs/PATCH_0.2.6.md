# Patch 0.2.6 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.6
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- All original candidate images (cap 16) are kept on the draft. 1–2 images are usable; more than 4 stay in the picker instead of being discarded.
- Default suggestion: if there are four or fewer candidates, all go to the body and the closest 3:4 image is also cover. If there are more, cover is the closest 3:4 shot and the next three become body images.
- Workbench image picker: local `/api/image-proxy` thumbnails, set cover, add/remove body images, move body order up/down. Selection is saved through `/api/articles/images` and the body credit block is rewritten.
- WeChat live publish uploads the selected cover as `thumb` and embeds only selected body images, still displayed at 3:4. Xiaohongshu/dry-run packages store the v2 asset bundle.

## Boundaries

- Article generation now fetches the source page (browser User-Agent) for `og:image` / in-page images. The picker sits under the title so previews are visible without scrolling the body. Existing empty drafts can use 重新扫描.
- Thumbnails depend on the local proxy being able to fetch the source URL. Hotlink or login-walled images stay blank.
- Cover upload uses the temporary WeChat `media/upload?type=thumb` API. If that fails, the configured `thumb_media_id` is kept.
- Old list-shaped `assets_json` is still read as “first image is cover, all images are body”.
- No AI-generated pictures.

## Verification

- Unit/integration/page-contract tests after this patch.
- Offline self-check and extracted workbench `node --check`.
- Live service restarted; `/api/status` reports `0.2.6`.

Source snapshot: `archives/autoposting-patch-0.2.6-update-20260813.tar.gz`.
