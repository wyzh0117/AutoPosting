# STYLE.md 迁移报告

原文件：`docs/image_prompts/STYLE.md`（508 行）  
完整备份：`docs/image_prompts/legacy/STYLE.original.md`  
活文件：`STYLE.md` 现为图像模型资产生成规范，不再混装页面结构与内容字段。

没有整段原样复制到某一层。同一段落若跨层级，按职责拆开。

## 对照表

| STYLE.md 原内容 | 新层级 | 新文件 | 处理方式 | 原因 |
|---|---|---|---|---|
| 任务类型 / 科技信息可视化海报目标 | Design System + Assets | `DESIGN_SYSTEM.md`；`STYLE.md` 保留资产版 | 拆分。整页中文改由 HTML 负责 | 原「海报」目标拆成品牌目标 vs 资产生成目标 |
| 「不是宣传海报」及媒体+SaaS+Dashboard 类比 | Design System | `DESIGN_SYSTEM.md` Brand Identity | 迁入并改写为品牌陈述 | 无具体数据，是品牌定位 |
| 画布竖版、9:16、1k 高清 | Design System | `tokens.json` canvas 1080×1920；`STYLE.md` 仍写 9:16 | 具体化为 1080×1920 | 「1k」需落地；与既有 XHS 卡一致 |
| 安全边距、禁止越界/裁切/Logo 截断 | Rendering Rules + Design System | `RENDERING_RULES.md`；`tokens.json` safeArea | 拆：边距是 token，越界策略是规则 | 同时涉及两层 |
| 视觉关键词 Dark Technology 等 | Design System + Assets | `DESIGN_SYSTEM.md`；`STYLE.md` | 两边保留（资产 prompt 仍需要） | 图像模型读 STYLE.md；HTML 读 token |
| 低饱和中性 + 高亮主题 + 模块化卡片 | Design System | `tokens.json` 色板；`DESIGN_SYSTEM.md` | 定性→定量 | 原文无 hex |
| 不要赛博朋克 / 霓虹 / 电商 / PPT | Design System Do/Don't + Assets | `DESIGN_SYSTEM.md`；`STYLE.md` 禁止事项 | 去重后两处各留一份短列表 | 资产生成与 HTML 都要守 |
| 科技感来自排版/卡片/数据而非装修 | Design System | `DESIGN_SYSTEM.md` | 迁入 | 品牌原则 |
| 颜色黄金法则 60/30/10 | Design System | `DESIGN_SYSTEM.md` + token 角色注释 | 迁入并指定 hex | 原文无具体色相 |
| 允许少量 glow | Design System | `tokens.json` glow / color.glow | 定量 | |
| 纵向信息层级 logo→标题→模块→页码 | Page Schema + Design System | 两个 page-schema；`DESIGN_SYSTEM.md` Hierarchy | 拆：层级原则归 DS，具体页面归 schema | 原文把「所有页面」和「cover/poster 结构」写在一起 |
| 左对齐、卡片分区、网格、忌全居中 | Design System | `DESIGN_SYSTEM.md` Spacing/Grid | 迁入 | 全局版式 |
| 中英文字体、字重 | Design System | `tokens.json` typography | 指定栈与字号 | 原文无 px |
| 图标线性/半填充、统一视觉重量 | Design System + Component | `DESIGN_SYSTEM.md`；ProjectLogo 组件 | 风格归 DS，容器归组件 | |
| Logo 放入圆角容器 | Component Library | ProjectLogo | 迁入组件规则 | |
| 「整个系统主要生成 cover 和 poster」 | Page Schema | `ranking-overview` = cover；`project-detail` = poster | **重命名并改技术路线** | 原 cover/poster 是图像模型整页；现为 HTML 页。`cover.md`/`poster.md` 仍作资产 prompt |
| cover 页面结构 01–07 | Page Schema | `page-schemas/ranking-overview.md` | 去掉「GitHub」「本周爆火」等写死文案，改成组件槽 | 原文把 schema 和 example 混在一起 |
| 排行榜单项：排名/logo/名称/介绍/指标/进度条 | Component Library | RankingItem, RankingBadge, Metric, ProgressBar | 从页面结构拆出可复用积木 | |
| poster 页面结构 01–12 | Page Schema | `page-schemas/project-detail.md` | 项目名称在原文出现两次（01 与 03），合并为一次 HeroTitle | **冲突**：重复标题槽 |
| 留白规则（模块间 > 模块内，标题区最大） | Design System + Rendering Rules | spacing token；RENDERING_RULES 密度 | 原则归 DS，冲突处理归 Rules | |
| 卡片主色背景、1px 边框、中等圆角、不要厚阴影 | Design System | surface / border / radius / shadow | 定量 | |
| 细节：分割线、圆点、状态灯、徽章、页码、期数、来源 | Component Library | IssueBadge, PageNumber, RankingBadge, TopicTags, Footer | 能组件化的落地；「状态灯」暂未单独做 | 状态灯见未归类 |
| 每张最多 3 个视觉焦点 | Design System + Rendering Rules | Hierarchy；priority 列表 | 拆 | |
| 文字必须正确完整、禁止伪文字/乱码 | Rendering Rules | `RENDERING_RULES.md` | 迁入 | 这是冲突处理，不是色板 |
| 专有名词用英文、中英混排自然 | Rendering Rules / 文案约定 | `RENDERING_RULES.md` | 保留为规则 | 不是 token |
| 内容数据接口 BRAND_NAME, SERIES_NAME, ISSUE… | Content Schema | `ranking.schema.json` `project.schema.json` | 改名为 camelCase JSON 字段 | 原文是提示词变量，现为数据契约 |
| ITEMS 统一结构 rank/name/icon/description/metric/category/status | Content Schema | rankingItem | `icon`→`logo`；`status` 保留可选 | 与 GitHub adapter 对齐 |
| cover 页 ITEM 01… 示例 | Examples | `examples/ranking.example.json` | 用虚拟项目，复用 agency-agents | 原文是占位，不是真实周榜 |
| poster 页 PROJECT_NAME… 字段表 | Content Schema + Example | `project.schema.json`；`project.example.json` | 字段迁 schema，agency-agents 实例迁 example | 用户指定优先复用该实例 |
| 系列一致性：同背景/字体/色/卡片/边框/圆角/标题位置/品牌位置/页码 | Design System | `DESIGN_SYSTEM.md` 视觉一致性 | 迁入 | 允许变的是内容与主视觉 |
| 禁止事项长列表 | Design System Don't + STYLE.md | 两边短列表 | 去重 | 重复规则 |
| 最终视觉要求「专业媒体卡片而非 AI 随便生成」 | Design System | Brand Identity | 迁入 | 品质标准 |
| `{style}` 被 cover.md/poster.md 引用 | Assets（保留） | 活 `STYLE.md` | **未删除** | `image_gen.py` 与测试仍依赖整文件拼进 prompt |

## 重复规则

- 禁止赛博朋克 / 霓虹 / PPT / 电商：Design System 与 STYLE.md 各留一短份。资产 prompt 无法去读 DESIGN_SYSTEM.md。
- 9:16：token、STYLE.md、cover.md、poster.md 都出现。token 是权威尺寸；prompt 文件只声明比例，避免图像模型丢掉约束。
- 「卡片 + 细边框 + 中等圆角」在原文出现于版式、边框、一致性三处，合并进 token。

## 冲突规则

| 冲突 | 裁决 | 原因 |
|---|---|---|
| 「深色文字」vs 关键词 Dark Technology / Premium Dashboard | 深色画布 + 浅色正文（`#F3F0E8`） | 深色仪表盘是主关键词；浅色纸面与现有 cover.md 静物风是资产层，不是 HTML 页 |
| cover.md/poster.md 是奶油色静物摄影；STYLE.md 是暗色仪表盘 | **并存、职责分离** | HTML 页走暗色 DS；图像模型资产仍可走静物 prompt。以后主视觉插入 HTML，而不是再生成整页中文静物海报 |
| poster 结构里「项目名称」写了两次（01 与 03） | 只保留一次 HeroTitle | 重复槽位 |
| 「1k 高清」未给像素 | 1080×1920 | 与仓库已锁定的 9:16 卡一致 |
| 颜色系统无色相，用户说明里举例「黑底紫强调」 | 近黑 + 紫晶 `#8B7CFF` + 铜金指标 | 举例符合 60/30/10，且避免霓虹 |

## 模糊规则

- 「1k 高清尺寸」：已落地 1080×1920，原文保留在 legacy。
- 「状态灯」：未做独立组件，暂未归类（见下）。
- 「细分割线 / 小型圆点」：圆点用在 FeatureList；分割线未单做，用卡片边框代替。
- cover 文案「GitHub / 本周爆火AI项目榜」：这是内容示例，不是 Design System。出现在 example JSON，不写进 token。

## 已废弃（相对新架构）

- 用一个 STYLE.md 控制整页中文海报生成。
- 把 cover/poster 当作图像模型整页输出的唯一页面类型名称（代码页名改为 ranking-overview / project-detail；`cover.md`/`poster.md` 降为资产模板）。

未删除任何原文：legacy 备份完整存在。

## 暂未归类 / 未实现

| 内容 | 原因 |
|---|---|
| 状态灯 | 原文只说「允许加入」，无结构、无字段。不删，待组件化 |
| Motion | 用户允许以后再做 |
| DashboardCard / DataCard / SourceLabel / ImageCard 的完整变体 | 第一阶段用 Panel / HeroVisual / Footer 覆盖主路径 |
| 将工作台「抓取周榜」切到 HTML 渲染 | 有意不做，避免和现有 11 张模型出图抢入口。adapter 已就绪 |
| 新闻 / 论文 / 工具页面 | 架构预留，第一阶段不实现 |

## 图像模型 STYLE.md 还留下什么

活 `STYLE.md` 保留：任务类型（改为「视觉资产」）、画布 9:16、视觉关键词、颜色法则（定性）、图标、系列一致性、禁止事项、最终品质。这些是 `{style}` 仍要喂给 cover/poster 资产 prompt 的部分。页面结构、字段表、cover/poster 槽位已迁走。
