# Component Library

回答：页面可以由哪些标准化视觉积木组成？

组件实现：`src/autoposting/visual/components.py`。样式引用 Design Token，不写某一期的项目数据。

| 组件 | 用途 | 必填 | 可选 | 结构 | 视觉规则 | 变体 / 溢出 |
|---|---|---|---|---|---|---|
| BrandHeader | 品牌条 | `brand` | `trailing`（期数）、`avatar` | 头像/色块 + 词标 \| 右侧徽章 | 默认著作 id「EE_萤飞」+ 头像；CJK 词标不大写 | 无 |
| HeroTitle | 主标题 | `title` | `subtitle` | h1 + 副标题 | 最多两行，副标题单行省略 | ranking 76px / project 56px |
| IssueBadge | 期数胶囊 | `text` | | 描边 pill | 拉丁大写 | 文本过长省略 |
| CategoryTag | 分类 | `label` | | 描边 pill | 不强制英文大写 | 单行 |
| RankingItem | 榜单行 | `rank,name,description,metric` | `logo,progress` | 排名+Logo+文案+指标 | 内部紧凑，外缘圆角卡片 | 名称/介绍单行省略 |
| RankingBadge | 名次 | `rank` | | 两位数字 | 前三名高对比，其后降权 | `01` 格式 |
| ProjectLogo | 标识 | `name` | `src` | 56×56 圆角容器 | 无图则首字母 | `contain` |
| Metric | 数字 | `value` 或 `display` | `label` | 数字 + 标签 | 铜金色，compact-k | 右对齐 |
| ProgressBar | 相对热度 | `progress` 0–1 | | track + fill | 最小可视 8% | 不超出卡片 |
| QuoteCard | 一句话看懂 | `message` | `label` | 标签 + 段落 | 白话能力范围，最多 4 行 | 超出 clamp |
| BulletPanel | 短列表积木 | `title,items[]` | `featured` | 标题 + 图标 bullet | 详情页主体；featured=做什么 | 每条 ≤16 字，最多 5 条 |
| FeatureList | BulletPanel 的做什么变体 | `items[]` | | 同 BulletPanel | 兼容旧调用 | |
| Footer | 来源/口号 | `text` | `pageNumber` | 左文案 \| 右页码 | 低对比 | |
| PageNumber | 页码 | `text` | | pill | `02 / 11` | |
| ProjectLogo | 仓库/产品标识 | `name` | `src,kind,markSvg` | 圆角容器 | 不用作者头像；无专用 logo 用主题 SVG | 详情页右上与标题块同高 |
| RankingList | 榜单容器 | `items[]` | | RankingItem × N | flex 均分剩余高度 | 最多 10 条 |
| TopicTags | 底部标签 | `tags[]` | | pill 横排 | 最多 6 个 | 不换行，溢出隐藏 |
| DataCard / DashboardCard / SourceLabel | 预留 | | | 复用 Panel | 第一阶段未单独实现 | 可按 Panel 扩展 |

新增组件：在本表补一行 → `components.py` 增加纯函数 → 页面 schema 引用 → CSS 只使用 token。
