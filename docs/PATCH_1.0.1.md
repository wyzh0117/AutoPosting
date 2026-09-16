# AutoPosting 1.0.1

## 变更

- 修复出图模型配置后无法出图：通义千问 `qwen-image*` 在 `/images/generations` 失败时，回退到 DashScope multimodal 出图接口；HTTP 错误把接口原文带回前端。选好后必须点「用于出图」才会写入设置。
- 周榜改为优先本周新建（GitHub Search `created:>=7 天`，按星排序），不够再用 Trending weekly 的本周涨星补齐。列表标明「本周新建」或「本周涨星」。
- 应用版本 `1.0.1`。

## 边界

- 无第三方运行时依赖。
- 未点「用于出图」保存时仍会提示去设置页，不会静默跳过。
- 本周新建不足 10 个时，补位仍可能是老仓库，只是会标「本周涨星」。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：255 项通过。
- 出图：兼容接口失败后走 multimodal；错误信息包含 API 原文。
- 周榜：新建仓库排在涨星补位之前。

Source snapshot: `archives/autoposting-patch-1.0.1-update-20260831.tar.gz`.
