# AutoPosting 1.2.8

## 变更

- GitHub weekly poster variant 保留 `template-poster-a/b` 的新版深蓝几何底板，但详情 body 改为复用稳定 `project_detail()`：BrandHeader、Masthead、QuoteCard「一句话看懂」、四个 bullet panel 与 Footer。
- poster 画布统一恢复为 1080×1440（3:4）；cover 的新版 smart-cover 映射保持不变。
- poster wrapper 仅在 `template-poster-a/b.page-project` 上恢复 token safe-area（上 56、右 48、下 52、左 48）；shell 维持零内边距，保留 16px 纵向与 12px pair 横向 gap。
- 增加详情海报功能回归测试，锁定四 panel、动态字段、clamp 以及不再输出旧 geometric poster panel-less body。
- 版本从 1.2.7 提升到 1.2.8，并同步包版本、运行时版本、状态徽标、HTTP User-Agent 与测试断言。

## 边界

- 未修改 schema、prompt、adapter、rules 或运行时依赖；AI 字段仍由 `project_from_repo()` 与 `apply_project_rules()` 进入旧详情组件。
- 未改变 cover 视觉、API 路径、job id、文件名或 URL 契约；旧 `geometric_poster()` 仍保留给非 weekly 直接调用者。

## 验证

- 定向测试：`PYTHONPATH=src:tests python3 -m unittest test_visual test_web test_llm -v`，200/200 通过；safe-area 修正后 `PYTHONPATH=src:tests python3 -m unittest test_visual test_skill_finder -v`，66/66 通过。
- 全量测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`，400/400 通过。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`，`ok: true`，10 项检查通过。
- `graft build` 完成，1484 nodes / 3748 edges。
- Chrome-backed `screenshot_html` safe-area fixture：`/tmp/autoposting-weekly-safe-HV8XpX`。PNG 为 1080×1440；Chrome `getBoundingClientRect()` 实测 page 1080×1440、shell x=49/right=1031，四 panel 均 x=49/right=1031（含 1px 外框），pair 横向 gap 12px、两处纵向 gap 16px，未重叠。详情 HTML 含几何底板、project shell、四个 bullet panel、五类动态内容，未输出旧 poster body；多字段 fixture `/tmp/autoposting-weekly-multi-PSxg8U/01-full-fields.png` 也验证规则上限内条目完整出现。PNG 已目视检查。

## 归档

- `archives/autoposting-patch-1.2.8-update-20260916.tar.gz` 已重打并排除 `docs/.Rhistory`、`docs/article_styles/.Rhistory`、所有 `.Rhistory`、`__pycache__`、`*.pyc`、`data/`、`output/` 与旧 archives。
- SHA-256 已写入同名 `.tar.gz.sha256` sidecar（避免把自引用摘要写入归档内容）。
