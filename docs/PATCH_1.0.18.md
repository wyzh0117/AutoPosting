# AutoPosting 1.0.18

## 变更

热点候选点「生成文章」时报 `文章结果缺少 title 或 body 字段`。根因是模型已经返回了合法 JSON，但字段对不上校验：中文键名（标题/正文）、包一层 `article`/`output`、把 `body` 写成段落数组、或用 `content` 代替 `body`。当前文章模型是 DashScope `qwen3.8-max`，默认会思考；思考过程或前后废话也会把 JSON 埋进正文里。

校验现在会认这些常见形状，拼出 `title`/`body`。JSON 解析会从前后文里抽出最后一个对象。空的 `content` 会回退到 `reasoning_content`；数组形态的 content parts 会拼成文本。`qwen3.8-*` 在 DashScope 上 `effort=none` 时额外发送 `enable_thinking: false`。文章 prompt 升到 `article-v11`，在数据块后再重复一次输出 schema，避免模型把输入的 `{title, summary, background}` 原样吐回来。

## 边界

- 不会把输入里的 `summary` 当成正文。
- 小红书手动发帖包未改。
- 旧缓存仍按 `article-v10` 的 key，不会命中；重新生成即可。

## 验证

- unittest 308 passed.
- Offline self-check 10/10.
- `GET /api/status` version 1.0.18.
- 对先前失败的热点「Discovery of a new OpenAI agent message board」走 `POST /api/articles`：返回中文标题和含 AIGC 的正文，不再报缺少 title/body。

Source snapshot: `archives/autoposting-patch-1.0.18-update-20260905.tar.gz`.
