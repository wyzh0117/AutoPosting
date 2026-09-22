# PATCH 3.2.0 — 资源探索收口、创作出图工作台与发布中心

版本基线：`3.1.0` → `3.2.0`。本版完成 `AUTOPOSTING_TWO_ITERATION_REFACTOR_PLAN_2026-09-22.md` 第二迭代的主体交付：统一 Resource Discovery 进入 Web/DB/UI、周榜与 Skill 出图工作台在新 IA 下重新可达、发布中心落地、旧四栏目残留 UI 退场，并同步文档、版本与归档。

## 本版交付

### 1. Resource Discovery 进入产品（承接 3.1.0 的草稿并收口）

- `src/autoposting/resource_discovery.py`（新增）：`ResourceCandidate` / `ResourceAssessment` / `ResourceSet` 契约，GitHub、Skill、Web 三个 adapter（复用既有 GitHub 客户端、Skill Finder 的 `SKILL.md` 读取与 skills.sh 检索、Reader/网页检索），两轮 Decision/Jev 判断，去重合并（含粘贴 Skill 与检索结果的同身份合并），`github_weekly` preset，`legacy_payload()` 兼容旧渲染。
- 数据库 v4：`PRAGMA user_version` 升到 4，新增 `resource_sets` 表与 `resource-set-envelope-v1` 版本化 envelope；`save/get/list/delete_resource_set` 四个基础存取方法，存取两侧都做凭据形态脱敏。
- HTTP：`POST /api/resources/discover`、`GET /api/resources`、`GET /api/resources/{set_id}`、`POST /api/resources/{set_id}/refresh`（只回读，不重跑外部搜索）、`DELETE /api/resources/{set_id}`。
- 资源页：自然语言输入 + Repo/Skill/Web 勾选 + 类型筛选 + 粘贴 Skill 链接 + `GitHub Weekly Preset`；结果渲染 ResourceSet 状态、来源状态、诊断与候选（类型/客观分/推荐标记）。

### 2. 持久化往返保真修复

`Database._sanitize_resource_value` 对 `adapter_status` 节内的空字符串也会走 `sanitize_detail`，而 `sanitize_detail("")` 的兜底是 `"unknown_error"`，导致 `cause_code: ""` 在**保存时**就被改写，`discover` 与 `restore/refresh` 的结果不相等（`test_web` 原先 2 个失败之一）。修复为：空字符串直接透传（空串不可能包含凭据、URL、路径或 SQL，脱敏语义不受影响），非空串仍走原脱敏边界。

### 3. 旧四栏目残留 UI 退场

- 删除隐藏的 `#legacy-lane-tabs`（热点候选 / 观点候选 / GitHub 周榜 / Skill查找器）与隐藏文案契约块 `#legacy-ui-contract`，以及只服务它们的 CSS。
- 顶栏只剩 8 个页面入口 + 微信/小红书平台分段；`setLane()` 与 lane 参数保留为薄兼容（CLI、旧 API payload、tests 仍在使用），但不再有任何可见入口。
- 资讯页说明改为「按综合分、时效与独立来源聚合资讯事件」，社区来源并入统一 Event Cluster。

### 4. 创作中心：出图工作台重新可达

- `#github-studio` 与 `#skills-studio` 去掉 lane 门控，常驻创作页；周榜的「生成图片 / 浅色暗色 / 三色盘」控件从资讯页头部移入周榜工作台（单一 DOM，未复制）。
- 新增「抓取本周周榜」（`POST /api/github-weekly`）、「读取上次收录」（`GET /api/skills`）、两块「去资源探索」入口与工作室状态行 `#studio-status`。
- 资源页新增桥接：「送入 Skill 出图」把资源页的关键词或粘贴链接接到同一条 skills 管线并跳转创作页；「周榜出图工作台」加载周榜快照并跳转。发现与出图分离，但能力与实现仍各只有一份。

### 5. 发布中心

- 新增 `Database.list_drafts(limit)`（按更新时间的最近优先，并 LEFT JOIN 每份草稿最近一次 publication attempt）、`WebApp.list_drafts` 与 `WebApp.open_draft`。
- HTTP：`GET /api/drafts?limit=N`（安全摘要）、`GET /api/drafts/{draft_id}`（复用 `_draft_view`，含标题/正文/来源 ID/配图选择/小红书卡片），未知草稿返回 404。
- 发布页从占位升级为草稿中心：列出标题、平台、状态（待审批/已审批）、更新时间、审批信息与最近发布尝试；动作「继续编辑 / 到草稿箱|生成发布包 / 定时到草稿」全部复用既有 `approve → publish → schedule` 链路，微信只写草稿箱、小红书只生成本地包的安全边界不变。

### 6. 文档与测试

- `README.md`：Web 工作台章节按新 IA 重写；补充 `resources` 与 `drafts` 端点说明；周榜/Skill 章节改为「资源探索 + 创作页出图工作台」口径；`hooks/find` 标注弃用（薄兼容）；热点规则章节改为统一 Event Cluster 口径。
- `docs/WEB_FUNCTIONAL_TEST.md`：新增「3.2.0 新信息架构验收」七组步骤（顶栏退场、资讯→研究、资源探索、出图工作台、发布中心、信息源/运行记录、设置集中化）。
- tests：`test_resource_discovery.py` 新增 16 项（多来源去重与两轮 Jev、单源失败降级、Jev 低置信退回 objective-only、preset、粘贴合并与拒绝、持久化 refresh 不走网络、custom decision 失败降级、无 secret）；`test_db.py` 新增 v4 空 `cause_code` 往返回归与 `DraftListingTests`；`test_web.py` 新增发布中心 API/UI 契约测试，并把旧栏目契约更新为新 IA 契约（`legacy-lane-tabs` 不存在、`unified` slot、两个工作室常驻）。

## 验证

- 全量测试（允许本机 Chrome 的提权环境，`test_visual` 需要写系统临时目录）：
  `PYTHONPATH=src:tests python3 -m unittest discover -s tests`
  — `Ran 760 tests in 153.420s`，`OK (skipped=1)`。
- 定向测试：`test_web.py` 138→141 项全绿（含 node 运行时 JS 解析）；`test_db.py` 17 项；`test_resource_discovery.py` 16 项；`test_visual.py` 45 项（本沙箱需提权跑 Chrome 渲染）。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
  — `ok: true`，10 项检查全 true；指标 `drafts=2`、`dry_run_attempts=1`、`events=2`、重复采集后 `items=1`。
- 真实服务（`bash scripts/start_8090.sh`，`http://127.0.0.1:8090/`）：
  - `GET /api/status` → `version: 3.2.0`，31 个来源；
  - `GET /api/drafts?limit=5` → 真实草稿（含 2026-09-22 生成的微信稿），字段为安全摘要；
  - `POST /api/resources/discover`（单 web adapter，真实网络）→ 返回 3 条 website 候选，`adapter_status.web = ok/count 3`，因未配置 Planner/Jev 而 `degraded: true` 且带脱敏诊断；
  - `GET /api/resources` → `GET /api/resources/{set_id}` → `POST /api/resources/{set_id}/refresh` 三者在真实 SQLite 上完全相等，`cause_code` 保持 `""`（验证第 2 节修复）。
- 真实浏览器（ego-browser，`http://127.0.0.1:8090/`）：
  - 顶栏 8 个页面 + 平台分段，`legacy-lane-tabs` 与 `[data-lane]` 计数为 0，徽章 `v3.2.0 · 本地服务`；
  - 发布页渲染真实草稿列表（`共 20 份草稿 · 14 份待审批`，卡片含平台/状态/更新时间与三个动作按钮）；
  - 创作页两个出图工作台 `display` 可见且标题正确（`GitHub 周榜出图工作台`、`Skill 海报出图工作台`），「抓取本周周榜」按钮在位；
  - 资源页 `data-resource-discovery-slot="unified"`、3 个 adapter 勾选框、preset 与两个桥接按钮在位；在页面里真实执行一次搜索 → 渲染 15 张候选卡、摘要 `ResourceSet · degraded`、可见诊断 `planner · plan · planner_failed · not_configured ｜ jev · assess · decision_degraded · not_configured`。
  - 截图证据：`output/verification/autoposting-3.2.0-resources-page.png`。
- `git diff --check` 通过；`py_compile` 通过；新增行扫描只有测试里的 `Bearer SUPERSECRET` 脱敏夹具，无真实凭据。

## 边界与未纳入

- `hooks/find`（找切口）与 `/api/related-search`（相关检索）保留为**薄兼容入口**：文章素材链目前仍经它们取补充素材，产品概念已由 `研究 / Topic Dossier` 取代，README 不再作为推荐入口。它们的 API、行为与测试均未改变，也没有第二套业务实现。下一步收口点是让文章生成直接消费 Dossier 素材，再删除这两个入口。
- lane 机制（`hotspots`/`viewpoints`/`mixed`）与 `body.lane-*` 样式规则保留为兼容面（CLI、`/api/hotspots?lane=`、旧 payload、既有测试仍在使用）；`#skill-search-row`、`#skill-source-bar`、`#skill-degraded-note`、`#skill-list` 仍是技能包渲染的隐藏写入目标，不再作为产品入口。
- 未调用真实微信 / X / 小红书发布；平台行为由注入 fixture 与既有边界测试覆盖。
- 本版未改动视觉技术路线（仍是 HTML/CSS → 浏览器截图）、未新增图像模型调用点、未新增发布平台。
- 环境限制：`test_visual.ExamplePngTests` 需要本机 Chrome 写系统临时目录，在默认受限沙箱下会得到环境性 `ScreenshotError`；提权后 45 项全绿。该失败与本次改动无关，已在验证章节注明执行条件。

## 归档

- 归档：`archives/autoposting-patch-3.2.0-update-20260922.tar.gz`
- 校验：`archives/autoposting-patch-3.2.0-update-20260922.tar.gz.sha256`
- 归档只包含 `src/`、`tests/`、`docs/`、`config/` 和 `README.md`、`AGENTS.md`、`pyproject.toml`、`service_control.py`、`AutoPosting服务控制.command`；排除 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv`、`.env`、密钥和临时工作文件。
