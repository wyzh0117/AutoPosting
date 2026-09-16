# AutoPosting MVP

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

- `config/sources.example.json`：来源注册表。默认启用一手 RSS（官方实验室 + 可靠媒体）以及 DeepSeek 官方文档 sitemap（`deepseek_official`，S1）。DeepSeek 没有公开 RSS，采集其官方 `api-docs.deepseek.com/sitemap.xml` 里的 `/news/news*` 静态页，不是二手聚合。`ithome` / `hn_frontpage` 带 `content_filter: "ai"`：标题必须是 AI 主题，早报、开售/优惠价、耳机手机汽车等零售稿即使正文里出现 “AI” 也会丢掉。生成文章前会读原文、查库内相关旧闻并做一次公开网页检索，把前因脉络写给没追过这件事的普通人。`aihot_aggregate` 保留但关闭。S4 讨论源（HN、可选 X 账号）可以直接写成观察帖；讨论量过阈值后该条升温为 S3 关注级，仍不是官方证实。X 抓取对齐 AIHOT 的方法：官方用户时间线、排除回复、丢掉纯转发、只收推文附图（`pbs.twimg.com/media`），不爬 X 网页、不把头像当配图。Bearer 只留进程内存；示例表登记了实验室官方号和观察号，默认关闭，填令牌后再启用。S4 讨论号可不做身份核验，S1/S2 仍要 `identity_verified=true`。成稿配图会丢掉头像、页眉图标和过小的方图。公众号没有公开 RSS，本仓库不抓。
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

测试或记录热点候选：

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

浏览器打开 `http://127.0.0.1:8080/` 即单页工作台。页面最上方切换四个栏目（热点候选 / 观点候选 / GitHub 周榜 / Skill查找器）以及微信/小红书。热点来自 RSS，观点来自 Reddit 使用/经验向板块的当前热帖，周榜封面为 3:4，Skill查找器按覆盖地图检索并打包 skill。下方仍是左右两栏。左栏默认高度刚好露出两条候选，其余在栏内滚动；点开看来源后才展开订阅健康。原文配图收在独立配图栏内滚动，照片再多也不靠整页滚轮翻。标题和正文可在文章区修改并保存；模型选择和发布位于文章区右下角。文章生成区正下方提供独立的原生模型聊天框，聊天记录在首次输入/输出前不显示且只保存在当前浏览器会话；输出出现后页面保持上部尺寸并向下自然扩展，由页面滚动承载长对话。顶栏「设置」分别配置文章与翻译模型、聊天模型、微信公众号 API 和小红书账号；模型接口元数据持久化到 SQLite，API Key 与平台账号凭据存入 macOS 钥匙串，重启后自动恢复，可手动删除。小红书仍只生成本地包，需到官方创作中心人工提交。

`--host` 只接受 `127.0.0.1` 或 `localhost`，其他值直接拒绝，避免无认证暴露。端点为：

```bash
curl http://127.0.0.1:8080/api/status
curl -X POST http://127.0.0.1:8080/api/collect -d '{"hours": 4}'
curl -X POST http://127.0.0.1:8080/api/schedule -d '{"draft_id": "DRAFT_ID", "platform": "wechat", "live": true, "run_at": "2026-08-13T20:00:00"}'
curl -X POST http://127.0.0.1:8080/api/models -H 'Content-Type: application/json' -d '{"base_url": "https://api.example/v1", "api_key": "..."}'
curl -X POST http://127.0.0.1:8080/api/model -H 'Content-Type: application/json' -d '{"model": "selected-model", "base_url": "https://api.example/v1", "api_key": "...", "effort": "high"}'
curl -X POST http://127.0.0.1:8080/api/chat/model -H 'Content-Type: application/json' -d '{"model": "chat-model", "base_url": "https://api.example/v1", "api_key": "..."}'
curl -X POST http://127.0.0.1:8080/api/chat -H 'Content-Type: application/json' -d '{"messages": [{"role": "user", "content": "你好"}]}'
curl -X POST http://127.0.0.1:8080/api/hooks/find -H 'Content-Type: application/json' -d '{"item_id": "ITEM_ID"}'
curl -X POST http://127.0.0.1:8080/api/articles -H 'Content-Type: application/json' -d '{"item_id": "ITEM_ID", "platform": "wechat", "model": "selected-model", "effort": "high", "chosen_hooks": []}'
curl -X POST http://127.0.0.1:8080/api/articles/edit -d '{"draft_id": "DRAFT_ID", "title": "...", "body": "... AIGC ... SOURCE_ID ..."}'
curl -X POST http://127.0.0.1:8080/api/approve -d '{"draft_id": "DRAFT_ID", "reviewer": "human"}'
curl -X POST http://127.0.0.1:8080/api/config -d '{"platform": "wechat", "app_id": "...", "app_secret": "...", "author": "..."}'
curl -X POST http://127.0.0.1:8080/api/publish -d '{"draft_id": "DRAFT_ID", "platform": "wechat", "live": true}'
curl -X POST http://127.0.0.1:8080/api/skills/search -H 'Content-Type: application/json' -d '{"query": "从零做播客"}'
curl -X POST http://127.0.0.1:8080/api/skills/render -H 'Content-Type: application/json' -d '{"theme": "dark"}'
curl http://127.0.0.1:8080/api/skills
```

- `collect` 默认强制刷新已启用一手 RSS（`force: true`）；可传 `source_id` 只刷新一个源。不抓 AIHOT 聚合页或 API。响应含 `rankings.heat`（独立来源数优先，其次新旧）和 `rankings.time`（新旧优先，其次来源数），以及 `sources` 健康状态（`ok`/`error`/`stale`/`never`/`disabled`）和 `collection.inserted`。`candidates` 仍是时间优先列表，兼容旧调用。窗口内最多各取 10 条，不是 `hotspot-check` 的统计显著热点。刷新全部只抓源、不阻塞翻译，列表先显示库里已有候选。`hours` 接受 1–720；工作台默认 4 小时，并提供 4/6/24/72/168/720 与自定义。左栏上方是话题/新闻卡片；底部折叠的是订阅源健康（抓取成功 / 只刷这个），不是新闻标题。左栏可切换热度优先 / 时间优先。网页逐步点选验收见 `docs/WEB_FUNCTIONAL_TEST.md`。
- `github-weekly` 是顶栏第三栏「GitHub 周榜」。`POST /api/github-weekly` 只抓仓库和文案，不生成图片。优先本周新建的 AI 仓库（Search `created:>=7d`），不够再用 Trending weekly 补齐，固定前 10。「生成图片」走 `POST /api/github-weekly/render`：HTML/CSS 排版后用本机 Chrome 截 PNG。周榜封面 3:4（1080×1440）；项目卡与小红书纸卡仍为当前比例；图像模型默认仍 9:16。旁边可选浅色/暗色，以及主色、副色、强调色三个色盘；这三色写入 Design Token 后改卡片背景、表面和强调色。3 列 3:4 网格平铺，可点大图、勾选，只导出已勾选且保持网页顺序。设置里的「榜单模型」只改写简介四句。
- `skills` 是顶栏第四栏「Skill查找器」。`POST /api/skills/search` 接收 `{"query":"..."}`（1–200 字），按覆盖地图拆 6–12 个阶段，检索 GitHub / 网页上的 `SKILL.md`，质量门槛过滤后写入 `output/skills/<hash>/`，并生成总览文。模型未配置时 502 `skill_search_failed`。`POST /api/skills/render` 用本地大字报截 3:4 封面，项目卡复用周榜视觉。`GET /api/skills` 读上次结果。工作台左侧是搜索框、阶段条和 skill 列表，右侧是介绍文和图片画廊。
- `models` 用 Hermes 式三步添加接口：接口地址 + API Key → 连接并拉取模型 → 点「用于文章」「用于翻译」「用于对话」或「用于榜单」。四个入口可共用同一接口。`model` 保存前会对所选模型执行一次真实 `/chat/completions` 请求；验证失败不会覆盖旧配置。文章与翻译只使用验证成功的模型，不再回退到预设 DeepSeek 模型。`effort` 支持 `none|minimal|low|medium|high|xhigh|max|ultra`，非 `none` 时以 `reasoning_effort` 发送。
- `chat/model` 独立设置聊天模型的 OpenAI 兼容 `model`、`base_url` 和 `api_key`，不会改变文章生成或热点翻译模型；`chat` 接收最多 50 条 `user`/`assistant` 消息，每条最多 20,000 字符，原样转发给聊天模型。聊天记录不写入 SQLite、日志或响应；模型接口元数据写入 SQLite，API Key 只写入 macOS 钥匙串；请求体上限为 4 MiB。
- `hooks/find` 对已选 item 抽点并现场搜 `{短语} 争议`，返回可多选切口卡；一张没有则 `hooks` 为空并给稳定文案「未发现可用切口」。检索页只当素材，不写入事实清单、不分配 `source_id`。不点「找切口」或点「生成文章」都不会偷偷搜索。
- `articles` 从所选 item 构建可追溯事实清单，通过来源门禁后生成中文平台稿；微信和小红书使用独立事件，可分别审批发布。可选 `chosen_hooks` 按勾选顺序回传完整切口卡。文章模型的共享调教在 `docs/ARTICLE_BASE_PROMPT.md`，可选写作类型在 `docs/article_styles/` 下的具名文件（AI专家、经验分享）；下拉值为 `AI专家` 与 `经验分享`，默认 AI专家，切到观点候选时自动选「经验分享」。一次点击只生成一版。微信返回 `{title, body}`。小红书同一次调用返回 `{title, body, caption}`：`body` 是完整稿，排进 9:16 手写纸插图（封面=标题+所选封面图，没有封面图则标题+第一段；每张最上是 id/头像占位）；`caption` 是帖子正文。缺少 caption 仍生成：稿子 ≤500 字则正文完整引用，超过 500 字则提炼吸引阅读的要点。观点候选若原帖含 GitHub 等作品外链，会抓取 README 并作为 `linked_works` 交给经验分享拆解（这是什么 / 场景 / 如何使用 / 亮点）。成稿区插图每行 3 张，文字区输出 caption。设置页“写作风格”是最高优先级的自由文本覆盖层。生成时抽取原文全部候选图（最多 16 张，不使用 AI 生图），默认只勾选封面，正文图保持未选，由作者按需勾选。工作台可预览缩略图、点击看大图、勾选封面/正文并调整正文顺序。`POST /api/articles/images` 保存选择；微信 live 把已选封面上传为永久素材（`material/add_material`），已选正文图经 `media/uploadimg` 按顺序填入。没有已选配图时才生成本地标题大字报。模型不可用时返回稳定 502 `article_generation_failed`，并附底层 `error_code` 和安全 `error_reason`，不落草稿。
- `articles/edit` 保存人工标题/正文；修改内容会撤销旧审批。标题、AIGC 声明和精确来源 ID 在保存前校验。
- `config` 的微信凭据和平台账号写入 macOS 钥匙串（响应只回是否已配置）；模型接口元数据写入 SQLite，模型 API Key 写入 macOS 钥匙串。任何响应只回非敏感字段。
- 本补丁直接复用现有 OpenAI 兼容 `/chat/completions` 入口，不部署 `new-api`；需要统一计费、多租户、渠道路由或重试策略时再单独部署网关。
- `publish`：未传 `live: true` 时只生成本地 dry-run 包；`platform=wechat` 且 `live=true` 且已配置凭据时才调用微信官方接口；`platform=xiaohongshu` 始终返回 `manual_required` 与小红书发布页链接。工作台点「到草稿」会先自动保存未保存的标题/正文，再审批并写入公众号草稿箱；微信 live 会把已选封面上传为永久素材、已选正文图按 3:4 填进正文。没有已选配图时才生成标题大字报。最终稿写入 `output/archives/`，响应含 `archive_path`。
- `schedule`：审批后预约发布时间（ISO 本地或带时区）；服务进程内轮询到期任务并走同一套 `publish`。`GET /api/schedule` 列出任务，`/api/schedule/cancel` 取消未执行任务。重启服务不会丢 SQLite 中的预约，但需服务在跑才会触发。

错误统一为 JSON：`{"ok": false, "error": "...", "error_type": "..."}`；AI 失败另含 `error_code` 与 `error_reason`。400/404/502/500 对应请求、路由、模型和服务端错误，响应不回显 prompt、stderr 或任何秘密。

## 视觉出版

GitHub 周榜的中文信息图不再只靠一张图像模型 Prompt。`docs/image_prompts/` 拆成五层：Design System、Component Library、Page Schema、Content Schema、Rendering Rules。渲染器在 `src/autoposting/visual/`，周榜封面输出 1080×1440（3:4）HTML，再用本机 Chrome 或可选 Playwright 截 PNG。图像模型只负责主视觉等资产，默认比例仍 9:16。

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
Schedule -> RSS -> normalize -> deduplicate -> topic -> event
         -> fact list -> WeChat/Xiaohongshu drafts -> validation
         -> human approval -> local dry-run package -> audit state
```

核心表为 `sources`、`items`、`events`、`claims`、`drafts`、`publication_attempts`、`model_endpoints`、`model_endpoint_purposes`；`item_sources` 保留跨来源重复内容的完整溯源关系；`llm_cache` 保存模型翻译/文章结果以控制成本。模型 API Key 不写入 SQLite。

正常状态为 `NEW -> VERIFIED -> DRAFTED -> APPROVED -> PUBLISHED`。本阶段没有任何路径写入 `PUBLISHED`；失败和人工分支使用 `FAILED`、`REJECTED`。条目通过外部 ID、规范 URL 和内容哈希去重，事件、草稿和发布尝试均使用稳定幂等键。

## 热点规则

Web 工作台左栏分两个页签。「热点候选」是时间窗口内 RSS 条目的排序，不代表统计显著热点，也不触发任何事件。「观点候选」是 Reddit 使用/经验向板块当前热帖（正文+热评），刷新该页签不会重拉 RSS。

统计热点由 `hotspot-check` 独立判定。默认规则全部透明且可配置：最近 60 分钟至少 6 条、至少 3 个独立来源、严格超过过去 7 天同期均值加 3 个总体标准差、至少一个 S1 或两个独立 S2/S3、触发后冷却 6 小时。互动量不是触发条件。S1 重大公告只进入候选，仍需人工审批。

## AI 模型与成本控制

- 默认模型为 `unconfigured`；设置页可配置文章/翻译模型与独立聊天模型，Hermes 命令仍有 90 秒超时。
- 缓存：`llm_cache` 按任务、平台、提示词版本、内容、模型、provider 和 effort 隔离，只存翻译/文章结果，不存凭据。
- 失败降级：翻译失败时保留原文并显示原因/错误码；文章生成失败返回稳定 502、底层错误码与安全原因，不写草稿。
- 平台稿约束（输出 JSON `title`/`body`，校验强制 CJK、精确来源 ID、AIGC 和标题长度）：微信标题 ≤ 64 字；小红书标题 ≤ 20 字。模型稿写入 `drafts`，`generator=<provider>/<model>:<effort>:<prompt-version>`。

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
