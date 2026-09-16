# AutoPosting 1.2.1

## 变更

Skill 查找器的介绍文（文本总结）在 1.2.0 流式查找里消失：轮询把 `skills_ready` 当成结束，此时 `write_article` 还没写完；随后自动出图用空 `article` 快照把数据库里的介绍文盖掉；右侧 3:4 画廊又把 `#skills-article` 顶到折叠线以下。

本次修复：

- 介绍文模块放到画廊上方，空态显示「查找完成后会生成介绍文」。
- 查找轮询等到 `article.body` 才结束；`skills_ready` 仍可先出图。
- 出图保存时合并最新介绍文，不再用空正文覆盖。
- `GET /api/skills` 若库里介绍文为空，从包装目录 `article.md` / `index.json` 恢复并写回。

## 边界

- 重启仍会丢掉写作风格。
- 查找的模型拆覆盖地图和写介绍文仍可能各占最多约 90 秒。
- 微信 live 仍停在 draft/add。

## 验证

- unittest 345 passed（含介绍文位置、出图不覆盖正文、从包装恢复）。
- 重启后 `GET /api/status` version 1.2.1。
- 浏览器：徽章 `v1.2.1 · 本地服务`；Skill查找器右栏 `#skills-article` 在画廊上方（top 207 vs 1375），恢复正文 1160 字，含 watercolor-sketch-style。

Source snapshot: `archives/autoposting-patch-1.2.1-update-20260914.tar.gz`.
