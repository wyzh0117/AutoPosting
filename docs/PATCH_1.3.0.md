# AutoPosting 1.3.0

## 变更

### 热点候选页

- **A1 生成文章后自动清空「焦点」**：新增 `clearFocusAfterGenerate()`，只在 `POST /api/articles` **成功回调内、payload 组装之后**清空 `state.item` / `state.newsFocus`；正文、标题、`state.draft`、`state.articleRequestGen`、钩子与「已选 N」多选集合全部保持原样。失败分支（`.catch`）不清空，D1 生效。
- **A2 整卡点击即进入「焦点」**：移除标题上隐藏的多选手势（`title.addEventListener("click")` 调 `togglePicked`），点标题 = 点摘要 = 点留白 = 进焦点（D2）。多选改由卡片上显式勾选框 `.ht-pick`（`role="checkbox"` + `aria-checked`，右下角，带 `stopPropagation`）承担。
- **A2 dock 双分组**：`renderPickedDock()` 不再在「已选」非空时提前 return；「焦点」与「已选 N」同时渲染，焦点 chip 用 `.is-static` 保持只读，已选 chip 仍可点击移除。焦点态与已选态视觉解耦（`.hotspot-item.selected` 保持无 accent、无 `box-shadow:inset`）。
- **A3 相关检索修复**：
  - 超时解耦：子进程预算 14s → **20s**，父进程 join 由「= 预算」改为 **30s**（`_RELATED_SEARCH_PROCESS_MARGIN_SECONDS=10`），单请求 4.0s → 3.5s × 4 次尝试；不变式 14s < 20s < 30s，总墙钟 ≤35s。
  - 选区预处理：新增 `derive_related_query()`，中英停用词 + CJK 片段 + n-gram 候选 + 词频/邻字多样性筛选，查询 ≤48 字符；>200 字符走显式 `degraded_leading_clause` 降级（≤24 字符），原始选区仍作为上下文保留。修复中文选区被压成单个 ASCII token 的老问题。
  - 错误分级：`timeout` / `transport_unavailable` / `parse_error` 由 `RelatedSearchError` 抛出，`no_qualified_result` 作为「成功但为空」报告；`Workbench.related_search()` 映射为 `LLMError("related_search_failed", <中文原因>, cause_code=<四级之一>)`，服务端 `LOGGER.warning` 记录 cause/sources/已脱敏 detail，响应体不含上游异常文本。**顶层 `error` 仍是 `related_search_failed`**。
  - 源集合按实测重排（中文优先 Bing News → cn.bing → www.bing → DuckDuckGo；ASCII 优先 www.bing），Bing News 跳转链接解包；Google News 因本机完全不响应而移出源列表（fetcher 与 token 解包代码保留，一行可加回）。
  - 前端：新增 `RELATED_SEARCH_FAILURE_HINTS`，按 `cause_code` 给「检索超时 / 网络不可达 / 解析失败」三种可读文案；空结果显示「没找到相关新闻：检索词「…」没有匹配」，与「检索失败」不再共用一句。

### Skill 查找器

- **B1 接入 skills.sh（D6）**：新增独立数据源适配器，走 `GET https://skills.sh/api/search?q=<urlencoded>`（免费、免鉴权、无官方定价页；第三方 `mastra-ai/skills-api` 的存在正说明没有官方付费 API），标准库 `urllib` 实现，20s 超时，与 GitHub 并列而非替换。`_names_from_web()` 返回带类型的 ref（GitHub / skills.sh / 其他 web）而非静默 `continue` 丢弃非 `github.com/owner/repo`；`_ingest_repo()` 的 `source` 词表扩展为 GitHub / registry / skills.sh，skills.sh 可只凭自身 id 入库。检索词模板按源分派（GitHub 保留 `site:github.com`，skills.sh 用站内检索）。质量门槛为装机量提供等价分支 `SKILLS_SH_MIN_INSTALLS=100`。skills.sh 超时/403/429/不可达/JSON 畸形一律降级：保留 GitHub 结果并置 `degraded=True` + `degraded_reasons`。
- **B2 图片区上移（D3）**：`#skills-studio` 内 DOM 顺序改为 `工具栏 → #skills-gallery → #skills-article`（推翻 1.2.x 的「介绍文必须在画廊上方」决策）；介绍文改为 `max-height:24rem; overflow-y:auto` 的有界滚动框，不再被 3:4 图挤出视野。
- **B3 阶段级拆分（D4）**：移除三处「查找完成即自动出图」（`pollSkillSnapshot()` 与 `searchSkills()` 的两段 `.then`）；查找结束只铺占位 tile（`queueSkillImagesForRender()`），真实出图仅由 `#skills-render-btn` 触发。`skill_search` / `skill_render` 两个 wait job 各自独立、互不干扰。**未新增** `POST /api/skills/image` 一类的张级端点。状态文案分离：查找显示收录数量并提示点「生成图片」，出图显示已生成张数。
- **B4 覆盖率与缺口可见**：`MAX_FETCHES 80→160`，新增按阶段公平切片（调用数与墙钟双维度，`MIN_STAGE_FETCHES=4`），尾部阶段不再被饿死；快照新增 `budget`（每阶段调用/检索词/配额跳过）、`rejected[]`（含原因）、`degraded_reasons`；**空阶段不再被静默删除**，改为 `gap: true` + `gap_reason` 输出，前端 chip 显示「阶段名 · 没找到合格 skill」并在 `#skill-gap-note` 汇总。`SKILL_PATHS` 补齐 `COVERAGE.md` 已承诺的布局：根 / `.agents/skills/<name>/` / `.claude/skills/<name>/` / `skills/<name>/` / `.cursor/skills/<name>/`（有根目录清单才探测，缓存）。
- **B5 海报文字重做（核心）**：根因是 `skills_as_repos()` 把 `kind` 写成 **source**，导致 `KIND_ONE_LINER/DOES/HOW/AUDIENCE` 全部查空、五个板块都落到同一句兜底文案且 `how=""`。
  - `kind` 改为**能力分类**（新增 `SKILL_KIND_RULES`，覆盖 视频/图像/写作/数据/测试/部署/演示/设计/翻译/检索/采集/音频，含「封面→图像」「脚本→写作」「选题/热榜→检索」等复合意图；分类只看 name/description/excerpt，不看覆盖阶段）。`visual/adapter.py` 为这些类别补齐 `KIND_*` 五张表（含「AI 工具」通用档），共 22 类。
  - 新增 `_skill_excerpt()`：从 SKILL.md 提取 600 字符可读正文（去 front-matter/代码块/表格/徽章），随 `_public_skill()` 落库；`_ingest_repo()` 同步补 `topics` / `icon_url`。
  - `skills_as_repos()` 重写：`summary` / `does` / `how` / `audience` / `scene` 优先取 skill 自身中文材料（英文材料一律过滤，不上屏），缺失时用**类别文案 + 覆盖阶段**拼接，且每个板块措辞不同，避免同一张海报出现模板复读。`stars_label` 过 `format_stars()`（装机量，如 `1.9k`）。
  - `fallback_copy()` 改为**尊重调用方已给的字段**，只在为空时回落类别文案。周榜在 10 个既有类别与语言类别上逐字节不变；`kind == "AI 工具"`（无 language 的仓库）会从旧的通用占位句换成本次新增的类别文案（D-7，属改进，非回归）。
  - 退化输入仍有区分度：`_disambiguate_boards()` 对「两个 skill 源材料完全相同」的批次按「阶段 → 名称」逐级加标注（幂等、不叠加标签、不编造能力），保证无阶段、无描述、同描述同阶段等极端批次下五个板块仍两两不同。
  - 文案层与周榜同源：新增 `docs/image_prompts/copy_skill.md` + `leaderboard_prompt.load_skill_copy_prompt()` + `LLMService.enrich_skill_repos()`；`render_skill_package(copy_writer=…)` 由 `web.py` 注入「榜单模型」，`full_name` 复用周榜的 `apply_leaderboard_copy()` 合并逻辑，模型失败保留模板文案。
  - 品牌参数化：`ranking_from_repos()` / `project_from_repo()` / `render_weekly_visual()` 全部接受 `brand/series/source/footer/kicker/issue`（`None`=周榜默认，`""`=留空）；skill 侧传 `issue=""`、`source="skill 查找器"`、`footer="skill 查找器 · 按覆盖阶段整理"`，**不再印 GitHub 周榜字样与「2026年第X周」**。
  - 页码按实际张数：`smart_cover()` 与 `ranking_overview()` 不再写死 `01 / 11`；`render_weekly_visual()` 的 `total` 与真实张数对齐。
  - `visual_kicker="SKILL FINDER"` 真正上屏：`smart_cover()` 新增 `.c-smart-cover-kicker` 渲染（已补 CSS）。
- **B6 删除死代码**：删 `src/autoposting/skill_cover.py`、`docs/image_prompts/skill_cover.md`、`tests/test_skill_finder.py` 的 `SkillCoverTests` 与 import。`title_poster.py`、`images.py` 未动。
- **B7 图像模型链路彻底拆除（D5 + D8）**：
  - 删 prompt 层：`image_gen.py` 的 `IMAGE_PROMPT_FILES` / `load_image_prompt()` / `load_weekly_image_prompts()` / `_render_prompt()` / `GenerateFn` / `render_weekly_images()`、`visual/story.py::generate_missing_images()`、`docs/image_prompts/{STYLE.md,cover.md,poster.md}`、`tests/test_image_gen.py`、`tests/test_visual.py::StyleBibleCompatTests`。
  - 删配置面：`llm.py` 的 `IMAGE_ASPECT_RATIO` / 尺寸候选 / `HermesRunner.generate_image` 系列 / `LLMService.generate_image` / `configure_verified_image`；`web.py` 的 `set_image_model()`、`/api/image/model` 路由、`image_llm` 状态字段、`_image_model_configured` 全部读写点、`image_service` / `image_generator` 注入、设置页「出图模型」卡片与「用于出图」按钮、前端 `state.image` 与提供方映射。
  - **被牵连链路的修复（拆除后不留断点）**：`build_weekly_image_jobs()` → **`build_weekly_render_plan()`**，只产出 `id/kind/rank/title/url/file/selected`，**不再有 `prompt` 字段**，`_safe_name()` 保留（`/api/github-weekly/image` 仍按 `id` 匹配，已验证不断）；`image_prompt_dir()` 能力迁到 `visual/paths.py::asset_root()`（保留 `AUTOPOSTING_IMAGE_PROMPT_DIR` 覆盖与回落 `docs/image_prompts` 行为，`visual_root()` 保留为别名）；`docs/image_prompts/` 下 HTML 出图资产（design-system / components / page-schemas / content-schemas / rendering / examples / templates / assets / legacy）与文案 prompt `copy.md` 全部保留。
  - 数据边界：`model_endpoints` 表中 `purpose='image'` 的历史行**不删除用户数据**，仅移除读写入口与 UI；人工清理见下。

## 边界

- 出图只剩一条链路：HTML/CSS 页面 → 浏览器截图（`visual/pages.py` → `visual/css.py` → `visual/screenshot.py`）；**不得再新增图像模型调用点**，`AGENTS.md` 的 Image prompting 段已改写为这一条约束，并新增源码级回归护栏（`tests/test_web.py::test_no_image_model_call_site_can_come_back`）。
- 纵横比统一 3:4（1080×1440）。`.xhs-paper{aspect-ratio:9/16}` 是小红书纸卡 CSS，与图像模型无关，保留。
- 未改文本生成链路（文章 prompt、翻译、榜单改写逻辑）；`docs/image_prompts/copy.md` 未改，新增仅 `copy_skill.md`。
- 未新增运行时依赖，未新增张级出图端点，Xiaohongshu 仍为手动打包。
- `model_endpoints` 历史行清理方式（可选，仅影响设置页里不再显示的幽灵条目）：
  `sqlite3 data/autoposting.db "DELETE FROM model_purposes WHERE purpose='image'; DELETE FROM model_endpoints WHERE endpoint_id NOT IN (SELECT endpoint_id FROM model_purposes);"`
  未执行该清理；`model_endpoints` 里 image 行既不读也不写。注意 `db.py::_ensure_image_model_purpose` **保留**：它是把 `model_endpoint_purposes.purpose` 的 CHECK 约束放宽到含 `'image'` 的历史迁移，删掉会让老库里的 image 行违反约束、被迫删数据，属于「保留用户数据」边界的一部分，不是遗留配置面。

## 独立验收发现的缺陷与修复

一次对抗式复验发现并已修复以下问题（每条都有回归测试）：

| 编号 | 严重度 | 问题 | 修复 | 证据 |
|---|---|---|---|---|
| D-1 | 高 | `fetch_skill_md()` 把 `{name}` 填成**仓库名**，skills.sh 的 monorepo 布局（`<skill>/SKILL.md`）永远探不到；skills.sh 装机量第一的 `genmedia-labs/skills/video-edit`（513,911）因此必然被拒 | 新增 `skill_name` 参数，`{name}` 用**skill 名**；`SKILL_PATHS` 补 `{name}/SKILL.md` | 实测 `fetch_skill_md("genmedia-labs/skills", getter, "video-edit")` → 命中 `video-edit/SKILL.md`（11,159 字符）；旧行为对照仍失败 |
| D-2 | 高 | GitHub 403 时把 `skill_path` 硬写成 `"SKILL.md"`，空内容也能过质量门槛，于是「已定位 SKILL.md」是伪造的，诊断报告的 8/8 覆盖是假象 | 限流时 `skill_path` 留空；`quality_reason` 的 `has_file` 改为「有正文 / URL 自带 SKILL.md / **显式** `skill_verified`（仅由上一轮已通过的复用标记）」；不再用裸 `skill_path` 当证据 | 新增 `test_rate_limited_candidate_is_not_recorded_as_verified`（拒绝理由 `no_skill_md`）、`test_a_declared_skill_path_alone_does_not_clear_the_gate` |
| D-3 | 中 | B5 的「两两不同」在退化输入下仍会塌陷；防线只要求「含一个中文字」 | `_disambiguate_boards()` 对完全相同的板块按「阶段 → 名称」幂等加标注（不叠加标签、不编造能力）；`skills_as_repos` 侧同名/同来源去重，与 `_ingest_repo` 一致 | 新增 `test_degenerate_inputs_still_produce_distinct_boards`（4 种极端批次，含同描述同阶段 ×5） |
| D-4 | 中 | 收藏夹是同一个「已选」集合的第二个入口，却只有隐藏的标题手势、没有勾选框，与 A2 原则不一致 | 收藏项改用同一个 `pickToggle()` 勾选框，去掉标题手势；`syncPickedHighlights()` 覆盖 `#favorites-list .ht-pick` | 重写 `test_favorites_use_the_same_explicit_checkbox_as_hotspot_cards` |
| D-5 | 中 | `redact_related_detail()` 只去 URL userinfo，token 形式的查询串会**原样进服务端日志**（响应体一直是干净的） | 扩展脱敏规则（查询串凭据、`Bearer`、长不透明 token），并覆盖全部日志调用点 | 见「日志脱敏」小节 |
| D-6 | 低 | 失败路径只给 `error_code`，成功路径给 `cause_code`，命名不一致 | 失败响应同时给出 `cause_code` 别名（`error_code` 保留，兼容既有前端与断言） | `test_web.py` related-search 契约仍绿 |
| D-7 | 低 | 文档称「周榜行为不变」，但 `fallback_copy()` 尊重调用方字段后，`kind == "AI 工具"`（无 language 的仓库）的兜底文案确实变了 | 保留新文案（旧的正是那条通用占位句），**更正文档表述**：周榜在 10 个既有类别上加语言类别上逐字节不变，仅 `AI 工具` 这一类换成更具体的类别文案 | 68 例对照差异（仅 8 例属 `AI 工具`） |
| D-8 | 低 | 文档/文案残留：README 与功能测试文档仍写「图像模型 9:16」，`project-detail.md` 与 type-scales 示例仍是 9:16，PATCH 清理 SQL 表名写错，设置页说明仍列「出图」 | 全部改正：README、`docs/WEB_FUNCTIONAL_TEST.md`、`page-schemas/project-detail.md`、`examples/type-scales/*`（含其 README）改为 3:4；清理 SQL 表名改为真实的 `model_endpoint_purposes`；设置页说明去掉「出图」；`copy_skill.md` 权限 600→644；删除残留的 `tests/__pycache__/test_image_gen.cpython-311.pyc` | `grep 9:16` 仅剩小红书纸卡 3 处（有意保留） |
| D-9 | 低 | `COVERAGE_DIAGNOSTIC_1.3.0.md` 部分数字不可复现，且「8/8 覆盖、嵌套布局无额外开销」的结论受 D-1/D-2 影响 | 在该文件顶部加勘误小节，标明哪些数字可复现、哪些是 D-1/D-2 造成的假象 | 见该文件「勘误」 |
| D-11 | 中 | 复用同一个包目录（含共享 `latest`）时，上一轮缩小后的旧页面不会被清掉，导致封面写 `01 / 03` 而残留页仍写 `04 / 04`，页码与实际张数不一致 | `render_skill_package()` 渲染前按本轮 job id 清理 `images_dir` 里多余的 `*.html`/`*.png`（`_prune_stale_pages()`） | 新增 `test_rerender_prunes_pages_that_are_no_longer_in_the_package`；实机重启服务后复测：残留的 `03-topic-radar.html` 被清掉，页码收敛为 `01 / 03`、`02 / 03`、`03 / 03` |
| D-10 | 低 | `.rstrip(".git")` 是字符集剥离会损坏仓库名；`MIN_STAGE_FETCHES` 在 `budget < 4×阶段数` 时会饿死尾部；同一 skills.sh 仓库的兄弟 skill 被仓库级去重吃掉；skills.sh 链接正则未锚定域名；降级状态无 UI | 逐个修复：`removesuffix(".git")`；阶段份额改为「为后续每阶段预留 1 次调用」；skills.sh 跳过仓库级去重；正则锚定 `//`/`.`/行首；新增 `#skill-degraded-note` 展示降级原因 | 新增 `test_stage_floor_keeps_one_call_for_every_later_stage`、`test_skills_sh_monorepo_siblings_survive_repo_level_dedupe` |

## 验证

- 全量测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests -v` → **472 tests OK（1 skipped = 需显式开启的真实网络冒烟）**。其中 `test_visual.py` 的 PNG 用例需要浏览器可写 `~/Library/.../Chrome`（DSH 沙箱下会失败，非代码问题）。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check` → `ok: true`，10 项检查全通过。
- 服务版本：`/api/status` → `1.3.0`，且**不含 `image_llm`**。
- 实机（浏览器，`ego-browser`，服务跑在本仓库 1.3.0 上）：
  - A1：点「生成文章」→ `文章已生成`、正文 1121 字符落地；dock 由 `["焦点","已选 2"]` 变为 `["已选 2"]`（焦点消失、多选与勾选框保留、`selected` 归零、正文未重置）；**制造真实失败**（模型返回缺 AIGC/来源索引）→ 焦点 chip 与其标题仍在；生成后再点卡片 → 焦点立即回来。
  - A2：勾选第 1、2 张卡的勾选框 → 仅 `已选 2`、无焦点、无 `.selected`；点第 3 张的**标题** → 同时出现 `焦点` + `已选 2`（`is-static` 焦点 chip）；点第 4 张摘要 → 焦点切换而多选不变。
  - A3：200 字中文选区点「相关检索」→ `找到 1 条相关新闻`（8 秒，真实网络）；**连续 5 次 5/5 成功、0 次假失败**；`relatedSearchFailureMessage()` 对 timeout / transport_unavailable / parse_error 返回三种不同可读文案，未知码回落原文案。
  - B2：skill 车道 DOM 顺序 `gh-toolbar → skills-gallery → skills-article`，画廊 top 253 < 介绍文 top 1042；介绍文 `max-height:384px; overflow-y:auto` 且完整可读。
  - B3：真实查找（GitHub + skills.sh + 网页，8 阶段）→ `已收录 2 个 skill，点「生成图片」开始出图`，介绍文 417 字符、3 个占位 tile、**磁盘 PNG 数量不变（0 张新增）**；点「生成图片」→ `已生成 3 张`、tile 变为真实图、介绍文仍在；勾选/取消 + 导出真实下载 74KB PNG。
  - B4：查找结果显示 8 个阶段，其中 6 个为 `· 没找到合格 skill` 缺口 chip，`#skill-gap-note` 汇总「有 6 个环节没找到合格 skill：…」。
  - B5：真实查找产出的 poster 逐张比对 —— 分类 `视频` / `写作`（不再是 `GitHub`），「一句话看懂 / 做什么 / 如何实现 / 适合哪些人群 / 使用场景」五个板块两两不同，`做什么` 取到 skill 自身中文材料；图上无 `GitHub`、无 `2026年第X周`、无写死 `01 / 11`、无 `Stars`；封面出现 `SKILL FINDER`；页码 `01 / 03`、`02 / 03`、`03 / 03`；4 张 PNG 全为 1080×1440（3:4）。
  - B7：`POST /api/github-weekly` → 10 repo + 1 cover = 11 条渲染计划，第一条 `kind=cover`、`title=本周爆火ai工具`，**响应无 `prompt` 字段**；`POST /api/github-weekly/render` 出 11 张真实 3:4 PNG（带 `?v=` 缓存破坏）；`POST /api/github-weekly/image {"id":"01-i-have-adhd"}` 按 id 命中并返回新 `?v=` URL，取图 1080×1440（**job id 匹配未断**）；`/api/skills/render` 出 cover + N 张 3:4。
  - D6/D8：设置弹窗不再出现「出图模型」卡片与「用于出图」按钮、无 `image_provider` 选择器、`window.state` 无 `image`；`/api/status` 无 `image_llm`。
  - 残留：`grep -rn "generate_image|IMAGE_ASPECT_RATIO|configure_verified_image|load_image_prompt|render_weekly_images|generate_missing_images|image_llm|_image_model_configured|STYLE\.md|poster\.md|cover\.md|build_weekly_image_jobs|skill_cover" src/` → 无输出；四个 model-image prompt 文件均不存在。
- B1 真实查找证据：`视频制作` 一次真实查找出现 `source == "skills.sh"` 的行（如 `yzfly/douyin-mcp-server/douyin-video`、`youtube-clipper`、`absurdity`）；降级路径实测（连接被拒 / 真实 401）→ `degraded=True` 且 GitHub 结果照常返回。
- B5 文案层实机证据：渲染时模型（本地 OpenAI 兼容 fixture）返回的 skill 文案被 `enrich_skill_repos()` 按 `full_name` 合并进海报，`做什么 / 如何实现 / 适合哪些人群 / 使用场景` 直接采用模型改写结果（`给做短视频的人，手上素材要变成成片的时候，把粗剪这一步接过去。` 等），证明「与周榜同源文案层」在生产路径上真的接通了。
- B5 退化输入实测：英文-only 同类别 ×5、无阶段无描述 ×5、同描述不同阶段 ×3、同描述同阶段 ×5 四种极端批次下，五个板块（不含本就应当相同的 `category`）均两两不同，且无标签叠加。
- B4 诊断报告：`docs/skill_finder/COVERAGE_DIAGNOSTIC_1.3.0.md`（改动前后对比：对外可见阶段 3/8 → 8/8，预算 80/80 打满 → 102/160 且尾阶段仍有检索，拒绝理由可解释，`SKILL.md` 可定位布局 1 → 5）。
- 服务端口说明：本机 `127.0.0.1:8080` 由兄弟目录 `~/Documents/MiniWork/AutoPosting`（另一个 checkout）占用，遵循「不与兄弟目录混用」未接管；本次实机验证在本仓库 1.3.0 服务的 `127.0.0.1:8090` 上完成，验证后已停止。

## 同步修改的测试

| 测试 | 关联 | 处置 |
|---|---|---|
| `test_web.py` 标题点击契约 | A2 | 重写为「热点卡内无 title click 劫持」+ 断言 `pickToggle` |
| `test_web.py` 新增 4 条 | A1/A2 | 焦点与已选并存、显式勾选框、成功后才清空（含顺序与失败分支） |
| `test_web.py` 周榜 3:4 用例 | B7 | 删 `IMAGE_ASPECT_RATIO` 与 `cover.md` 断言，保留 3:4 / 9:16 纸卡断言，新增四文件不存在断言 |
| `test_web.py` 零图像模型护栏 | B7 | 由「注入 `image_generator` 计数」强化为「属性不存在 + `/api/image/model` 非 200 + 源码级残留扫描」 |
| `test_skill_finder.py` 布局契约 | B2（D3） | **推翻**：改为断言画廊在介绍文上方 + 工具栏紧贴画廊 + 介绍文有界滚动 |
| `test_skill_finder.py` 自动出图契约 ×2 | B3（D4） | 改为断言查找链路**不含** `renderSkillImages()`、只铺占位、仅按钮触发、无 `/api/skills/image` |
| `test_skill_finder.py` `skills_as_repos` | B5 | `kind` 断言由 source 改为能力分类；新增 7 条海报文字/品牌/页码/装机量/文案层/退化输入用例 |
| `test_skill_finder.py` skill_cover / 9:16 | B6/B7 | 随删除模块一并移除 |
| `test_llm.py` 7 处图像模型用例 | B7（D8） | 整类 `ImageGenerationRunnerTests` 删除 |
| `test_visual.py` `StyleBibleCompatTests` | B7（D5） | 删除；新增渲染计划契约（无 `prompt` 键）、车道参数、封面 kicker/页码守卫 |
| `test_image_gen.py` | B7（D5） | 整文件删除 |
| `test_background.py` / `test_workbench.py` | A3 | 新增超时解耦不变式、四种 cause_code、查询预处理、日志与不泄漏、可选真实网络冒烟 |
