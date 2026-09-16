# AutoPosting 1.0.12

## What changed

GitHub 周榜「抓取周榜」会卡住两分多钟，即使榜单模型选了 flash。实测 GitHub 搜索 + Trending 只要约 8 秒；剩下的时间全在榜单改写。

`deepseek-v4-flash` / `deepseek-v4-pro` 默认开思考，且 effort 为 high。设置里的「none」以前不会发给 DeepSeek，模型仍会先想很久再写 10 条仓库文案。

- effort 为 none 时，请求带 `thinking: {type: disabled}`。
- 榜单改写超过 25 秒则放弃，抓取仍返回仓库和中文 fallback 文案，不再空等。

## Boundaries

- 抓取和出图仍是分开的 API。
- 思考关闭只针对 `deepseek-v4*`。Qwen 3.8 仍用原来的 `reasoning_effort=none`。
- 设置里把 effort 调成 low/high/max 会重新打开 DeepSeek 思考。

## Verification

- unittest 291 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.12.

Source snapshot: `archives/autoposting-patch-1.0.12-update-20260905.tar.gz`.
