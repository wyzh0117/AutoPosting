# Patch 0.2.9 Report

Captured: 2026-08-13 CST
Package version after patch: 0.2.9
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

- Homepage candidates were hidden behind the source-health list. Seventeen rows of「正常 / 刷新」filled the left rail; news cards sat below `overflow: hidden`. The left rail now shows topic cards first. Source health is a collapsed footer.
- Labels:「抓取成功」means that subscription’s last RSS pull succeeded.「只刷这个」re-fetches one feed. These are not news titles.
-「刷新全部」keeps existing cards, immediately reloads the SQLite window if the list is empty, and no longer waits for translation after the feeds return. An empty 4-hour window automatically widens.
- `https://api.deepseek.com` is stored as `https://api.deepseek.com/v1`.
- 「翻译标题」asks the configured model to fill Chinese titles without re-fetching feeds.
- Full click-through checklist: `docs/WEB_FUNCTIONAL_TEST.md`. Local DeepSeek test credentials are in that file only.

## Boundaries

- Refresh still takes as long as the slowest live feed (about 20s per timed-out source). The list is no longer blank while that happens.
- GET `/api/hotspots` stays cache-only unless `translate=1`.
- X Bearer and WeChat live publish are unchanged and were not exercised in the 0.2.9 walkthrough.

## Verification

- 163 unit/integration/page-contract tests.
- Extracted workbench `node --check`.
- Walked `docs/WEB_FUNCTIONAL_TEST.md` in the browser except live publish and X API:
  - Home showed 10 cards above collapsed source health.
  - 刷新全部 kept the 10 cards and reported 新入库 3 条.
  - DeepSeek connect listed `deepseek-v4-flash` / `deepseek-v4-pro`; article, chat, WeChat draft, Xiaohongshu draft, save, schedule, and hard refresh all passed.
  - 「翻译标题」filled Chinese titles for the English cards.
- Live service restarted; `/api/status` reports `0.2.9`.

Source snapshot: `archives/autoposting-patch-0.2.9-update-20260813.tar.gz`.
