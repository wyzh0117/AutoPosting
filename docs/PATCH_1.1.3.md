# AutoPosting 1.1.3

## 变更

Skill 查找器在真实网络下会串行打出成百上千次 GitHub / 网页请求（每个仓库最多探 6 次 `SKILL.md`，每阶段多组检索词，再加 topic 根检索）。经 Clash 代理时单次握手也能拖到数秒，界面会停在「正在查找」超过 9 分钟。

本次限制：

- 网络阶段最多 40 次抓取、45 秒截止；到期交已找到的结果和缺口。
- GitHub 单次超时 8 秒，每页 5 条。
- 每阶段第一轮只跑第一条检索词，补洞只再跑一轮。
- `SKILL.md` 只探 raw + contents 各一次。
- 去掉 `topic:claude-code-skill` / `topic:agent-skills` 预热。

## 边界

- 模型拆覆盖地图和写介绍文仍可能各占最多约 90 秒。
- 截止后未填满的阶段标 `gap`，不假装齐活。

## 验证

- unittest 331 passed（含 `test_network_budget_caps_http_instead_of_crawling_every_repo`）。
- 重启服务后 `GET /api/status` version 1.1.3。

Source snapshot: `archives/autoposting-patch-1.1.3-update-20260913.tar.gz`.
