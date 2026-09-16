# AutoPosting 1.0.2

## 变更

- 修复出图模型保存失败：探测请求不再发送千问不认的 `768*1024`。`qwen-image` / `qwen-image-plus` / `qwen-image-max` 自动落到合法 3:4 尺寸 `1104*1472`；若接口仍报 size 不合法，会按预设列表重试（含省略 size）。
- 周榜出图改为先读 `docs/image_prompts/` 下的 markdown，再据此生成：`STYLE.md`（系列风格）、`cover.md`（封面）、`poster.md`（单仓库海报）。占位符：`{style}` `{ranking}` `{name}` `{rank}` `{rank02}` `{summary}` `{does}` `{scene}` `{kind}`。
- 应用版本 `1.0.2`。

## 边界

- 无第三方运行时依赖。
- 保存出图模型仍会打一枪真实出图探测；请继续选 qwen-image / wan，不要选对话模型，并点「用于出图」。
- wan2.2 仍可用 `768*1024`；被拒时自动换合法尺寸。
- 缺 prompt 文件时周榜出图会失败，不会退回代码内写死的英文 prompt。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：259 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：`ok: true`，10/10 checks。
- 千问探测请求使用 `1104*1472`；`768*1024` 被拒时会重试。周榜 jobs 来自 markdown 模板。

Source snapshot: `archives/autoposting-patch-1.0.2-update-20260903.tar.gz`.
