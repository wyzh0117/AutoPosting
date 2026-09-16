# Page Schema · ranking-overview

回答：榜单总览页由哪些组件组成、怎么排列？

本文件只描述结构。具体项目名、星标、期数写在 Content Schema 里。

## 用途

GitHub 本周爆火 AI 项目榜封面 / 总览。

## 绑定内容

`RankingContent`（`content-schemas/ranking.schema.json`）

## 结构

```
BrandHeader          brand + issue
↓
HeroTitle            title + subtitle
↓
IssueMetadata        kicker + issue + dateRange
↓
RankingItem × N      rank, logo, name, description, metric, progress
↓
TopicTags            tags
↓
Footer               footer/source + page number
```

## 布局规则

- 画布 3:4，纵向 flex。
- 列表吃掉标题和页脚之外的剩余高度，条目均分。
- 左对齐。条目内部四列：排名 | Logo | 文案 | 指标。
- N ≤ 10。超过由 Rendering Rules 截断。
- 不写死「agency-agents」或某一周日期。
