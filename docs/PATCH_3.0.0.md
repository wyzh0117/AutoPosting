# PATCH 3.0.0 — P0/P1 可观测性与外部契约

版本基线：`3.0.0`。本版是 `PROJECT_READABILITY_REMEDIATION_PLAN_2026-09-19.md` 的第一阶段产品交付，范围收敛为 P0/P1 工作包 WP-01～WP-07；其中 WP-06 本版只交付严格 API 序列化和现有边界校验，不声称完成 platform/status/lane/phase 等全局值域集中化。该值域集中化工作后置到 `3.0.1+`；WP-08～WP-16 的大规模模块拆分、重复管线合并、legacy/规则治理也后置到 `3.0.1+`，不作为本版完成条件；本记录不声称附件中的全部工作包已完成。

## 本版交付

- Scheduler 异常可观测、运行中任务保护、过期任务对账和失败终态闭合；publisher 结果采用严格 JSON 边界。
- Web 未知 500、采集/GitHub/Reader/图片/Skill/WeChat 降级统一为稳定、脱敏、可关联的诊断；不返回 secret、token、Authorization、请求 body 或原始敏感 URL。
- GitHub Weekly、Skill Finder、favorite、scheduled result 持久化 JSON 增加 envelope/schema version；损坏、错误形状、合法空值和真实无结果保持可区分，兼容旧数据读取。
- SQLite 使用 `PRAGMA user_version` 的事务迁移序列；迁移失败不推进版本，重复初始化幂等，并归一化历史 `sources.last_error`。当前临时库检查结果为 `foreign_key_check=[]`、`foreign_keys=1`。
- HTTP 只显式序列化已知基本类型/`Path`/日期时间；未知对象返回稳定脱敏错误。
- WeChat adapter 将非法 JSON、数组/标量、缺 `access_token`/`media_id`/`url`、未知 `errcode` 和网络失败统一为稳定 `PublicationError`，保留既有 errcode 映射、dry-run/live 边界和不调用 `freepublish` 的草稿语义。
- 复核补丁：迁移会重新脱敏历史 `code=` 诊断，HTTP 请求日志不记录原始 query；GitHub Weekly 持久化诊断 envelope，后续读取仍区分降级与正常空结果；scheduler 拒绝非对象结果并闭合为稳定失败。
- 产品版本统一为 `3.0.0`：`pyproject.toml`、`src/autoposting/__init__.py`、`src/autoposting/web.py`（API 版本和页面 badge）、`src/autoposting/llm.py`（User-Agent）及相关测试断言。未修改 prompt 版本体系或 prompt 文件。

## 验证记录

- 全量测试（最终 tree）：
  `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`
  — `Ran 691 tests in 139.546s`，`OK (skipped=1)`。含 loopback fixture 的测试在允许本地 TCP 的提权环境执行。
- Offline self-check：
  `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
  — `ok: true`；approval、dry-run、幂等采集、故障错误、热点触发、双平台草稿等 10 项检查均为 true。
- Loopback 行为验收（faithful fixture，无真实发布）：POST `/api/collect` 后 DB 插入 2 条、候选 2 条；再次刷新后 GET `/api/hotspots` 仍保留全部候选（`refresh_retained=true`）。
- 长文章编辑区、页面运行时 JavaScript、版本 badge、status API targeted tests 通过；Skill 与 GitHub Weekly 的 HTML/CSS/3:4 PNG 和主题/调色渲染 targeted tests 通过。
- SQLite 迁移中断/重试、重复初始化、严格 JSON/未知对象、敏感信息脱敏、WeChat 外部坏响应 targeted tests 通过；`git diff --check` 通过。
- Reviewer 定向回归：`test_db`、`test_scheduler`（19 项）及新增历史诊断、GitHub Weekly diagnostics、非对象 scheduler result 用例通过；旧诊断中的 token/URL 不再出现在存储值。
- ego 中已有本地 AutoPosting tab reload 后显示 `本地工作台 v3.0.0 · 本地服务`。未调用真实微信/X/小红书或任何真实发布接口；外部凭据相关全部使用注入 fixture。

## 环境限制与未纳入本版

- 当前 CUA 没有可创建的独立 IAB/Chrome surface；为避免干扰用户当前页面，未继续执行独立浏览器交互。浏览器证据限于已有本地 AutoPosting tab 的只读 reload/status 页面，候选/刷新行为以真实 loopback WebApp fixture 验收；不得将其扩大解释为外部浏览器或真实源验收。
- 全量测试输出包含由故障 fixture 主动产生的脱敏诊断日志；断言结果均为通过。视觉临时数据库清理时还出现过一次非致命的 fixture persistence warning，不影响测试结果。
- WP-06 的 platform/status/lane/phase 全局值域集中化尚未在本版实现，后置为 `3.0.1+`；本版仅纳入严格 JSON/API 序列化和现有边界校验。WP-08～WP-16（`web.py`/Skill/Database/Workbench 边界拆分、发布/文章/视觉去重、legacy 与规则治理）全部后置为 `3.0.1+` 后续切片，不在 3.0.0 交付范围内；不能据此推断其余附件工作包已完成。

## 归档

- 归档：`archives/autoposting-patch-3.0.0-update-20260919.tar.gz`
- 校验：`archives/autoposting-patch-3.0.0-update-20260919.tar.gz.sha256`
- 归档只包含 `src/`、`tests/`、`docs/`、`config/` 和 AGENTS.md、README.md、pyproject.toml、service_control.py、`AutoPosting服务控制.command`；排除 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv`、`.env` 和 secrets。
