# AutoPosting 1.0.5

## 变更

- 周榜出图不再在「抓取周榜」时连打图像模型。抓取只拉仓库；「生成图片」单独一步。
- 生成走 HTML 视觉出版（浅色/暗色 + 主色/副色/强调色），截 1080×1920 PNG。这是反复换模型仍失败的根因修复：整页中文不该由图像模型排。
- `POST /api/github-weekly/render` 接收 `theme` 与 `palette`。

## 边界

- 仍无第三方运行时依赖。截图需要本机 Chrome 或 Playwright。
- 出图模型配置保留，但周榜整页不再调用它。

## 验证

- unittest 全量。
- `self-check` 10/10。
- 工作台 GitHub 栏可见「抓取周榜」「生成图片」、浅色/暗色与三色盘。

Source snapshot: `archives/autoposting-patch-1.0.5-update-20260904.tar.gz`.
