# AutoPosting 1.2.3

## 变更

1. 热点候选左栏右下增加 `#picked-dock`：展示真正会送去「生成文章」的标题（点标题多选为「已选 N」芯片；仅点卡片时显示「焦点」）。芯片可点取消。GitHub 周榜 / Skill查找器隐藏。
2. 修左右同时选取时的高亮不同步：卡片 `.selected` 只保留左侧细条（打开原文用的焦点），点标题的 `.is-picked` 才是生成用的多选。再点已焦点卡片会取消焦点。收藏夹有多选时，左栏只点过卡片、没点标题的篇目不会进融合稿，高亮也不再像「选中」。

## 边界

- 重启仍会丢掉写作风格。
- 微信 live 仍停在 draft/add。

## 验证

- unittest 363 passed。
- 重启后 `GET /api/status` version 1.2.3。
- 浏览器：徽章 `v1.2.3 · 本地服务`。点左栏标题 → 已选栏「已选 1」且 `.is-picked`。开收藏再点抽屉标题 → 「已选 2」。再点左栏标题 → 左栏 `.is-picked` 消失，已选栏只剩收藏那一篇。

Source snapshot: `archives/autoposting-patch-1.2.3-update-20260914.tar.gz`.
