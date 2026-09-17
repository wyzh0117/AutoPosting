# PATCH 1.4.3 — Skill 查找器列出结果并勾选出图

版本：`1.4.2` → `1.4.3`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 改了什么

1. **查找 / 加入之后列出全部 skill**：来源栏下方出现 `#skill-list`。点 skill 名字在新标签打开对应页面（GitHub 或 skills.sh）。
2. **勾选才出图**：列表每行有复选框，默认全选。未勾选的不进入「生成图片」：前端只排队勾选的海报，`POST /api/skills/render` 带 `selected_ids`，后端用 `apply_skill_selection` 过滤后再渲染。
3. 未勾选的 skill 仍留在列表里，方便下次改选再出图。一个都不勾时接口返回 `skill_none_selected`。不传 `selected_ids` 时仍渲染全部（兼容旧调用）。

## 边界

- 介绍文仍按查找 / 加入时的全集生成，不随勾选重写。
- 封面海报仍会生成；少的是未勾选 skill 的项目卡。
- 没有 URL 的 skill 显示名字但不做成链接。

## 验收

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests` → 613 通过，1 跳过。
- 合同测试 `test_patch_143_skill_selection_list`；API `test_render_honors_selected_ids` / `test_render_rejects_empty_selected_ids`。

源码快照：`archives/autoposting-patch-1.4.3-update-20260917.tar.gz`（同名 `.sha256`）。
