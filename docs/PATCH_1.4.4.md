# PATCH 1.4.4 — 放大预览左右翻页、来源全选、海报模版二

版本：`1.4.3` → `1.4.4`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **放大预览翻页**：GitHub 周榜 / Skill 查找器 / 原文配图的列表不再放「上一张 / 下一张」按钮。翻页按钮只留在放大预览。放大后点照片左半边上一张、右半边下一张，Esc 退出。
2. **粘贴 skill 与来源 chip**：列表同时收录检索结果和手动粘贴；每行仍是勾选 + 可点开的链接。点「skills.sh 6」「GitHub 3」「手动粘贴 7」会全选或取消该来源。
3. **海报模版选择**：切到 GitHub 周榜或 Skill 查找器时，左侧展开模版一 / 模版二预览。切换只改背景和风格，文案模块、画布尺寸（1080×1440）不变。
4. **模版二**：封面按 `docs/image_prompts/templates/toc/skill_directory_10.html` 做成羊皮纸目录格；内容卡按 `toc_background_only.html` 套双线框和转角，五个文案模块仍在。

## 边界

- 海报正文复读（身份 + 情景 + 功能套话）这次**没有改**。排查结论见本轮回复：真正落版的是 `KIND_*` / `_skill_boards` / `project_from_repo`，不是 `copy.md`。
- 模版一切到模版二不会改 `summary` / `does` / `how` / `audience` / `scene`。
- 未点「生成图片」前，切换模版只影响下一次出图。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 619 通过，1 跳过。
- 合同测试 `test_patch_144_*`；视觉 `PosterTemplateSkinTests`。

源码快照：`archives/autoposting-patch-1.4.4-update-20260917.tar.gz`（同名 `.sha256`）。
