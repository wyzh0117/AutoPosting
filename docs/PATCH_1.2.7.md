# AutoPosting 1.2.7

## 变更

- 将 HTML 视觉模板系统升级为深蓝、斜切几何、线框与留白语言；不复制参考模板静态文案。
- `smart` 映射为 3:4 封面骨架；`page_04` / `intelligent` 映射为两套 4:3 poster 骨架，并按实际 repo rank 稳定交替。
- 保留热点候选与 Skill 查找器的动态内容、job/schema/id、文件名、URL、渐进 `on_image` 与原有渲染函数入口。
- 修复页面截图尺寸透传：封面为 1080×1440，poster 为 1440×1080；gallery 按 image kind 使用对应画幅，灯箱使用 `contain`。
- 封面只视觉呈现前四条榜单摘要，完整榜单仍保留在数据 payload；指标统一为白色/蓝灰色。
- 增加 poster-B 左侧媒体框、右侧线性信息区、右上 chevron；长内容使用 clamp，避免底框裁切。

## 边界

- 未改变 `/api/generated/...` 路径、业务字段、图片 id、文件名或 URL 契约。
- 旧 ranking/project 组件仍作为非 weekly fallback 保留；weekly cover/poster 使用新 variant 骨架。
- 未修改模型图片 prompt 文案或引入运行时依赖。

## 验证

- 全量测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`，400/400 通过（86.989s）。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`，`ok: true`；10 项检查全部通过。
- 服务版本：重启本地服务后 `GET http://127.0.0.1:8080/api/status` 返回 `version: 1.2.7`。
- Python 编译检查：相关渲染、封面、Skill、web、llm 模块通过 `py_compile`。
- 真实 PNG 截图（Chrome-backed `screenshot_html`）已生成并核验：
  - `/tmp/autoposting-audit-v3/cover.png`，1080×1440。
  - `/tmp/autoposting-audit-v3/poster-a.png`，1440×1080。
  - `/tmp/autoposting-audit-v3/poster-b.png`，1440×1080。
  - `/tmp/autoposting-audit-v3/long-poster.png`，1440×1080，长文案无溢出且保留 chevron。
- 独立浏览器验收：使用隔离临时数据库/包目录启动 `127.0.0.1:18080`，`GET /api/status` 返回 HTTP 200、`version: 1.2.7`；Chrome headless 首页显示 `v1.2.7`，CDP `Runtime.exceptionThrown` 为空，无 `Uncaught`、`SyntaxError`、`TypeError` 或 `ReferenceError`。仅有预期 favicon 资源 404，临时服务已停止。
- 本次隔离服务未触碰仓库 `data/`、`output/`；空库没有 gallery 实例，因此 gallery 行为由真实 PNG 截图与定向 gallery 测试验证。
