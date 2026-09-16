# AutoPosting 0.4.13

## 变更

- 写作风格只留「AI专家」和「经验分享」。删除其余 6 个 `docs/article_styles/` 文件；下拉框和 API 取值改为 `AI专家` / `经验分享`（不再用 `8`）。热点栏默认 AI专家，观点栏仍自动切到经验分享。
- 一次点击只生成一版。微信仍是 `{title, body}`。小红书同一次调用改为 `{title, body, caption}`：`body` 是完整稿，只排进图片；`caption` 是帖子正文（先说清事情，再用 `- ` 列要点，结构完整，文末来源 / AIGC / 标签）。
- 小红书成稿区显示 9:16 手写纸插图，每行 3 张。每张最上是 id/头像占位；封面=标题+所选封面图，没有封面图则标题+正文第一段；后续页分页放入完整稿。文字区输出 caption。可导出 PNG。
- GitHub 周榜封面/海报改为 9:16（1080×1920）。
- 文章 Prompt 版本 `article-v9`。应用版本 `0.4.13`。

## 边界

- 无第三方运行时依赖。插图在浏览器 Canvas 出图，不调用生图模型。
- 头像和小红书号先占位（`@小红书号`），账号设置以后再补上传。
- 小红书仍是手动发布包，不自动发帖。
- 微信原文配图嵌入比例未改；本次 9:16 只约束模板出图（周榜与小红书插图）。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：243 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 本地服务 `/api/status` 返回 `0.4.13`。浏览器：风格下拉只剩 AI专家 / 经验分享；小红书生成后 3 列 9:16 米白纸卡片，最上 `@小红书号`，封面标题+第一段，文字区为开场加 `- ` 要点；切回微信清空成稿；GitHub 周榜封面/海报 `aspect-ratio: 9 / 16`。

Source snapshot: `archives/autoposting-patch-0.4.13-update-20260829.tar.gz`.
