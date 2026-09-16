# AI Visual Publishing System

这套东西解决的问题：GitHub 周榜（以及以后的新闻、论文、工具）不要再靠「一张巨长 Prompt 让图像模型排中文海报」。

正确分工：

- LLM：理解、提炼、选页面、选组件、写文案
- Design System + Components + Page Schema：决定长什么样、由哪些积木组成
- Content Schema：这一页要展示什么（纯数据）
- Rendering Rules：数据和布局冲突时怎么退让
- HTML / CSS：精确排字、间距、对齐
- 浏览器截图：输出 9:16 PNG
- 图像模型（可选）：只生成主视觉 / 插画 / 图标，作为 asset 插入页面

`cover.md` / `poster.md` 仍给图像模型用，继续被 `{style}` 拼进周榜出图任务。它们不再承担整页中文排版。

## 五个核心层级

1. **Design System** — `design-system/`  
   品牌应该长什么样。色板、字体、圆角、间距。没有「第27期」。
2. **Component Library** — `components/` + `src/autoposting/visual/components.py`  
   可复用积木：BrandHeader、RankingItem、QuoteCard…
3. **Page Schema** — `page-schemas/`  
   某一种页面由哪些组件、如何排列。已实现 `ranking-overview`、`project-detail`。
4. **Content Schema** — `content-schemas/`  
   JSON Schema。`stars = 11000`，不写「右侧 32px 白字」。
5. **Rendering Rules** — `rendering/`  
   溢出、裁切、数字格式、最多条数。

## STYLE.md 如何被拆分

原文件完整保存在 `legacy/STYLE.original.md`。

活着的 `STYLE.md` 只保留**图像模型资产生成**需要的风格、画布、禁止项（仍被 `image_gen.py` 以 `{style}` 读入）。

其余内容按职责迁到五层，见 `STYLE_MIGRATION_REPORT.md`。没有整文件复制。

## 目录

```
docs/image_prompts/
  README.md
  STYLE.md                         # 图像模型 style bible
  cover.md / poster.md             # 仍是资产 Prompt，不是整页排版
  design-system/  DESIGN_SYSTEM.md  tokens.json
  components/     COMPONENTS.md     components.schema.json
  page-schemas/   ranking-overview.md  project-detail.md
  content-schemas/ ranking.schema.json project.schema.json
  rendering/      RENDERING_RULES.md  rules.json
  templates/      ranking-overview/   project-detail/
  examples/       ranking.example.json project.example.json
  legacy/         STYLE.original.md
  STYLE_MIGRATION_REPORT.md

src/autoposting/visual/            # stdlib 渲染器
```

代码放在现有包 `autoposting.visual` 下，不另起前端工程。运行时仍是 Python 3.11 标准库；截图优先 Playwright（若已安装），否则用本机 Chrome/Chromium headless。不把 Playwright 写进 `pyproject.toml` 依赖。

## 数据流

```
External Content
  → Content Agent（现有 GitHub weekly / 未来新闻 Agent）
  → Normalized Content
  → Content Schema Validation
  → Select Page Schema
  → Resolve Components
  → Apply Design System + Rendering Rules
  → HTML / CSS
  → Chrome / Playwright
  → PNG 1080×1920
```

GitHub 仓库字典可先经 `autoposting.visual.adapter.ranking_from_repos` / `project_from_repo` 进入 Content Schema。第一阶段未改工作台「抓取周榜」按钮，避免和工作台模型出图抢路径。

## 如何添加一个新组件

1. 在 `components/COMPONENTS.md` 写 name / purpose / fields。
2. 在 `components.py` 增加返回 HTML 的纯函数。
3. 在 `css.py` 用 token，不写 magic number。
4. 哪个 Page Schema 要用，就写进该 schema。

## 如何添加一种新页面

1. `page-schemas/<name>.md` 只写组件顺序。
2. `content-schemas/<name>.schema.json`。
3. `pages.py` 增加组装函数，并在 `schema.py` 的 `KIND_BY_PAGE` 登记。
4. `examples/<name>.example.json`。
5. 跑 `visual-render --input ...`。

## 如何添加一条新内容

写一份符合 schema 的 JSON（参考 `examples/`），不要把颜色或字号写进去。

```bash
PYTHONPATH=src python3 -m autoposting.cli visual-render \
  --input docs/image_prompts/examples/ranking.example.json \
  --out output/visual
```

## 如何渲染图片

```bash
PYTHONPATH=src python3 -m autoposting.cli visual-render --example --out output/visual
```

输出：

- `output/visual/ranking-overview/ranking-overview.html|png`
- `output/visual/project-detail/project-detail.html|png`

只要 HTML：加 `--no-screenshot`。

## 如何执行 Example

同上 `--example`。测试：`PYTHONPATH=src:tests python3 -m unittest test_visual -v`。

## 如何扩展新品牌

复制或改 `tokens.json` 的 color / typography / radius。不要改组件函数里的文案。BrandHeader 读 Content 的 `brand` 字段，换品牌只换数据。

## 如何未来接入 Image Model

1. 用 `STYLE.md` + 专用资产 prompt 生成主视觉 / logo / 背景 PNG。
2. 把路径写入 Content 的 `heroVisual` / `logo`。
3. 组件按 `object-fit` 规则插入。不要再让模型输出带大量中文的整页海报。

## 技术约束

与仓库其余部分相同：无强制第三方运行时依赖。截图是可选能力，缺浏览器时仍写出 HTML。
