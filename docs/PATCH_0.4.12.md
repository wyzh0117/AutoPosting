# AutoPosting 0.4.12

## 变更

- 热点栏新增第三栏「GitHub AI项目周榜」。点「抓取周榜」按本周星标热度取前 10 个 AI 仓库（先 GitHub Trending weekly，不足再用 Search `created:>=7d` 补齐）。
- 封面和每张仓库海报是固定 UI 模板：刷新只换数据。封面为大标题 GitHub +「本周爆火ai工具」+ 十行排名（图标、名字、类型、星标条、星数）。海报含名字、简介、logo、它能做什么、适合什么场景、如何实现。
- 浏览器 Canvas 导出 3:4 PNG（封面 + 10 张海报）。小红书文案标题固定「本周值得关注的github ai项目前10」，正文为每个仓库的名字、简介和地址。
- 设置 → 模型新增「榜单模型」。它只改写简介/能做什么/场景/如何实现四字段；项目名、星标、地址、类型和版式不走模型。未配置时用仓库描述填模板。
- 应用版本 `0.4.12`。

## 边界

- 无第三方运行时依赖。海报出图在浏览器本地完成，不调用生图模型。
- 未配置榜单模型时整条链路可以没有 AI。
- 小红书仍是复制文案 + 导出图片，不自动发布。
- GitHub 未认证请求受官方速率限制。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：235 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 本地服务 `/api/status` 返回 `0.4.12`。`POST /api/github-weekly` 实抓 10 个仓库。浏览器：第三栏「GitHub AI项目周榜」显示封面 GitHub / 本周爆火ai工具、10 行排名、10 张海报；小红书标题固定且正文含仓库地址；设置模型页有「榜单模型」。

Source snapshot: `archives/autoposting-patch-0.4.12-update-20260829.tar.gz`.
