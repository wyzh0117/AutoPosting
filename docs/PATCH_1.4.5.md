# PATCH 1.4.5 — 海报文案 Prompt 优先、失败显式报错

版本：`1.4.4` → `1.4.5`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **GitHub 周榜 / Skill 查找器海报正文**：LLM 按 `copy.md` / `copy_skill.md` 生成的字段是唯一正文来源。抓取和 `skills_as_repos()` 只保留事实数据，不再预填 `KIND_*` / `_skill_boards` / `fallback_copy`。
2. **格式校验**：`copy_validate.py` 只检查必填字段是否存在、是否为非空字符串、是否覆盖全部项目。不再因英文、「它」开头、像套话而改写或丢弃模型输出。
3. **失败显式报错**：模型未配置、调用失败、JSON 无法解析、缺字段时抛 `llm_enrichment_failed`（`LLM_ENRICHMENT_FAILED: …`），周榜刷新/出图和 skill 出图不再吞掉 `LLMError` 后继续用模板文案出图。
4. **adapter / pages**：`project_from_repo` 只做字段拆行和 markdown 剥离，不再补类别模板。版式、五个文案模块、1080×1440 画布不变。
5. **观点候选**：已配置模型时，`extract_hook_candidates` 解析失败会报 `LLM_HOOK_FAILED`，不再返回空列表假装没有切口。未配置模型仍跳过这一可选步骤。热点文章生成原本就是失败即抛错，未再加模板正文。

## 边界

- Skill 查找/粘贴仍可在没有文章模型时给出机械介绍（`_fallback_article`），这不是海报正文。
- 翻译仍按条目记录成功/失败，不中断整份热点列表。
- 检索源的 RSS/Bing/DDG 传输回退未改。
- 未配置榜单模型时可以抓周榜事实，但不能生成海报；`POST /api/github-weekly/render` 会 502。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests`
- 新增 `tests/test_copy_pipeline.py`；合同测试 `test_github_weekly_render_without_enrichment_returns_error`。

源码快照：`archives/autoposting-patch-1.4.5-update-20260918.tar.gz`（同名 `.sha256`）。
