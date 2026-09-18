# PATCH 1.4.8 — Skill 短名对齐、出图上限 10、海报框内自适应

版本：`1.4.7` → `1.4.8`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **榜单改写宽松对齐**：LLM 返回完整 `full_name` 或唯一的 skill 短名都可以对上输入条目。短名在本批输入里不唯一时仍要求完整名。未改 KIND 模板、未加静默补写。
2. **备选仍 16、出图最多 10**：查找/粘贴上限仍是 `SKILL_CAP = 16`。点「生成图片」时勾选超过 10 条，前端和 `POST /api/skills/render` 都提示「超过条目上限」。只重出封面不走这条上限。
3. **海报短名、介绍文要有链接**：项目卡标题用 skill 短名即可。介绍文把短名做成可跳转链接（已包在链接里的名字不再拆第二次）；列表里的名字链接仍在。不再因为图片或正文里没有 `owner/repo/skill` 整段而失败。
4. **海报文本留在自己的框里**：内容卡按框高缩小字号（列表字号跟面板走），长文不再画进隔壁栏或页脚。没有新的文案校验或 KIND 兜底。

## 边界

- 周榜 enrich 仍走同一套对齐：唯一短名可匹配，重名仍要完整 `full_name`。
- 未配置榜单模型仍立即 `LLM_ENRICHMENT_FAILED: model not configured`。
- 不合并到 `main`、不推 GitHub。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 640 通过，1 跳过。
- 用原先溢出的 `byted-mediakit-video` / `video-edit` / `reddit-automation` 文案重绘模版二海报：四块正文留在各自框内，页脚不被盖住。

源码快照：`archives/autoposting-patch-1.4.8-update-20260918.tar.gz`（同名 `.sha256`）。
