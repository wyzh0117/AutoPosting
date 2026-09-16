# AutoPosting 0.4.6

## 变更

- 左栏增加「观点候选」页签，与「热点候选」并列；右侧生成流程共用。
- 新增 `type: reddit` 源，默认六个板块：ChatGPT、ClaudeAI、LocalLLaMA、PromptEngineering、OpenAI、ChatGPTPromptGenius。优先公开 `hot.json` + 热评 JSON；当前 IP 若被 Reddit 拦截 JSON（403），自动改拉板块 `.rss`，评论接口不通则只保留帖正文。
- 每帖收录正文 selftext 与（JSON 通道下）热评前 5 条（跳过楼主重复、已删）。评论拉取失败仍保留帖子。
- 观点页签刷新只拉 Reddit；时间范围按发帖时间；热度/时间排序沿用现有控件。RSS 订阅健康不列出 reddit 源。
- 打开 Reddit 原文不再抓板块 HTML，使用已收录正文+热评。
- 新增 `docs/article_styles/经验分享.md`，生成栏选项值 `经验分享`。切到观点候选时自动选中，切回热点时恢复原先 1–8。
- 应用版本 `0.4.6`。已 `graft init` / `graft build`。

## 边界

- 不申请 Reddit OAuth，限流时该板块记失败，其它板块继续。
- 不把 Reddit 帖混进新闻热度榜。
- 经验分享不得冒充亲历；材料不够则降级，不编未出现的步骤。
- 未改变微信/小红书发布安全边界。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：216 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check-viewpoints`：ok。
- 夹具覆盖 stickied 过滤、热评跳过 OP/[deleted]、评论超时仍入库、热点/观点分栏。
