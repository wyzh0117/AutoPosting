# AutoPosting 1.0.4

## 变更

- 从 `docs/image_prompts/STYLE.md` 拆出可扩展的 AI 视觉出版五层：Design System、Component Library、Page Schema、Content Schema、Rendering Rules。原文完整备份在 `legacy/STYLE.original.md`。
- `STYLE.md` 现为图像模型资产生成规范（仍被 `{style}` 读入）。页面结构、字段、排版冲突规则不再堆在一个 Prompt 里。
- 新增 stdlib 渲染器 `src/autoposting/visual/`：内容 JSON → HTML/CSS（token）→ 本机 Chrome 或可选 Playwright → 1080×1920 PNG。
- 第一阶段两个可运行页面：`ranking-overview`、`project-detail`。CLI：`visual-render --example`。
- 工作台 GitHub 周榜 11 张模型出图入口未改；adapter 已能把仓库字典映射到 Content Schema。

## 边界

- 无第三方运行时依赖。Playwright 不写入 `pyproject.toml`。
- 图像模型仍不负责整页中文排字；缺 `heroVisual` 时用抽象光斑占位。
- 未把 HTML 渲染接到「抓取周榜」按钮，避免与现有模型出图抢路径。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
- `visual-render --example` 产出 1080×1920 PNG，中文完整、未越界。

Source snapshot: `archives/autoposting-patch-1.0.4-update-20260903.tar.gz`.
