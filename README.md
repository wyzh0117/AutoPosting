# AutoPosting MVP

当前版本：`3.2.2`

3.2.2 状态：`Product Hardening + Source Expansion + UI Refactor: CLOSED`。本版做了四件事：把订阅源从 37 条扩到 **54 条（36 条启用）** 并给每条健康度（`health` / `last_success_at` / `consecutive_failures` / `topic_scope` / `language`，DB schema v5）；修好 Planner 与 Jev 的 provider 接缝与响应契约（此前每一次真实调用都因契约不匹配而静默降级，`configured` 却报 true）；给 Reader 加抓取失败分类与「已采集正文」合法回退（被 403/429 拦截时不再丢证据，且绝不伪造正文）；整站界面按统一设计标尺重构（信息架构、外壳、页头、数据行、排版与对比度基线）。小红书仍只走人工发布。

两轮重构状态：`Two-Iteration Refactor: CLOSED`。官方链路固定为 **Information Discovery → Event Cluster → Topic Dossier → Creation → Article Generation**：资讯页发现事件，研究页把选中事件变成可持久化的 Topic Dossier，创作页消费 Dossier 生成文章。「找切口」（hooks）、独立的相关搜索入口与 lane 产品机制都已从产品中删除，不再作为能力提供。

本项目是一个本地优先、可配置的 AI 资讯自动化骨架。它使用 Python 3.11 标准库和 SQLite，覆盖 RSS 增量采集、规范化去重、热点汇总、事实清单、微信/小红书独立草稿、人工审批、本地发布包，以及可选的微信官方 live 发布适配器（需显式配置和选择）。小红书没有普通账号官方发文 API，始终走人工发布。

## 安装

要求 Python 3.11+。运行时无第三方依赖。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
autoposting self-check --workspace data/self-check
```

不安装也可直接运行：

```bash
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
```

## 配置

- `config/sources.example.json`：来源注册表，54 条（36 条启用）。等级映射为 A→S1、B→S2（社区为 S4）、C→S3；每条都有 `topic_scope` 与 `language`。GitHub Releases 用 `adapter_type: rss` 走 `releases.atom`（没有独立的 github 适配器，写 `adapter_type: github` 会被判 `unsupported_source`）；`identity_verified` 只对核验过的来源为真，本机够不到的一律为 false 并保持关闭。默认启用一手 RSS（官方实验室 + 可靠媒体）以及 DeepSeek 官方文档 sitemap（`deepseek_official`，S1）。DeepSeek 没有公开 RSS，采集其官方 `api-docs.deepseek.com/sitemap.xml` 里的 `/news/news*` 静态页，不是二手聚合。`ithome` / `hn_frontpage` 带 `content_filter: "ai"`：标题必须是 AI 主题，早报、开售/优惠价、耳机手机汽车等零售稿即使正文里出现 “AI” 也会丢掉。生成文章前会读原文、查库内相关旧闻并做一次公开网页检索，把前因脉络写给没追过这件事的普通人（手动选材的单条 / 多条融合路径仍如此；从 Topic Dossier 生成时不再二次检索，只消费 Dossier 已有证据）。`aihot_aggregate` 保留但关闭。S4 讨论源（HN、可选 X 账号）可以直接写成观察帖；讨论量过阈值后该条升温为 S3 关注级，仍不是官方证实。X 抓取对齐 AIHOT 的方法：官方用户时间线、排除回复、丢掉纯转发、只收推文附图（`pbs.twimg.com/media`），不爬 X 网页、不把头像当配图。Bearer 只留进程内存；示例表登记了实验室官方号和观察号，默认关闭，填令牌后再启用。S4 讨论号可不做身份核验，S1/S2 仍要 `identity_verified=true`。成稿配图会丢掉头像、页眉图标和过小的方图。公众号没有公开 RSS，本仓库不抓。
- `.env.example`：部署时可能使用的本地路径和规则变量。当前 CLI 以命令行参数为准，不自动读取 `.env`。
- `config/hotspot.example.json`：热点规则输入示例。
- `config/facts.example.json`：结构化事实清单示例。

来源等级：S1 官方来源；S2 人工确认员工账号；S3 可靠媒体或讨论升温后的关注级；S4 社区/一线观察，可以单独成篇，文中按「某某称」转述。

## 运行

初始化数据库并注册来源：

```bash
autoposting init --db data/autoposting.db --sources config/sources.example.json
```

运行一轮增量 RSS 采集：

```bash
autoposting collect --db data/autoposting.db --sources config/sources.example.json
```

建议由系统 cron 每 15 分钟调用一次。程序会根据每个来源的 `fetch_interval` 再次判断是否到期：

```cron
*/15 * * * * cd /absolute/path/to/AutoPosting && .venv/bin/autoposting collect >> data/collector.log 2>&1
```

查询过去 24 小时候选：

```bash
autoposting daily-candidates --db data/autoposting.db --hours 24
```

测试或记录事件候选：

```bash
autoposting hotspot-check --input config/hotspot.example.json
autoposting hotspot-check --input config/hotspot.example.json --db data/autoposting.db --record
```

创建事件、校验事实并生成双平台草稿：

```bash
autoposting event-create --db data/autoposting.db --name "Example AI release" --topic models --stable-key "daily:2026-08-08:example"
autoposting draft --db data/autoposting.db --event-id EVENT_ID --facts config/facts.example.json
```

事实清单中的每个 `source_id` 必须已注册。以下情况会进入 `FAILED` 并阻断草稿或审批：无来源、未知来源、不可回溯事实、超出证据推断、超过 280 字的逐字引用、未说明的来源冲突。

人工逐份批准或拒绝：

```bash
autoposting approve --db data/autoposting.db --draft-id DRAFT_ID --reviewer reviewer-name
autoposting reject --db data/autoposting.db --draft-id DRAFT_ID --reviewer reviewer-name --reason "需要补充来源"
```

事件下所有已存在草稿均批准（至少一份）后，生成本地 dry-run 发布包：

```bash
autoposting package --db data/autoposting.db --draft-id DRAFT_ID --output output/packages
```

只生成单平台草稿（见下文 Web API）同样适用：单份草稿批准即视为事件批准。

所有命令输出 JSON，便于 cron 或后续编排器读取。

## Web API（本地）

macOS 可直接双击项目根目录的 `AutoPosting服务控制.command`。控制窗口只有一个按钮：

- 服务已停止时显示“启动服务”；点击后启动 `127.0.0.1:8080`，健康检查通过后自动打开浏览器。
- 服务运行时显示“停止服务”；点击后只停止本项目的 AutoPosting 进程。
- 关闭控制窗口不会停止已经运行的服务。启动日志写入 `output/service-control.log`。

启动仅绑定 127.0.0.1 的 HTTP API：

```bash
autoposting web --db data/autoposting.db --sources config/sources.example.json --port 8080
```

浏览器打开 `http://127.0.0.1:8080/` 即单页工作台。页面按信息架构组织：顶栏八个栏目**首页、资讯、资源、创作、发布、信息源、运行记录、设置**，对应 `data-app-page="home|news|resources|creation|publishing|sources|runs|settings"`；另有 `data-app-page="research"` 的「事件研究」页，由资讯页的「研究已选事件」进入，不在顶栏。旧的「热点候选 / 观点候选 / GitHub 周榜 / Skill 查找器」四个一级栏目已删除——社区来源并入统一资讯探索，GitHub 周榜降级为资源探索里的 Preset，Skill 只是资源探索的一种 Resource Type；`[data-lane]` 页签、`legacy-lane-tabs`、`body.lane-*` 与 lane 状态一并删除，编辑源 / 社区源的区分改由资讯工具栏的「来源范围」（`#news-scope-select`，`scope=all|official|community`）承担。资讯页以 Event Cluster 为展示单位，官方源与 Reddit 可在一次采集中合并并保留 Collection Run 记录；选中事件后点「研究已选事件」调用 `POST /api/research`，研究页展示 Topic Dossier 的状态、事件 / 主题 / 证据统计与证据清单、诊断。资源页用自然语言同时搜索 Repo / Skill / Web，结果是与研究共享的 ResourceSet（含来源状态与诊断）；需要出图时从资源页送入创作页的「GitHub 周榜出图工作台」或「Skill 海报出图工作台」，两条工作台继续复用原来的 3:4 视觉、`SKILL.md`/monorepo/粘贴链接能力，但只做「HTML/CSS → 浏览器截图」，Skill 的查找 / 粘贴输入留在资源探索页（`#resource-query` / `#resource-pasted-text`），工作室只按传入的关键词或链接出图，收录来源与降级说明在 `#skills-sources`（`#skills-source-chips` / `#skills-degraded-note`）里可见，GitHub Token 只在设置页配置。研究页的「进入创作」（`#research-to-creation-btn`，有 Dossier 后才可点）把当前 Dossier 作为 `ArticleGenerationContext` 带入创作页，文章区顶部出现 `#article-context` 横幅（可点 `#article-context-clear` 清除），此时不必再挑候选 item 就能生成文章；不带 Dossier 时仍可手动选 item 生成。发布页集中列出草稿、审批状态与最近发布尝试，微信与小红书动作仍复用同一条发布链路。创作页只有一栏文章区（候选列表在资讯页，选中状态跨页保留）：标题和正文可在文章区修改并保存；原文配图收在折叠的配图栏（`#image-picker`）内滚动，照片再多也不靠整页滚轮翻；模型选择和发布位于文章区；两块出图工作台在文章区下方。顶栏「设置」配置文章 / 翻译 / 榜单模型、写作风格、GitHub Token、微信公众号 API、小红书账号与 X 读取令牌（`POST /api/chat/model` 与 `POST /api/chat` 仍作为接口保留，但工作台当前没有聊天输入框）；模型接口元数据持久化到 SQLite，API Key 与平台账号凭据存入 macOS 钥匙串，重启后自动恢复，可手动删除。小红书仍只生成本地包，需到官方创作中心人工提交。

`--host` 只接受 `127.0.0.1` 或 `localhost`，其他值直接拒绝，避免无认证暴露。端点为：

```bash
curl http://127.0.0.1:8080/api/status
curl 'http://127.0.0.1:8080/api/hotspots?hours=24&scope=official'
curl -X POST http://127.0.0.1:8080/api/collect -H 'Content-Type: application/json' -d '{"hours": 4, "scope": "all"}'
curl -X POST http://127.0.0.1:8080/api/research -H 'Content-Type: application/json' -d '{"event_ids": ["EVENT_ID"], "query": "可选：限定研究问题"}'
curl http://127.0.0.1:8080/api/research/DOSSIER_ID
curl -X POST http://127.0.0.1:8080/api/schedule -d '{"draft_id": "DRAFT_ID", "platform": "wechat", "live": true, "run_at": "2026-08-13T20:00:00"}'
curl -X POST http://127.0.0.1:8080/api/models -H 'Content-Type: application/json' -d '{"base_url": "https://api.example/v1", "api_key": "..."}'
curl -X POST http://127.0.0.1:8080/api/model -H 'Content-Type: application/json' -d '{"model": "selected-model", "base_url": "https://api.example/v1", "api_key": "...", "effort": "high"}'
curl -X POST http://127.0.0.1:8080/api/chat/model -H 'Content-Type: application/json' -d '{"model": "chat-model", "base_url": "https://api.example/v1", "api_key": "..."}'
curl -X POST http://127.0.0.1:8080/api/chat -H 'Content-Type: application/json' -d '{"messages": [{"role": "user", "content": "你好"}]}'
curl -X POST http://127.0.0.1:8080/api/articles -H 'Content-Type: application/json' -d '{"dossier_id": "DOSSIER_ID", "platform": "wechat", "model": "selected-model", "effort": "high"}'
curl -X POST http://127.0.0.1:8080/api/articles -H 'Content-Type: application/json' -d '{"item_id": "ITEM_ID", "platform": "wechat", "model": "selected-model", "effort": "high"}'
curl -X POST http://127.0.0.1:8080/api/articles/edit -d '{"draft_id": "DRAFT_ID", "title": "...", "body": "... AIGC ... SOURCE_ID ..."}'
curl -X POST http://127.0.0.1:8080/api/approve -d '{"draft_id": "DRAFT_ID", "reviewer": "human"}'
curl -X POST http://127.0.0.1:8080/api/config -d '{"platform": "wechat", "app_id": "...", "app_secret": "...", "author": "..."}'
curl -X POST http://127.0.0.1:8080/api/publish -d '{"draft_id": "DRAFT_ID", "platform": "wechat", "live": true}'
curl -X POST http://127.0.0.1:8080/api/skills/search -H 'Content-Type: application/json' -d '{"query": "从零做播客"}'
curl -X POST http://127.0.0.1:8080/api/skills/paste -H 'Content-Type: application/json' -d '{"links": "https://github.com/anthropics/skills/tree/main/skills/web-artifacts-builder"}'
curl -X POST http://127.0.0.1:8080/api/skills/github-token -H 'Content-Type: application/json' -d '{"token": "ghp_..."}'
curl -X POST http://127.0.0.1:8080/api/skills/render -H 'Content-Type: application/json' -d '{"theme": "dark"}'
curl http://127.0.0.1:8080/api/skills
curl -X POST http://127.0.0.1:8080/api/resources/discover -H 'Content-Type: application/json' -d '{"query": "能自动制作 PPT 的 Agent Skill / Repo", "adapters": ["github", "skills", "web"], "limit": 20}'
curl -X POST http://127.0.0.1:8080/api/resources/discover -H 'Content-Type: application/json' -d '{"preset": "github_weekly", "adapters": ["github"]}'
curl http://127.0.0.1:8080/api/resources?limit=20
curl http://127.0.0.1:8080/api/drafts?limit=20
curl http://127.0.0.1:8080/api/drafts/DRAFT_ID
```

- `collect` 默认强制刷新已启用一手 RSS（`force: true`）；可传 `source_id` 只刷新一个源。不抓 AIHOT 聚合页或 API。响应含 `rankings.heat`（独立来源数优先，其次新旧）和 `rankings.time`（新旧优先，其次来源数），以及 `sources` 健康状态（`ok`/`error`/`stale`/`never`/`disabled`）和 `collection.inserted`。`candidates` 仍是时间优先列表，兼容旧调用。窗口内最多各取 10 条，不是 `hotspot-check` 的统计显著热点。刷新全部只抓源、不阻塞翻译，列表先显示库里已有候选。`hours` 接受 1–720；工作台默认 4 小时，并提供 4/6/24/72/168/720 与自定义。资讯页是单栏：工具栏（来源范围、时间范围、刷新全部、翻译标题、排序、打开原文、研究已选事件）下面是话题/新闻卡片列表，列表底部折叠的是订阅源健康（抓取成功 / 只刷这个），不是新闻标题；排序可在热度优先 / 时间优先之间切换。网页逐步点选验收见 `docs/WEB_FUNCTIONAL_TEST.md`。
- `scope` 取代了旧的 lane 页签，是编辑源 / 社区源的唯一产品概念：`scope=all`（默认，全部来源）、`official`（仅订阅源，即非 reddit 类型）、`community`（仅社区来源，即 reddit 类型）。`GET /api/hotspots?hours=24&scope=official` 用它过滤窗口，`POST /api/collect` 用它决定本次真正抓哪些源（`{"hours": 4, "scope": "community"}`）。非法值报错而不静默放宽。兼容说明：`lane=hotspots|viewpoints|mixed` 仍作为**薄 HTTP 边界别名**被接受（`GET /api/hotspots` 的查询参数、`POST /api/collect` 的请求体），分别映射到 `official` / `community` / `all`；同时传 `scope` 与 `lane` 时以 `scope` 为准。映射实现在 `src/autoposting/legacy_compat.py`，内部代码只说 `scope`，lane 已不是产品概念。
- `research` 是 Topic Dossier 的唯一入口。`POST /api/research` 接收 `{"event_ids": ["EVENT_ID", ...], "query": "可选：限定研究问题"}`（`event_ids` 必须是非空字符串数组，否则以 `event_ids_must_be_list` / `event_ids_count_invalid` / `event_id_invalid` 等稳定码报 400），由 Research Service 统一编排：Planner 出计划 → Web 检索（复用保留的 related-search 引擎，`src/autoposting/research/adapters.py` 的 `RelatedNewsSearchAdapter` 即其适配器）→ Reader 读正文 → Decision/Jev 逐条判断证据，最后产出 `schema_version = "topic-dossier-v1"` 的 Dossier（含 `dossier_id`/`title`/`query`/`event_ids`/`topics`/`plan`/`evidence`/`groups`/`status`/`degraded`/`diagnostics`/`created_at`）并持久化到 SQLite。`GET /api/research?limit=N` 列出最近 Dossier（1–100），`GET /api/research/{dossier_id}` 回读单份。Jev 不可用时退回 objective-only 并保留诊断，单个来源失败不拖死整次研究。
- `resources` 是资源探索的统一入口，替代原来的「GitHub 周榜 / Skill 查找器」两个一级栏目。`POST /api/resources/discover` 接收 `{"query":"自然语言需求","adapters":["github","skills","web"],"pasted_text":"可选 Skill 链接","limit":20,"persist":true}`；`preset":"github_weekly"` 时忽略 query，走原来的周榜抓取（`本周 AI 新项目`）。结果是一个版本化 ResourceSet：包含 Planner 计划、每个来源的 `adapter_status`（requested/attempted/status/count/cause_code）、脱敏 `diagnostics`、候选（kind = repo/skill/website/tool/other、客观分、两轮 Decision/Jev 判断与 `selected`）和 `degraded` 标记。`GET /api/resources?limit=N` 列出已持久化的 ResourceSet，`GET /api/resources/{set_id}` 回读，`POST /api/resources/{set_id}/refresh` 只回读不重跑外部搜索。核心约束：Jev 不可用时退回 objective-only 并保留诊断，单个来源失败不拖死整组结果；未知/损坏 payload 报稳定错误而不是静默为空。
- `drafts` 是发布页的数据入口。`GET /api/drafts?limit=N` 返回最近草稿摘要（`draft_id`/`event_id`/`platform`/`title`/`status`/审批信息/时间戳/最近一次发布尝试 `attempt_status`），`GET /api/drafts/{draft_id}` 返回可载入创作页的完整草稿（标题、正文、来源 ID、配图选择、小红书卡片）。发布动作仍走既有的 `/api/approve`、`/api/publish`、`/api/schedule`，安全边界不变。
- `github-weekly` 的出图能力保留在创作页的「GitHub 周榜出图工作台」，发现入口改由资源探索的 `github_weekly` preset 承担。`POST /api/github-weekly` 只抓仓库和文案，不生成图片。优先本周新建的 AI 仓库（Search `created:>=7d`），不够再用 Trending weekly 补齐，固定前 10。「生成图片」由界面逐张调用 `POST /api/github-weekly/image`（`{id, theme, palette, poster_template}`），批量入口 `POST /api/github-weekly/render` 仍保留给脚本用：出图只有一条链路——HTML/CSS 页面排版后用本机 Chrome 截 PNG（装了 Playwright 时优先走它，否则回落 Chrome/Chromium/Edge），画布统一 3:4（1080×1440），周榜封面、项目卡与项目海报同为 1080×1440，小红书纸卡仍是 9:16 手写纸比例。工作室里可选浅色/暗色，以及主色、副色、强调色三个色盘；这三色写入 Design Token 后改卡片背景、表面和强调色。3 列 3:4 网格平铺，可点大图、勾选，只导出已勾选且保持网页顺序。设置里的「榜单模型」只改写简介四句。
- `skills` 是资源探索里的一种 Resource Type，海报出图保留在创作页的「Skill 海报出图工作台」；原来的独立「Skill 查找器」一级页面已删除。`POST /api/skills/search` 接收 `{"query":"..."}`（1–200 字），**按用户自己的关键词抓取**：按空格 / 逗号 / 分号 / 斜杠切词（最多 6 个，去重保序），一个关键词一条检索通道，同时打 GitHub 代码检索、skills.sh 站内搜索、网页检索，外加 `anthropics/skills` 兜底清单；质量门槛过滤后写入 `output/skills/<hash>/`，并生成总览文。**查找不再需要模型**（模型只在写介绍文时用，没配模型时介绍文按工作流环节机械生成；`coverage` 参数可显式传一份检索计划）。查找是**累加**的：上一轮的 skill 会重新归档保留，`POST /api/skills/clear` 回到空盘（保留标题）。`POST /api/skills/paste` 接收 `{"links":"..."}`（≤20000 字，一次最多 16 条，可多行/多个 GitHub 或 skills.sh 链接），解析出的 skill 与检索到的一样进入同一个包：按内容归到最合适的工作流环节（名字 > 描述 > `SKILL.md` 正文加权匹配；匹配不上进兜底桶「通用工具」）、抓 `SKILL.md`、重写介绍文、参与缩略图上限排序；同一个 monorepo 里的多个 skill 目录各算一条，重复粘贴只替换不翻倍，超出 16 条的以 `too_many` 报告。`POST /api/skills/github-token` 保存/清除可选 GitHub Token（`{"token":""}` 为清除），只回 `{configured}`，不回显明文，清空后回落环境变量 `AUTOPOSTING_GITHUB_TOKEN` / `GITHUB_TOKEN`。`POST /api/skills/title` 接收 `{"title":"..."}`（≤120 字）保存用户标题：它决定封面海报大字与介绍文标题，且查找 / 粘贴 / 清空都不会冲掉它。`POST /api/skills/render` 把包内**全部** skill（检索到的 + 粘贴的）汇总成一份、按工作流顺序排好，出一张封面海报和每个 skill 一张海报，画布 3:4（1080×1440），项目卡复用周榜视觉。`GET /api/skills` 读上次结果，并返回 `crawl_sources` / `crawl_source_count`（当前配置的自动抓取源）。资源页负责「找」，工作室负责「出图」：查找 / 粘贴的输入框在资源探索页（`#resource-query` / `#resource-pasted-text`），工作室的 `searchSkills(query)` / `pasteSkills(links)` 只消费调用方传入的关键词或链接；`送入 Skill 出图` 会把资源页的关键词/粘贴链接接到同一条 skills 管线，再跳到创作页生成海报。收录来源与降级说明在工作室**可见**：`#skills-sources` 里每个来源一颗 chip（`#skills-source-chips`，其中「手动粘贴」是虚线条，点一下可全选/取消该来源的 skill），`#skills-degraded-note` 用中文说明哪些来源本次没返回（原始 `degraded_reasons` 记号在该行 tooltip 里）；`#skills-clear-btn` 清空收录，GitHub Token 只在设置页的 `#settings-github-token` / `#settings-save-github-token` 配置，创作页不再有第二个 Token 输入框。出图依赖本机 Chrome 写系统临时目录，服务不要跑在只允许写工作区的受限沙箱里；`explain_chrome_failure` 会把 Chrome 的原始报错翻成中文提示。
- GitHub 接口失败会分类上报：`degraded_reasons` 里是 `github:rate_limited` / `github:timeout` / `github:unreachable` / `github:server_error` 等记号（页面翻成中文，原始记号在 tooltip），404 不算来源故障。未鉴权 `api.github.com` 只有 60 次 core/小时与 10 次 search/分钟，配额耗尽时该额度桶在本次运行内熔断，候选改走 `raw.githubusercontent.com` 探测（不占 API 额度）；配置 GitHub Token 后额度为 5000 次/小时。
- `models` 用 Hermes 式三步添加接口：接口地址 + API Key → 连接并拉取模型 → 点「用于文章」「用于翻译」或「用于榜单」（聊天模型不在这三个入口里，走 `POST /api/chat/model`）。三个入口可共用同一接口。`model` 保存前会对所选模型执行一次真实 `/chat/completions` 请求；验证失败不会覆盖旧配置。文章与翻译只使用验证成功的模型，不再回退到预设 DeepSeek 模型。`effort` 支持 `none|minimal|low|medium|high|xhigh|max|ultra`，非 `none` 时以 `reasoning_effort` 发送。
- `chat/model` 独立设置聊天模型的 OpenAI 兼容 `model`、`base_url` 和 `api_key`，不会改变文章生成或热点翻译模型；`chat` 接收最多 50 条 `user`/`assistant` 消息，每条最多 20,000 字符，原样转发给聊天模型。工作台没有聊天输入框，这两个接口供脚本或外部调用。聊天记录不写入 SQLite、日志或响应；模型接口元数据写入 SQLite，API Key 只写入 macOS 钥匙串；请求体上限为 4 MiB。
- `hooks/find` 与「找切口」整条链都已**删除**，不是弃用：`POST /api/hooks/find`、`src/autoposting/hooks.py` 整个模块、`Workbench.find_hooks`、`App.find_hooks`、创作页的「找切口」按钮与 `#hook-list`、`hooks` 等待状态、文章请求体里的 `chosen_hooks` 字段都不存在了。文章素材只能来自 Topic Dossier（`dossier_id`）或手动选中的 item，创作页不会再现场搜索。相关搜索也不再是产品能力：`POST /api/related-search`、`App.related_search`、`Workbench.related_search`、资讯页的 `#related-search-btn` 与整个 `#news-supplement-*` 抽屉都已删除；引擎（`src/autoposting/background.py` 的 `search_related_news` 等）只作为 Research Service 的 Web 检索适配器保留，没有自己的 HTTP 端点。
- `articles` 有两条输入路径，最终都落到同一个 `ArticleGenerationContext`（`src/autoposting/article_context.py`，`schema_version = "article-generation-context-v1"`；声明的来源类型是 `topic_dossier` / `resource_set` / `manual`）。传 `dossier_id` 走官方研究路径：从持久化的 Topic Dossier 重建上下文，**不需要候选 item**，也不会再发起任何检索（无找切口、无相关搜索）；Dossier 缺失或不可用时返回 400，`error` 是稳定码（`dossier_id_required` / `dossier_not_found` / `dossier_invalid` / `dossier_evidence_required` / `dossier_sources_required` 等）、`error_reason` 是中文说明、`error_type` 为 `ArticleContextError`。不传 `dossier_id` 时走保留的手动路径：`item_id`（单条）或 `item_ids`（多条融合），从所选 item 构建可追溯事实清单，通过来源门禁后生成中文平台稿；微信和小红书使用独立事件，可分别审批发布。文章模型的共享调教在 `docs/ARTICLE_BASE_PROMPT.md`，可选写作类型在 `docs/article_styles/` 下的具名文件（AI专家、经验分享、klinkenborg）；下拉值为 `AI专家`、`经验分享` 与 `klinkenborg_style`，默认 AI专家。一次点击只生成一版。微信返回 `{title, body}`。小红书同一次调用返回 `{title, body, caption}`：`body` 是完整稿，排进 9:16 手写纸插图（封面=标题+所选封面图，没有封面图则标题+第一段；每张最上是 id/头像占位）；`caption` 是帖子正文。缺少 caption 仍生成：稿子 ≤500 字则正文完整引用，超过 500 字则提炼吸引阅读的要点。社区来源的原帖若含 GitHub 等作品外链，会抓取 README 并作为 `linked_works` 交给经验分享拆解（这是什么 / 场景 / 如何使用 / 亮点）。成稿区插图每行 3 张，文字区输出 caption。设置页“写作风格”是最高优先级的自由文本覆盖层。生成时抽取原文全部候选图（最多 16 张，不使用 AI 生图），默认只勾选封面，正文图保持未选，由作者按需勾选。工作台可预览缩略图、点击看大图、勾选封面/正文并调整正文顺序。`POST /api/articles/images` 保存选择；微信 live 把已选封面上传为永久素材（`material/add_material`），已选正文图经 `media/uploadimg` 按顺序填入。没有已选配图时才生成本地标题大字报。模型不可用时返回稳定 502 `article_generation_failed`，并附底层 `error_code` 和安全 `error_reason`，不落草稿。
- `articles/edit` 保存人工标题/正文；修改内容会撤销旧审批。标题、AIGC 声明和精确来源 ID 在保存前校验。
- `config` 的微信凭据和平台账号写入 macOS 钥匙串（响应只回是否已配置）；模型接口元数据写入 SQLite，模型 API Key 写入 macOS 钥匙串。任何响应只回非敏感字段。
- 本补丁直接复用现有 OpenAI 兼容 `/chat/completions` 入口，不部署 `new-api`；需要统一计费、多租户、渠道路由或重试策略时再单独部署网关。
- `publish`：未传 `live: true` 时只生成本地 dry-run 包；`platform=wechat` 且 `live=true` 且已配置凭据时才调用微信官方接口；`platform=xiaohongshu` 始终返回 `manual_required` 与小红书发布页链接。工作台点「到草稿」会先自动保存未保存的标题/正文，再审批并写入公众号草稿箱；微信 live 会把已选封面上传为永久素材、已选正文图按 3:4 填进正文。没有已选配图时才生成标题大字报。最终稿写入 `output/archives/`，响应含 `archive_path`。
- `schedule`：审批后预约发布时间（ISO 本地或带时区）；服务进程内轮询到期任务并走同一套 `publish`。`GET /api/schedule` 列出任务，`/api/schedule/cancel` 取消未执行任务。重启服务不会丢 SQLite 中的预约，但需服务在跑才会触发。

错误统一为 JSON：`{"ok": false, "error": "...", "error_type": "..."}`；AI 失败另含 `error_code` 与 `error_reason`。400/404/502/500 对应请求、路由、模型和服务端错误，响应不回显 prompt、stderr 或任何秘密。

## 视觉出版

GitHub 周榜与 Skill 海报的中文信息图都不靠图像模型排字。`docs/image_prompts/` 拆成五层：Design System、Component Library、Page Schema、Content Schema、Rendering Rules。渲染器在 `src/autoposting/visual/`，画布统一输出 1080×1440（3:4）HTML，再用本机 Chrome 或可选 Playwright 截 PNG；两条视觉产线只有一份渲染实现——`src/autoposting/web.py` 的周榜工作台调用 `render_weekly_visual`，Skill 工作室的 `render_skill_package` 也复用它。1.3.0 已删除图像模型层（原 `STYLE.md`/`cover.md`/`poster.md`/`skill_cover.md` 与 `render_weekly_images`/`load_image_prompt` 只剩迁移记录）：出图只有一条链路（HTML/CSS 页面 → 浏览器截图），设置页不再有「出图模型」，也不得再新增图像模型调用点；`src/autoposting/image_gen.py` 只生成页面计划（`id`/`kind`/`rank`/`title`，没有 `prompt` 字段）。

```bash
PYTHONPATH=src python3 -m autoposting.cli visual-render --example --out output/visual
```

示例 PNG：`output/visual/ranking-overview/ranking-overview.png`、`output/visual/project-detail/project-detail.png`。架构说明与 STYLE.md 拆分记录：`docs/image_prompts/README.md`、`docs/image_prompts/STYLE_MIGRATION_REPORT.md`。

## 平台边界与配置条件

- 微信 live（`token -> material/add_material` 封面 -> `media/uploadimg` 正文图 -> `draft/add`）只在「用户显式选择 wechat 且请求带 `live: true`」时触发，写入公众号草稿箱，不调用 `freepublish/submit`。缺少内存配置时返回 `wechat_configuration_required`；有已选原文配图时上传该图（封面优先，没有封面则用已选正文图），下载失败不改成大字报；没有已选配图时才生成本地标题大字报再上传；大字报失败且没有备用 `thumb_media_id` 时返回 `wechat_cover_required`。不重试、不轮询审核结果。
- 小红书不提供普通账号通用官方发文 API：批准草稿后生成标准本地包（`manual_required=true`），由人工到 `https://creator.xiaohongshu.com/publish/publish` 发布。不使用 Cookie、私有接口或模拟登录。
- 微信配置必需字段：`app_id`、`app_secret`；`thumb_media_id` 与 `author` 可选。`thumb_media_id` 只在没有已选配图且标题大字报生成失败、或已选配图上传失败时作为备用。`app_secret` 与 access_token 绝不落盘、不进入日志或响应。

## 数据流与状态

```text
CLI / 手动路径：
Schedule -> RSS -> normalize -> deduplicate -> topic -> event
         -> fact list -> WeChat/Xiaohongshu drafts -> validation
         -> human approval -> local dry-run package -> audit state

Web 官方路径：
Information Discovery -> Event Cluster -> Topic Dossier
         -> ArticleGenerationContext -> Draft -> human approval -> publish
```

核心表为 `sources`、`items`、`events`、`claims`、`drafts`、`publication_attempts`、`model_endpoints`、`model_endpoint_purposes`；Topic Dossier 存在 `topic_dossiers` 表里（`dossier_json` 保存整份 `topic-dossier-v1`，回读走 `GET /api/research/{dossier_id}`）；`item_sources` 保留跨来源重复内容的完整溯源关系；`llm_cache` 保存模型翻译/文章结果以控制成本。模型 API Key 不写入 SQLite。

正常状态为 `NEW -> VERIFIED -> DRAFTED -> APPROVED -> PUBLISHED`。本阶段没有任何路径写入 `PUBLISHED`；失败和人工分支使用 `FAILED`、`REJECTED`。条目通过外部 ID、规范 URL 和内容哈希去重，事件、草稿和发布尝试均使用稳定幂等键。

## 热点规则

资讯页不再分「热点候选 / 观点候选」两个页签：时间窗口内的 RSS 条目与 Reddit 等社区来源在一次采集里合并成一个 Event Cluster 列表，先由客观指标（时效、独立来源数、内容数、来源等级、互动量、spike）排序，再由 Decision/Jev 按 60/40 混合语义判断；Jev 不可用时退回 objective-only 并在结果里标 `degraded`。列表不代表统计显著热点，也不触发任何事件。编辑源 / 社区源的过滤由工具栏「来源范围」的 `scope` 承担，旧的 `lane` 参数只是 HTTP 兼容别名。

统计热点由 `hotspot-check` 独立判定。默认规则全部透明且可配置：最近 60 分钟至少 6 条、至少 3 个独立来源、严格超过过去 7 天同期均值加 3 个总体标准差、至少一个 S1 或两个独立 S2/S3、触发后冷却 6 小时。互动量不是触发条件。S1 重大公告只进入候选，仍需人工审批。

## AI 模型与成本控制

- 默认模型为 `unconfigured`；设置页可配置文章 / 翻译 / 榜单模型；聊天模型只在 API 层配置（`POST /api/chat/model`，工作台没有聊天输入框）。Hermes 命令仍有 90 秒超时。
- 缓存：`llm_cache` 按任务、平台、提示词版本、内容、模型、provider 和 effort 隔离，只存翻译/文章结果，不存凭据。
- 失败降级：翻译失败时保留原文并显示原因/错误码；文章生成失败返回稳定 502、底层错误码与安全原因，不写草稿。
- 平台稿约束（输出 JSON `title`/`body`，校验强制 CJK、精确来源 ID、AIGC 和标题长度）：微信标题 ≤ 64 字；小红书标题 ≤ 20 字。模型稿写入 `drafts`，`generator=<provider>/<model>:<effort>:<prompt-version>`；当前文章 prompt 版本为 `article-v13`。

## 测试

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
```

离线自检会实际创建 SQLite、采集内嵌 RSS 两次、触发与拒绝热点规则、阻断无来源事实、生成并批准两份独立草稿、创建幂等 dry-run 发布包。它不访问网络。

## 安全边界

- `package` 命令没有 live 选项；发布服务在 `dry_run=false` 时直接失败。
- 微信官方接口（`token -> 永久封面 -> 正文图 -> draft/add`）仅在 Web API 中显式选择 wechat 且 `live=true` 时调用，只写入草稿箱；AppSecret 与 access_token 不进入日志或响应，凭据写入 macOS 钥匙串，重启后自动恢复，可手动删除。接口错误只回 errcode 映射，不重试、不轮询审核。
- 小红书仅生成标题、正文、标签、来源组成的本地包并要求人工到官方发布页发布，不使用 Cookie、私有 API 或模拟登录。
- Web API 只允许绑定 127.0.0.1/localhost，避免无认证暴露。
- 普通 JSON 请求体上限为 1 MiB，聊天请求体上限为 4 MiB；微信 live 提交在单进程内串行化并复用已提交记录，避免并发重复提交。
- 当前没有 X 采集器；非 RSS 来源会记录明确失败。员工账号必须人工确认后才可启用。
- 不把明文模型 API Key 写入 SQLite、日志或响应；模型 API Key 由 macOS 钥匙串管理，`.env` 已被 Git 忽略。
- 草稿包含来源索引和 AIGC 声明；不处理无授权图片，也不删除模型或平台标识。

架构选择和升级条件见 `docs/DECISIONS.md`。

## 补丁记录与归档

每次补丁完成后必须留下可复查记录，不能只改代码。模型切换后仍按此执行：

1. 在 `docs/PATCH_{版本}.md` 写本次变更、边界和验证结果。
2. 把该版本源码快照打进 `archives/autoposting-patch-{版本}-update-{YYYYMMDD}.tar.gz`，并写同名 `.sha256`。
3. 归档只含源码与文档（`src/`、`tests/`、`docs/`、`config/`、`README.md`、`pyproject.toml`、服务控制脚本），不含 `data/`、`output/`、`.git/`、缓存和密钥。
4. 文章发布归档是另一件事：工作台点「发布」后的终稿写在 `output/archives/`。

Agent 执行细则见 `AGENTS.md`。
