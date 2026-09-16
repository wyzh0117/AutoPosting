# AutoPosting 1.2.0

## 变更

### Skill 查找器

1. 右侧图片模块在进入栏目时就可见（空态 / 排队砖），不再等第二次「生成图片」才出现画廊。查找完成后自动出图；按钮保留为重试。
2. 查找边运行边吐结果：先增量展示 skill，全部 skill 展示完毕后再生成并展示图片。`POST /api/skills/search` 立即返回，前端轮询 `GET /api/skills`（`phase`: searching / skills_ready / rendering / done）。
3. 相近二次查找不再因 GitHub 403/429/超时被当成「没有 skill」。进程内缓存 GitHub 响应（15 分钟），复用上一包仍匹配新阶段的 `source_id`；限流不是质量门失败。
4. 缺口阶段/缺口 skill 完全不输出：侧栏芯片、列表、图片、介绍文都不出现。`docs/skill_finder/PACKAGE_ARTICLE.md` 去掉「缺口」节。

### 热点候选

1. 顶栏「设置」旁新增「收藏」。
2. 候选可一键加星，SQLite 持久化（`hotspot_favorites`）。
3. 点击原文标题多选，高亮 `.is-picked`。
4. 「收藏」打开侧边栏，主栏/顶栏随抽屉宽度让位。
5. 多选可同时包含左栏候选和收藏抽屉。
6. 多选生成不改文章 prompt。新脚本 `article_fuse.py` 把多篇真实稿融成一篇，再走现有 `build_article_prompt`。

## 边界

- 重启服务仍会丢掉写作风格。
- Skill 查找的模型拆覆盖地图和写介绍文仍可能各占最多约 90 秒；网络阶段仍有抓取预算。
- 融合路径不按篇抓原站正文，避免一篇摘录盖住拼接稿。
- 微信 live 仍停在 draft/add。

## 验证

- unittest 342 passed。
- 重启后 `GET /api/status` version 1.2.0。
- 浏览器：Skill 查找器画廊空态可见；热点「收藏」按钮、抽屉让位、标题多选高亮、星标存在。

Source snapshot: `archives/autoposting-patch-1.2.0-update-20260914.tar.gz`.
