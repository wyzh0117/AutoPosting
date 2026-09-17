# AutoPosting 1.3.1

本次三件事：① Skill 查找器的结果说明不再溢出到另外三个页面；② GitHub 失败（`github:RuntimeError`）改成可读、可恢复、可配置；③ Skill 查找器新增「粘贴 skill 链接」入口，粘贴进来的 skill 与检索到的一样走完「简介 → 海报」全流程。

## 变更

### A. 结果说明不再溢出到其他三个页面（用户报的「出现在所有四个页面」）

- **根因**：`#skill-gap-note`（缺口汇总）与 `#skill-degraded-note`（来源降级）都渲染在四个栏目共用的 `#hotspot-pane` 里。共用的兄弟元素（搜索框、阶段 chip、skill 列表）都有 `body.lane-skills` 前缀的显示规则，只有这两条 `<p>` 例外：它们平时各自带 `hidden`，一旦查找过并写入文案就 `hidden=false`，从此在热点候选 / 观点候选 / GitHub 周榜里同样可见 —— 也就是用户看到的「9 个环节没找到合格 skill…」到处都在。
- **修法**（`src/autoposting/web.py`，纯 CSS，不动 JS 语义）：
  - 把 `#skill-gap-note,#skill-degraded-note` 加入默认隐藏组（与 `#skill-search-row`、`#skill-list` 同一组 `display:none`）；
  - 新增 `body.lane-skills #skill-gap-note:not([hidden])` / `…#skill-degraded-note:not([hidden]){display:block;}`，保留各自的视觉样式与 `[hidden]{display:none}` 语义；
  - 反向约束：其他三个 lane 不得出现这两条的任何选择器（回归测试用正则断言）。
- **回归测试**：`tests/test_web.py::IndexPageContractTests::test_skill_result_notes_are_scoped_to_the_skills_lane`。
- **顺带**：本次新增的粘贴框、粘贴提示、GitHub Token 折叠块同样按 `body.lane-skills` 作用域，不引入第二处泄漏。
- **缺口说明补一句实话**：缺口不一定等于「全网没有」——本轮实测有 13 个候选是「检索到了、但阶段时间片用完没验证」。`renderSkillPackage()` 现在会数 `rejected[]` 里 `stage_quota` 的条数，并在缺口说明后面追加「另有 N 个已检索到的候选因为本轮检索时间用完没能验证，再查找一次通常会补上（已抓到的结果会复用）」，避免用户面对一排「没找到合格 skill」无从下手。

### B. GitHub 失败：从 `github:RuntimeError` 到可读、可恢复

先定位事实（本机实测，不是推测）：

- 本机未鉴权访问 `api.github.com` 的额度是 **core 60 次/小时、search 10 次/分钟**（`GET /rate_limit` 实测 `limit:60, remaining:14, resource:core`）。
- 用户那次「视频制作」查找：`budget = {calls:78, fetch_calls:69, search_calls:9, stage_quota_skips:320, elapsed_ms:262114}`，`degraded_reasons:["github:RuntimeError"]`。仅 `fetch_skill_md()` 在每个候选仓库上就要花 1 次 `contents` 调用，几十个候选直接把 60 次/小时的 core 额度打光，之后每次 api.github.com 都是 403。
- 403 被 `default_github_get()` 统一包成 `RuntimeError("github_fetch_failed:<url>")`，调用方只能记下异常类名 → 页面上就是 `github:RuntimeError`。

改动：

- **`src/autoposting/github_weekly.py`（HTTP 层）**
  - 新增 `GitHubFetchError(RuntimeError)`，带 `kind`（`rate_limited` / `forbidden` / `not_found` / `timeout` / `unreachable` / `server_error` / `http_error` / `bad_json`）、`status`、`url`、`reset_at`；`raise … from exc` 保留原始 `HTTPError`，`_is_rate_limit()` 的旧文本匹配（403/429/rate limit）继续成立。
  - `default_github_get(url, timeout=20, token=None)`：有 token 就带 `Authorization: Bearer`；token 绝不出现在异常消息、日志或任何响应体里。
  - `set_github_token()` / `github_token()`：优先级 `set_github_token()` 覆盖 → `AUTOPOSTING_GITHUB_TOKEN` → `GITHUB_TOKEN`；`None` 表示「回到环境变量」。
  - `github_rate_state()`：最近一次响应的 `X-RateLimit-*`（成功和 403 都记），供查找器判断额度。
- **`src/autoposting/skill_finder.py`（查找器）**
  - 降级原因改成「来源:kind」：`github:rate_limited` / `github:timeout` / `github:unreachable` / `github:server_error` / `budget_exhausted` …；页面对应一张中文映射表（`DEGRADE_LABELS`、`degradeLabel()`、`degradedSummary()`），原始记号只留在 `title` 里。旧快照里的 `github:RuntimeError` 也有中文文案。
  - **404 不算降级**：走 `SKILL_PATHS` 时 404 是正常结果（这个布局没有 SKILL.md），把它报成「来源没返回」是误报；`_NON_DEGRADING_KINDS = {"not_found"}`。
  - **额度熔断**：`_FetchBudget.github_api_ok()` / `_block_github()`。core/search 分开计（GitHub 本身就是两个独立额度桶）：收到对应桶的 403、或 `github_rate_state()` 显示该桶 `remaining <= 2` 时，本桶在本次运行内不再调用（search 最多停 60s、core 最多停 900s，取 `X-RateLimit-Reset` 与实际等待的较小值），避免把 160 次预算继续喂给必然 403 的请求。
  - **熔断后仍能验证候选**：`fetch_skill_md(..., api_ok=…)` 在 API 被熔断时跳过 `contents` 清单，改为把 `COVERAGE.md` 承诺的全部布局走 `raw.githubusercontent.com` 直连探测（CDN，不占 api 额度），候选不再因为「额度没了」被记成 `no_skill_md` 丢掉。
  - **可选 Token**：设置入口在 Skill 栏内（`<details>` 折叠），保存走 `POST /api/skills/github-token`，存进与模型密钥同一个钥匙串（`github:token`），接口只回 `{configured: true/false}`，不回显明文；清空时回落环境变量而不是强制「无 token」。配好后额度 60/小时 → 5000/小时。

### C. 粘贴 skill 链接（新功能）

- **入口**：Skill 栏搜索框下方新增一行输入 + 「加入」按钮（`#skill-links` / `#skill-paste-btn`），下面一行说明支持的形式。回车等同点击。
- **解析**（`src/autoposting/skill_links.py`，纯函数、无 IO、无网络）：
  - 支持 `github.com/<owner>/<repo>`、`/tree/<ref>/<path>`、`/blob/<ref>/…/SKILL.md`、`raw.githubusercontent.com/.../SKILL.md`、`skills.sh/<owner>/<repo>[/<skill>]`、裸 `owner/repo`；
  - 多个链接用换行/空格/中英文逗号分隔，容忍列表序号、markdown 包裹、行尾标点；
  - 去重键是「来源 + 仓库 + skill 槽位」，所以同一个 monorepo 里的两个 skill 会各算一个，而不是被当成重复；无法识别的输入返回原因（`unsupported_host` / `reserved_owner` / `not_a_link` / `duplicate`），不抛异常。
- **合流**（`skill_finder.add_manual_skills()`）：
  - 每个链接抓 `SKILL.md`（先用链接自带的路径提示做一次 raw 请求，再退回 `SKILL_PATHS` 探测），拿仓库元信息（star/描述/头像）失败也不影响入库；
  - **不走质量门槛**：粘贴是用户的明确选择，不再用 star/装机量/新鲜度判定；
  - 分配到覆盖阶段（按阶段名/检索词匹配），匹配不上就进合成阶段「手动补充」（该阶段标记 `manual: true`，检索循环会跳过它，不会拿「手动补充」当检索词）；
  - 重写包内介绍文（有模型调模型，没模型/模型失败用机械清单兜底，保留原标题），刷新阶段 chip 与缺口；
  - **保留位**：`cap_skills()` 把 `manual` 的 skill 钉住，热度排序不会把它们挤掉；`find_skills()` 会把上一轮的 `manual_skills` 重新注入，所以再查一次也不会丢；
  - 清空旧 `images`（旧海报不包含新 skill），UI 立刻铺占位图并自动触发现有「生成图片」流程 → 粘贴后拿到的是「简介 + 海报」两件套。
- **API**：`POST /api/skills/paste {links}`，一次最多 16 条（= 包内 skill 上限，超出以 `too_many` 报告，避免后面被 `cap_skills` 静默丢掉）；查找进行中返回 400 `skill_search_running`、正在出图返回 400 `skill_render_running`（出图期间它自己持有一份快照，此时写入会被覆盖），前端各有一句可读提示。
- **读不到的链接也如实说**：粘贴的链接如果连 `SKILL.md` 都抓不到（路径写错、仓库私有、纯网页链接），skill 仍然按用户意愿入库，但响应里带 `unverified[]`，前端提示「有 N 条没读到 SKILL.md，海报会用模板文案」——不静默、也不丢弃。
- **描述不再引用 front-matter**：`_first_paragraph()`（粘贴链路的兜底描述）与 `_skill_excerpt()` 共用同一份元数据前缀表（`name:` / `description:` / `license:` / …），避免海报上出现「name: web-artifacts-builder」这种 YAML 行。
- **文档**：`README.md` 补 `/api/skills/paste` 与 `/api/skills/github-token` 的 curl 示例、第四栏与 GitHub 限流说明；`docs/WEB_FUNCTIONAL_TEST.md` 修正第 17 步两处与 1.3.0 实现不符的描述（介绍文在画廊下方、缺口必须可见），新增第 19 步「粘贴链接与 GitHub Token」9 条点选验收。

### D. 版本号

`1.3.0 → 1.3.1`，五处同步：`pyproject.toml`、`src/autoposting/__init__.py`、`src/autoposting/web.py`（`APP_VERSION` + 状态徽标）、`src/autoposting/llm.py`（`HTTP_USER_AGENT`）、以及 `tests/test_llm.py` / `tests/test_web.py` 里的版本断言。

## 独立验收发现的缺陷与修复

一次对抗式复验（另一 agent，自己起实例、自己造数据，只读用户 8090）报出 8 条，全部已修，每条都有回归测试：

| 编号 | 严重度 | 问题 | 修复 | 证据 |
|---|---|---|---|---|
| F1 | 高 | 粘贴链接的路径段里带控制字符（如 DEL 0x7f）→ 出图时 `urllib` 抛 `http.client.InvalidURL`（是 `HTTPException`，不被 `story.py` 的 `except (OSError, TimeoutError, ValueError)` 接住）→ `/api/skills/render` 500 且 `phase` 永久停在 `rendering` → 之后每次粘贴都被拒 | `visual/media.py::fetch_bytes` 把 `InvalidURL` 归一成 `ValueError`（`fetch_data_uri` 也补 `HTTPException`）；`skills_render` 整体 try/except，失败写 `phase="skills_ready"` + `render_error` 后照实抛 500 | 实机复现原故障后再跑：粘贴控制字符链接 → 出图 **200 / phase=done / 2 张图**（原先 500+卡死）；`test_failed_render_resets_the_phase_for_the_next_paste` |
| F2 | 高 | 粘贴还在抓 SKILL.md 时点「生成图片」：出图用的是它启动时那份旧快照，落盘时把刚粘贴的 skill 连同 `manual_skills` 一起覆盖，**永久丢失** | 出图只写自己负责的字段，并在锁内重读快照比对 `revision`；不一致就 `ValueError("skill_package_changed")` 拒绝落盘（前端提示重新出图）。粘贴改为「锁外抓取 → 锁内合并最新快照」，并在快照里维护 `revision`（粘贴/查找各自 +1） | `test_render_refuses_to_overwrite_a_package_that_moved`、`test_paste_is_refused_only_by_a_live_render`；实机：出图中粘贴 → 400 `skill_render_running`，出图结束后再粘贴 → 新增成功且原 skill 全在 |
| F3 | 中 | 粘贴结果（新增/跳过/没读到 SKILL.md）写在共享状态行，紧接着的 `renderSkillImages()` 在同一个 tick 覆写它 → 用户从来没见过这条消息 | 新增第四栏专属结果行 `#skill-paste-status`（独立于共享状态行），出图也改走 `laneStatus()` | 实机浏览器：出图进行中该行显示「替换 1 个；当前共 4 个 skill」，出图结束后仍在（共享行显示「已生成 5 张」） |
| F4 | 中 | 出图崩溃/服务重启后 `phase` 停在 `rendering`，粘贴入口被永久锁死，提示还让用户「等这一轮出完」 | 粘贴改用**进程内** `_skill_render_active` 标志判断，而不是读库里的 phase；标志在 `finally` 里清掉，重启后自然为 False | `test_paste_is_refused_only_by_a_live_render`（库里 phase=rendering 时仍可粘贴；标志为真时才 400） |
| F5 | 低-中 | 输入框 `maxlength="4000"` 让浏览器先截断，服务端校验永远走不到；一次真实粘贴被切成 `.../anthropics/skil` 并当成真仓库入库 | 去掉 `maxlength`，改为前端显式检查长度并提示「链接太长…请分几次粘贴」 | `test_paste_result_has_its_own_visible_line` 断言该 input 无 `maxlength` 且提示存在；服务端 400 校验仍在 |
| F6 | 低 | 重复粘贴同一链接时返回 `added: 1`，但实际只是替换 | `added` 只算真正新增，替换数走新的 `paste_replaced`，前端显示「新增 X 个，替换 Y 个」；同一 monorepo 的兄弟算新增，不算替换 | 实机：首次粘贴 `theme-factory` 曾显示「替换 1 个」（错误），修后单测断言 `paste_replaced == 0`（兄弟）与 `== 1`（同槽位重粘） |
| F7 | 低 | `%2e%2e` 这类百分号编码的点段绕过 `.`/`..` 校验进入 raw 抓取 URL | `skill_links._path_parts` 一并拒绝 `%2e` / `%2e%2e` / `.%2e` / `%2e.` | `test_dot_segments_are_rejected_even_percent_encoded`（5 种写法） |
| F8 | 低 | 出图结束后才切栏目，结果会写进别的栏目状态行 | `renderSkillImages()` 的所有状态写入改走 `laneStatus()`，只在 `state.lane === "skills"` 时落笔（等待卡片仍全局可见） | `test_render_status_updates_are_lane_scoped` |

复验另外确认**没问题**的部分（原文摘录）：栏目泄漏修复在计算样式与 `getClientRects()` 下双向成立；粘贴在「未配置模型」的干净库上仍能出介绍文与海报（含磁盘上的 HTML 板块非空）；多链接/中文逗号/markdown 包裹/跳过计数正确；XSS 全走 `textContent`，恶意 name/描述不会变成元素；token 接口只回布尔、环境变量金丝雀不出现在任何响应里；查找路径的熔断、404 不降级、raw 不污染额度快照都成立；`save_skill_finder` 是单条原子 UPSERT。

## 边界

- 未新增运行时依赖（仍然只用 Python 3.11 标准库 + SQLite），未新增图像模型调用点，出图仍只有「HTML/CSS → 浏览器截图」一条链路。
- 未改检索阶段的公平切片参数（`MIN_STAGE_FETCHES` / `MAX_FETCHES` / `FETCH_DEADLINE_SECONDS` 原值不动），本次没有借机调预算。
- 未改 `docs/skill_finder/COVERAGE.md` 承诺的 `SKILL_PATHS` 布局集合，只是「额度被熔断时也把这些布局走完」。
- GitHub Token 只用于 `api.github.com`（含 search/contents/repos）与 raw 直连；不改变 `skills.sh` 与网页检索的调用方式。
- 粘贴链接不会写进 `skills/` 之外的路径：文件名仍走 `_safe_name()`，包目录仍由 `write_package()` 的 `sha256(query)` 决定。
- `git` 状态：本次改动**未提交、未推送**（工作区里 1.3.0 的改动本来就未提交）。

## 验收

- 测试：`PYTHONPATH=src:tests python3 -m unittest discover -s tests` → **Ran 564 tests, OK (skipped=1)**（跳过的是真实网络 smoke test）。新增覆盖：
  - `test_web.py::test_skill_result_notes_are_scoped_to_the_skills_lane`（含其他三个 lane 的反向断言）；
  - `test_skill_finder.py::ManualSkillPasteTests`（10 例：入库/阶段/海报计划/重复粘贴替换/monorepo 兄弟与替换计数/无搜索无模型/写包目录/搜索重注入+保留位/阶段匹配/front-matter 描述）；
  - `test_skill_finder.py::SkillFinderApiTests` 新增 13 例（paste 成功、非法链接原因、入参校验、16 条上限、出图中拒绝粘贴、陈旧 rendering 不锁死、出图失败复位 phase、revision 防覆盖、token 存取与不回显、lane 内 UI 契约、降级文案中文化、缺口说明含时间片原因、粘贴结果独立成行、出图状态按栏目作用域）；
  - `test_skill_finder.py::SkillFinderTests` 新增 4 例（403 熔断并给出 `github:rate_limited`、额度快照提前熔断且不误伤另一个桶、API 熔断时 raw 布局探测、404 不算降级）；
  - `test_skill_links.py`（50 例，含点段编码）与 `test_github_weekly.py::GitHubHttpLayerTests`（14 例）。
- 离线自检：`PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check` → `{"ok": true, …}`。
- 服务版本：本仓库实例 `127.0.0.1:8090`（`/api/status`）→ `1.3.1`。**8080 属于兄弟目录 `~/Documents/MiniWork/AutoPosting` 的实例，按「不与兄弟目录混用」未接管。**
- 实机（浏览器 `ego-browser`，本仓库 8090，用的是用户真实库里的「视频制作」快照：9 个缺口 + `github:RuntimeError` + 13 条 `stage_quota` 拒绝）：
  - Skill 栏：缺口说明可见（「有 9 个环节没找到合格 skill：…。另有 13 个已检索到的候选因为本轮检索时间用完没能验证…」），降级说明可见且已中文化（「部分来源本次没有返回：GitHub 接口本次没返回（多半是限流），结果可能偏少。」）。
  - 切到热点候选 / 观点候选 / GitHub 周榜：两条说明 `getClientRects().length == 0`、`getComputedStyle().display == "none"`，`#hotspot-pane` 里实际绘制的子节点只有该栏目自己的列表 + 状态行 —— **泄漏消失**。
  - 粘贴框、粘贴提示、Token 折叠块只在 Skill 栏可见。
  - 验证只读：脚本只切栏目、读 DOM，没有提交粘贴、没有保存 token、没有触发新的检索；用户库里的 `skill_finder` 行保持 `视频制作 / 1 个 skill / 2 张图` 原样。
- 实机（同一浏览器，另起一个副本实例 `127.0.0.1:8095`，数据库/输出目录都在 `/tmp`，不动用户数据）：
  - 粘贴 `https://github.com/anthropics/skills/tree/main/artifacts-builder` → skill 列表 1 → 2（`absurdity` + `artifacts-builder`），介绍文被模型重写为《视频制作 Skill 打包介绍》并新增「手动补充（兜底工具链）」一节，输入框清空，状态行提示后自动出图；
  - 出图完成：3 张（封面 + `01-absurdity` + `02-artifacts-builder`），`02-artifacts-builder.html` 里四个板块（做什么 / 如何实现 / 适合哪些人群 / 使用场景）都有真实内容，页脚张数 `03 / 03` 与包内张数一致。
- GitHub 分类实机（真实 API，非 mock）：
  - 连续 10 次 search 全部 200（`X-RateLimit-Remaining` 9→0），第 11 次得到真实 **HTTP 403** → `GitHubFetchError(kind="rate_limited", status=403, reset_at=1789569805.0)`，消息含 `rate limit`；
  - 另测真实 404 → `not_found`、bogus token → `forbidden`、坏域名 → `unreachable`、极短超时 → `timeout`；`fetch_weekly_ai_repos()` 生产链路仍能取回真实仓库。

## 已知问题（未在本次修，需另开一刀）

同一条「视频制作」查询在修复后重跑（副本实例、真实网络），结果仍然是 1 个 skill / 9 个缺口，但**降级原因里已经没有 GitHub**：

- `budget = {calls:79, fetch_calls:69, search_calls:10, stage_quota_skips:312, elapsed_ms:258477}`；
- 被拒候选里 10 条 skills.sh + 3 条 GitHub 的原因是 **`stage_quota`**：候选已经被检索到，但该阶段的「墙钟切片」用完了，`SKILL.md` 验证被拒；
- 单次 HTTP 平均 ~3.7s（skills.sh 常见 1–7s，raw 探测 ~1.2s），10 个阶段 × （GitHub 检索 + skills.sh 检索 + 网页检索 + 若干个候选验证）远超 `FETCH_DEADLINE_SECONDS=240`，于是每个阶段只够跑完检索、验证全被跳过。

也就是说：**GitHub 报错是「结果难看」的一部分原因，但不是主因；主因是 240s 检索预算在「三路检索 + 候选验证」之间不够分。** 本次刻意不动预算参数（避免顺手改行为），只在 UI 上把这件事说清楚（缺口说明会带上「另有 N 个候选因时间用完没验证」）。建议下一步单独决策：抬高墙钟预算 / 让验证请求不占阶段切片 / 候选验证并发化，或放宽「必须有 SKILL.md 才能入库」的门槛（skills.sh 的 id 本身已证明该 skill 存在，只是拿不到正文，海报可退回模板文案）。
