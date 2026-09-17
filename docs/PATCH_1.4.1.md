# PATCH 1.4.1 — 海报文案链、热点文章保留、Google 相关检索、焦点栏、粘贴原文

版本：`1.4.0` → `1.4.1`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **海报文案链**：取消「给…时候」身份句闸；没有模型文案时兜底句带上项目名，避免同品类海报一字不差；剥掉 `**` / `#` / 列表符；prompt 示例不再把「一句话看懂」当字段值。
2. **热点候选**：点其他标题不再清空已生成文章，直到再次点「生成文章」。
3. **相关检索**：优先用 Google 新闻 RSS（不需要 API key），一次最多 8 条；Bing / DDG 只作空结果兜底。
4. **焦点栏**：固定在页面左下角悬浮，打开收藏夹不再挡住它。
5. **生成文章旁粘贴框**：可粘贴原文页面，生成时作为 `pasted_page` 进入文章提示。

## 边界

- Google 新闻 RSS 被墙或为空时，仍会试 Bing / DDG；这不是 Google 搜索页的完整搬家，也不使用付费 API。
- 粘贴原文上限 20000 字。
- 旧库 `purpose='image'` 行仍不删。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v` → 605 通过，1 跳过。
