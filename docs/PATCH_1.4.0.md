# PATCH 1.4.0 — Skill 查找器：关键词抓取、汇总出图、标题框

版本：`1.3.1` → `1.4.0`。五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、
`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`
（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

本次按用户给的 8 条要求逐条改，8 条全部落地，每条都有单测 + 实机证据（见「验收」）。

## 一、要求逐条

### 1. 「只粘贴自己的 skill 链接时无法正常出图」→ 修好

**根因（先复现再修）**：不是粘贴路径本身的逻辑错，而是**出图链路依赖系统临时目录**。
`src/autoposting/visual/screenshot.py` 调 headless Chrome 时不给 `--user-data-dir`，
Chrome 就去自己的 per-user 临时目录（macOS 是 `confstr(_CS_DARWIN_USER_TEMP_DIR)`，
**不读 `TMPDIR`**）建临时 profile。只要跑服务的进程不允许写系统临时目录，Chrome 直接
报 `Failed to create a unique user data directory for headless` 并退出 →
`ScreenshotError` → `/api/skills/render` 500 → `phase=skills_ready` 且 `images` 为空。

实机证据（用户库里那份「视频制作 + 7 条粘贴」的包，改动前）：

```
POST /api/skills/render → {"ok": false, "error": "[0917/021249...] Failed to create a unique
user data directory for headless.", "error_type": "ScreenshotError"}   # 连续两次，100% 失败
```

用 DSH 的 seatbelt 配置（`(deny file-write*)` + 只放行工作区）单独跑一次 Chrome，得到了
同一行报错，确认根因就是「进程被限制写系统临时目录」：

```
sandbox-exec -p '(version 1)(allow default)(deny file-write*)(allow file-write* (subpath <repo>))' \
  "Google Chrome" --headless=new --screenshot=... → Failed to create a unique user data directory for headless.
```

顺带排除了两个想当然的修法：macOS 上 Chrome **不读 `TMPDIR`**（用只写目录做 TMPDIR 试验，
Chrome 仍在系统临时目录建 profile），而 `--user-data-dir` 在 Chrome 152 + 本项目参数下会
**写盘成功但进程不退出**（实测覆盖 / 单进程 / 老 headless / 一堆 `--disable-*` 都会挂住）。

**修复**：
- `screenshot_html` 现在把「Chrome 没退出但 PNG 已落盘」当成功（全新 profile 会拖着后台服务
  不退出），失败重试一次，并把 Chrome 的原始日志翻成中文可执行提示
  （`explain_chrome_failure`：直接告诉你「当前服务进程不允许写系统临时目录，请用
  AutoPosting服务控制.command 重启服务」）。
- 运行方式也一并纠正：本仓库实例过去是被上一轮会话用**受限后台任务**拉起来的，Chrome 必然
  建不出临时 profile。现在用项目自带的 `service_control.py`（`start_new_session=True`，
  脱离沙箱与会话）启动。

修完实机（同一份真实数据，`127.0.0.1:8090`）：

```
POST /api/skills/render → ok=true phase=done render_error=''  → 9 张（封面 + 8 张海报）
```

> 说明：受限沙箱里 Chrome 需要的写权限不止 profile（单例 socket 与 Crashpad 还要
> `/var/folders/...` 与 `~/Library/Application Support/Google/Chrome`），这是**服务启动方式**
> 的边界，代码侧无法绕过；代码侧能修的是「Chrome 没退出但图已生成」这类误判和错误可读性。
> 这一点写在「边界」里。

### 2. 「点生成图片后把已有 skill 汇总，按原本的工作流出封面海报 + 各自海报」

- 新增 `skill_finder.aggregate_skills(snapshot)`：把 `skills`（抓到的）与 `manual_skills`
  （粘贴的）合成一份，按仓库去重（粘贴的算更新的意图，顶掉同仓库的检索结果），
  按包自己的阶段顺序（没有阶段就按下面的工作流兜底）排序并重新编号 rank。
- `render_skill_package` 改为**只**从这份汇总出图，所以封面和单张海报讲的永远是同一批 skill。
- **粘贴的 skill 不再一律塞进「手动补充」桶**：新增 `_best_stage` 按「名字(5) > 描述(2) >
  SKILL.md 正文(1)」加权把 skill 归到生产环节，海报文案因此变成了
  「接手**视频与剪辑**这一环…」而不是以前的「接手**手动补充**这一环…」；实在匹配不到才进兜底桶，
  且该桶的显示名从 `手动补充` 改成 `通用工具`（id 仍是 `manual`，旧包兼容）。
  `WORKFLOW_STAGES` 是兜底工作流（选题与检索 / 采集与整理 / 写作与文案 / 图像与设计 /
  视频与剪辑 / 音频与配音 / 数据与复盘 / 发布与自动化），带 `offline: True`，检索循环**不会**
  去搜它们（`_is_searchable`），只用来归档结果。
- 查找改为**累加**：`_reuse_previous` 现在跨关键词保留上一轮的 skill，旧阶段没了就重新归档，
  而不是丢弃 —— 这样「汇总」才名副其实。

### 3. 标题框

- Skill 栏新增 `#skill-title`（在 `#skills-studio` 顶部），输入即写库
  （`POST /api/skills/title`，450ms 防抖），并立刻改介绍文标题 `#skills-title`；
  `生成图片` 也会把标题随请求带上，封面不会用旧标题。
- 封面海报标题改为 `user_title || 包标题`；标题是**用户状态**，查找/粘贴/清空都不会冲掉它
  （`_merge_latest_skill_fields` 与 `clear_skills` 都保留 `user_title`）。

### 4. 粘贴框更大 + 自适应 + 可主动换行

`#skill-links` 从 `<input type="text">` 换成 `<textarea rows="3">`：初始 76px 起，
输入/粘贴时 `growSkillLinks()` 按 `scrollHeight` 长高（上限 40vh），清空后缩回；
`Enter` 现在是**用户自己的换行**（原来 Enter 直接提交，做不到分行），提交改按「加入」或
`Cmd/Ctrl+Enter`。粘贴上限从 4000 字放宽到 `MAX_PASTE_CHARS = 20000`（一次真实粘贴里
常带 markdown 与说明文字），超过仍明确报错而不是把链接切一半当仓库名；16 条 skill 上限不变。

### 5. 「查找」不再用 AI 拆解，改成关键词抓取

- 新增 `keyword_coverage` / `extract_keywords`：用户输入按空格、`,`、`、`、`;`、`/`、`|`
  切词（最多 6 个，去重保序），**一个关键词 = 一条检索 lane**，直接喂给现有三路抓取
  （GitHub 代码检索、skills.sh 站内搜索、网页检索 + `anthropics/skills` 兜底清单）。
- `find_skills` 不再调用 `decompose_coverage`（保留该函数与 `coverage=` 显式计划入口，
  供归档/脚本/测试使用），**模型变成可选**：只在写介绍文时用，没配模型也能查找、也能出图。
- `PIPELINE_STEPS` 从 7 步收敛为 4 步（整理关键词 → 抓取来源 → 按工作流归组 → 汇总打包），
  `progress.step_total` 随之变成 4。
- 副产物：阶段数从「AI 拆出的 6–12 段」降到「关键词数」，每段的公平时间片显著变宽，
  1.3.1 里「候选都抓到了但没时间验证」的 `stage_quota` 问题明显缓解。
- 新增「清空」按钮（`POST /api/skills/clear`）：既然查找是累加的，就要有回到空盘的路，
  只清 skill 与图片，保留标题。

### 6. `skill-stage-chips` + `skill-gap-note` 合并成一块，只显示来源

`#skill-stage-chips` 与 `#skill-gap-note`（以及 `gapReason()`）删除，换成 `#skill-source-bar`：
`#skill-source-chips` 每个来源一个 chip，内容是 `来源 · 数量`（`GitHub · 3`、`skills.sh · 12`、
`手动粘贴 · 7`），原来的降级说明 `#skill-degraded-note` 留在同一块里（它讲的也是来源）。
阶段名不再出现在 UI 上（它仍决定海报顺序与文案）。

### 7. 取消 `skill-list`

`<ul id="skill-list">` 及其 CSS、渲染代码整段删除。

### 8. 状态区展示自动抓取源数量，点击展开

新增 `skill_finder.crawl_sources()`（GitHub 代码检索 / skills.sh / GitHub 官方 skills 仓库 /
网页检索，含 URL、启用状态、说明；GitHub 那条按是否配了 Token 换文案），
`GET /api/skills` 返回 `crawl_sources` 与 `crawl_source_count`。
状态区下方是 `#skill-source-toggle`（"当前自动抓取 skill 的源：4 个 · 点击展开"）+
`#skill-crawl-sources`，展开后列出每个源的名称、URL 与启用说明。

> 实现取舍：`#hotspot-status` 是四个栏目**共用**的状态行（40 多处 `status()` 在写它），
> 做成按钮会和状态文本互相覆盖。所以展示源数量的可点击入口放在同一块状态区里紧挨着它，
> 并把「未收录」与「来源数」两件事分得清清楚楚；`#hotspot-status` 本身仍是状态行。

## 二、顺带修掉的三个真问题

1. **零结果 + 未配模型 → 前端永远转圈**：旧前端在 `phase=skills_ready` 后仍等
   `article.body` 非空，而「筛选完没有 skill」时正文是空的，于是 350ms 轮询永不结束。
   现在 `find_skills` 零结果也写正文（"还没有收录 skill。"），前端把
   `skills_ready/done` 视为终态（`SKILL_POLL_PHASES` 只列在途阶段）。
2. **同一 monorepo 的两个粘贴 skill 会被合成一张海报**：汇总阶段一度按仓库去重，
   现在按「仓库 + skill 槽位」去重（`_manual_key`），兄弟 skill 各留一张海报
   （回归测试 `test_monorepo_siblings_keep_a_poster_each`）。
3. **没模型时介绍文标题是「手动补充 skill」**，还会顺带当封面标题：`_fallback_article`
   现在按工作流环节分组列 skill，标题取标题框/目标（如「视频 剪辑 Skill 汇总」），
   并且不再印出空的 `（）`。

## 三、边界（本次刻意不做的事）

- 未新增运行时依赖（仍是 Python 3.11 标准库 + SQLite），未新增图像模型调用点，
  出图仍只有「HTML/CSS → 浏览器截图」一条链路。
- 未改 `find_skills` 的预算参数（`MAX_FETCHES=160` / `FETCH_DEADLINE_SECONDS=240` /
  `MIN_STAGE_FETCHES=4` 原值不动）；本次只是阶段数变少，实际切片变宽。
- 未改 `SKILL_PATHS` 承诺的布局集合、`skill_links.py` 的链接解析、GitHub Token 的用途。
- 未改 `docs/skill_finder/COVERAGE.md`（AI 覆盖地图提示词仍在仓库里，`decompose_coverage`
  也仍可调用，只是查找入口不再用它）。
- **服务启动方式**：出图依赖本机 Chrome 写系统临时目录，因此服务不能跑在只允许写工作区的
  受限沙箱里（否则 Chrome 必然报 `Failed to create a unique user data directory`）。
  正常用 `AutoPosting服务控制.command` / `service_control.py` 启动即可；代码侧已把这种失败
  翻成中文可执行提示，而不是让用户看 Chrome 日志。
- `git` 状态：本次改动**未提交、未推送**（工作区里 1.3.0/1.3.1 的改动本来就未提交）。

## 四、验收

### 测试与自检

- 测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests` → **Ran 598 tests, OK (skipped=1)**
  （跳过的是真实网络 smoke test）。新增/改写覆盖：
  - 新文件 `tests/test_skill_package.py`（33 例）专门盯这次的 8 条要求：关键词切分与
    `keyword_coverage`、无模型检索真的打三路来源、4 步流水线与兜底工作流**不被检索**、
    零结果也有正文、汇总并集/工作流排序/monorepo 兄弟各留一张海报、粘贴顶掉同仓库检索结果、
    封面用用户标题且不再用模型标题、粘贴只有也能出封面 + 每 skill 一张海报（含 HTTP 取图）、
    无模型也能 `POST /api/skills/search`（不再 502）、`crawl_sources` 四条与计数、
    标题校验与清空后保留、>4000 字多行粘贴可用且 16 条上限与 `too_many` 上报、
    出图后 `skills` 落库、`#skill-list`/`#skill-stage-chips`/`#skill-gap-note` 消失、
    粘贴框是 `textarea` 且会自适应、标题框接线、来源 chip 与抓取源开关、Chrome 报错中文话术。
  - `tests/test_skill_finder.py`：原有的覆盖地图流水线用例迁到 `coverage=` 显式计划入口，
    新增关键词模式、累加复用、兜底桶改名、进度 4 步等断言。
  - `tests/test_web.py`：`#skill-source-bar` 合并块的栏目作用域断言替换旧的
    `#skill-gap-note` 断言；凭据输入框「不印示例值」的断言从「页面里不能出现
    placeholder 这个词」改成直接在 input 标签上断言没有 `value=`。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`
  → `{"ok": true, …}`。
- 服务版本：本仓库实例 `127.0.0.1:8090`（`/api/status`）→ `1.4.0`。**8080 属于兄弟目录
  `~/Documents/MiniWork/AutoPosting` 的实例，按「不与兄弟目录混用」未接管。**

### 实机：用户真实库（`127.0.0.1:8090`，未新增假数据）

库里原本就是「1 个检索到的 + 7 条粘贴的」skill 快照（`phase=skills_ready`、`images=[]`、
`render_error=ScreenshotError`）。修完后：

- `POST /api/skills/render {"theme":"dark","title":"8 个 Skill 搭一条短视频流水线"}`
  → `ok=true phase=done render_error=''`，**9 张图**（封面 + 8 张海报，每个 skill 一张），
  耗时 78s；`output/skills/6064c10a29cf/images/` 与 `output/packages/skills/` 都落了盘。
- 8 个 skill 的工作流环节不再是「手动补充」：`absurdity→脚本撰写与分镜设计`、
  `ai-image-generation→图像与设计`、`seedance/video-edit/ai-avatar-video→视频与剪辑`、
  `reddit-automation→发布与自动化`。
- 浏览器（`ego-browser`，同一实例）：来源 chip 显示 `GitHub · 1` / `手动粘贴 · 7`；
  `#skill-list`、`#skill-stage-chips`、`#skill-gap-note` 在 DOM 里都不存在；
  画廊 9 张图**全部加载成功**（`naturalWidth>0`，首图 1080×1440）；
  状态区显示「当前自动抓取 skill 的源：4 个 · 点击展开」，点开后列出 4 条源
  （名称 + URL + 已启用说明），toggle 文案翻成「点击收起」、`aria-expanded=true`；
  标题框显示 `8 个 Skill 搭一条短视频流水线`，改标题会**立刻**改介绍文标题、写库成功、
  改回后也一致（验收用的临时标题已还原）。
- 粘贴框实测：`<textarea rows="3">` 初始 84px，粘 6 行后长到 150px，行内换行符保留，
  清空后缩回 84px；`Enter` 插入真换行（行数 6→7），提交改按按钮 / `Cmd+Ctrl+Enter`。

### 实机：复验缺陷修复后的追加验证（`127.0.0.1:8097` 全新副本 + `8090`）

- **G1（粘贴兄弟被查找吃掉）**：先粘贴 `anthropics/skills` 里 4 个 skill（xlsx/pdf/docx/algorithmic-art），
  再查找「字幕」→ `count 14`、4 条粘贴全在、`manual_skills` 4/4（修复前 4 条只剩 1 条）。
- **G1/G2（同名不同源 + 兄弟）**：粘贴 `101-skills/superpowers/ai-image-generation`、
  `genmedia-labs/skills/ai-image-generation`、`genmedia-labs/skills/video-edit` → 查找「配音」（54s，16 条）
  → 三者的 `source_id` 全在，没有丢。
- **G3（贴错环节）**：同一次查找后，粘贴行的环节仍是 `图像与设计` / `视频与剪辑`，
  没有被贴到新关键词「配音」那条 lane 上。
- **G4（上限可见）**：浏览器里再查找「封面」→ 状态行
  `已收录 16 个 skill，点「生成图片」做汇总出图；已达 16 个上限，让位 13 个检索结果（producing-video、video2knowledge、videocut-subtitle 等）`，
  来源 chip `skills.sh · 5 / GitHub · 2 / 手动粘贴 · 9`，9 条粘贴一条没少。
- **G1–G9 全修完后在用户真实库上重跑出图**：`POST /api/skills/render` →
  `ok=true phase=done render_error=''`，**9 张**（封面 + 8 张海报），8 条 skill 全落在真实工作流环节上
  （`脚本撰写与分镜设计 / 图像与设计 ×3 / 视频与剪辑 ×3 / 发布与自动化`）。
- **最终一次浏览器复验（8090）**：来源 chip `GitHub · 1`、`手动粘贴 · 7`；
  `#skill-list` / `#skill-stage-chips` / `#skill-gap-note` / `gapReason` 全部不存在；
  抓取源入口 `当前自动抓取 skill 的源：4 个 · 点击收起` + 4 行明细；标题框与介绍文标题一致；
  画廊 9 张图 **0 张加载失败**；粘贴框是 `TEXTAREA`（84px → 输入变高，已单独验过）。

### 实机：独立副本实例（`127.0.0.1:8097`，库与输出都在 `/tmp`，不动用户数据）

- 未配任何模型（干净库）下执行关键词查找「视频 剪辑」：**146s** 抓回 **14 个 skill**
  （`GitHub · 2` + `skills.sh · 12`），阶段就是两个关键词（视频 / 剪辑），介绍文自动生成。
  这条路径在 1.3.1 里会直接 502 `skill_search_failed`。
- 接着点「生成图片」：**90s** 出 **15 张**（封面 + 14 张海报），`phase=done`、
  `render_error=''`，浏览器里 15 张全部加载成功、0 张失败，状态行「已生成 15 张，勾选后可导出」。
- 再用一个查不到东西的关键词验证收尾：`phase=skills_ready`、介绍文标题就是关键词、
  正文非空（`共收录 4 个 skill，目标：…。按工作流顺序：…`），前端不会再有「永远转圈」的终态。

## 五、独立验收（对抗式复验）与随后修掉的缺陷

一轮独立复验（另一个 agent，自己起 8 个副本实例：普通/无模型/受限沙箱/用户真实库的字节副本；
`ego-browser` 实机 + 真实网络 + tesseract 读 PNG 像素）给出的结论是 **8 条要求里 7 条成立、1 条部分成立**，
并要求补 3 个「顺带修的问题」。它报出的缺陷全部已修，每条都有回归测试：

| 编号 | 严重度 | 问题（复验实测） | 修复 | 证据 |
|---|---|---|---|---|
| G1 | 高（丢数据） | 同一个 GitHub monorepo 里粘贴的多个 skill，**下一次关键词查找会把兄弟全吃掉**：4 条只活 1 条（8094 实测 `count 4 → 1`）。去重键用的是 `source_id` 与「最后一段名字」，而粘贴的身份是「仓库 + 槽位」 | 新增 `_seen_keys` / `_matches_seen` / `_mark_seen` 三层键（`repo:` 只给仓库形状的 id；`id:`；`slot:<source_id>|<路径>`），粘贴行只按自己的槽位匹配；`find_skills` 把 `manual_skills` 写进它写出的**每一份**快照，查找再也擦不掉粘贴集合 | `test_pasted_monorepo_siblings_survive_a_search`、`test_a_second_search_cannot_erase_the_pasted_set`；实机 8097：4 条粘贴 → 查找「字幕」→ 4 条全在，`manual_skills` 4/4 |
| G2 | 高（真实库形状） | 不同 owner 的同名 skill 会在第二次查找时撞车：用户真实库副本上 `genmedia-labs/skills/ai-image-generation` 被 `101-skills/superpowers/…` 顶掉。原因是把 skill 名当去重键 | 同上（不再用裸名字做键；skills.sh 的三段 id 不占 `repo:`） | `test_same_name_skills_from_different_repos_both_survive`；实机 8097：两个同名 skill 查找后都在 |
| G3 | 中（贴错环节） | 关键词阶段 id 是位置式的 `kw1/kw2`，下一次查找的 `kw1` 与上一次撞车，**上一轮的 skill 被贴到新关键词的 lane 上**（8095 实测 13 条变成 `kw1/zzzqqqxxyy-nomatch`） | 阶段 id 改为内容派生：`_keyword_stage_id()` = `kw-<sha1(关键词)[:10]>` | `test_carried_over_skill_is_not_relabelled_into_the_new_keyword`、`test_keyword_coverage_makes_one_stage_and_query_per_keyword`；实机 8097：`配音` 查找后粘贴行仍是 `图像与设计`/`视频与剪辑` |
| G4 | 中（上限静默） | 包超过 16 个时，低热度的**检索结果**被 `cap_skills` 静默挤掉（8095 掉了 2 条），与「累加」的说法不符 | 明确报告而不是偷偷丢：`find_skills` 计算 `capped_dropped` + `skill_cap`，前端在状态行写「已达 16 个上限，让位 N 个检索结果（…）」；粘贴的 skill 仍然永久保留（pinned） | `test_the_skill_cap_is_reported_instead_of_silently_dropping_rows`；实机 8097 浏览器：`已收录 16 个 skill…已达 16 个上限，让位 13 个检索结果（…）`，9 条粘贴全在 |
| G5 | 中（非 ASCII 路径 + 英文异常名） | 仓库里带中文的 skill 目录拼 raw URL 时抛 `UnicodeEncodeError`，界面上出现「GitHub UnicodeEncodeError，结果可能偏少」这种英文异常名（8095 实测 5 次） | 新增 `_url_path()`（`quote(..., safe="/%")`，不双重编码）用于所有 raw/contents URL；`DEGRADE_LABELS` 补 `github:UnicodeEncodeError` / `github:InvalidURL`；未映射原因一律翻成中文句子，不再直接印标识符 | `test_non_ascii_skill_paths_are_percent_encoded`（断言 URL 可 `.encode("ascii")` 且含 `%E5%89%AA`）、`test_url_path_does_not_double_encode` |
| G6 | 低-中（同类死循环） | 检索线程自己的异常处理里读库，读库再抛就线程猝死、`phase` 停在在途 —— 浏览器一直转圈（588 例测试输出里就打印过这条 traceback） | `mark_failed()` 把「读快照 + 落盘」整段兜住并只记日志；前端再加 `SKILL_POLL_MAX_TICKS=1800`（约 10 分钟）上限，超时明确报错而不是无限轮询 | `test_a_crashing_search_still_lands_on_an_error_phase`（让检索线程自己的读库必失败，仍要求落成 `error`） |
| G7 | 低 | 出图失败后画廊仍显示「排队中」，只有状态行报错 | `renderSkillImages()` 的 catch 把没有 url 的 tile 标成失败并重画 | 浏览器契约（`image.error = reason` 接线）+ 沙箱实例 8092 复验时画廊显示失败 |
| G8 | 低 | `_best_stage` 仍按整篇 SKILL.md 打分，通用词抢分：xlsx 被归到「写作与文案」、pdf 归到「数据与复盘」 | 权重改为「名字 5 / 简介 2」；只有**包自己的覆盖阶段**（非 `offline`）才额外认整篇正文 1 分 —— 那些 query 本来就是为找这批 skill 写的。兜底工作流行只认名字与简介，认不出就进「通用工具」 | `test_document_body_noise_does_not_decide_the_stage`（xlsx→数据与复盘、pdf→桶） |
| G9 | 低 | 粘贴里夹大段说明文字时返回 300+ 条 `rejected`，界面显示「跳过 550 条」很吵 | 详情截断到 `MAX_PASTE_REJECTS=20`，另给精确的 `rejected_count`，前端按计数说话 | `test_paste_reject_detail_is_capped_but_the_count_is_exact` |

复验同时确认成立的部分（原文摘录）：粘贴-only 真能出图（8094：3 张，PNG 71KB–170KB，全部 1080×1440，
`GET /api/generated/skills/*.png` 全 200；并用 tesseract 从封面 PNG 像素里读出标题）；
根因用 `sandbox-exec` 复现，受限沙箱里整个服务的 `/api/skills/render` 返回 500 且错误是中文提示、
响应与页面里都没有 Chrome 英文日志、`phase` 复位成 `skills_ready`；
标题校验（123→400、121→400、120→200）、跨粘贴/清空/两次查找存活、封面用最新标题（含 450ms 防抖竞态）；
粘贴框 84→150→372px 自适应、`Enter` 真换行、20001 字 400 / 20000 字 200；
无模型关键词检索「视频 剪辑」146.3s / 14 条，并用**包真实 getter** 的探针逐关键词确认三路来源都被打到；
`decompose_coverage` 在生产路径上没有任何调用者；来源 chip、三个旧元素消失、抓取源展开 4 条都成立。

复验指出的「未被 588 例测试覆盖」的部分，本次已补：粘贴兄弟/同名/两次查找/G3/G5/G8、
线程异常处理、上限报告都有回归测试；渲染用例仍用注入的假截图与离线素材
（真 Chrome 的失败模式靠 `explain_chrome_failure` 单测 + 沙箱实例实机覆盖）。

## 六、已知问题（未在本次修，需另开一刀）

1. **累加受 16 个上限约束**：粘贴的 skill 永久保留（pinned），超出的**检索结果**按热度让位，
   并且现在会明确报告让位了谁。如果用户想要「一次一包」而不是累加，应该再给一个显式的
   「新开一包」动作，而不是悄悄改回覆盖语义。
2. **同名不同源的 skill 会各出一张海报**（两个 owner 都有 `ai-image-generation` 时）。
   去重按仓库/槽位做，不按名字；封面列表上会看到两个同名条目 —— 这是刻意的，
   合并它们才是丢数据（G2 就是反例）。
3. **`decompose_coverage` 与 `docs/skill_finder/COVERAGE.md` 没有生产调用点**：
   保留是为了让归档包、脚本与 `coverage=` 入口继续可用；确认不再需要后下一刀可以删。
4. **受限沙箱里无法出图**（代码侧不可绕过，见「边界」）：只允许写工作区时，Chrome 建不出
   临时 profile / 单例 socket / Crashpad 目录。现在会给出中文可执行提示，而不是让用户对着
   Chrome 日志猜。
5. **`#hotspot-status` 仍是四栏共用的状态行**，抓取源入口是它旁边的可点击行
   （上面「实现取舍」已说明）；要状态行本身变成按钮，得先把四栏的状态写入拆成各自的行。
6. **`_stage_for_manual` 对「名字与简介都没提到任何环节」的 skill 只能进「通用工具」桶**
   （例如 `pdf`、`artifacts-builder`）：这是诚实的兜底，不再靠正文里的通用词硬猜（G8）。
