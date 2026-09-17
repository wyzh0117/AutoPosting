# 3:4 字号样例

项目详情页画布统一 3:4（1080×1440），字号需要比 4:3 / 16:9 更大。下面四档只改 typography 倍率，**还没有写进全局 tokens**。看 PNG 后告诉我用哪一档，再统一改项目。

| 档 | 倍率 | 含义 |
|---|---|---|
| scale-a-current | 1.00 | 现在线上用的字号 |
| scale-b-medium | 1.18 | 中号 |
| scale-c-large | 1.32 | 大号 |
| scale-d-xlarge | 1.48 | 特大号 |

渲染结果（不进 git 归档）：`output/visual/type-scales/<档>/project-detail.png`

```bash
PYTHONPATH=src python3 -c "from pathlib import Path; from autoposting.visual.pipeline import render_type_scale_examples; render_type_scale_examples(Path('output/visual/type-scales'))"
```
