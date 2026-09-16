# AutoPosting 1.0.6

## 变更

- 详情页：HeroVisual 是中部功能示意图，不是 repo logo。Logo 在右上；「一句话看懂」在副标题下；无 logo 用 3D 圆角 SVG；无模型图用 SVG 流程示意。
- 核心能力每条先 SVG 图标再文字。
- 四档 9:16 字号样例（未统一 tokens）：`docs/image_prompts/examples/type-scales/`，PNG 在 `output/visual/type-scales/`。
- 周榜放大预览：同源 `/api/generated/` 不再走 image-proxy（原先会 400，所以大图空白）。
- 已配置出图模型时，详情功能图会尝试让模型生成；失败则保留 SVG 示意。

## 验证

- unittest 277 项通过。
- 新详情 example PNG 中部为流程示意，右上为 3D 图标。

Source snapshot: `archives/autoposting-patch-1.0.6-update-20260904.tar.gz`.
