# Patch 0.4.0 Report

Captured: 2026-08-15 CST
Package version after patch: 0.4.0
Runtime: Python 3.11 standard library and SQLite (no new dependencies)

## Implemented

Workbench 增加独立「找切口」步骤。选中一条候选后可点；点「生成文章」不会偷偷搜索。

- 规则抽点（中文至少 4 字，另收试点班 / 医学院 / 计划 / 通道；「4+4 试点班」保持一条）再可选文章模型短 JSON 补点；模型失败只用规则。
- 标题主词降权不删除。最多送搜 8 个、上卡 8 张。
- 每条现场搜 `{短语} 争议`，复用现有 `search_web`。单条失败不影响其他条。
- 过滤必须同时满足：结果含该短语（或紧缩形式）且含争议信号（争议 / 舆论 / 风波 / 质疑 / 热议 / 丑闻）。
- 卡片多选，点选顺序即优先级：第一张主切口，其余须进开头。列表和勾选只挂当前新闻、当前浏览器会话。
- `POST /api/hooks/find` 返回卡；`POST /api/articles` 可选 `chosen_hooks`。勾选后标题以主切口起笔，开头写完所有勾选切口，旧争议写成「公开报道曾关注…」。
- 不点找切口、或找了但一张未勾：生成路径与 0.3.4 相同。

## Boundaries

- 不改 `FactList` 的构建与校验。检索页只当素材，不分配 `source_id`。
- 不自动改写已生成稿；换新闻或再点「找切口」才清空勾选。
- 不建热搜库、不加向量检索、不重抓整站、不打冲击分。
- 小红书仍只生成本地包；微信 live 仍需显式 `live: true`。

## Verification

- 193 unit/integration/page-contract tests, including hook extract/search/filter, `/api/hooks/find`, `chosen_hooks` on generate, workbench 「找切口」 contract, and version lockstep `0.4.0`.
- Offline self-check `ok`.
- Live `GET /api/status` reports `version=0.4.0`; index badge `v0.4.0 · 本地服务`.
- Browser acceptance for the new step is `docs/WEB_FUNCTIONAL_TEST.md` §7b.

Source snapshot: `archives/autoposting-patch-0.4.0-update-20260815.tar.gz`.
