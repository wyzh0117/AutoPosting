# AutoPosting 1.0.20

## 变更

保存微信公众号账号时，若 App Secret / 作者等字段含中文或其他非 ASCII 字符，macOS `security find-generic-password -w` 会把钥匙串里的密码打成 hex。写入后的回读校验把 hex 当成原文，于是报 `model credential was not saved in macOS keychain`。`MacOSKeychainCredentialStore.get()` 现在把这类 hex 解回 UTF-8，微信/小红书账号 JSON 可以完整 roundtrip。

## 边界

- 纯 ASCII 的 App ID / App Secret 本来就能保存；这次修的是带中文作者、或粘贴进了非 ASCII 字符的路径。
- 错误文案仍写 “model credential”，因为账号和模型 key 走同一把钥匙串。
- 重启服务后写作风格会丢，账号和模型 key 仍在钥匙串/SQLite。

## 验证

- unittest 312 passed。
- Offline self-check 10/10。
- 真实钥匙串：含「测试作者」的微信 JSON `set`/`get` 一致后删除诊断条目。
- `GET /api/status` version 1.0.20。

Source snapshot: `archives/autoposting-patch-1.0.20-update-20260911.tar.gz`.
