# AutoPosting 0.4.11

## 变更

- `docs/article_styles/` 中 1–8 号文件改为具名：`消息.md`、`通讯.md`、`新闻特写.md`、`新闻专访.md`、`调查报道.md`、`深度报道.md`、`评论述评.md`、`AI专家.md`。下拉值和 API 仍用 `1`–`8`，避免已存草稿失效。
- 八类风格 prompt 按「成品读感」重写，去掉「按 A—B—C 展开」的栏目式提纲；统一要求不要把写作提纲暴露给读者，禁止「首先/其次/最后」「值得注意的是」「这意味着」「综上所述」以及「导语：/背景：/影响：」式衔接。
- `docs/ARTICLE_BASE_PROMPT.md` 同步去掉「按类型规定的顺序展开」；微信/小红书呈现改为连贯成篇，来源与 AIGC 收在文末但不做成栏目标题。文章 Prompt 版本 `article-v8`。
- 不再把「某某称」当作必写句式；来源说法改为点明谁在什么场合说了什么。
- 应用版本 `0.4.11`。

## 边界

- 「经验分享」未改。设置页写作风格仍是最高优先级覆盖层。
- 事实边界、来源 ID、AIGC、JSON 输出校验未改。
- 未对现有候选做八类实模批量生成；用户将直接查阅并修改这八份 prompt。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：225 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 加载器把 `1`–`8` 映射到具名文件；八类均含「不要把写作提纲暴露给读者」；「一般人容易看漏的角度」仍只出现在第 8 类。
- 本地服务 `/api/status` 返回 `0.4.11`。页面徽章 `v0.4.11 · 本地服务`；风格下拉仍为 1–8 具名选项，默认「8 AI专家」，可切换到「1 消息」。

Source snapshot: `archives/autoposting-patch-0.4.11-update-20260828.tar.gz`.
