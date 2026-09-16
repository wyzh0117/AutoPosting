# AutoPosting 1.1.2

## 变更

1. GitHub 周榜封面改为 3:4（1080×1440）。`.gh-cover` CSS、`ranking-overview` 画布和 `cover.md` 封面任务对齐 3:4。项目海报 `.gh-poster`、小红书纸卡仍 9:16；`IMAGE_ASPECT_RATIO` 仍 9:16。设置文案写明：周榜封面 3:4；项目卡与小红书纸卡仍为当前比例；图像模型默认仍 9:16。
2. 第四栏目「Skill查找器」。输入关键词后按 `docs/skill_finder/COVERAGE.md` 拆 6–12 个阶段，检索 GitHub / 网页上的 `SKILL.md`，质量门槛过滤，写入 `output/skills/<hash>/`，再用 `PACKAGE_ARTICLE.md` 写总览文。封面是 walnut `#3d2b1f` 白框 Songti 大字报（1080×1440），不走图像模型。项目卡复用周榜视觉。
3. 四个栏目和微信/小红书切到页顶 `<nav id="app-nav">`，进度条仍在导航下方文档流里。`setLane('skills')` 隐藏新闻控件，显示搜索框、阶段条、skill 列表、介绍文和 3 列 3:4 画廊。

## API

- `POST /api/skills/search` `{"query": str}`（1–200 字）→ `{ok, query, title, theme, stages, skills, article, package_path, images}`
- `POST /api/skills/render` `{theme?, palette?}` 生成封面和项目卡
- `GET /api/skills` 上次结果
- 空查询 400；文章模型未配置 502 `skill_search_failed`

## 边界

- 单元测试不打真实网络；不现场检索「AI制作短剧工作流」。
- 封面大字报用 Chrome/Playwright 截图，与标题大字报同一条路。
- 控制器负责验收和打包 tarball；本补丁不重启 8080。

## 验证

- unittest 330 passed（控制器复跑）。
- Offline self-check 10/10。
- `GET /api/status` version 1.1.2（重启服务后）。
- 浏览器：顶栏四个栏目 + 微信/小红书；`main` 仍左右两栏；`.gh-cover` 计算为 3/4，`.gh-poster` 为 9/16。
- 浏览器：Skill查找器显示搜索框「查找」，新闻时间范围隐藏，左右分栏仍在。
- 封面大字报 prompt：`docs/image_prompts/skill_cover.md`。

Source snapshot: `archives/autoposting-patch-1.1.2-update-20260912.tar.gz`.
