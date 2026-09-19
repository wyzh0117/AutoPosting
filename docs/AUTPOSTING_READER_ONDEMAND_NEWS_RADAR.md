# AutoPosting：ReaderService + 按需新闻雷达（On-demand News Radar）改造说明

> 目标：在**尽量不改变当前用户使用方式**的前提下，增强两项底层能力：
> 1. 更稳定、更广泛地抓取新闻正文；
> 2. 在用户主动点击采集时，从大量新闻中筛出“重要、受关注、值得写”的少数事件。
>
> 本方案明确保持当前项目的核心交互：**用户主动点击时才抓取、计算、排序；任务完成后停止，不要求 24 小时常驻运行。**

---

## 0. 最高优先级约束

### 0.1 保持现有用户功能与交互

实施时优先遵守以下原则：

1. 保留当前“主动点击 → 抓取 → 返回结果”的使用方式。
2. 不把项目改造成必须 24 小时运行的监控服务。
3. 不要求新增云服务器、常驻 daemon、cron 或持续轮询任务。
4. 尽量保留现有 API、按钮、页面入口、返回结构和发布流程。
5. 优先复用已有 RSS / Web / Sitemap / Reddit / X、SQLite、`page_fetcher`、热点检测等能力。
6. 新能力优先作为底层服务插入现有调用链，而不是重新设计用户操作流程。
7. 现有定时发布功能属于发布能力，不应被误改造成新闻持续监控功能。

### 0.2 禁止静默删功能

如果实施过程中发现旧功能与新架构冲突：

- 优先兼容；
- 其次弃用但保留兼容入口；
- 只有确有必要时才删除。

**不得静默删除功能。**

### 0.3 任务结束必须提交“功能变更说明”

模型/开发者完成改造后，必须明确报告：

```text
新增功能：
- ...

修改功能：
- ...

删除功能：
- ...
- 如果没有，明确写“无”

用户可见行为变化：
- ...

兼容性变化：
- ...

新增配置/依赖：
- ...

数据库变更：
- ...

已知限制：
- ...
```

如果删除了任何现有入口、参数、字段、按钮、返回值、默认行为或兼容逻辑，必须逐项说明原因。

---

# 1. 总体设计原则

本项目不是持续监控型新闻服务，而是：

> **On-demand News Radar / 按需新闻雷达**

运行模型：

```text
用户主动点击
     │
     ▼
一次性抓取当前世界状态
     │
     ▼
获取候选新闻 + 当前关注度信号
     │
     ▼
读取正文
     │
     ▼
合并同一事件
     │
     ▼
计算重要性 + 当前关注度
     │
     ▼
输出当前最值得关注/写作的事件
     │
     ▼
任务结束
```

系统空闲期间：

```text
不抓取
不刷新互动量
不计算实时 Velocity
不需要后台服务器
```

如果未来再次主动运行，则可以顺手利用上一次保存的历史快照；没有历史也不影响本次运行。

---

# 2. 总体架构

```text
Source Discovery
RSS / Sitemap / Web / HN / Reddit / X / Search / GitHub
                    │
                    ▼
               URL Candidate
                    │
                    ▼
               ReaderService
        获取标题 / 元数据 / 完整正文
                    │
                    ▼
              Content Filter
          是否与目标领域相关、是否有效
                    │
                    ▼
              Event Clustering
            同一事件的多篇报道合并
                    │
             ┌──────┴──────┐
             ▼             ▼
       Importance       Attention
         Score            Score
             │             │
             └──────┬──────┘
                    ▼
              Priority Score
                    │
                    ▼
             Trending Events
                    │
                    ▼
               Evidence Pack
                    │
                    ▼
                  Writer
                    │
                    ▼
               Final Audit
```

核心变化不是增加更多“网站专用爬虫”，而是建立两个稳定服务：

```text
ReaderService
负责“把 URL 读懂”

EventScoringService
负责“判断这件事值不值得关注”
```

---

# 3. ReaderService：统一正文读取层

## 3.1 为什么需要独立 ReaderService

Collector 只负责：

> **发现新闻。**

ReaderService 负责：

> **真正进入新闻页面并读取正文。**

不要让 RSSCollector、SitemapCollector、WebCollector、Background Search 分别维护不同的正文解析逻辑。

目标：

```text
任何来源
RSS
Sitemap
Web
HN
Reddit
搜索结果
手动 URL
    │
    ▼
ReaderService.read(url)
    │
    ▼
统一 ArticleDocument
```

---

# 4. ReaderService 抓取降级链

推荐顺序：

```text
                    URL
                     │
                     ▼
               URL Normalize
                     │
                     ▼
        RSS 已包含完整正文？
             │              │
            Yes            No
             │              │
             ▼              ▼
           保存       普通 HTTP GET
                            │
                            ▼
                     JSON-LD 检查
                            │
                      有 articleBody？
                      │            │
                     Yes          No
                      │            │
                      ▼            ▼
                    保存      Trafilatura
                                    │
                                    ▼
                             Content Quality Gate
                                    │
                            ┌───────┴───────┐
                            │               │
                          合格            不合格
                            │               │
                            ▼               ▼
                           保存       Readability fallback
                                            │
                                            ▼
                                      Quality Gate
                                            │
                                    ┌───────┴───────┐
                                    │               │
                                  合格            不合格
                                    │               │
                                    ▼               ▼
                                   保存        Playwright
                                                  │
                                                  ▼
                                             渲染 HTML
                                                  │
                                                  ▼
                                             Trafilatura
                                                  │
                                                  ▼
                                             Quality Gate
                                                  │
                                      ┌───────────┴───────────┐
                                      │                       │
                                    合格                    不合格
                                      │                       │
                                      ▼                       ▼
                                     保存              Site Adapter
                                                              │
                                                              ▼
                                                        partial / fail
```

---

# 5. Fetcher 与 Extractor 分离

建议拆为：

```text
Fetcher
├── HttpFetcher
└── BrowserFetcher

Extractor
├── JsonLdExtractor
├── TrafilaturaExtractor
├── ReadabilityExtractor
└── SiteAdapterExtractor

QualityChecker
└── ArticleQualityGate
```

Playwright 只负责：

> 渲染页面。

不要让 Playwright 自己维护另一套正文解析逻辑。

---

# 6. ArticleDocument 数据契约

建议新增稳定数据结构：

```python
@dataclass
class ArticleDocument:
    url: str
    canonical_url: str | None

    title: str | None
    author: str | None
    published_at: str | None

    text: str
    excerpt: str | None

    source_domain: str | None
    source_type: str | None

    fetch_method: str
    extractor: str

    status: str
    quality_score: float
    failure_reason: str | None

    fetched_at: datetime

    content_hash: str | None
```

建议 `status` 至少包含：

```text
full
partial
blocked
paywalled
empty
failed
```

---

# 7. 正文质量门禁

HTTP 200 不等于正文抓取成功。

至少检查：

```text
正文字符数
段落数量
平均段落长度
链接文本占比
标题与正文相关性
重复文本比例
```

典型失败文本：

```text
enable javascript
sign in
subscribe to continue
access denied
checking your browser
cookie preferences
```

返回示例：

```json
{
  "status": "full",
  "quality_score": 0.91
}
```

或：

```json
{
  "status": "partial",
  "quality_score": 0.32,
  "failure_reason": "paywall_or_truncated"
}
```

Writer 必须知道：

> 这是全文、摘要、截断内容还是失败。

不得把摘要当全文。

---

# 8. 建议抓取哪些页面

这里必须区分：

```text
发现页 Discovery Page
正文页 Content Page
讨论页 Engagement Page
```

三种页面职责不同。

---

## 8.1 第一优先级：一手官方来源

### 推荐抓取内容

#### A. 官方 News / Blog / Announcements

例如：

```text
OpenAI News / Research / Safety / Security
Anthropic Newsroom / Research
Google DeepMind News / Research
Meta AI Blog / Research
Microsoft AI / Research
Google AI / Cloud AI
NVIDIA AI / Developer Blog
Apple Machine Learning Research
Amazon / AWS AI
```

用途：

```text
发现重大产品发布
模型发布
价格/API变化
安全事件
政策/公司公告
研究突破
```

### 页面抓取策略

列表页：

```text
用于发现 URL
```

文章页：

```text
用于 Canonical Source / 核心事实
```

---

## 8.2 官方 Research / System Card / Safety 页面

这类页面优先级非常高。

建议重点抓：

```text
System Card
Model Card
Safety Report
Alignment Report
Security Report
Technical Report
Research Publication
Evaluation Report
Preparedness / Frontier Safety
```

原因：

媒体可能只摘取一个结论，而这些页面通常包含：

```text
实验条件
限制
指标定义
评测范围
原始表述
```

对于事实核验非常重要。

---

## 8.3 官方 Product / Changelog / Release Notes

很多真正重要的产品变化未必会进入 Newsroom。

建议增加：

```text
Product Changelog
API Changelog
Release Notes
Developer Docs Updates
Pricing Updates
Deprecation Notices
```

用途：

```text
新模型
新 API
Context Window
价格
速率限制
功能上线
功能下线
SDK 更新
```

但不要全量抓文档站。

策略：

```text
只抓 changelog / release note / update index
↓
发现变化
↓
进入对应正文页
```

---

# 9. 第二优先级：技术社区与热度发现页

## 9.1 Hacker News

强烈建议保留/增强。

推荐抓：

```text
HN Front Page
HN Best（建议 24h）
HN Newest（有限量）
HN Item Discussion Page
```

职责：

### Front Page

```text
当前社区正在关注什么
```

### Best 24h

```text
过去 24 小时真正跑出来的高关注内容
```

### Newest

只用于发现：

```text
刚出现但尚未进入首页的重要内容
```

不建议把 `newest` 全量深抓，否则噪音过高。

建议只抓：

```text
最近 N 页
+
目标关键词过滤
+
来源白名单/领域过滤
```

### Item Discussion

保存：

```text
points
comments
created_at
rank
```

可选读取少量高质量评论，但不要默认抓完整评论树。

HN 应主要提供：

> Attention Signal

而不是事实来源。

---

# 10. Reddit

建议抓取经过选择的 subreddit，而不是 Reddit 全站。

AI/开发相关可优先：

```text
r/MachineLearning
r/LocalLLaMA
```

根据项目定位再增：

```text
r/artificial
r/OpenAI
r/ClaudeAI
r/singularity
```

但不同 subreddit 的质量差异明显。

推荐抓：

```text
hot
top?time=day
new（有限量）
```

保存：

```text
score
num_comments
created_utc
upvote_ratio（如果可得）
```

Reddit 主要提供：

```text
社区关注度
社区争议
用户真实使用反馈
```

不要默认把 Reddit 帖子当新闻事实。

---

# 11. GitHub

GitHub 不应当作为普通新闻网站处理。

推荐两类：

## 11.1 Trending

抓：

```text
GitHub Trending
```

用于发现：

```text
突然增长的 AI 项目
Agent
框架
模型工具
开发工具
```

## 11.2 官方仓库

发现候选项目后再读取：

```text
README
Releases
Tags
CHANGELOG
关键 issue / security advisory
```

如果一个项目突然 Trending：

```text
Trending = Attention Signal
README / Release = Canonical Fact Source
```

不要把 Trending 页面直接当正文来源。

---

# 12. 论文与科研来源

建议作为“研究新闻”专线，而不是普通新闻池全量抓取。

推荐：

```text
arXiv
机构官方 research page
论文项目主页
GitHub repo
作者博客
```

arXiv 可关注：

```text
cs.AI
cs.LG
cs.CL
cs.CV
cs.RO
```

但论文量极大。

建议：

```text
先通过外部关注度/媒体/社区发现
↓
再去 arXiv 拉原论文
```

而不是：

```text
每天全量 arXiv
↓
让模型自己判断几千篇
```

---

# 13. 第三优先级：科技媒体

媒体的作用：

```text
补充背景
寻找采访
寻找公司回应
获得行业上下文
发现尚未发布官方说明的事件
```

建议优先选择有稳定科技报道能力的媒体。

例如：

```text
Reuters Technology / AI
TechCrunch AI
The Verge AI
Ars Technica AI
Wired AI
Financial Times Tech / AI
Bloomberg Tech / AI
The Information（如果合法可访问）
```

策略：

```text
媒体列表页 → 发现
文章正文 → Supporting Source
```

如果媒体报道引用了：

```text
法院文件
公司公告
论文
GitHub
政府文件
内部 memo
```

Reader / Researcher 应继续追踪到原始来源。

---

# 14. 聚合站的正确用法

AIHOT 等聚合站很适合作为：

```text
Discovery / Benchmark
```

不建议作为：

```text
Canonical Fact Source
```

正确链路：

```text
AIHOT 发现事件
      │
      ▼
获取它引用的报道/来源
      │
      ▼
寻找官方/一手来源
      │
      ▼
进入本项目 Event
```

聚合站还可以用于：

```text
检查本项目是否漏掉重大事件
比较榜单
离线评估排序效果
```

而不是简单复制聚合站的排名。

---

# 15. 不建议默认抓取的页面

为了减少运行负担，以下页面不应默认深抓：

```text
整站首页的所有导航链接
标签归档的全部历史页
作者全部历史文章
所有评论树
所有相关新闻推荐
无限滚动内容
搜索结果的深分页
通用 sitemap 全历史内容
完整 arXiv 全量流
大型论坛全站 new feed
```

原则：

> 列表页只发现，正文页才深读。

---

# 16. 页面优先级建议

## Tier A：每次主动抓取优先

```text
官方 News / Blog
官方 Research / Safety / Security
官方 Changelog / Release
HN Front
HN Best 24h
精选 Reddit hot/top-day
现有 RSS
现有 Sitemap recent URLs
```

## Tier B：发现候选后抓

```text
TechCrunch / Verge / Ars / Reuters 等正文
官方技术报告
GitHub README / Release
arXiv 论文
作者项目页
HN discussion
Reddit discussion
```

## Tier C：必要时抓

```text
Playwright 才能打开的动态页面
付费墙附近的公开片段
法院/监管/政策附件
长 PDF
大型评论区
Site-specific Adapter
```

---

# 17. Event Clustering：从新闻变成事件

不要给所有 URL 独立打最终分。

例如：

```text
OpenAI 官方
TechCrunch
The Verge
HN
Reddit
X
```

可能实际上都是：

```text
EVENT:
某模型发布
```

建议事件结构：

```text
Event
├── Canonical Source
├── Supporting Sources
├── Community Sources
├── Discussion Sources
├── first_seen_at
├── latest_seen_at
└── entities / topic / fingerprint
```

---

# 18. Canonical Source 选择

优先级：

```text
1. 官方原始公告
2. 原始论文/技术报告/代码仓库
3. 当事人直接声明
4. 一手采访/法院/政府文件
5. 高质量媒体
6. 聚合/转载
7. 社交媒体二手讨论
```

同一事件不得简单因为 URL 数量多就认为更可信。

---

# 19. 新闻评分：两个分数，不要混成一个概念

## 19.1 Importance Score

回答：

> 这件事本身有多重要？

建议：

```text
Impact        30%
Authority     20%
Novelty       15%
Scope         15%
Consequence   20%
```

### Impact

是否影响：

```text
大量用户
开发者
公司
行业结构
研究方向
政策
安全
```

### Authority

来源是否：

```text
官方
论文
监管机构
法院
当事人
核心开发者
```

### Novelty

是否是真正的新事实，而不是：

```text
旧闻重写
观点文章
重复报道
```

### Scope

影响范围：

```text
单项目
单公司
细分领域
整个 AI 行业
跨行业
```

### Consequence

是否会产生明显后续影响：

```text
产品替代
价格变化
开发方式变化
安全风险
监管变化
商业模式变化
```

---

# 20. Attention Score：一次运行即可计算

不要求实时 Velocity。

建议：

```text
Cross-source Coverage   25%
Normalized Engagement   30%
Source Diversity        20%
Freshness               15%
Snapshot Momentum       10%
```

---

# 21. Cross-source Coverage

计算：

```text
独立来源数量
```

不是：

```text
URL 数量
```

例如：

```text
Reuters 转 Yahoo
Reuters 转 MSN
Reuters 转其他聚合站
```

应该接近一个来源。

而：

```text
OpenAI
TechCrunch
HN
Reddit
```

才代表不同信息渠道。

---

# 22. Normalized Engagement

可利用：

```text
HN points/comments
Reddit score/comments
X likes/reposts/replies
GitHub stars / stars delta（如果已有）
```

必须按平台归一化。

不要直接比较：

```text
100 HN points
=
100 X likes
```

建议使用：

```text
平台历史 percentile
或
平台 baseline normalization
```

保留现有 baseline 思路。

---

# 23. Source Diversity

建议分类：

```text
Primary / Official
Mainstream Media
Technical Community
Social Community
Research
Code / GitHub
```

同一事件同时出现在多个独立渠道，比同类转载十次更有价值。

---

# 24. Freshness

事件越新：

```text
同等关注度下优先级越高
```

但避免简单线性衰减。

重大事件应该允许在一定时间内保持较高 Importance。

---

# 25. Snapshot Momentum

本项目不做实时 Velocity。

第一次运行：

```text
Snapshot Momentum
=
当前 engagement 相对于事件年龄
+
当前 coverage 相对于事件年龄
```

可以采用类似：

```text
engagement_component =
normalized_engagement / sqrt(age_hours + 1)

coverage_component =
normalized_coverage / sqrt(age_hours + 1)
```

具体参数应使用历史数据调优，而不是一开始写死成“真理”。

---

# 26. Lazy Velocity：有历史就用，没有就不用

每次主动运行时保存当前快照：

```text
event_id
platform
metric
value
observed_at
```

第一次：

```text
没有历史
→ 只使用 Snapshot Momentum
```

下一次用户主动点击，如果再次遇到同一事件：

```text
有历史
→ 可额外计算真实 delta / elapsed_time
```

程序结束后：

```text
不继续运行
```

因此无需 24 小时服务器。

---

# 27. 推荐的最终 Priority 逻辑

示意：

```text
Priority =
0.55 × Importance
+
0.45 × Attention
```

但不要完全依赖单一加权公式。

建议保留两个特殊入口：

```text
Importance >= 高阈值
→ 即使 Attention 低，也进入重点候选

Attention >= 高阈值
→ 即使 Importance 中等，也进入社区热议候选
```

最终可以出现：

```text
重要 + 热
重要 + 尚未热
中等重要 + 极热
```

---

# 28. Cooldown 的角色调整

现有 cooldown 建议保留。

但：

```text
Cooldown ≠ News Importance
```

它应该控制：

```text
是否重复通知
是否重复占榜
是否再次推送
```

不要因为：

```text
刚展示过
```

就降低事件本身的重要性。

---

# 29. 建议的数据流

```text
用户点击抓取
      │
      ▼
Discovery Collectors
      │
      ▼
候选 URL / 社区条目
      │
      ▼
快速 Relevance Filter
      │
      ▼
ReaderService
      │
      ▼
ArticleDocument
      │
      ▼
Event Clustering
      │
      ▼
Canonical Source Resolution
      │
      ▼
Importance Score
      │
      ├──────────────┐
      │              │
      ▼              ▼
当前互动数据     历史 snapshot（如果有）
      │              │
      └──────┬───────┘
             ▼
       Attention Score
             │
             ▼
        Priority Rank
             │
             ▼
          Top Events
             │
             ▼
        Evidence Pack
             │
             ▼
            Writer
```

---

# 30. Evidence Pack

写作模型不要直接接收“几十篇网页全文”。

建议每个事件生成：

```text
EVENT
标题
时间
核心实体

CANONICAL SOURCE
官方/一手来源

SUPPORTED FACTS
事实 1
事实 2
事实 3

SUPPORTING REPORTS
媒体 A
媒体 B

ATTENTION SIGNALS
HN
Reddit
X
GitHub

UNCERTAINTY
尚未确认内容

CONFLICT
来源冲突

INFERENCE
允许推论但不是事实
```

事实状态：

```text
SUPPORTED
INFERRED
CONFLICTED
UNSUPPORTED
```

Writer 不能把：

```text
INFERRED
```

写成：

```text
SUPPORTED
```

---

# 31. 对现有项目的最低侵入式改造建议

优先：

```text
现有 page_fetcher
      ↓
升级/封装
      ↓
ReaderService
```

而不是废弃原逻辑。

现有：

```text
RSS / Web / Sitemap
```

继续负责发现。

现有：

```text
frequency
tier
baseline
cooldown
```

继续保留，但重新解释：

```text
frequency
→ Cross-source Coverage 的输入

tier
→ Engagement Strength 的输入

baseline
→ Platform Normalization

cooldown
→ Display / Notification Suppression
```

---

# 32. 性能原则

本项目按需运行，因此必须遵守：

## 32.1 不为评分重复抓网页

如果 Collector 已取得：

```text
points
comments
score
created_at
```

直接复用。

不要：

```text
采集一次
↓
为了打分再次请求一遍
```

## 32.2 先轻后重

```text
RSS
HTTP
JSON-LD
Trafilatura
Readability
Playwright
Site Adapter
```

越昂贵的步骤越靠后。

## 32.3 两阶段正文读取

候选过多时：

### Stage 1

只做：

```text
标题
摘要
来源
时间
互动数据
快速相关性
```

### Stage 2

只对可能进入高排名的候选进行：

```text
完整正文抓取
Playwright
深度 Source Resolution
```

避免一次点击抓 500 篇全文。

---

# 33. 推荐执行顺序

## Phase 1：ReaderService

先统一：

```text
page_fetcher
article_page_text
excerpt_page
```

建立：

```text
ArticleDocument
Fetcher
Extractor
Quality Gate
```

不改变用户界面。

## Phase 2：Event Clustering

把：

```text
item ranking
```

升级为：

```text
event ranking
```

先解决重复新闻。

## Phase 3：Importance / Attention

保留现有热点系统作为输入。

新增：

```text
Importance Score
Attention Score
Priority
```

## Phase 4：Lazy Snapshot

只有在前面稳定以后再增加：

```text
engagement_snapshots
```

不增加常驻服务。

---

# 34. 验收标准

实施完成后至少验证：

### Reader

```text
普通 HTML 可读取
JSON-LD 可读取
RSS full-content 可复用
JS 动态页可 fallback
失败页面不会冒充全文
```

### Event

```text
同一事件多来源可聚类
不同事件不会错误合并
官方来源优先成为 canonical
```

### Scoring

```text
重大官方消息即使刚发布也能进入候选
高社区热度内容可进入热议候选
旧闻大量转载不会异常霸榜
同一事件不会多条重复占榜
```

### Product

```text
仍然由用户主动点击触发
程序空闲时不抓取新闻
不要求 24h 服务
现有发布/审批/dry-run 流程保持可用
```

---

# 35. 最终原则

整个改造应始终遵守：

> **发现页负责告诉系统“哪里可能有新闻”；正文页负责告诉系统“究竟发生了什么”；讨论页负责告诉系统“现在有多少人在关心”。**

三者不能混用。

最终目标不是：

> 抓最多的新闻。

而是：

> **用户点击一次以后，用尽可能有限的网络请求，找出当前真正值得花时间阅读和写作的少数事件。**
