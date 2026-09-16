# AutoPosting 1.2.9

## 变更

- GitHub 周榜海报默认主色/副色/强调色同步当前 HTML 模板：`#06155A` / `#101F63` / `#F3F0E8`；palette 真实作用于渲染；封面恢复完整 10 个 repo。
- Skill Finder 的图片生成全面复用 GitHub 周榜的视觉、3:4（1080×1440）尺寸与颜色选择/海报工作流，文本生成逻辑保持独立。
- 热点候选相关检索改为直接检索用户选中的词条或内容：移除旧 LLM gate，使用确定性 ASCII 压缩 fallback，经 Google News/JXA 安全传输，spawn 硬限 14 秒，并过滤首篇合格报道。
- 新闻补充标题点击后进入焦点；焦点重复点击保持稳定选中，兼容收藏夹打开，不再限制 4 篇。
- 版本同步至 1.2.9（包版本、运行时版本、状态徽标、HTTP User-Agent 与测试断言）。

## 边界

- 未修改文本生成逻辑；Skill Finder 与周榜仅共享图片生成工作流。
- 未改变 XHS 手动打包约束；未新增运行时依赖或改变既有 API 契约。

## 验证

- 全量测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`，413/413 通过。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`，通过。
- 服务版本：1.2.9。
- 浏览器验收：GitHub 周榜 11 图/10 repo/颜色生效；Skill Finder 1080×1440；焦点 5 篇；重复点击、收藏夹、`newsFocus` 均正常。
- `127.0.0.1:8080` exact API：10.9 秒返回 HTTP 200，首篇为 TechCrunch。
