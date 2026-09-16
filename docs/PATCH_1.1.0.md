# AutoPosting 1.1.0

## 变更

没有选中封面时，「到草稿」不再直接 `wechat_cover_required`。本地用文章标题生成一张 1080×1080 红底大字报 PNG（复用现有 Chrome/Playwright 截图），再上传成微信永久素材。大字报失败才用设置里的备用 `thumb_media_id`；两者都没有才报 `wechat_cover_required`。

小红书导出封面卡在没有配图时同样改成红底标题大字报，不再用纸纹封面。

## 边界

- 有选中封面时仍上传该图，不生成大字报。
- 截图依赖本机 Chrome 或 Playwright；没有截图能力且没有备用 Media ID 时仍失败。
- 不调用图像模型，不改 `docs/image_prompts/`。
- 微信 API 仍然要求图文草稿有封面；本补丁只补本地封面。

## 验证

- unittest 316 passed。
- Offline self-check 10/10。
- 本机 Chrome 实渲 `没有封面时的标题大字报`：1080×1080 PNG，标题可读。
- `GET /api/status` version 1.1.0（重启服务后）。

Source snapshot: `archives/autoposting-patch-1.1.0-update-20260911.tar.gz`.
