# AutoPosting 0.4.8

## 变更

- 热点标题翻译不再因为某一条摘要不合格而整批作废。
- 根因：HN 等源的摘要经常是空字符串或占位词 `Comments`，旧逻辑要求每条 `summary_zh` 都必须含中文，一条不过就返回 `llm_translation_invalid` /「翻译结果字段缺失或不含中文」，所有标题都不显示。
- 现在按条验收：标题含中文即保留；空摘要或 `Comments` 等占位允许 `summary_zh` 为空或非中文；多余 item_id 忽略，缺失条目只让该条失败。
- 应用版本 `0.4.8`。

## 边界

- 标题仍无中文的条目会单独失败，不再拖垮同批其它条目。
- 不重试模型、不轮询。重启服务后内存中的微信凭据仍会丢失。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：223 项中 221 通过。新增翻译批次测试全部通过。失败 2 项为既有文章 Prompt 断言（`某某称`），与本补丁无关。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 服务重启后 `/api/status` 为 `0.4.8`。`GET /api/hotspots?translate=1` 10 条英文候选全部 `translated`，含摘要为 `Comments` 或空的 HN 条目。
- 浏览器热点列表显示中文标题，不再出现「翻译结果字段缺失或不含中文」。
