# AutoPosting 1.0.3

## 变更

- 出图模型保存不再打真实出图探测。点「用于出图」只写入所选模型，任意已接入模型都可以，包括对话模型；不再限定 qwen-image / wan。
- 真正出图时固定 9:16（默认 `928*1664`，被拒则换其他 9:16）。顺序：`/images/generations` → DashScope multimodal → 该模型的 `/chat/completions`（从返回里取图）。
- 周榜 prompt 与画廊格子改为 9:16。应用版本 `1.0.3`。

## 边界

- 无第三方运行时依赖。
- 对话模型必须能在 chat 响应里带回图片（data URL / 图片 URL / base64）。只回文字的对话模型保存会成功，出图仍会失败。
- 微信正文配图仍是原文 3:4，与周榜模型出图无关。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：261 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 保存出图模型不调用 generate。聊天模型走 `/chat/completions` 仍能出 PNG。请求尺寸为 9:16。

Source snapshot: `archives/autoposting-patch-1.0.3-update-20260903.tar.gz`.
