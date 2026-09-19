# PATCH 2.0.0 — Reader 按需正文与 News Radar

版本：`1.4.9` → `2.0.0`。本次更新把原文读取、事件雷达和证据链纳入同一条可观测、可回退的本地工作流；不引入常驻服务，也不要求安装第三方运行时依赖。

## 新增功能

- 新增统一 `ArticleDocument` / `ReaderService`：
  - 统一 URL 规范化、canonical URL、正文 `content_hash` 和来源域名。
  - 复用 RSS/Feed 中已经足量的正文，避免再次 HTTP 抓取。
  - 用标准库解析 HTML、JSON-LD `articleBody` 和轻量正文结构，并通过质量门禁分类为 `full`、`partial`、`blocked`、`paywalled`、`empty`、`failed`。
  - 返回 `quality_score`、`failure_reason`、`fetch_method`、`extractor` 等可观测信息。
  - 对 JavaScript shell 或静态正文质量不足的页面，支持一次可注入的动态渲染 fallback；默认 renderer 对 Playwright 惰性导入，未安装时明确返回 `dynamic_renderer_unavailable`。
- 新增 News Radar 事件聚类与证据链：
  - 对跨来源候选进行事件级聚类，选择 canonical source/URL，同时保留 supporting、community、discussion 来源。
  - 分离 `importance_score`、`attention_score`、`priority_score` 三个分数，并保留可解释的评分分解和选择原因。
  - 新增 Evidence Pack Writer：明确区分 `SUPPORTED`、`INFERRED`、`UNSUPPORTED`/不确定信息及冲突容器；推断信号不会混入支持事实。
  - Web 工作台显示重要、关注、优先分数；文章生成把 Evidence Pack 传给既有写作链路。
- 新增 `engagement_snapshots` 历史记录能力：显式 collect 路径保存平台/指标/数值/时间快照，雷达可据此计算 momentum；只读热点查询不会隐式写入快照。
- 版本统一为 `2.0.0`：同步包元数据、包版本、Web `APP_VERSION` 与状态徽标、LLM HTTP User-Agent 及测试断言。

## 修改功能

- `background.article_page_text` / `excerpt_page` 兼容性地路由到 `ReaderService`，保留原函数签名和调用方预期。
- `Workbench`、`WebApp.original` 和 CLI 默认 Web 路径接入统一 Reader；保留可注入 `page_fetcher` / `render_fetcher`，避免重复抓取并支持测试替身。
- 原文 API 保留既有字段和 `content_source` 行为，新增 `status`、`reader_status`、`quality_score`、`failure_reason`、`canonical_url`、`fetch_method`、`extractor`。
- Web 热点响应保留旧候选形状，同时增加嵌套 `radar.events`；前端热点卡片显示雷达分数和事件关联。
- 文章生成在有雷达事件时追加 typed Evidence Pack，不改变未提供事件时的旧生成路径。

## 删除功能

无。本版本不删除旧接口、旧原文字段或旧候选字段；只增加统一读取、观测字段和兼容别名。

## 用户可见行为变化

- 打开原文时可以看到正文读取状态、质量分数、规范 URL、抓取方式、抽取器和失败原因。
- RSS 已包含完整正文时直接使用 Feed 内容，不再重复请求原网页。
- JavaScript shell 页面会在静态内容不足时尝试一次动态渲染；动态渲染不可用或失败时会保留明确的非全文状态，不把 shell 冒充成全文。
- 热点列表按事件而非重复候选展示，并显示重要、关注、优先三个分数；来源角色和证据包可继续用于文章生成。
- 服务状态徽标和 HTTP User-Agent 显示 `2.0.0`。

## 兼容性变化

- Python 3.11 标准库、SQLite 和既有 API 仍是基础运行环境；旧的抓取器签名、Workbench 调用方式和原文 API 字段保持兼容。
- `page_fetcher` 与 `render_fetcher` 都可由调用方注入；直接使用 `ReaderService` 时仍可只提供 HTTP fetcher。
- 旧 SQLite 数据库通过幂等初始化补齐新表，不删除既有数据；旧热点候选仍以兼容字段返回。
- 没有引入常驻浏览器服务、后台 daemon 或新的强制第三方运行时依赖。

## 新增配置/依赖

- 无新增必需配置。
- 无新增必需依赖；Reader 的 HTML/JSON-LD/质量门禁使用标准库。
- 动态页面能力可选使用已安装的 Playwright 和浏览器执行文件，采用惰性导入；未安装或不可用时自动降级并报告 `dynamic_renderer_unavailable`。也可通过 `render_fetcher` 注入已有浏览器能力。

## 数据库变更

- 新增 SQLite 表 `engagement_snapshots`：`snapshot_id`、`event_id`、`platform`、`metric`、`value`、`observed_at`，并以 `(event_id, platform, metric, observed_at)` 去重。
- 新增 `idx_engagement_snapshots_event_time` 索引以及记录、批量记录、查询最新快照的兼容 API。
- 表和索引通过 `CREATE TABLE/INDEX IF NOT EXISTS` 幂等创建；只有显式 collect/保存路径写入，GET 热点查询保持只读。

## 已知限制

- 动态 fallback 依赖外部 Playwright 包及其浏览器二进制；最终验收环境没有可用 browser 时，动态页面只能得到 `dynamic_renderer_unavailable`，不会伪造全文。
- 标准库正文抽取是轻量启发式实现，未覆盖所有站点的登录墙、反爬、复杂 shadow DOM、站点专用正文规则或需要用户交互的页面。
- 事件聚类和三分评分是确定性的规则模型，极少数标题相似事件可能被拆分或合并，分数尚未经过长期线上校准。
- Evidence Pack 使用可观察来源和结构化信号；社区/讨论内容只作为推断或不确定信息，不自动升级为官方事实。
- 验收期间真实 API 路径曾成功完成验证；浏览器路径曾在初次环境成功渲染，但最终验收环境无可用 browser，因此最终结果以可观测降级为准。
- 两个工作树原有的 `docs/article_styles/AI专家.md` 改动已做语义合并；仅同步了对应的过时测试文案断言，未改动业务代码。

## 验证

- 全量：`PYTHONPATH=src:tests python3 -m unittest discover -s tests -v` → **666 tests：666 passed，1 skipped**（受限外执行以允许测试绑定本地临时 HTTP 端口）。
- 定向验证：Reader、background、Radar、Workbench、Web 原文 API、CLI 默认 renderer wiring 及动态 fallback 测试通过；覆盖普通 HTML、JSON-LD、RSS 不重复抓取、阻断/付费墙/失败页、质量评分、JS shell fallback 和静态全文不触发 renderer。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check` → **ok**。
- 真实 API：按验收流程验证了真实 API 路径；密钥、AppSecret、访问令牌未写入本记录或归档。
- 浏览器：初次验收环境曾成功完成渲染；最终环境无 browser，已验证明确的 `dynamic_renderer_unavailable` 降级。
- 发布前 `git diff --check` 通过；归档内容和 SHA-256 在生成后再次核验。

## 边界与归档

- 本版本不新增常驻服务、不强制安装 Playwright；保留并语义合并两工作树的文章风格改动，不把 `.Rhistory` 或密钥纳入归档。
- 源码快照：`archives/autoposting-patch-2.0.0-update-20260919.tar.gz`（同名 `.sha256`）。
