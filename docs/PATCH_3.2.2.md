# PATCH 3.2.2 — 产品硬化、订阅源扩容与界面重构

版本基线：`3.2.1` → `3.2.2`。本版执行 `AUTOPOSTING_3.2.2_PRODUCT_HARDENING_SOURCE_EXPANSION.md` 的四条工作线（Git 收口、死代码清理、订阅源扩容、Planner/Jev/Reader 降级诊断修复），并叠加一次整站界面重构。

不新增产品主线，不引入前端框架，不重写 `web.py`，不改动正式链路（`Information Discovery → Event Cluster → Topic Dossier → Creation → Article Generation`）与发布安全边界。

## 一、Planner / Jev：从「静默降级」到「真的能用」

这是本版最深的一处缺陷，且是三层叠加的，任一层单独修好都不足以让功能可用。

### 1.1 第一层：provider 接缝不存在（已修）

`LLMPlanner` / `JevAdapter` 只认 `callable(provider)`、`.run` 或 `.assess`，而 `LLMService` 三者都没有——于是每一次真实调用都在 `not_configured` 上失败，而 `configured` 属性却报 `True`。按文档要求「先让接缝存在，再让诊断说真话」：

- `src/autoposting/llm.py`：新增 `LLMService.run(prompt)` 接缝（委托给既有 `_run`，不复制第二套执行逻辑）。选择 `.run` 而不是 `__call__`：Jev 会把「可调用的 provider」优先当作 **payload** 调用方，`__call__` 会让 Jev 把请求体当 prompt 传进去。
- `src/autoposting/diagnostics.py`：`_KNOWN_CAUSES` 补齐 `auth_failed` / `blocked_http` / `connection_reset` / `dns_failure` / `invalid_content` / `low_confidence` / `not_configured` / `provider_error` / `reader_internal_error` / `tls_failure` / `too_large` / `unsupported_content`；`cause_code_for` 未改。
- `src/autoposting/decision/planner.py`：provider 存在但没有可调用接口时，`not_configured` → `provider_error`；`configured` 改为 `callable(provider) or callable(getattr(provider, "run", None))`。
- `src/autoposting/decision/adapters/jev.py`：同一语义，`jev_not_configured` → `jev_invalid_provider`（`cause_code=provider_error`）。
- 真正的 `provider is None`（用户没配模型）仍然如实报 `not_configured`——这是唯一应该报它的情形，`test_genuinely_unconfigured_providers_stay_not_configured` 钉住这一点。
- `LLMError` 增加只读 `detail`；hermes CLI 非零退出 → `provider_error` + `detail=process_exit_{code}`；`HermesRunner._openai_completion` 在 `OSError`/`URLError` 之前先捕 `HTTPError`：401/403→`auth_failed`、429→`rate_limited`、其他→`provider_error`、`OSError`/`URLError`→`unreachable`、读超时→`timeout`、非 JSON→`invalid_response`。对外 `.code` 值保持不变（`llm_api_failed` / `llm_timeout` / `llm_invalid_output`）。

### 1.2 第二层：`resource_discovery` 拿不到 planner（已修）

`web.py::_build_resource_discovery()` 构造 `ResourceDiscoveryService` 时漏传 `planner=` 与 `decision_service=`，于是资源探索永远报「未配置」，与用户是否配好模型无关。现按 `ResourceDiscoveryService.__init__` 的关键字形参补齐，并加注释说明这是长期 `not_configured` 的来源。

### 1.3 第三层：响应契约与 prompt 不匹配，任何模型回复都无法通过校验（已修）

接缝修好后，真实调用仍然全部失败。根因不在模型：

- `build_planner_prompt` **从未要求** 输出 `schema_version`，而 `_validate_result → from_dict → _schema` 把它当必需字段。缺它就报 `research_intent_unknown_schema_version`，任何回复都不可能通过。
- 三个检索通道键 `search_repo` / `search_skill` / `search_web` 在契约里是**布尔开关**，但键名读起来像词表，模型必然回数组（真实回复就是 `"search_repo": ["github.com"]`），`_bool(...)` 直接判 `research_intent_must_be_boolean`。
- Jev 的 `.run` prompt 更彻底：它只说了「按提供的 Jev 契约返回 JSON」，然后把**请求体**（`jev-request-v1`）附上，从未描述**响应**契约（`jev-assessment-v1`）。

修法与边界：

- `build_planner_prompt` 改为给出一份含全部键与 `schema_version` 的字面 JSON 骨架，并显式声明类型：三个通道是 true/false 开关而非词表，`concepts`/`preferred_signals`/`adapters`/`source_event_ids` 是字符串数组，`max_candidates` 是 1–500 的整数，且至少要开一个通道。
- 新增 `_normalize_provider_payload`（planner）与 `_normalize_provider_payload`（jev），**只在 LLM 边界**把一次模型回复规范进严格契约：补 `schema_version`（仅在缺失时）、通道值从词表/字符串折成布尔、单字符串折成单元素数组、数字字符串折成数字、`""`/`"null"` 折成 `None`。
- **`from_dict` 保持严格**：它同时守护持久化载荷，不能放宽。规范化只发生在 LLM 边界。
- **错误版本号仍然失败**：`setdefault` 只在键缺失时补；模型回了 `"bogus-v9"` 一样报错，规范化不会掩盖真正的契约违规。
- **不伪造数据**：规范化只做类型折算，不填 `goal`/`concepts`/`score`/`decision` 等语义字段；`{}` 依旧失败。
- 新增 `build_jev_prompt`，把响应骨架、`score` 0–100、`confidence` 0–1、`decision` 三值枚举、`resource_type` / `relation` 的全部合法取值写进 prompt。

**实测结果**（真实配置的 endpoint，非 fixture）：`LLMService.run()` → `'可用'`（1.0s）；`planner.plan("AI agent 工具链")` 返回带三个 `schema_version`、5 个 concepts、真实 `plan.query` 与 `adapters` 的完整 `PlannerResult`；`decision.assess(...)` → `status: ok`、`confidence: 0.65`、无诊断、无 `not_configured`。

## 二、Reader：抓取失败分类与「已采集正文」合法回退

此前所有读取失败都被压成 `fetch_failed`，且研究读取走的是裸 `urlopen`（无大小上限 / SSRF / 重定向保护），一条被 403 拦的证据会整条丢掉。

- `src/autoposting/reader.py`：`classify_http_status(status, body=None, *, final_url="")` 与 `classify_fetch_error(exc)`（均导出）。`cause_code` 刻意由 HTTP 状态推导，保持生产日志行字节不变；`blocked_kind` 再按响应体特征细分：`cloudflare` → `user_agent` → `anti_bot` → `robots` → `login_wall` → `js_required` → 兜底看最终 URL 是否落在挑战页。异常映射：`SSLError`→`tls_failure`、`gaierror`→`dns_failure`、timeout 族→`timeout`、reset 族→`connection_reset`、`URLError` 解包 `.reason`、解码/`ValueError`→`invalid_content`、NUL/`%PDF-`/JSON 头→`unsupported_content`、>12 MiB→`too_large`、其余→`reader_internal_error`。
- `ArticleDocument` 追加 `cause_code` / `degraded` / `fallback_source` / `blocked_kind` 与只读 `original_url`；原 16 个字段顺序未动。
- 每个失败分支（HTTPError、传输异常、非法载荷、状态 ≥400、`too_large`、不支持的内容、渲染失败）统一收口到 `_feed_fallback()`：用本次读取已经拿到的 feed 文本 `replace(..., degraded=True, fallback_source="feed", cause_code=<具体原因>)`，**保留** 已采集正文；没有任何可用 feed 文本时原样返回失败文档。
- **不重试、不换 header、不补第二次请求**：`tests/test_reader.py` 用 patch 过的 `autoposting.reader.urlopen` 断言恰好一次尝试且浏览器 header 未变。
- `src/autoposting/research/adapters.py`：`ReaderServiceEvidenceAdapter(reader=None, fallback_resolver=None)`。研究层不 import 数据库，由调用方注入查表函数；`fallback_resolver=None`、抛异常的 resolver、非 Mapping/空结果一律安全降级为「没有可回退正文」，绝不把一次读取变成异常。
- `src/autoposting/research/service.py`：`_reader_result` 复制新字段；`degraded = document.degraded or not (status in {full,partial} and content)`——回退替换与「有状态但没正文」都算降级，正常的 partial 页读法不变（不新增诊断）；降级但有正文时发 `evidence_read_degraded` 并带上**具体** `cause_code`。
- `src/autoposting/article_context.py`：证据行保留五个新字段，prompt 素材追加 `（降级：<cause_code> / 来源：<fallback_source>）`。
- **接线**：`src/autoposting/workbench.py` 注入 `fallback_resolver=self.database.get_item_by_url`（显式传入的 `research_reader` 仍然优先）；`web.py::_reader_observability` 暴露 `cause_code`/`degraded`/`fallback_source`/`blocked_kind`。

**实测结果**（同一真实条目，把页面请求强制成 403 做反事实对照）：

| | status | cause_code | fetch_method | 正文 | 页面请求次数 |
|---|---|---|---|---|---|
| 注入 resolver（本版） | `full` | — | `rss` | 210 字符 | **0** |
| 不注入（3.2.1 行为） | `blocked` | `blocked_http` | `http` | 0 字符 | 1 |

反向检查：本工作台从未采集过的地址被拦截时保持 `blocked`、正文为空、不出现降级徽标——**不伪造正文**。

## 三、订阅源扩容与健康度（DB schema v5）

- `src/autoposting/db.py`：`DB_SCHEMA_VERSION = 5`。`sources` 新增 `topic_scope` / `language`（`NOT NULL DEFAULT ''`）、`last_success_at`、`consecutive_failures`。字面 `SCHEMA`、新的 `_migrate_v5`（`PRAGMA table_info` 守护的 `ALTER TABLE ADD COLUMN`）与「版本已是最新时的修复通道」三处同时接线，因此半旧库也能收敛。**不伪造 `last_success_at` 历史**：没有 `last_error` 的旧行升级后仍是 NULL。
- `derive_source_health(row, now=None)`：纯派生、不落库，判定顺序 `disabled → error → never → stale → ok`，stale 阈值为 `max(interval,1) × 2` 分钟；`list_sources()` 附带 `health`。有一条交叉测试逐行比对 DB 派生值与 `web.py::_runtime_source_health`，两者不允许漂移。
- `record_fetch_result`：成功 → 清 `last_error`、同时写 `last_fetched_at` 与 `last_success_at`、失败计数归零；失败 → 只写 `last_fetched_at` 与 `last_error`、计数 +1、**不动** `last_success_at`（失败不得抹掉历史）。
- 新 API：`Database.get_item_by_url(url) -> {item_id, title, content_text, canonical_url} | None`（空值/未命中返回 None，从不抛）；`Database.set_collection_run_clusters(run_id, count)`（负数/空 id 报 `ValueError`，未知 run 静默无操作，幂等）。
- `src/autoposting/workbench.py`：`HotspotService.collect` 在 `list_window` 之后取 `len(payload["radar"]["events"])` 写入运行记录，`clusters_created_or_updated` 从「恒为 0」变成真实值（实测 3 条 → 3）。
- `src/autoposting/models.py` / `config.py`：`Source` 增加两个字段并在 `__post_init__` 归一（strip；language 转小写），`config.py` 校验 `_TOPIC_SCOPE_RE` / `_LANGUAGE_TAG_RE`。两者**刻意不进入** `REQUIRED_SOURCE_FIELDS`：v5 之前的用户注册表必须仍能加载，缺失时取 `""`。
- `config/sources.example.json`：37 → **54 条**（启用 20 → **36 条**），新增 17 条、未删任何条目。等级 A→S1、B→S2（社区 S4）、C→S3，**未放宽 `tier` 的 CHECK**。GitHub Releases 以 `adapter_type: "rss"` 指到 `releases.atom`（`pipeline.py` 只实现了 rss/html/x/sitemap/reddit 五种派发，写 `adapter_type: "github"` 会被判 `unsupported_source`，已由测试钉住）。`google_ai_blog` 规范化为 `https://blog.google/innovation-and-ai/technology/ai/rss/`。Anthropic 无可用 feed 形态，改用其官方 SDK 仓库的 Releases 作为 S1 代理。本机够不到的条目（11 条 reddit + 关闭的 Meta）`identity_verified` 一律为 false，不谎报。
- 实时核验：25 条启用的 rss/sitemap 全部可解析（openai 1219、hf 866、deepmind 100、nvidia_dev 100、5 条 github releases 各 10、reddit 的 `.rss` 回退亦成功）。

### 3.1 修掉的重复计数

每个 collector 在 `except` 里记一次失败再 `raise`，`pipeline.py` 的异常处理又用更丰富的 diagnostic 记第二次——**一次失败让 `consecutive_failures` 加 2**。本版把失败记录收敛到 pipeline 一处（它持有更好的 diagnostic），`ingestion.py` 的 4 个 collector 只负责抛出。`reddit.py` 走的是「捕获后返回 `CollectionResult(failed=1)`」而非抛出，pipeline 只读取已存的 `last_error`，因此本来就是一次，未改。

实测（真实 pipeline、真实失败地址）：连续三次失败 → `1, 2, 3`；一次成功 → 归零并写入 `last_success_at`。回归测试 `test_one_failed_attempt_increments_the_streak_exactly_once` 用 `assertLogs` 同时钉住「计数只加一次」和「仍然只记一条日志」。

## 四、界面重构

前端仍是 `web.py` 内联的单个 `<script>` + CSS，无框架、无构建步骤、无新端点。改动集中在**设计标尺**与**数据呈现**，不动业务逻辑。

### 4.1 设计层（Ink & Amber）

- 旧 `:root`（802 字符）替换为约 3.35 KB 的 token 层，**保留全部旧变量名作为别名**（`--bg-main`、`--accent-gradient-1/2`、`--radius-*`、`--hotspot-list-h` 等），因此既有规则无需重写。新增墨色阶梯 `--ink-000..300`、`--line-1/2/3`、纸色 `--paper-1/2/3`、琥珀 `--amber-1/2/3`（+ `--amber-deep`）、语义色 `--jade/--gold/--coral/--azure/--slate`、三套字族、8 级字号 token（`--fs-2xs` 11.5 / 12.5 / 13.5 / 15 / 17.5 / 21 / 26 / `clamp(30px,3.4vw,46px)`）、4 档间距与圆角、3 档动效。
- 追加约 54 KB 设计层（基础/重置、工具类、外壳、导航、按钮、芯片、卡片、页头、KPI、数据行、筛选栏、表单、提示、逐页收口、对话框、等待条、1180/900/560 断点、`prefers-reduced-motion`）。
- 全局 `[hidden]{display:none !important}` 取代十余处按组件重复的覆盖；机械对齐颜色 token（`rgba(196,164,106,` → `rgba(207,169,107,` 等 29 处），修掉 `#e0e0e4` / `#a0a4b0` 这类冷灰漂移。

### 4.2 信息架构与外壳

- 顶栏改为「AP」图形标记 + 主标题 + `AI 信息生产工作台` 副标题 + 状态徽章；右侧「收藏」「设置」收进 `.header-actions`，收藏按钮带 `aria-expanded` / `aria-controls`。
- 导航加两处分隔符，分成「首页/资讯/资源」「创作/发布」「信息源/运行记录/设置」三组（8 个入口与 `data-app-page` 契约不变）。
- 8 个页面统一 `.page-head`（标题 + 说明 + 右侧动作区）；资讯页页头占满整行；创作页新增可见的 `Creation Studio` 页头与「去发布」动作（复用既有导航接线，不新增路由）。

### 4.3 数据呈现

- 信息源页：6 格健康统计（总源/启用/正常/待刷新/异常/停用，异常与待刷新分色）、`.filter-bar` 四个筛选胶囊 + 搜索框 + `#source-filter-count`、每行重建为数据行（等级/类型/主题/语言标签 + 「每 N 分钟 · 上次成功/上次抓取」+「连续失败 N 次」+ 具体错误 + 状态胶囊）。筛选与搜索是**纯前端**，不触发新请求。
- 运行记录页：每条 run 可展开逐源明细，带 `.outcome-bar`（新增/去重比例）与中文失败原因。
- 研究页：`renderResearchRuntime` 重建为 6 格运行时网格（Planner / Jev / 文章模型 / 对话模型 / 翻译模型 / 榜单模型），并新增琥珀色**降级读取**徽标（说明具体 `cause_code`、回退来源与拦截类型）。
- 资讯页的「来源健康」条由 `#source-health-wrap{display:none !important}` 的永久隐藏改为**可见披露**：折叠标题给出真实计数，点开列出全部来源。

### 4.4 删掉的死代码

`drawGithubCover`、`drawGithubPoster`、`stepImagePicker`、`stepGithubGallery`、`stepSkillsGallery`、`renderSettingsModels`、`endpointLabel`、`loadResearchDossier` 八个函数（3.2.1 明确留待后续迭代）与死元素 `<canvas id="github-canvas" hidden>` 一并删除；`tests/test_web.py` 相应改为**负向**断言，钉住它们不会回来。抽取出的脚本 `node --check` 通过。

### 4.5 一致性与可读性基线

用真实浏览器（CDP）逐页量测，而不是读代码判断：

- **文字截断归零**。改造前信息源 `STRONG`（sw=73 / cw=46，12 行）、运行记录 `.summary-stat`（80/73）与 `STRONG`（200/102）、发布页 H3（109/96）、首页 `.home-search`（141/130）均被裁切；改造后 8 个页面除两处刻意为之（首页 hero 的装饰性溢出、`.sr-only` 屏读文本）外为 0。
- **字号收敛**。改造前 16 种字号、被 13.6px/14px 这类非标尺值主导，最小 **10.88px**；改造后全站最小 **11.50px**（每页逐元素实测）。新的排版都走 `--fs-*` token；少数沿用 `rem` 的旧标题仍留在标尺之外（16.8 / 20.25 / 20.8 / 24 / 26px），都在下限之上，属可读区间，未强行归一以免改动既有标题层级。
- **对比度**。用「渐变按最暗一端计算」的检查器逐页扫描，8 页**全部 0 处**低于 WCAG AA 4.5:1。过程中修掉两个真实缺陷：
  1. 全站除 `#original-sources a` 外没有任何 `<a>` 样式，技能介绍文、资源候选等处的链接回落到浏览器默认深蓝 `rgb(0,0,238)`，在暖墨底上只有 **1.96:1**，等于看不见。补上全局锚点样式。
  2. 琥珀渐变按钮（`.primary` / `.btn-magic`）的最暗端走到 `--amber-3`（`#8a6d34`），深色按钮文字压在上面只有 **3.59:1**。新增 `--amber-deep`（`#b8914f`，5.9:1）作为「承载深色文字」的渐变下限。
  3. `--slate`（`#7d786d`）承载「已停用 / 还没抓过」胶囊只有 4.2:1，提到 `#978f81`。
- **焦点与多选保持两种视觉语言**。`.hotspot-item.selected`（正在读）只是 3px 细条 + 表面抬升，**没有**金色底、金色描边或内阴影；金色 `.is-picked` 只表示「已加入多选」。重构中途曾把 selected 做成金色，已回退——`test_hotspot_selected_css_is_quiet_focus_not_pick_gold` 正是钉这个语义的。
- **统计口径唯一**。信息源页的统计卡与行胶囊此前一个读存储列 `status`、一个读派生 `health`，会互相矛盾（且「待刷新」在统计里完全不可见）。现统一走 `sourceStatusOf()`（优先 `health`）。

## 五、版本与归档

版本 `3.2.1` → `3.2.2`，五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、`src/autoposting/web.py`（`APP_VERSION` + `#service-status` 徽章）、`src/autoposting/llm.py`（`HTTP_USER_AGENT`）、以及测试中的版本断言。

## 六、验证

| 项目 | 结果 |
|---|---|
| 全量测试 | `Ran 881 tests in 191.3s`，`FAILED (errors=1, skipped=1)`；唯一错误是既有的环境性 `test_visual.ExamplePngTests.test_examples_write_html_and_3_by_4_png`（沙箱内 Chrome 建不出临时用户目录；复核用 `git archive` 抽出 3.2.1 树复现同一失败，证明与本版无关）。基线 823 → 881，新增 58 条 |
| 离线自检 | `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check-322` → `"ok": true`，十项检查全 true |
| 语法 | `py_compile` 全部改动模块通过；抽取的内联脚本 `node --check` 通过 |
| 密钥扫描 | `src/`、`tests/`、`config/` 无硬编码密钥；命中项均为字段名/变量名（`x_bearer_token` 输入框与钥匙串赋值） |
| 服务版本 | `GET /api/status` → `version: 3.2.2`，54 路订阅，`planner_configured` / `jev_configured` 均为 true |
| Planner（真实 endpoint） | `planner.plan("AI agent 工具链")` 返回完整 `PlannerResult`（三个 `schema_version`、5 concepts、真实 query 与 adapters） |
| Jev（真实 endpoint） | `decision.assess(...)` → `status: ok`、`confidence: 0.65`、无诊断、无 `not_configured` |
| Reader 回退（反事实） | 注入 resolver：`full` / `rss` / 210 字符 / **0 次页面请求**；不注入：`blocked` / `blocked_http` / 0 字符 / 1 次请求 |
| 失败计数 | 连续三次真实失败 → `1, 2, 3`；一次成功 → 归零并写 `last_success_at` |
| 界面（真实浏览器 CDP） | 8 页文字截断 0、最小字号 11.5px、渐变按最暗端计算的对比度违规 0；信息源统计卡与行胶囊逐项一致；筛选（异常 5 / 启用 36 / 停用 18 / 全部 54）与搜索（`openai` → 4 条、`探针` → 1 条）均生效；运行记录展开 37 行含 15 条 outcome bar 与真实错误原文 |
| 界面证据 | `output/verification/autoposting-3.2.2-{page}-{before,after}.png`，8 个页面 × 前后各一张（1440×900）；`output/` 按既有约定不入库、不入归档 |
| 整改后复测 | 全站最小字号 11.50px（8 页逐元素实测）、对比度违规 0、健康条与信息源页逐项一致（54/36/0/16/5/18）、5 路失败带名字与中文原因 |

## 七、独立复核与复核后修正

交付后由一个未参与实现的 subagent 做了对抗式复核（独立进程、独立临时库；未改任何源码、未动运行中的服务与 `data/autoposting.db`）。结论 **SHIP WITH NITS**：无 blocker、无 major，本版宣称的行为逐条复现（含用 `git archive` 抽出 3.2.1 树复现 `test_visual` 的环境性失败，证明它不是本版引入的）。复核提出的问题中，下列几条已在本版修掉：

1. **资讯页「来源健康」条漏掉全部失败源（本版自身缺陷，最严重的一条）**。该折叠条同时被 `/api/status`（54 行、带 `health`）与资讯/热点载荷（43 行、旧结构、无 `health`）渲染，两者到达顺序不定；后到的旧列表会把注册表结果覆盖掉，于是 5 路失败的 Reddit 源在条上**一行都不显示**，计数也少算。修法：`renderSourceHealth` 优先读 `sourceStatusOf()`（派生 `health`），并记住「注册表形态」——一旦读到带 `health` 的列表，旧结构的子集列表不再允许覆盖它。现状：条与信息源页逐项一致（总 54 / 启用 36 / 待刷新 16 / 异常 5 / 停用 18），5 路失败带名字列出。
2. **失败原因说机器话**。信息源行与健康条原本直接截断打印 `code=source_fetch_failed;cause_code=…;stage=…`。新增 `parseDiagnosticString` / `sourceFailureText` 与 29 个 cause 的中文标签（覆盖后端 `_KNOWN_CAUSES` 全集，已用脚本核对无遗漏），行上显示「抓取失败：HTTP Error 429: Too Many Requests」，原始诊断串仍在该行 tooltip 里。
3. **HTTP 200 的拦截页没有原因码**。按状态码分类只覆盖 4xx/5xx；最常见的拦截其实是 200 + 挑战页，此前 `status=blocked` 但 `cause_code`/`blocked_kind` 都是空。抽出 `classify_block_markers()`，质量闸门判 `blocked_page` / `paywall_or_truncated` 时用同一套标记词给 `blocked_kind`，并据此给 `cause_code`。
4. **`llm.py` 仍有未分类分支**：hermes 进程起不来、`llm_output_too_large`（两处）、响应缺 `messages`、无文本、输出非 JSON 现在都带 `cause_code`（`provider_error` / `invalid_response`）与 `detail`，与同族的退出码分支一致。
5. **`record_fetch_result` 的 docstring 与测试断言还停在修复前**：docstring 仍写「一次失败可能被记两次、streak 会多算」，`tests/test_db.py` 只断言 `>= 1`（双计回归也能通过）。docstring 改写为「每次失败恰好记一次，只有成功才清零」，断言收紧为 `== 1`，双计回归现在会红。
6. **`enabled` 的潜在类型陷阱**：SQLite 回的是 `0/1`，而 JS 用 `source.enabled === false`。新增 `sourceEnabled()`（接受 `0`/`"0"`/`false` 等）并替换 4 处判定，避免将来某条原始行直接进前端时把停用源显示成启用。
7. **`paintCarouselNav` 传了 6 个不存在的 id**：3.2.1 删掉翻页按钮后只留下计数条，函数仍收 `*-prev`/`*-next`。去掉这两个参数与 7 处调用点的悬空 id。
8. **原文回退查表不做规范化**：`research/adapters.py` 直接用候选原始 URL 查库，只差一个 fragment 或跟踪参数就会静默查不到。改为先 `normalize_url()` 再查（仍只用调用方自己的 URL 去读页面）。
9. **`content_source` 判断依赖长度巧合**：原文接口靠「页面文本比库内文本长 80 字符」推断来源。现在有了 `degraded` / `fallback_source`，改为先看显式信号——降级读取永远不会被标成 `page`。
10. **文档里的路由名写错**：验收文档写的 `GET /api/items/{item_id}/original` 不存在，实际是 `POST /api/original`，已改正；行为本身复核通过（`content_source='feed'`、正文与库内字节一致、被拦截时 1 次请求）。
11. **重复的 `#source-health*` CSS**：旧块与设计层定义同一批选择器，后者生效因而今天表现正确，但留着第二份真源迟早漂移。删掉被完全取代的旧规则，只保留设计层未重定义的 `.sh-dot` 取色（顺手把 `#5a564c` 换成 `var(--slate)`）。
12. **品牌副标题字号低于下限**：`.brand-kicker` 用了 `0.62rem`（9.92px），低于本版自定的 11.5px 下限——复核的排版扫描没覆盖到，是我按「全站最小字号」再测时抓到的。改用 `--fs-2xs`，全站最小字号实测回到 11.50px。

复核提出但**有意不改**的两项：`cli collect` 不建热点窗口，因此运行记录的聚类数就是 0（这是真值，不是漏写，已在 `cli.py` 注明）；`db.derive_source_health` 与 `web._runtime_source_health` 仍是两份实现，已有一条逐行比对的交叉测试钉住二者一致，拆成一份属于结构重构，不在本版范围内。

## 八、边界与未做

- 不新增前端框架、不重写 `web.py`、不加新端点；`INDEX_HTML` 仍是单文件内联。
- 正式链路、发布安全边界（微信 live 只写草稿箱、小红书 `manual_required`）、凭据边界（API Key / AppSecret / token 不进日志与响应）均未改。
- 无独立 `github` 适配器：GitHub Releases 仍走 rss `releases.atom`；新增该适配器前不得把条目改成 `adapter_type: "github"`。
- 未加 per-source 条目上限。去重由 `UNIQUE(source_id, external_id)` / `UNIQUE(canonical_url)` / `UNIQUE(content_hash)` 保证，重复抓取不会新增行；`openai_news` 这类 1219 条的 feed 只影响抓取耗时（每条 `insert_item` 各开一次连接/事务），如延迟成为问题再在 `ingestion.py`/`pipeline.py` 加 `max_items`。
- 11 条 reddit 与 Meta 条目在本机保持 `identity_verified: false`；不做任何绕过拦截的抓取。
- 受限沙箱内启动的服务，Chrome 无法创建临时用户目录，出图仍会失败并提示改用 `AutoPosting服务控制.command` 启动（既有行为，本版未改）。
- 历史文档不回改：`docs/PATCH_*.md`（3.2.1 及更早）记录的是当时的真实状态，保留原文。

## 归档

- 归档：`archives/autoposting-patch-3.2.2-update-20260923.tar.gz`
- 校验：`archives/autoposting-patch-3.2.2-update-20260923.tar.gz.sha256`
- 归档只包含 `src/`、`tests/`、`docs/`、`config/` 和 `README.md`、`AGENTS.md`、`pyproject.toml`、`service_control.py`、`AutoPosting服务控制.command`；排除 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv`、`.env`、密钥和临时工作文件。