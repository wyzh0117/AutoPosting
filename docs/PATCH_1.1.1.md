# AutoPosting 1.1.1

## 变更

1. 进度条不再盖住按钮。`#wait-banner` 从右上角 `position:fixed` 浮层改成标题栏下方的文档流提示；卡片本身不拦截点击，只有「停止」可点。加载时其它按钮仍可继续操作。
2. 「到草稿」分清两种封面：有已选原文配图时上传该图（封面优先，没有封面则用已选正文图），下载/上传失败也不改成大字报；没有已选配图时才生成本地标题大字报。顺带给图片请求补 Referer、放宽体积，并按实际格式命名；WebP/AVIF 尽量用本机 `sips` 转 JPEG。
3. 热点候选原文配图改为勾选。默认只勾选封面，正文图保持未选。封面和正文仍分开勾，正文勾选后可上移/下移。

## 边界

- 不调用微信 `freepublish`。已选配图全部失败时回退备用 `thumb_media_id`，没有备用才 `wechat_cover_required`。
- GitHub 周榜勾选逻辑未改。
- 重启服务会丢掉写作风格；模型接口和账号仍从 SQLite / 钥匙串恢复。

## 验证

- unittest 319 passed。
- Offline self-check 10/10。
- `GET /api/status` version 1.1.1（重启服务后）。
- 浏览器：抓取时进度条在标题栏下，不与「设置」「生成文章」重叠；加载中可打开设置。
- 浏览器：微信 AI 条生成后 4 张原图均为勾选框，仅封面勾选，正文 0/4。
- `POST /api/articles` 同一条：`cover_id` 有值，`body_ids` 为空。

Source snapshot: `archives/autoposting-patch-1.1.1-update-20260912.tar.gz`.
