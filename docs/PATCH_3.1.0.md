# PATCH 3.1.0 — 资讯采集、决策与研究工作流

版本基线：`3.0.0` → `3.1.0`。本版完成两轮重构计划的第一轮交付：统一来源与采集运行记录、让 Decision/Jev 进入资讯排序、增加 Planner/Research/Dossier 契约，并落地首页、资讯、研究、来源和运行记录的信息架构。第二轮 ResourceSet/资源发现尚未纳入本版归档。

## 本版交付

- `SubscriptionSource` 采用 canonical `source_type` / `adapter_type`，保留旧 `type` 兼容读取；数据库运行态的启停与采集间隔是权威状态。
- CLI 与 Web 共用 `collect_sources`，每次采集持久化 `CollectionRun`、来源 attempt、状态、失败原因和脱敏 diagnostics；迁移使用 `PRAGMA user_version`，旧库可幂等升级。
- 热点、观点和 mixed 资讯支持官方源与 Reddit 同次采集；Radar 保留多来源 cluster，并在生产路径中使用 DecisionService 的 objective + Jev 60/40 排序，Jev 故障或低置信度退回 objective-only 并保留诊断。
- 新增严格版本化的 Planner、DecisionAssessment、ResearchIntent、SearchPlan、EvidenceCandidate、TopicDossier 契约；多选研究可生成 Dossier，并通过详情/列表接口恢复。
- Web 工作台按首页、资讯、资源、创作、发布、来源、运行记录和设置组织；旧 API 与旧 lane 保留兼容入口。已有 GitHub Weekly 3:4 视觉、Skill `SKILL.md`/monorepo/粘贴 URL/海报能力继续复用。
- 版本同步更新至 `3.1.0`：`pyproject.toml`、`src/autoposting/__init__.py`、`src/autoposting/web.py`（API 版本与页面 badge）、`src/autoposting/llm.py`（HTTP User-Agent）以及测试断言；README 补充版本和工作台说明。

## 验证

- 全量测试（允许本地 loopback 的提权环境）：
  `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`
  — `Ran 736 tests in 136.908s`，`OK (skipped=1)`。
- 离线自检：
  `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
  — `ok: true`；10 项检查均为 true，指标为 `drafts=2`、`dry_run_attempts=1`、`events=2`、重复采集后 `items=1`。
- 真实 loopback fixture：`POST /api/collect` 的 mixed 官方源 + Reddit 采集、`GET /api/hotspots?lane=mixed` 的 cluster 回读、`GET /api/collection-runs/{run_id}` 的持久化 attempt 回读均通过；多选研究 `POST /api/research`、详情和列表回读也通过。对应 Web/Workbench tests 在上述全量回归中通过。
- `git diff --check` 通过；外部凭据、token、Authorization、请求 body 和敏感 URL 未写入代码、诊断、PATCH 或归档。

## 边界与未纳入

- 本版没有把第二轮 `ResourceDiscoveryService`、`ResourceCandidate`、`ResourceAssessment`、`ResourceSet` 接入 Web/Workbench，也没有新增资源持久化表；对应草稿暂存于工作区之外，确保不进入本版归档。
- 没有调用真实微信/X/小红书发布接口；平台行为使用注入 fixture，微信 live 边界和小红书人工包语义由既有测试覆盖。
- 当前执行环境没有独立浏览器 surface；UI 行为以允许 loopback 的真实 HTTP fixture、运行时 JavaScript 解析和 Web API 行为测试为证，不将静态 HTML 断言扩大解释为浏览器验收。

## 归档

- 归档：`archives/autoposting-patch-3.1.0-update-20260922.tar.gz`
- 校验：`archives/autoposting-patch-3.1.0-update-20260922.tar.gz.sha256`
- 归档只包含 `src/`、`tests/`、`docs/`、`config/` 和 `README.md`、`AGENTS.md`、`pyproject.toml`、`service_control.py`、`AutoPosting服务控制.command`；排除 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv`、`.env`、密钥和临时工作文件。
