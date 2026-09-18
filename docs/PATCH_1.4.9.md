# PATCH 1.4.9 — 写作风格 klinkenborg_style

版本：`1.4.8` → `1.4.9`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

热点候选 / 观点候选共用的「生成风格」下拉增加 `klinkenborg_style`。对应文案在
`docs/article_styles/klinkenborg.md`（由 `klinkenborg-sentence-craft/SKILL.md` 迁入并改名）。
生成文章时 `prompt_style: klinkenborg_style` 会把该文件整份送进文章模型。默认仍是 AI专家；切到观点候选仍自动选经验分享。

## 边界

- 设置页「写作风格」仍是最高优先级自由文本覆盖层，与下拉预设是两件事。
- GitHub 周榜 / Skill 查找器不显示该下拉。
- 未提交、未合并 main、未推 GitHub。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 640 通过，1 跳过。
- 浏览器：热点候选与观点候选的生成风格下拉都有 `klinkenborg_style`，可选中。

源码快照：`archives/autoposting-patch-1.4.9-update-20260918.tar.gz`（同名 `.sha256`）。
