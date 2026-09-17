# Rendering Rules

回答：真实内容和理想布局冲突时怎么办？

机器可读副本：`rules.json`。实现：`src/autoposting/visual/rules.py` + CSS overflow。

## 画布

固定 1080×1920，`overflow: hidden`。禁止把重要信息裁出安全边距。

## 文字

| 场景 | 优先 | 禁止 |
|---|---|---|
| 标题过长 | 1. 换行 2. 容器限高 3. 省略 | 压到 `minFontPx` 以下 |
| 副标题 / 项目名 / 介绍 | 单行 ellipsis | 伪文字、随意删词 |
| 一句话看懂 | 最多 3 行 clamp，数据侧先裁到 72 字 | 为装饰牺牲正文 |
| 中文 | 完整、自然、无乱码 | 交给图像模型重绘正文（已废弃：不再有图像模型调用点） |

## 列表与密度

- 榜单最多 10 条；能力 5；人群 4；工作流 5；标签 6。
- 条目 ≥ 8 时使用 compact 密度（均分剩余高度，不压缩字号）。
- 内容过多：先去装饰，再压缩辅助说明，再缩短描述，最后挤间距。禁止牺牲核心正文。

## 数字与 URL

- `11000` → `11k`（与周榜 `format_stars` 同一数量级习惯）。
- 进度条相对本页最大值，最小可视宽度 8%。
- 长 URL 去掉协议后按 36 字裁切，CSS 再 ellipsis。

## 图片

- 主视觉 `object-fit: cover`。
- Logo `contain`，缺省为首字母块。
- 不拉伸破坏品牌标。

## 视觉优先级

title → keyMessage → rank → metric → features → url → decoration

页面溢出时从优先级尾部删除。
