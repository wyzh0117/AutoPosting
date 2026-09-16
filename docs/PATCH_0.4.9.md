# AutoPosting 0.4.9

## 变更

- 设置 → 模型页按 DSH 布局重做：已保存接口以行显示（绿点=API Key 已保存），一次只展开一张编辑卡片。获取可用模型只列出候选、已配置项默认不勾选，点「添加所选」再「应用」才写入，不会覆盖已有模型列表。
- 添加自定义提供方与编辑共用同一卡片：API Key 只写不回显，自定义设置里填接口地址，卡片上仍可指定文章 / 翻译 / 对话模型。
- 账号配置写入 macOS 钥匙串，重启后自动恢复。对话框不再回填 AppID/Secret/Token，只显示是否已配置，并提供「删除配置」。
- 应用版本 `0.4.9`。

## 边界

- 写作风格仍只存在当前进程，重启后需重填。
- 密钥从不进入 SQLite、日志或 API 响应。
- 探测模型（`persist: false`）不会改已存列表。

## 验证

- 新增：探测不覆盖已存模型；微信配置跨 WebApp 重启恢复并可删除。
- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：225 项中 223 通过。新增设置页测试通过。失败 2 项为既有文章 Prompt 断言（`某某称`），与本补丁无关。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 服务重启后 `/api/status` 为 `0.4.9`。已有 DashScope 接口仍在，241 个模型未丢，绿点表示 Key 仍在。设置页账号表单为空，只显示未配置，不回填 Secret。
