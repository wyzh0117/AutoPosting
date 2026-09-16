# Design System

回答：整个视觉品牌应该长什么样？

本层只定义全局设计语言。不含某一期项目名、星标、周次。具体色值与尺寸以 `tokens.json` 为准。

## Brand Identity

专业 AI 科技媒体卡片。关键词：Dark Technology, AI Native, Cyber Minimalism, Premium Dashboard, Editorial Infographic, Modern SaaS UI, High-end Tech Media。

感觉：高雅、专业、克制、现代、干净。科技感来自排版、卡片、数据和少量光效，而不是装修。

## 色彩系统

黄金法则：不超过三种主要颜色。60% 主色、30% 辅助色、10% 强调色。禁止彩虹配色。

| Token | 角色 | 值 |
|---|---|---|
| `color.background` | 60% 空间基调 | `#0B0D12` 低饱和近黑 |
| `color.surface` / `surfaceRaised` | 30% 卡片层次 | `#141821` / `#1C2230` |
| `color.primary` | 10% 强调 | `#8B7CFF` 克制紫晶 |
| `color.text.primary` | 正文 | `#F3F0E8` 暖白 |
| `color.metric` | 数字 | `#E8D5A3` 铜金 |

允许极弱 glow（`color.glow`），禁止霓虹铺满。

STYLE.md 原文写过「深色文字」，同时关键词是 Dark Technology。本系统采用深色画布 + 高对比浅色正文，冲突记录见迁移报告。

## Typography

- 中文：现代无衬线（PingFang SC / Hiragino Sans GB / Noto Sans SC）
- 英文：grotesk / geometric sans（SF Pro Display / Inter）
- 标题：bold / heavy
- 正文：regular / medium
- 数字：semibold / bold
- 最小字号：13px（`rendering/rules.json` 的 `minFontPx`）

等级见 `tokens.json` → `typography` / `fontSize` / `fontWeight`。

## 画布与安全边距

竖版 9:16，1080×1920。`safeArea` 四周稳定留白。元素必须完整落在画布内。

## Surface / Border / Radius / Shadow / Glow

- 卡片背景用 surface，不是大面积渐变
- 边框 1px，颜色贴近主色调（`color.border`）
- 中等圆角：`radius.card` = 16
- 不要厚重阴影；可用极轻外发光 / 内高光
- Logo 放进统一圆角容器（`radius.logo`）

## Spacing / Grid / Hierarchy

模块之间的留白明显大于模块内部。标题区留白最大。优先左对齐、卡片分区、稳定列宽。避免所有元素居中。

纵向信息层级：

品牌 → 主题识别 → 核心标题 → 副标题 → 主题信息 → 辅助模块 → 来源 / 地址 / 页码

阅读节奏：「大标题」→「一句话理解」→「结构化信息」→「细节」。

每张最多 3 个视觉焦点：标题、核心结论/主视觉、主要数据。其他降权。避免所有区域一起发光。

## Icon / Image Style

线性或半填充 UI 图标，同一视觉重量、圆角、线宽。项目 logo 可保留自身特征，但必须落在统一容器中。

图像模型只生产主视觉、插画、3D 图标、抽象背景，作为 asset 插入页面。不负责大量中文正文排字。

## Motion

第一阶段不定义动效。

## Do / Don't

Do：模块化网格、清晰留白、克制强调色、真实语义文字、系列内背景/字体/色板/卡片/圆角一致。

Don't：赛博朋克城市、霓虹灯海报、电商促销、传统 PPT、廉价大渐变、粒子铺满、超小字、乱码、伪文字、模块间完全不同的设计语言。
