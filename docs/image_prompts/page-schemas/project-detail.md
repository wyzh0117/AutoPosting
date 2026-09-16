# Page Schema · project-detail

回答：项目详情页由哪些组件组成、怎么排列？

## 用途

周榜中某一个仓库的详情卡片。

## 绑定内容

`ProjectContent`（`content-schemas/project.schema.json`）

## 结构

```
BrandHeader                 brand + issue
↓
Masthead
  左：RankBadge + CategoryTag
      HeroTitle (name + tagline)
      QuoteCard「一句话看懂」
  右：ProjectLogo（专用 logo，否则主题 SVG）
↓
BulletPanel「做什么？」        主面积，短 bullet
↓
BulletPanel「如何实现？」 | BulletPanel「适合哪些人群」
↓
BulletPanel「使用场景」
↓
Footer                      footer + pageNumber
```

## 布局规则

- 画布 9:16。主体只保留一句话看懂、做什么、如何实现、适合哪些人群、使用场景。
- 「做什么？」占中部最大面积；各块都是短 bullet，不用大段说明。
- 不放功能示意图、指标面板、项目地址卡片。
- Logo 在右上：README 专用标可用；GitHub 作者头像不用；否则用带标签的本地主题 SVG 并复用。

## 预留页面（未实现）

news-detail、tool-detail、comparison、data-insight、weekly-summary。新增时复制本文件结构，绑定新的 Content Schema，不要改 Design System。
