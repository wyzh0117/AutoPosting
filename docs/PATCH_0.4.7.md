# AutoPosting 0.4.7

## 变更

- 微信 live 不再调用 `freepublish/submit`，只把文章写入公众号草稿箱（`draft/add`）。工作台主按钮由「发布」改为「到草稿」，定时按钮改为「定时到草稿」。小红书仍是本地包 + 人工提交。
- 封面改为上传永久素材（`material/add_material?type=image`），不再使用临时 `media/upload?type=thumb`。正文插图仍走 `media/uploadimg` 换微信 URL。
- 设置页 `thumb_media_id` 改为可选备用封面。每篇文章用当前选中封面自动上传，不必再手工填 Media ID。没有封面且没有备用 ID 时返回 `wechat_cover_required`。
- 微信配置只要求 `app_id` 与 `app_secret`。凭据仍只存进程内存。
- 应用版本 `0.4.7`。

## 边界

- 个人号/未认证号本来就不能调发布接口；本补丁按草稿箱权限对齐，不恢复直接发表。
- 不轮询草稿审核，不把永久素材 ID 展示在公众号管理后台（后台只能看到图片，看不到 `media_id` 字符串）。
- 重启服务仍会丢掉内存中的微信凭据。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：219 项中 217 通过。新增的微信草稿箱/永久封面测试通过。失败 2 项为既有文章 Prompt 断言（`某某称` 未进入组装 Prompt），与本补丁无关。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 服务重启后 `/api/status` 返回 `0.4.7`。
- 浏览器：微信默认按钮为「到草稿」「定时到草稿」；切到小红书后变为「发布」「定时发布」。设置页封面 ID 为可选，并有「一般不用填」说明。
