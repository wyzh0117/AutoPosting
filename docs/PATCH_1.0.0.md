# AutoPosting 1.0.0

## 变更

- 小红书缺少 caption 仍生成。稿子可见字数 ≤500 则正文完整引用；超过 500 则保留模型合格 caption，否则自动提炼带 `- ` 的要点，并补上来源 / AIGC。
- 观点候选：原帖正文里的 GitHub 仓库链接会抓 README，写入 `background.linked_works`。经验分享把这些作品当正文材料拆解：这是什么、应用场景、如何使用、有什么亮点。
- GitHub AI 项目周榜 11 张图（1 封面 + 10 海报）改由出图模型生成。画面描述写成固定风格圣经（米白纸、炭黑、哑光黄铜、无霓虹），保证每周一致。
- 周榜图 3×4 网格平铺；可点大图、单独勾选；左上角全选；导出/导入小红书只含勾选图，顺序与网页一致。
- 设置 → 模型新增「出图模型」。文章 Prompt 版本 `article-v10`。应用版本 `1.0.0`。

## 边界

- 无第三方运行时依赖。出图走已配置提供方的 OpenAI 兼容 `/images/generations`。
- 未配置出图模型时周榜仍抓仓库和文案，格子显示「待出图」。
- 一键套用提供方不会自动把对话模型配到出图入口（出图模型需单独选）。
- 小红书仍是手动发布包。外链抓取失败则跳过该仓库，不阻断成稿。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：251 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 本地服务 `/api/status` 返回 `1.0.0`。页面：风格下拉仍是 AI专家/经验分享；设置有出图模型；周榜 3×4 画廊、全选、导出选中。

Source snapshot: `archives/autoposting-patch-1.0.0-update-20260830.tar.gz`.
