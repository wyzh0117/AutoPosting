# PATCH 3.2.1 — 两轮重构收口：Topic Dossier 直连创作、旧链路退场

版本基线：`3.2.0` → `3.2.1`。本版执行 `AUTOPOSTING_3.2.1_FINAL_CLOSURE.md` 要求的 Final Closure：让文章生成真正消费 Topic Dossier，并让被两轮重构替代的旧能力退出正式业务路径。不新增产品主线，不拆分 Iteration 3 / 4。

> **AutoPosting Two-Iteration Refactor is CLOSED.**
>
> 资讯探索、资源探索、研究、创作、视觉与发布已进入统一产品结构；本轮重构所替代的旧热点 / 观点 / GitHub Weekly / Skill Finder 一级产品结构及旧研究链路已经退出正式业务路径。后续需求按正常产品迭代处理，不再继续扩展本次重构任务。

## 本版交付

### 1. 目标 A：Topic Dossier 真正进入文章生成

正式链路固定为 `Information Discovery → Event Cluster → Topic Dossier → Creation → Article Generation`。生成期不再有任何二次检索：没有 hook 打捞、没有 related-search、没有临时扩题。

- `src/autoposting/article_context.py`（新增）：`ArticleGenerationContext`、`build_dossier_context(dossier)`、`build_manual_context(...)`、`ARTICLE_CONTEXT_VERSION = "article-generation-context-v1"`，上限 `MAX_MATERIAL_CHARS=20_000` / `MAX_SUMMARY_CHARS=4_000` / `MAX_EVIDENCE_ITEMS=100`。dossier 缺失、schema 不符、无证据、无可用来源时抛 `ArticleContextError`，永不回退到搜索。
- `Workbench.get_research(dossier_id)` 复用 `ResearchService.restore`；`Workbench._generate_dossier_article(...)` 走 `build_dossier_context → FactList → LLMService.generate_article → _persist_article(inline_sources=True)`，来源记录全部来自 `context.source_refs`。
- `POST /api/articles` 接受 `dossier_id`；`dossier_id` 为空、找不到、格式不符、无证据、无来源，分别以 `dossier_id_required` / `dossier_not_found` / `dossier_invalid` / `dossier_evidence_required` / `dossier_sources_required` 返回 400，响应体统一为 `{ok:false, error, error_reason, error_type:"ArticleContextError"}`。
- 客户端只提交 `dossier_id`：创作页横幅（`articleContextFromDossier`）只摘要标题/事件数/证据数/状态，服务端用 `dossier_id` 重建权威 context，不采信客户端提交的证据。

### 2. 目标 B：被替代能力的退场

- `src/autoposting/hooks.py` 整体删除；`LLMService.extract_hook_candidates` 删除；`chosen_hooks` 从契约、prompt、样式文件、payload 全层移除。
- `POST /api/hooks/find`、`POST /api/related-search`、`App.find_hooks`、`App.related_search`、`Workbench.related_search`、`#news-supplement-*` 抽屉删除。
- lane 产品面删除：`[data-lane]`、`#legacy-lane-tabs`、`#legacy-ui-contract`、`body.lane-*` 主题与 lane 导航；官方/社区筛选改由 `scope` 契约承载，资讯页显示 `#news-scope-select`。
- Skill Finder 隐藏写入目标删除：`#skill-query`、`#skill-links`、`#skill-list`、`#skill-source-bar`、`#skill-degraded-note`、`#skill-search-row`、`#skill-token-box`、`#github-list`、`#find-hooks-btn`、`#hook-list`、`#related-search-btn`。降级与来源信息没有消失，改由正式契约承载：`#skills-sources` / `#skills-source-chips` / `#skills-degraded-note`。
- 随之删除重复的 JS/CSS/handler/tests/README 文案，不再保留第二套业务算法。

### 3. 保留的兼容面（仅必要面）

原则：**删除业务实现，保留真正必要的兼容面**，且不为「保险」永久保留两套业务算法。

- `src/autoposting/legacy_compat.py`（新增）：`translate_legacy_lane(lane)`、`normalize_scope(scope)`、`resolve_scope(scope=None, lane=None)`。`lane` 只作为 HTTP 边界别名存在（`hotspots→official`、`viewpoints→community`、`mixed→all`），无任何可见入口、无主题、无导航。
- 从未持久化过 lane：dossier、draft、resource set、run 记录里都没有 lane 字段，因此没有历史 payload 解析器，也没有双写。
- `background.py` 的关系搜索引擎保留：迁移后唯一消费者是 `research/adapters.py::RelatedNewsSearchAdapter`（资源探索的网页发现）；产品级 related-search 端点与抽屉已退场。
- `GitHub 周榜` 作为 preset / 工作室名称合法保留（`github_weekly`）。

### 4. 来源配对与引用修复（本轮真实链路实测发现）

原实现把排序后的 `source_ids` 与来源记录顺序的 `source_urls` 按下标对齐，导致「OpenAI 的句子链到 Reddit 帖子」这类错位归属；同一轮实测还发现句首引用因「前面没有可包住的从句」被整体丢弃，缓存里的四篇旧稿在成稿阶段出现 0 条内联链接。修复后规则明确并写入代码注释：

- `source_ids[i]` 只与 `source_urls[i]` 配对；`sources[]` 记录（同时携带 `source_id` 与 `url`）是唯一可信的配对来源；长度不一致时 URL 一律置空，**绝不错位配对**。
- `radar._build_event` 现在输出与 `source_ids` **逐位对齐**的 `source_urls`（同一 id 多 URL 时取首条记录，完整配对仍保留在 `sources[]`）；`workbench._radar_event_candidate` 的覆盖同样按 `sources` 顺序对齐，不再出现「排序 id 列表 + 记录顺序 URL 列表」的组合；`research/contracts.py` 用 `_paired_sources` / `_source_urls` 落地该规则。
- `article_context._topic_rows` 不再对 URL 列表去重（同一 URL 出现在两个来源上是正常的，去重会让后续 URL 整体前移）。
- `engagement.py`：句首与标签式引用改为链接其后从句（不再丢引用）；链接标签不再吞并其他来源记号；调用方传入的环境来源 id（`extra_source_ids`）只用于把机器记号清出正文、绝不生成链接；无法内联的来源保留 `原文链接` 参考行，避免文章失去全部出处、也避免草稿因此无法再编辑。
- 参考行覆盖两类「只剩引用行」的成稿：来源单独成行（`openai_news hn_frontpage`）与标签行（`来源：openai_news`）。两者都不可读、都会被删掉，删除后正文保留 `原文链接` 参考行；没有正文（只剩 `AIGC` 声明）时不补参考行。这样 `edit_draft` 的 `source_ids_required` 不再因为「正文既没有 id 也没有 URL」而永久拒绝人工修改。
- 效果（真实 dossier `445cad7d291ede6992b959d9`）：`openai_news → openai.com/index/introducing-gpt-6-sol-and-luna`、`reddit_openai → reddit.com/r/OpenAI/comments/1wngyux/`，正文无 `openai_news` / `hn_frontpage` / `reddit_openai` 记号，`AIGC` 与来源校验不退化。

### 5. 创作页出图工作台的真实缺陷修复

- Skill 海报：勾选海报与勾选 skill 原是两套选择，导致 16 条收录下无法降低出图条目、点「生成图片」只能得到「超过条目上限」。`toggleSkillImage` / `selectAllSkillImages` 现在通过 `skillsForImage` 回写同一条 skill 选择；实测勾选 3 条 → 生成 4 张（封面 + 3 张项目卡）。
- 首页「最近活动」卡不再停在加载占位：启动时读取 `/api/collection-runs`，用最近一次真实采集填充；无记录时给出明确指引。

### 6. 文档与测试

- `README.md`：写入 `Two-Iteration Refactor: CLOSED`，说明正式链路、退场能力与保留的兼容面；移除 `hooks/find`、`/api/related-search`、旧 lane UI、隐藏 Skill Finder 的产品说明。
- `docs/WEB_FUNCTIONAL_TEST.md`：按新 IA 重写验收步骤，含 `scope` / `dossier_id` 的真实 curl 示例、点击驱动的 B2 步骤、`/api/hooks/find` 负向检查与 `dossier_not_found` 400 校验。
- `docs/ARTICLE_BASE_PROMPT.md` 升到 `article-v13`；`docs/article_styles/AI专家.md` 的 `background.chosen_hooks` 段落改为 `### 开头切口`。
- 新增测试模块：`tests/test_article_context.py`、`tests/test_legacy_compat.py`、`tests/test_research_search.py`、`tests/test_research_to_article.py`；`test_article_sources.py` 增加句首引用、参考行（含标签行与诱饵图片 URL）与标记清理用例；`test_workbench.py` 增加雷达候选来源配对用例；`test_web.py` 增加资源网页检索走同一适配器与降级直通用例；`tests/test_hooks.py` 随实现删除。
- 生成期禁止旧链路的显式断言：`test_generation_never_touches_the_retired_search_chain` 在生成文章时把 `background.search_related_news`、`_search_related_news_impl`、`_search_related_news_bounded`、`RelatedNewsSearchAdapter.search` 全部替换为抛异常，要求文章仍然生成成功且四者调用次数为 0（把任意一次调用注入生成路径，该测试即失败——已实测）。

## 验证

- 独立验收：由未参与实现的 subagent 逐条复核本文件的事实性声明，并自行复跑测试 / 自检 / 静态扫描 / 归档校验；它额外用「把 `search_related_news`、`_search_related_news_impl`、`_search_related_news_bounded`、`RelatedNewsSearchAdapter.search` 全部替换为抛异常」的方式证明了生成期 0 次检索，并对真实 25 份草稿逐一验证可编辑。其提出的 5 项差异已在本轮修复：资源网页检索改走同一适配器（D1）、`body.lane-` 静态命中归零（D2）、`radar._build_event` 与 `_radar_event_candidate` 的来源配对逐位对齐并补强测试（D3/D5）、标签行引用不再导致草稿永久不可编辑（D4）。修复后复跑全量测试与真实链路均通过。
- 全量测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests` — `Ran 823 tests`，`OK (skipped=1)`。
- 定向测试：`test_web.py` 141 项、`test_workbench.py` 46 项、`test_article_sources.py` 22 项、`test_research_to_article.py` 21 项、`test_research_search.py` 20 项、`test_article_context.py` 14 项、`test_legacy_compat.py` 12 项，均全绿。其中 `test_the_radar_event_publishes_an_aligned_url_list` 已确认能捕获修复前的错位配对（回退实现即失败）。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check` → `ok: true`，10 项检查全 `true`。
- 静态清场（`src/` 全量检索）：`hooks/find`、`find_hooks`、`热点候选`、`观点候选`、`Skill查找器`、`legacy-lane-tabs`、`legacy-ui-contract`、`body.lane-`、`skill-search-row`、`skill-source-bar`、`skill-degraded-note`、`skill-list` 命中数为 0（`GitHub 周榜` 仅作为 preset / 工作室名称存在）。
- 真实服务（`127.0.0.1:8080`，`GET /api/status` → `version: 3.2.1`）与真实浏览器（ego-browser）逐项验证：
  1. 首页正常，最近活动显示真实采集（「最近一次采集：有 5 个源失败 · 新增 69 条 · 23/09/2026, 04:54:27」）；
  2. 资讯页 9 张候选卡，刷新后仍在；
  3. 真实采集 9 条候选 → 研究 → Topic Dossier `445cad7d291ede6992b959d9`（`degraded`，7 条证据）；
  4. 创作页横幅 `Topic Dossier · 445cad7d291ede6992b959d9 · 1 个事件 · 7 条证据 · degraded`；
  5. `POST /api/articles {dossier_id}` → 文章 2012 字、含 `AIGC`、2 条内联链接且归属正确；
  6. 6/7. 客户端 `fetch` 埋点与浏览器网络记录中都没有 `hooks/find` 与 `/api/related-search`；
  8. 资源页输入/粘贴/检索槽位正常；
  9. Skill 出图真实成功（封面 + 3 张项目卡，1080×1440 PNG，`/api/generated/skills/*.png` 200）；
  10/11/12. 创作页无隐藏旧 Skill Finder 产品 UI（11 个旧 id 计数为 0）、发布页渲染 20 份真实草稿（含「继续编辑」入口）、设置页正常；
  13. 刷新后关键状态符合 contract（徽章 `v3.2.1 · 本地服务`、9 个 `data-app-page` 面板、候选仍在）；
  14. 逐页切换过程中无 JS runtime error（`window.onerror` / `unhandledrejection` 埋点为空）。
- 旧草稿链路（Case 2）：发布页「继续编辑」→ 编辑正文并追加段落 → `POST /api/articles/edit` 成功、状态回到 `DRAFTED`、重读草稿确认修改已落库；dossier 稿与手工稿均通过。
- 手工创作链路（Case 3）：不强制所有文章来自 dossier，`item_id` + 粘贴素材仍可生成（1438 字、含 `AIGC`、内联链接 1 条）。

## 边界与未纳入

- 未做（按 closure 文档明确排除）：资源→配图、新 Search Adapter、更多订阅源、Jev 权重调整、Topic Dossier 图形化/时间线、新视觉主题、新发布平台、非阻塞 UI 美化。
- 发布业务未改：`approve → publish → schedule` 与安全边界不变（微信 live 只写草稿箱、不调用 freepublish；小红书仍 `manual_required`）。
- 预存但未消费的字段：`workbench._radar_event_candidate` 不再带 `event_id`（3.2.0 起如此），资讯页也不读取候选的 `source_urls`，因此不影响任何现有 UI；本轮只把该处来源配对改为逐位对齐，未扩展该 payload。
- 预存死 JS 本轮按「不做非阻塞美化」保留，已确认无调用方，留待后续正常迭代：`loadResearchDossier`、`drawGithubCover`、`drawGithubPoster`、`endpointLabel`、`renderSettingsModels`、`stepImagePicker`、`stepGithubGallery`、`stepSkillsGallery`；出图轮播的 `skills-gallery-prev/next` 仅由 `paintCarouselNav` 绘制，没有绑定翻页监听。
- 受限沙箱内启动的服务，Chrome 无法创建临时用户目录，出图会失败并提示用 `AutoPosting服务控制.command` 重新启动；本轮海报验收即在该非沙箱方式下完成。渲染失败在 UI 上仍统一显示 `internal_error`（既有行为，本轮未改）。
- 历史文档不回改：`docs/PATCH_*.md`（3.2.0 及更早）与 `docs/superpowers/**` 的旧设计稿/计划记录的是当时的真实产品结构，保留原文；面向当前版本的文件（`README.md`、`docs/WEB_FUNCTIONAL_TEST.md`、`docs/ARTICLE_BASE_PROMPT.md`、`docs/article_styles/*`、`config/*`）中的旧关键词只出现在「已删除 / 不应出现」的陈述与负向检查里。

## 归档

- 归档：`archives/autoposting-patch-3.2.1-update-20260923.tar.gz`
- 校验：`archives/autoposting-patch-3.2.1-update-20260923.tar.gz.sha256`
- 归档只包含 `src/`、`tests/`、`docs/`、`config/` 和 `README.md`、`AGENTS.md`、`pyproject.toml`、`service_control.py`、`AutoPosting服务控制.command`；排除 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv`、`.env`、密钥和临时工作文件。