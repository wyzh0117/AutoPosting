# AutoPosting 1.2.4

## 变更

1. 已选栏改为页面右下角固定卡片（`position:fixed`），滚动时跟着视口；收藏抽屉打开时左移避开抽屉。GitHub 周榜 / Skill查找器仍隐藏。
2. 收藏夹每条增加「打开原文」，不依赖左栏焦点；点标题仍是多选。

## 边界

- 重启仍会丢掉写作风格。
- 微信 live 仍停在 draft/add。

## 验证

- unittest 366 passed。
- 重启后 `GET /api/status` version 1.2.4。
- 浏览器：`#picked-dock` `position:fixed; bottom:24px; right:24px`；收藏四篇全选后已选栏「已选 4」；生成标题「前沿AI公司呼吁减速，白宫顾问反问：谁在定义规则？」；封面 1 张、正文图 0。到草稿返回 `wechat_api_error:40164`（IP 白名单），本地稿已审批。

Source snapshot: `archives/autoposting-patch-1.2.4-update-20260914.tar.gz`.
