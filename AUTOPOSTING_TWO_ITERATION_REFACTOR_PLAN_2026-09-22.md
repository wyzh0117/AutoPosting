# AutoPosting 两次迭代重构执行文档

> 日期：2026-09-22  
> 适用基线：AutoPosting `v3.0.0+`  
> 依赖基线文档：`README.md`、`AutoPosting 更新开发指导`  
> 执行目标：在 **两次迭代内** 完成产品主线、发现链路、Jev 判断层、统一设置入口与 UI 信息架构重构，并及时清理被替代的旧板块。  
> 核心约束：本文件不是推翻 v3.0.0 安全底座的“大重写”。所有修改继续遵守现有迁移、错误边界、诊断、持久化、发布安全与测试规范。

---

## 0. 本次重构最终要得到什么

AutoPosting 不再以“热点候选 / 观点候选 / GitHub 周榜 / Skill 查找器”四个历史栏目组织产品。

新的产品结构统一为：

```text
AutoPosting
│
├── 资讯探索 Information Discovery
│   ├── 长期订阅源采集
│   ├── 去重 / 聚类 / 排序
│   ├── 关联研究
│   └── Topic Dossier
│
├── 资源探索 Resource Discovery
│   ├── Repo
│   ├── Skill
│   ├── Tool / Website / Other
│   ├── 自然语言检索
│   └── Resource Set
│
├── 创作 Creation
│   ├── 文章生成
│   ├── 图片选择
│   ├── 视觉出版
│   └── 人工编辑
│
├── 发布 Publishing
│   ├── 审批
│   ├── 微信草稿
│   ├── 小红书本地发布包
│   └── 调度
│
└── 系统能力
    ├── 信息源管理
    ├── 运行记录
    └── 设置
```

产品主线从“四个栏目”收束为：

1. **资讯探索**
2. **资源探索**
3. **共享的研究 → 创作 → 视觉 → 发布链路**

---

# 1. 已确认的产品决策

以下决策视为本轮重构的固定需求，Agent 不再反问是否执行。

## 1.1 合并“热点资讯”和“观点 / 经验发现”

旧的：

- 热点候选
- 观点候选

合并为：

- **资讯探索 / 资讯雷达**

Reddit、HN、官方博客、媒体、社区、开发者账号等都只是不同来源类型，不再因为来源不同拆成一级栏目。

每个聚合后的资讯事件必须至少显示：

- 标题；
- 更新时间；
- 独立来源数；
- 相关内容总数；
- 来源类型；
- 排序分 / 热度信息；
- 可解释的靠前原因。

点击“X 个独立来源”后显示来源详情，而不是把所有来源平铺在主列表。

---

## 1.2 区分两类 Source

必须明确区分：

### Subscription Source

长期订阅、周期性抓取的固定来源。

例如：

- 官方新闻页；
- RSS；
- sitemap；
- 固定 subreddit；
- 固定 GitHub Release / API；
- 固定账号；
- 固定站点栏目。

进入 `Source Registry`，有启停、健康、抓取频率和运行状态。

### Evidence Source

某次“关联研究 / 自然语言搜索”过程中临时发现的页面、帖子、项目、文章或链接。

它属于某次 Research / Dossier 的证据，不自动进入长期订阅源。

禁止因为某次网页搜索访问了一个 URL，就自动把它永久加入 Subscription Source。

---

## 1.3 每次采集都形成 Collection Run

现有“来源健康”升级为真正的抓取运行记录。

每次采集至少记录：

```text
run_id
started_at
finished_at
requested_sources
attempted_sources
successful_sources
failed_sources
items_fetched
items_inserted
items_after_dedupe
clusters_created_or_updated
degraded
diagnostics
```

UI 至少展示：

```text
本次尝试 18 个源
成功 17
失败 1
获得 183 条
新增 61 条
去重后 74 条
聚类后 26 个事件
```

并可展开查看本次具体抓取了哪些 Subscription Source、每个源是否成功、抓到多少内容。

---

# 2. LLM + Jev 的职责划分

## 2.1 核心原则

采用：

```text
LLM = Planner / Interpreter / Generator
Jev = Judge / Scorer / Router
普通代码 = Fetch / Calculate / Persist / Enforce
```

不要把 Jev 当作搜索引擎，也不要让 LLM 和 Jev 用自由文本互相“聊天”。

二者之间必须通过显式 contract 连接。

---

## 2.2 自然语言资源搜索

用户可以直接输入：

> 帮我找一些能让 Agent 自动控制浏览器、截图并处理网页表单的工具。

处理链：

```text
用户自然语言
    ↓
LLM Query Planner
    ↓
ResearchIntent / SearchPlan
    ↓
普通代码调用 GitHub / skills.sh / Web / 其他 Search Adapter
    ↓
获得大量 Candidate
    ↓
Jev 执行快速结构化判断
    ↓
读取高价值候选的 README / SKILL.md / 页面正文
    ↓
Jev 二次判断 / 排序
    ↓
Resource Set
```

### LLM 的任务

LLM 负责把自然语言转换成结构化 `ResearchIntent / SearchPlan`，例如：

```json
{
  "goal": "寻找可用于 Agent 浏览器控制、截图和表单操作的可复用工具",
  "concepts": [
    "browser automation",
    "computer use",
    "screenshot",
    "form filling"
  ],
  "search_repo": true,
  "search_skill": true,
  "search_web": true,
  "preferred_signals": [
    "active maintenance",
    "clear documentation",
    "agent integration"
  ]
}
```

字段以项目实际 contract 为准，不要求照抄此示例。

### Jev 的任务

Jev 不生成搜索词，不访问 GitHub，不写介绍文。

Jev 负责：

- 是否符合用户需求；
- 与需求的相关程度；
- 候选属于什么资源类型；
- 是否值得进一步读取；
- 是否值得进入最终结果；
- 结果之间是否高度重复；
- 内容语义类别判断。

---

## 2.3 资讯关联研究

用户可以从资讯雷达选择一个或多个事件，点击：

**开始研究 / 扩展研究**

处理链：

```text
选中事件
   ↓
LLM 生成 ResearchIntent + SearchPlan
   ↓
搜索官方、媒体、社区、GitHub、网页等范围
   ↓
候选 Evidence
   ↓
Jev 判断与当前主题关系
   ↓
高价值 Evidence 深入读取
   ↓
再次归类与筛选
   ↓
Topic Dossier
```

Jev 至少支持以下关系判断：

```text
same_event
background
follow_up
industry_reaction
user_experience
competitor
criticism
related_project
topic_only
irrelevant
```

这些值必须进入共享 contract，不得以裸字符串散落。

---

# 3. Jev 正式进入排序，不使用 Shadow Mode

本重构不采用 Shadow Mode。

Jev 从第一版新排序开始即参与正式结果，但必须采用 **混合排序**，不能让 Jev 单独决定最终热点。

## 3.1 资讯综合排序

客观机器指标负责：

- 发布时间 / 时效；
- 独立来源数；
- 相关内容数量；
- 增长速度；
- 来源等级；
- 可用互动量；
- 统计异常 / spike；
- 是否存在官方原始来源。

Jev 负责机器规则难以稳定判断的部分：

- 行业重要性；
- 新颖性；
- 对目标用户的内容价值；
- 是否值得继续研究；
- 是否只是营销 / 旧闻换皮；
- 讨论价值。

初始默认：

```text
objective_score = 60%
jev_semantic_score = 40%
```

权重必须使用具名、集中配置的 Product Rule，不得成为散落 magic number。

Jev 置信度不足时，该维度不应硬算高分。

---

## 3.2 Jev 不可用时的降级

这不是 Shadow Mode。

这是外部服务故障降级。

```text
Jev 正常
→ objective + Jev 混合排序

Jev 超时 / 配额 / 无配置 / 非法响应
→ objective-only 排序
→ 标记 degraded
→ 给出稳定 diagnostics
```

禁止因为 Jev 不可用而导致：

- 资讯列表完全无法加载；
- Source 抓取失败；
- Resource Search 无结果；
- 原始客观排序能力被一起拖死。

---

# 4. Decision Service

禁止在以下位置各写一套 Jev API 调用：

- hotspot；
- skill finder；
- github weekly；
- web.py；
- related search；
- article。

建立统一：

```text
Decision Service
    ↓
Jev Adapter
    ↓
Question / Rubric Registry
```

建议职责：

```text
decision/
├── contracts.py
├── service.py
├── rubrics.py
└── adapters/
    └── jev.py
```

实际目录可以根据当前代码结构调整，但职责必须独立。

所有 Jev Question / Score / Choice 定义：

- 集中管理；
- 有稳定 id；
- 有版本；
- 可测试；
- 输出严格校验；
- 失败走现有安全诊断模型；
- API Key 不进入 SQLite、日志或响应。

---

# 5. 长期订阅源扩展策略

当前不建议简单把“31 路”扩成“100 路”。

目标不是 URL 数量，而是 **不同类型的一手信息覆盖率**。

优先顺序：

## Tier A：官方原始来源

优先补齐主要 AI 实验室、模型厂商、基础设施厂商、开发平台的：

- News / Blog；
- Product Updates；
- Research；
- Changelog；
- Sitemap；
- GitHub Releases。

优先考虑的长期来源池包括：

- OpenAI News；
- Anthropic Newsroom；
- Google DeepMind News；
- Google AI / Technology AI；
- Meta AI Blog；
- Hugging Face Blog；
- NVIDIA Technical Blog；
- DeepSeek 官方 sitemap（现有）；
- 重点模型 / Agent 项目的官方 GitHub Releases。

对于国内模型厂商，优先使用官方站点、官方文档更新页、官方 GitHub / Gitee 组织或可稳定抓取的公开 sitemap；不要优先依赖转载媒体。

## Tier B：高质量开发者 / 工程社区

用于发现官方信息之外的实际反馈：

- Hacker News；
- Reddit 精选 AI / LLM / Local LLM / Agent 社区；
- GitHub Trending / Search / Releases；
- Hugging Face 社区文章；
- 经过明确登记的开发者 / 研究人员公开账号。

## Tier C：可靠媒体

用途是补充：

- 行业影响；
- 采访；
- 商业背景；
- 跨公司比较。

媒体不替代 Tier A 的官方原始来源。

## Tier D：探索性来源

例如：

- 新增小众博客；
- 临时发现的网站；
- 某次研究中高质量 Evidence Source。

默认先作为 Evidence Source。

只有持续验证有价值后，用户再“加入长期订阅”。

---

# 6. Subscription Source Registry 应升级的字段

长期源至少支持：

```text
source_id
name
source_type
tier
url
adapter_type
enabled
fetch_interval
identity_verified
topic_scope
language
last_success_at
last_attempt_at
last_error_code
consecutive_failures
items_last_run
created_at
updated_at
```

具体字段应复用现有 source schema 能力，涉及 DB 变化时走现有 `PRAGMA user_version` 事务迁移体系。

### source_type 示例

```text
official
media
community
developer
research
repository
other
```

### adapter_type 示例

```text
rss
sitemap
html
reddit
github
api
account
```

不要把来源类型与抓取协议混成一个字段。

---

# 7. 新 UI：完全不沿用旧四栏目布局

最终一级导航：

```text
首页
资讯
资源
创作
发布

信息源
运行记录
设置
```

---

## 7.1 首页

首页首先回答：

> 今天想发现什么？

主输入框：

```text
搜最近值得关注的 AI 新闻
找能自动制作 PPT 的 Agent Skill / Repo
研究某个事件
……
```

提供快捷入口：

- 资讯探索；
- 资源探索；
- 继续最近研究。

下方显示最近：

- Topic Dossier；
- Resource Set；
- Draft。

---

## 7.2 资讯

主列表单位从“单篇 RSS item”升级为 **Event Cluster**。

每张卡显示：

```text
标题
综合分
更新时间
3 个独立来源 · 8 条相关内容
官方 / 媒体 / 社区
为什么靠前
```

核心操作：

- 查看来源；
- 选择；
- 多选；
- 开始研究；
- 刷新资讯。

筛选：

- 时间范围；
- Topic；
- 来源类型；
- 排序方式。

用户不再看到“热点候选 / 观点候选”两个 tab。

---

## 7.3 信息源

独立页面，不再塞在资讯列表底部。

顶部：

```text
31 总源
14 启用
17 停用
13 正常
1 异常
```

每行：

```text
名称
类型
Tier
状态
最后抓取
最近新增
开关
手动刷新
```

同时显示最近 Collection Run。

---

## 7.4 研究页

选择一个或多个 Event Cluster 后进入 Topic Dossier。

内容按关系组织：

```text
官方信息
背景
后续进展
行业反应
用户体验
质疑 / 反方
竞争产品
相关项目
```

统一替代：

- 找切口；
- 相关检索；
- 零散外链补充。

页面核心按钮：

**继续扩展研究**

---

## 7.5 资源

资源探索不再拆“GitHub 周榜 / Skill Finder”。

输入任意自然语言。

结果类型允许同时包含：

```text
Repo
Skill
Tool
Website
Other
```

每条至少展示：

```text
name
type
match score
why matched
source
maintenance / quality signals
```

### GitHub 周榜

降级为资源探索中的一个 Preset：

```text
本周 AI 新项目
```

其他 Preset 可包括：

```text
本周增长最快
Agent 工具
AI 创作工具
Agent Skills
```

GitHub Weekly 不再作为一级产品。

---

## 7.6 创作

创作页面与发现页面分离。

输入：

- Topic Dossier；
- Resource Set；
- 用户手工素材。

创作页负责：

- 写作类型；
- 模型；
- 标题；
- 正文；
- caption；
- 图片选择；
- 视觉渲染；
- 保存。

文章 AI 和原生聊天能力保留，但 API / 模型配置不在创作页面重复出现。

---

## 7.7 发布

发布页负责：

- 草稿审批；
- 微信草稿；
- 小红书发布包；
- 调度；
- 状态和历史。

继续遵守现有边界：

- 微信 live 只进入官方草稿箱；
- 不调用 `freepublish`；
- 小红书继续人工发布；
- 不因为 UI 重构改变发布安全策略。

---

## 7.8 设置

**所有 API / Credential / Provider 配置集中到“设置”。**

至少分组：

### AI Providers

- Planner / Article / Translation / Chat LLM；
- Jev；
- 后续其他模型。

### Search / Data

- GitHub Token；
- 需要凭据的搜索服务；
- 可选外部 Reader / Search Provider。

### Publishing

- WeChat；
- 其他已有发布配置。

### Appearance / Content

- 写作风格；
- 视觉主题；
- 其他真正属于用户偏好的参数。

原则：

> 功能页面只选择“用什么”，不负责“怎么配置 API”。

所有 secret 继续使用现有安全存储策略，不回显明文。

---

# 8. 第一迭代：产品骨架 + 资讯链路

## Iteration 1 目标

先完成：

> **资讯探索真正成立 + 新 UI 骨架成立 + LLM/Jev 决策链成立 + 设置集中化。**

不要在第一迭代同时重写所有文章和视觉代码。

---

## 8.1 必须完成

### A. 新产品导航和页面骨架

建立：

- 首页；
- 资讯；
- 信息源；
- 运行记录；
- 设置；
- 创作 / 发布入口占位或接入现有能力。

旧四栏目导航退出主 UI。

---

### B. 合并热点 / 观点

- Reddit 等观点源进入 Subscription Source Registry；
- 统一进入采集 / normalize / dedupe / cluster 管线；
- 不再保留独立“观点候选”主页面；
- Event Cluster 支持多个不同来源类型。

---

### C. Collection Run

采集必须有本次运行记录。

资讯页和“运行记录”页可以查看：

- 本次抓了哪些源；
- 成功 / 失败；
- 抓取量；
- 去重；
- 聚类。

---

### D. Event Cluster

把资讯展示单位从单 Item 推进到 Cluster。

至少支持：

- 独立来源数；
- 相关内容数；
- 来源详情；
- 多选；
- stable id；
- refresh 后可恢复。

---

### E. LLM Planner + Decision Service + Jev Adapter

第一迭代至少落地：

- ResearchIntent contract；
- SearchPlan contract；
- DecisionAssessment contract；
- Jev 配置；
- Jev adapter；
- 资讯综合评分。

Jev 直接进入正式排序。

---

### F. 资讯“关联研究”V1

至少实现：

```text
选中一个或多个事件
→ Planner 生成 SearchPlan
→ Search Adapter 拉候选
→ Jev relevance / relation
→ Topic Dossier
```

第一迭代不要求关系图等复杂可视化。

先用分组列表完成真实业务闭环。

---

### G. 设置集中化

把现有散落在各工作区中的：

- LLM API；
- GitHub Token；
- Jev；
- 发布 Credentials；

统一收口到设置入口。

功能页面不得继续出现“新增 API / 输入 Token”式配置动作。

---

## 8.2 第一迭代必须弃用 / 清理

### UI 直接删除

- “观点候选”独立 tab；
- “热点候选”旧命名；
- 页面底部旧式来源健康折叠块；
- 功能页面中的 API 配置入口。

### 功能语义弃用

- `hooks/find` 作为“找切口”产品功能；
- 独立“相关检索”产品概念。

两者由 `Research / Topic Dossier` 统一替代。

### API 处理

第一迭代可以保留旧 API 的 **薄兼容 façade**，但：

- 不允许继续新增功能；
- 内部必须调用新 service；
- 标记 deprecated；
- README 不再作为推荐入口；
- 明确第二迭代删除条件。

禁止保留两套业务实现。

---

## 8.3 第一迭代验收

必须真实验证：

1. 资讯页不再有热点 / 观点双 tab；
2. Reddit / 社区源能和官方 / RSS 源进入统一列表；
3. 每次刷新可以看到实际尝试的 Subscription Source；
4. Cluster 显示独立来源数和内容数；
5. 点击来源数可查看来源明细；
6. 多选事件可以进入研究；
7. Planner 产生结构化 SearchPlan；
8. Jev 实际参与候选判断和综合排序；
9. Jev 故障时 objective-only 降级且有 diagnostics；
10. 设置页可以完成相关 API 配置；
11. 刷新浏览器 / 重启服务后关键持久化状态符合 contract；
12. 全量测试和 self-check 不退化；
13. UI 行为有浏览器级或 faithful runtime 验收。

---

# 9. 第二迭代：资源探索 + 创作发布整合 + Legacy 清场

## Iteration 2 目标

第二迭代完成：

> **GitHub Weekly + Skill Finder 合并、自然语言资源探索成立、创作 / 发布最终布局落地，并清理本轮重构产生的全部旧兼容入口。**

完成后本次重构结束，不继续拆成 T3 / T4 / T5。

---

## 9.1 必须完成

### A. Resource Discovery

统一替代：

- GitHub Weekly；
- Skill Finder。

用户只需要自然语言输入。

Planner 决定：

- 搜 Repo；
- 搜 Skill；
- 搜 Web；
- 可以同时搜索多个类型。

---

### B. 搜索适配器复用

整理成：

```text
Search Orchestration
    ↓
GitHub Adapter
skills.sh Adapter
Web Search Adapter
其他已授权 Adapter
```

禁止把新逻辑继续塞进现有大 `find_skills()`。

如果本次触碰 `find_skills()`，应按开发指导做“小切片式治理”，把本次涉及阶段抽离。

---

### C. Resource Judge

Jev 至少判断：

- relevance；
- resource type；
- worth_reading；
- duplicate / near duplicate；
- fit_for_goal。

代码继续计算：

- stars；
- fork；
- last update；
- release freshness；
- repo activity；
- 来源；
- 文档是否存在等客观信号。

最终排名为客观信号 + Jev 语义判断。

---

### D. GitHub 周榜变成 Preset

保留产品价值，不保留一级栏目。

```text
Preset: 本周 AI 新项目
```

它调用统一 Resource Discovery。

周榜现有 3:4 视觉出版能力继续保留。

---

### E. Skill 变成 Resource Type

Skill 特有能力继续保留：

- 读取 `SKILL.md`；
- monorepo skill 拆分；
- workflow 分类；
- 粘贴 Skill URL；
- Skill 海报。

但不再维护一套独立“Skill Finder 产品”。

---

### F. 创作中心

把现有文章编辑、图片、模型输出、聊天等能力重新组织到独立 Creation 页面。

发现 / 研究页面不再混入完整文章编辑器。

---

### G. 发布中心

把：

- approve；
- publish；
- schedule；
- publication history；

统一进入 Publishing 页面。

不改变现有微信 / 小红书安全边界。

---

## 9.2 第二迭代必须清理

在静态调用、tests、CLI、README、AGENTS、Web、外部 import 兼容面检查完成后：

删除已经完全由新架构替代的：

- 旧观点候选 UI；
- 旧热点候选 UI；
- 独立 GitHub Weekly 一级页面；
- 独立 Skill Finder 一级页面；
- 独立找切口 UI；
- 独立相关检索 UI；
- 旧 API 配置入口；
- 已无调用者的旧 JS / CSS；
- 仅服务旧页面的 handler；
- 不再需要的重复排序 / 筛选实现；
- 第二迭代到期的 deprecated façade。

如果发现某个旧函数仍是明确的 CLI / external import 兼容面：

- 不允许悄悄改语义；
- 将其缩成调用新 service 的薄 façade；
- 在最终报告列明为何暂时保留。

目标是：

> **不保留两套产品，不保留两套业务算法，不因为“保险”让旧板块永久并存。**

---

# 10. 新数据对象建议

本次重构优先形成以下共享契约。

```text
SubscriptionSource
EvidenceSource
CollectionRun

DiscoveryItem
EventCluster

ResearchIntent
SearchPlan
EvidenceCandidate
DecisionAssessment
TopicDossier

ResourceCandidate
ResourceAssessment
ResourceSet
```

不要为了赶进度把这些对象全部做成无约束 dict。

如果需要持久化：

- 明确 schema version；
- 复用现有 envelope；
- DB 变化走 `PRAGMA user_version`；
- 未知版本不得静默为空。

---

# 11. HTTP / Web 层要求

遵守原开发指导：

```text
route
→ request validation
→ application service
→ domain / repository / adapter
→ stable response
```

禁止因为本轮 UI 变化：

- 在 `web.py` 堆新业务算法；
- 在 handler 中直接调用 Jev；
- 在 handler 中执行搜索聚合；
- 在前端重新实现评分规则；
- 为旧页面复制一份新 payload。

---

# 12. 失败与降级

必须明确区分：

```text
正常无结果
搜索源无结果
单个来源失败
部分来源降级
LLM Planner 失败
Jev 失败
Jev 低置信度
数据损坏
未知异常
```

不得全部显示成：

> 暂无内容

推荐用户可理解提示，例如：

```text
已找到 42 条候选，其中 GitHub 搜索因限流未完成；
当前结果来自 skills.sh 与网页搜索。

Jev 判断服务暂不可用；
当前排序仅使用来源、时间和传播等客观指标。
```

---

# 13. 测试要求

除现有全量测试外，本重构必须新增：

## 资讯

- mixed source collection；
- collection run；
- cluster 多来源；
- 来源弹窗数据；
- merge / dedupe；
- Jev scoring；
- Jev fallback；
- multi-select research；
- dossier restore。

## LLM Planner

- 普通自然语言；
- 中文长句；
- 模糊需求；
- repo + skill 同时 true；
- invalid structured output；
- provider timeout。

## Resource Discovery

- repo only；
- skill only；
- repo + skill；
- GitHub rate limited；
- skills.sh failure；
- Web failure；
- Jev low confidence；
- duplicate resource；
- pasted Skill merge。

## Settings

- 配置保存；
- secret 不回显；
- 无配置提示；
- 删除配置；
- 重启恢复。

## UI

至少真实验证：

- 首页输入；
- 资讯刷新；
- 来源明细；
- 多选；
- 研究；
- 资源搜索；
- preset；
- 设置；
- 创作；
- 发布；
- 刷新；
- 长内容；
- 异步状态；
- 错误提示。

---

# 14. 两次迭代的明确边界

## Iteration 1

```text
新 IA / UI 骨架
+ 热点 / 观点合并
+ Source Registry / Collection Run
+ Event Cluster
+ LLM Planner
+ Decision Service / Jev
+ 资讯关联研究 V1
+ 设置集中化
```

## Iteration 2

```text
Resource Discovery
+ GitHub Weekly → Preset
+ Skill Finder → Resource Type
+ 创作中心
+ 发布中心
+ Legacy / Deprecated 清理
+ 文档与测试最终收口
```

除非出现阻塞性架构缺陷，本轮重构 **不得继续拆成第三、第四轮**。

如果某项无法在第二迭代完成：

- 明确写入最终报告；
- 说明为什么；
- 不以“后续再优化”为理由保留两套实现。

---

# 15. 本轮明确不做

以下内容不属于这次重构目标：

- 更换现有 HTML/CSS → Browser Screenshot 的视觉技术路线；
- 新增图像生成模型；
- 擅自增加微信 `freepublish`；
- 绕过小红书人工发布边界；
- 新增未授权的发布平台；
- 全项目无差别大重写；
- 为了 UI 漂亮重做已经稳定的安全底座；
- 与本轮业务无关的 Orchestrator / 进程编排能力。

---

# 16. Agent 执行前置要求

每次迭代开始前必须阅读：

1. `README.md`
2. `AutoPosting 更新开发指导`
3. 本文件
4. 当前最新 PATCH / git 状态

先确认现状，再修改。

---

# 17. 每次迭代结束必须交付

按照现有项目规范：

- 更新 `README.md`；
- 写 `docs/PATCH_{版本}.md`；
- 更新必要架构 / API 文档；
- 更新真实 UI 行为测试文档；
- 跑 targeted tests；
- 跑全量 tests；
- 跑 self-check；
- `git diff --check`；
- 按现有规则生成源码归档与 sha256。

最终 Completion Report 必须明确：

```text
Completed
Changed
Deleted
Deprecated
Migration
Tests
Real UI Verification
Known Limitations
Remaining Work
Next Iteration
```

第二迭代的 `Remaining Work` 不应再出现本重构主体功能。

---

# 18. Definition of Done

本次重构只有在以下条件同时满足时才算完成：

- 用户不再看到“热点候选 / 观点候选 / GitHub 周榜 / Skill Finder”四个一级栏目；
- 资讯已经统一为多来源 Event Cluster；
- 用户知道每个事件有几个独立来源并可展开；
- 用户知道每次采集实际抓了哪些长期源；
- Subscription Source 与 Evidence Source 已分开；
- 用户可从一个或多个资讯事件启动关联研究；
- LLM 能把自然语言转换成 SearchPlan / ResearchIntent；
- Jev 已真实参与资讯和资源判断，而非只做实验记录；
- Jev 故障不会拖死核心流程；
- 用户可用自然语言同时搜索 Repo / Skill / 其他资源；
- GitHub Weekly 成为 Preset；
- Skill 成为 Resource Type；
- 所有 API / Token / Provider 配置集中在设置；
- 创作与发现解耦；
- 发布与发现解耦；
- 被替代的旧 UI / handler / duplicated business logic 已清理；
- 没有重新把复杂业务塞回 `web.py` / `find_skills()` / `Database` / `Workbench`；
- v3.0.0 安全底座保持；
- 全量测试、self-check 和真实 UI 验收完成或明确记录环境限制。

---

# 19. 产品定义（重构后的统一口径）

> **AutoPosting 是一个从“发现值得关注的信息与资源”，到“扩展研究”，再到“内容生产、视觉包装与发布”的本地优先 AI 信息生产系统。**

其中：

- LLM 负责理解、规划与生成；
- Jev 负责快速结构化判断；
- 普通代码负责采集、计算、约束、持久化和执行；
- 用户负责最终选择、编辑和发布决策。

这套职责边界应成为后续 AutoPosting 新功能设计的默认基线。
