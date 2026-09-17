# Skill 查找覆盖诊断 1.3.0（B1 skills.sh 拓宽 + B4 缺口可见）

本文件是 `src/autoposting/skill_finder.py` 在 1.3.0（skills.sh 源 + 缺口可见）改动前/后的真实网络诊断记录。
诊断对象：需求 `视频制作`。BEFORE 使用改动前的模块（`git show HEAD:src/autoposting/skill_finder.py`），AFTER 使用改动后的工作区代码；
两次都跑**真实网络**（GitHub API + DuckDuckGo + skills.sh），只有"覆盖地图的生成"用固定 8 阶段计划代替，
因为本机没有配置可用的模型凭据（`complete` 是唯一被替换的注入点，见第 1 节）。

---

## 勘误（1.3.0 定稿后追加，独立复验发现）

本文件的 AFTER 数据是在 `fetch_skill_md()` 仍把 `{name}` 填成**仓库名**、且 GitHub 限流时会把 `skill_path` 硬写成 `"SKILL.md"` 的那一版代码上跑出来的。两处都已在定稿前修复，因此下面这些结论**不再成立**，请连同本节一起读：

1. **「对外可见阶段 3/8 → 8/8」是被伪造证据撑起来的。** 复验对本文档自己收录的 13 个 skills.sh 仓库逐一探测 5 条 `SKILL_PATHS`：**10 个在任何路径下都没有 SKILL.md**，其中 3 个只存在于 `skills/<skill-name>/SKILL.md`（`elementsix/elementsix-skills` → `skills/seedance-storyboard/SKILL.md`、`mediastormdev/dream-to-video-skill` → `skills/dream-to-video/SKILL.md`、`wind-alice/alicemarket` → `skills/post-market-debrief/SKILL.md`）。这些行当年能进表，靠的是限流兜底写死的 `skill_path="SKILL.md"`，不是真的定位到了文件。**按诚实的探测口径，当时 8 个阶段里会空掉 5 个，与 BEFORE 的缺口集合相同。**
2. **「skill_path 全部是 SKILL.md（根布局），说明新加入的嵌套布局没有引入额外开销」是错的。** 那不是「根布局成立」，而是兜底值；嵌套布局当时根本没被正确探测（`{name}` 填的是仓库名）。
3. **修复后复测（同一仓库、同一 skill）**：`fetch_skill_md("genmedia-labs/skills", getter, "video-edit")` → 命中 `video-edit/SKILL.md`（11,159 字符）；旧口径对照仍然失败。新增的 `{name}/SKILL.md` 布局 + `skill_name` 参数才是 skills.sh monorepo 能真正入库的原因。
4. **§4④ 的「共 6 条 rejected」不可复现**：同一脚本重放得到 52 条拒绝记录（对外只暴露前 20 条），其中 46 条没有出现在那 6 行的表里。
5. **§4⑤ 引用的 `/tmp/diag-after-v2.json` 已不可恢复**，其中的 voice/cover/review 缺口样例无法再核对。
6. **`stage_quota_skips=95` 的归因写错了**：从本文档自己的逐阶段数字看（用量 11/12/16/15/15/11/11/10，对应上限 20/33/46/64/81/100/120/160），没有任何阶段用满自己的**调用**份额，所以这 95 次全部是阶段时间片/全局截止时间的拒绝，不是「阶段配额跳过」。
7. **§7.1/§7.2 已过期**：`COVERAGE.md` 的缺口规则在 19:53 被改写，其中列出的 2 个「失败测试」也已改名，全套测试现在是绿的。

**可复现、可以放心的数字**：BEFORE 对外可见阶段 3/8 = 62.5%、拒绝 9 + 7 = 16 条；AFTER 逐阶段调用 11+12+16+15+15+11+11+10 = 101，加 1 次阶段前的 `KNOWN_ROOTS` 调用 = 102 = `budget.calls`（那 1 次是真实的，今天在真实 GitHub 403 下仍复现）；BEFORE 预算 80/80 打满而尾部阶段 0 次请求。

---

## 0. 结论摘要

| 指标 | BEFORE（仅 GitHub） | AFTER（GitHub + skills.sh，缺口可见） |
|---|---|---|
| 计划阶段数 / 对外可见阶段数 | 8 / **3**（5 个被静默删除） | 8 / **8**（空环节带 `gap: true` + 中文原因） |
| 收录 skill 数 | 3（全部 GitHub） | 见第 4 节（含 `source == "skills.sh"` 行） |
| 抓取预算 | 80 / 80 **打满**，尾部阶段检索被饿死 | 见第 4 节（按阶段公平切片，尾部阶段仍有检索） |
| `degraded` | `True`（无原因字段） | `True/False` + `degraded_reasons`（如 `skills.sh:RuntimeError`） |
| 质量门槛拒绝理由 | 只有布尔值，无法解释 | `quality_reason()` + snapshot 的 `rejected[]` |
| 可定位的 SKILL.md 布局 | 仅仓库根 `SKILL.md` | 根 + `.agents/skills/<name>/` + `.claude/skills/<name>/` + `skills/<name>/` + `.cursor/skills/<name>/` |

---

## 1. 复现方式

```bash
cd /Users/youngi/Documents/MiniWork/AutoPosting-development

# BEFORE：用 HEAD（改动前）的 skill_finder.py 覆盖一份 src 副本，其余模块仍取工作区版本
rm -rf /tmp/before-src && mkdir -p /tmp/before-src
cp -R src/autoposting /tmp/before-src/autoposting
git show HEAD:src/autoposting/skill_finder.py > /tmp/before-src/autoposting/skill_finder.py

SKILL_FINDER_SRC=/tmp/before-src PYTHONPATH=/tmp/before-src \
  python3 /tmp/skill_finder_diag.py before /tmp/diag-before.json 80

# AFTER：直接跑工作区代码
PYTHONPATH=src python3 /tmp/skill_finder_diag.py after /tmp/diag-after.json 160
```

诊断脚本 `/tmp/skill_finder_diag.py` 的做法（脚本正文见第 6 节附注）：

- `find_skills(query, complete=<固定 8 阶段覆盖地图>, github_get=None, web_search=None, ...)`：
  `github_get=None` / `web_search=None` 让模块走**生产默认 HTTP 路径**（`default_github_get` / `background.search_web`）；AFTER 版本里 skills.sh 也走同一条注入式 `Getter`（`_default_get` 按 host 分发到 `_default_skills_sh_get`）。
- 通过猴子补丁**只加观测、不改行为**：记录每次 `_github_search` / `_skills_sh_search` / `_names_from_web` 的检索词与命中数、每个 `_search_stage` 的调用区间、每次 `passes_quality`/`quality_reason` 的判定，以及 `_FetchBudget` 实例。
- 唯一的非真实环节是 `complete`（覆盖地图）：本机没有模型凭据，所以用固定计划代替 LLM 规划。计划遵守 `docs/skill_finder/COVERAGE.md` 的 6–12 阶段规则：

| 阶段 id | 名称 | queries |
|---|---|---|
| topic | 选题 | 视频选题 / video topic ideas |
| script | 脚本与分镜 | 视频脚本 / video script storyboard |
| footage | 素材与拍摄 | 视频素材 / video footage b-roll |
| edit | 剪辑 | 视频剪辑 / video editing |
| voice | 配音与字幕 | 视频配音 / video voiceover subtitles |
| cover | 封面与标题 | 视频封面 / video thumbnail |
| publish | 发布与分发 | 视频发布 / video publishing |
| review | 数据复盘 | 视频数据复盘 / video analytics |

环境与可达性说明：

- GitHub `search/repositories` 匿名可达，但对这些长尾中文词**大多返回 200 + 0 条**；连续请求约 10 次/分钟后开始 **403**（实测同样 12 个检索词：10 个 200、2 个 403，见第 2.③ 节）。
- DuckDuckGo HTML 检索可达，单次约 1–3s；BEFORE 里只有第一条 query 返回了 5 行，后续 query 返回 0 行。
- `skills.sh/api/search` 可达：200 `application/json`，约 1.0s / 约 16KB，`视频制作` 返回 100 条（第 6 节）。

---

## 2. BEFORE（`HEAD` 版本：仅 GitHub，`MAX_FETCHES=80`，空阶段被删除）

运行：`/tmp/diag-before.json`，耗时 **215.1s**，`degraded=True`，抓取预算 **80/80 打满**。

### ① 实际生成的阶段（数量 + 名称）

计划 8 个阶段，`coverage["stages"]` 最终只剩 **3 个**：

```
emitted: ['topic', 'script', 'edit']
```

满足 `decompose_coverage` 的 6–12 阶段契约（8 个），但 `_filled_stages()` 把没有收录 skill 的环节整体删掉，
所以对外输出里用户看不到 `footage / voice / cover / publish / review` 这 5 个环节。

### ② 每个阶段的检索词与命中数

`_search_stage()` 使用固定模板 `f"{terms} SKILL.md site:github.com"`（web 检索）和 `f"SKILL.md {terms} fork:false"`（GitHub Search API）。
命中数 = GitHub Search API 返回的 repo 条数 / web 检索返回行数与其中可解析的 `owner/repo` 数。

| 阶段 | 轮次 | GitHub 检索词 → 命中 | web 行 → 可解析 ref |
|---|---|---|---|
| topic 选题 | 一轮 | `SKILL.md 视频选题` → 0 / `SKILL.md video topic ideas` → 0 | 5 → 5 / 0 → 0 |
| script 脚本与分镜 | 一轮 | `SKILL.md 视频脚本` → 1 | 0 → 0 |
| footage 素材与拍摄 | 一轮 | `SKILL.md 视频素材` → 0 / `SKILL.md video footage b-roll` → 0 | 0 → 0 / 0 → 0 |
| edit 剪辑 | 一轮 | `SKILL.md 视频剪辑` → 0 / `SKILL.md video editing` → 4 | 0 → 0 / 0 → 0 |
| voice 配音与字幕 | 一轮 | `SKILL.md 视频配音` → 0 / `SKILL.md video voiceover subtitles` → 0 | 0 → 0 / 0 → 0 |
| cover 封面与标题 | 一轮 | `SKILL.md 视频封面` → 0 / `SKILL.md video thumbnail` → 0 | 0 → 0 / 0 → 0 |
| publish 发布与分发 | 一轮 | `SKILL.md 视频发布` → 0 / `SKILL.md video publishing` → 1 | 0 → 0 / 0 → 0 |
| review 数据复盘 | 一轮 | `SKILL.md 视频数据复盘` → 0 / `SKILL.md video analytics` → 0 | 0 → 0 / 0 → 0 |
| topic 选题 | 补洞 | `SKILL.md 选题 claude skill OR agent skill` → 1 | 0 → 0 |
| footage 素材与拍摄 | 补洞 | `SKILL.md 素材与拍摄 claude skill OR agent skill` → 0 | 0 → 0 |
| voice 配音与字幕 | 补洞 | `SKILL.md 配音与字幕 claude skill OR agent skill` → 0 | 0 → 0 |
| cover 封面与标题 | 补洞 | `SKILL.md 封面与标题 claude skill OR agent skill` → 0 | 0 → 0 |
| publish 发布与分发 | 补洞 | `SKILL.md 发布与分发 claude skill OR agent skill` → 0 | 0 → 0 |
| review 数据复盘 | 补洞 | `SKILL.md 数据复盘 claude skill OR agent skill` → 0 | 0 → 0 |

web 检索一共只返回 5 行（全部来自第一条 query），解析出 5 个 `owner/repo`。

### ③ `degraded` 标志与抓取预算消耗

- `degraded = True`（旧实现没有原因字段，无法区分是 GitHub 失败、web 失败还是预算打满）。
- `_FetchBudget.calls = 80 / max_fetches = 80` → **预算被打满**；最后一个阶段（review）的补洞检索在预算耗尽后
  没有发出任何 HTTP 请求（记录到的 `ms = 0`），即**尾部阶段被饿死**。
- 预算耗尽后 `budget.get()` 直接 `RuntimeError("skill_search_budget")`，被 `_github_search` / `_try_get` 吞掉，
  结果是"没有命中"而不是"没有检索"，用户无法区分。
- 独立复测 GitHub Search API 的限流（同样 12 个检索词，10s 内连续请求）：

```
200 × 10（其中 8 个 0 条命中、1 个 4 条、1 个 1 条），403 × 2（'SKILL.md video analytics'、'SKILL.md 选题 claude skill OR agent skill'）
```

  即："200 + 0 条"与"403 限流"混在一起，旧实现无法分辨。真正可达的补充信号（skills.sh）完全没有被使用。

### ④ 被 `passes_quality()` 拒绝的候选与理由

旧实现只有布尔返回值，没有理由；诊断脚本记录了被判 False 的候选字段，按字段可推断原因
（`skill_path` 为空且 `skill_md` 为空 ⇒ 没有可定位的 SKILL.md）：

共 **16** 条被拒（`GitHub` 9 条、`registry` 7 条），**16/16 都是 `no_skill_md`**：

```
registry  anthropics/skills           ×7   （KNOWN_ROOTS 目录项全部归并到同一个仓库，根目录没有 SKILL.md）
GitHub    boyzen59/shi-ping-xuan-      ×2   stars=0, pushed_at 空
GitHub    Jst-Well-Dan/Skills-Hub           stars=0, 新鲜
GitHub    limmg/xingchuan-hub               stars=1, 新鲜
GitHub    alchaincyf/huashu-skills          stars=1550, 新鲜  ← 热门仓库，但根目录定位不到 SKILL.md 被拒
GitHub    Oak-Ridge-Ventures/openclip-skill stars=1, 新鲜
GitHub    krunal16-c/skillsclaw             stars=1, 陈旧
GitHub    arnans/video-pipeline-skills      stars=0, 陈旧
GitHub    ShortGenius/skills                stars=0, 陈旧
```

值得注意：`alchaincyf/huashu-skills`（1550 stars、180 天内有提交）**本该通过**质量门槛，却因为
`SKILL_PATHS = ("SKILL.md",)` 只认仓库根目录而被判 `no_skill_md`——这正是 B4/(e) 要修的问题。

### ⑤ 被丢弃的空阶段

`_filled_stages()` 把没有合格 skill 的阶段整体删除（`coverage["stages"] = filled`），
所以对外输出（snapshot / index.json / 介绍文 prompt）里看不到这些环节：

```
dropped: ['footage', 'voice', 'cover', 'publish', 'review']
```

旧行为等于"没有缺口"，与 `docs/skill_finder/COVERAGE.md`「仍为空的阶段从对外输出里删除：不要标记 `gap: true`」
一致，但用户因此看不到覆盖缺口（本次 8 个环节里有 5 个 = 62.5% 是空的）。

---

## 3. 本次改动（对应 B1 / B4）

### (b) skills.sh 源适配器（新增，与 GitHub 并列，不替换）

- `SKILLS_SH_SEARCH_URL = "https://skills.sh/api/search"`；`_skills_sh_search(query, getter, on_degrade=...)`
  通过既有注入式 `Getter` / `_try_get` 发请求（测试可完全离线伪造），URL 用 `urllib.parse.urlencode` 拼装。
- `_skills_sh_items()` 防御式解码：`skills` 不是数组 / JSON 坏掉 / payload 不是对象 → 返回 `None` → 记 `skills.sh:unparseable` 并降级，
  **绝不抛异常**；单行 `installs` 缺失或为 `None` → 记 0（`installs_missing=True`），由质量门槛判低热度而不是崩。
- 命中携带 `source="skills.sh"`、`source_id`(=API 的 `id`)、`skillId` / `name`、`installs`、以及可解析的 GitHub 仓库
  (`source` → `full_name`，例如 `genmedia-labs/skills`)，因此 SKILL.md 仍然从 GitHub 取。
- `_search_stage()` 里两个源各自用**按源分发的检索词模板** `SEARCH_TEMPLATES`：
  `github → "{terms} SKILL.md site:github.com"`（保持不变），`skills.sh → "{terms}"`（站内检索，无任何 github 域名过滤）。
- 失败（超时 / 403 / 429 / 不可达 / 解析失败）只做 `_LOG.debug` 低级别记录 + `degraded` 原因，GitHub 结果一条不丢。

### (c) 不再静默丢弃非 GitHub 命中

- `_names_from_web()` 现在返回规范化候选（`GitHub` / `skills.sh` / `web`），
  `skills.sh/<owner>/<repo>/<skill>` 会保留并带上仓库名（可继续取 SKILL.md），
  `/trending` 这类单段链接作为 `section` 保留，"其它 URL"也不再被丢掉，而是进 `rejected[]` 记 `unresolved_ref`。
- `_ingest_repo()` 的来源词表扩展为 `GitHub` / `registry` / `skills.sh`：GitHub 与 registry 仍要求 `owner/repo`，
  skills.sh 命中可以用自己的 `id`（例如 `skills.volces.com/byted-drama`）标识；无法定位 SKILL.md 的仍会被质量门槛挡住，
  但拒绝理由会被记录（可解释）。

### (d) 安装量等价的质量门槛

- `SKILLS_SH_MIN_INSTALLS = 100`（模块常量 + 注释说明理由）；`quality_reason()` 对 `source == "skills.sh"` 走
  "安装量 ≥ 100 → 通过；否则再看 stars ≥ 10 / 180 天内有提交；否则拒（`installs<100`）"。
- GitHub / registry 命中规则不变（stars ≥ 10 或 180 天内提交或登记处条目），`passes_quality()` 仍是薄封装。

### (e) SKILL.md 路径覆盖

```python
SKILL_PATHS = (
    "SKILL.md",
    ".agents/skills/{name}/SKILL.md",
    ".claude/skills/{name}/SKILL.md",
    "skills/{name}/SKILL.md",
    ".cursor/skills/{name}/SKILL.md",
)
```

成本控制：根布局仍然"一次 raw 请求"命中；嵌套布局先用**一次**仓库根 contents 列表判断
`.agents` / `.claude` / `skills` / `.cursor` 是否存在，只为真实存在的目录探测，列表不可用时退化为 raw 探测。

### (f) 预算公平 + 缺口可见

- `MAX_FETCHES: 80 → 160`（一条 query 现在最多两次源检索），`MIN_STAGE_FETCHES = 4`；
  `_fair_share()` 给每个阶段"剩余预算 ÷ 剩余阶段数"的份额，`_FetchBudget.start_stage/end_stage` 用软上限执行，
  未用完的份额自动回流给后续阶段 → 尾部阶段不再被前面的阶段吃光。
- 预算消耗可观测：`snapshot["budget"] = {max_fetches, calls, fetch_calls, search_calls, stage_quota_skips, elapsed_ms, degraded, degraded_reasons, stages:[{stage_id,name,calls}]}`，
  阶段行带 `fetches` / `searched_terms`，`payload["rejected"]` 是前 20 条带 `reason` 的拒绝记录。
- 空阶段不再删除：`_visible_stages()` 输出全部阶段，空的带 `gap: true` + `gap_reason = "这一环没有找到合格 skill"`；
  分组 `groups` 与介绍文 prompt 仍只含真正有 skill 的阶段（`_filled_stages()`），`SKILL_CAP=16` 仍然只约束真实 skill。

---

## 4. AFTER（工作区版本：GitHub + skills.sh，缺口可见）

运行：`/tmp/diag-after.json`，耗时 **240.4s**，`degraded=True`（原因 `['github:RuntimeError']`），
抓取预算 **102/160**（未打满）。

### ① 实际生成的阶段（数量 + 名称）

```
计划 8 个阶段 → 对外可见 8 个：['topic', 'script', 'footage', 'edit', 'voice', 'cover', 'publish', 'review']
本轮 0 个空环节；`gap` 标记的真实样例见 ⑤。
```

### ② 每个阶段的检索词与命中数

| 阶段 | 轮次 | GitHub 检索词 → 命中 | skills.sh 检索词 → 条数（耗时） | web 行 → ref |
|---|---|---|---|---|
| topic 选题 | 一轮 | `SKILL.md 视频选题` → 0 | `视频选题` → 10（2952ms） | 0 → 0 |
| script 脚本与分镜 | 一轮 | `SKILL.md 视频脚本` → 1 | `视频脚本` → 10（2552ms） | 0 → 0 |
| footage 素材与拍摄 | 一轮 | `SKILL.md 视频素材` → 0 | `视频素材` → 10（2192ms） | 0 → 0 |
| edit 剪辑 | 一轮 | `SKILL.md 视频剪辑` → 0 | `视频剪辑` → 10（2947ms） | 0 → 0 |
| voice 配音与字幕 | 一轮 | `SKILL.md 视频配音` → 0 | `视频配音` → 10（2067ms） | 0 → 0 |
| cover 封面与标题 | 一轮 | `SKILL.md 视频封面` → 0 | `视频封面` → 10（2751ms） | 0 → 0 |
| publish 发布与分发 | 一轮 | `SKILL.md 视频发布` → 0 | `视频发布` → 10（1906ms） | 0 → 0 |
| review 数据复盘 | 一轮 | `SKILL.md 视频数据复盘` → 0 | `视频数据复盘` → 10（2315ms） | 0 → 0 |

- skills.sh 每个检索词都返回 10 条（`SKILLS_SH_PER_QUERY` 上限），延迟 1906–2952ms。
- web 检索每轮都真实发出（`search_calls=8`），但本环境下 DuckDuckGo 连续返回 0 行（不可达/被挡），
  所以这一路的贡献为 0；GitHub Search API 对这些长尾中文词大多返回 200 + 0 条，尾部开始 403 限流。

### ③ `degraded` 标志与抓取预算消耗

- `degraded = True`，原因只有 **`github:RuntimeError`**（GitHub Search API 403 限流），skills.sh 侧全部成功；
  即：GitHub 挂掉时结果仍然成立，且原因可读。
- 预算 102/160，`stage_quota_skips=95`。
- **每个阶段都拿到了预算**（含尾部 `review`）：topic=11 / script=12 / footage=16 / edit=15 / voice=15 / cover=11 / publish=11 / review=10。
  对比 BEFORE：预算 80/80 打满后最后一阶段直接 0 次请求。

### ④ 被质量门槛拒绝的候选与理由

共 **6** 条（`snapshot["rejected"]` 暴露前 20 条），理由明确可读：

| source | source_id | installs | reason |
|---|---|---|---|
| skills.sh | skills.volces.com/byted-mediakit | 283 | `no_skill_md` |
| skills.sh | skills.volces.com/byted-kickart-marketing-material-generator | 969 | `no_skill_md` |
| skills.sh | b2b-material.bj.bcebos.com/cai-platform | 100 | `no_skill_md` |
| skills.sh | skills.volces.com/byted-las-video-edit | 382 | `no_skill_md` |
| skills.sh | aiworkskills/wechat-article-skills/aws-wechat-article-publish | 95 | `installs<100` |
| skills.sh | skills.volces.com/video-publish-all | 68 | `no_skill_md` |

`skills.volces.com` / `b2b-material.bj.bcebos.com` 这类 `source` 不是 GitHub 仓库的条目无法定位 SKILL.md → `no_skill_md`；
安装量不足 100 的条目 → `installs<100`（skills.sh 专属分支），都不会崩、也不会静默消失。

### ⑤ 空阶段（缺口可见性）

本轮 8 个环节全部收录到合格 skill，因此没有 `gap: true`。同样代码的上一轮真实运行
（`/tmp/diag-after-v2.json`，GitHub 限流更严重）出现了 3 个空环节，对外输出仍然保留它们：

| stage_id | 名称 | gap_reason | fetches |
|---|---|---|---|
| voice | 配音与字幕 | 这一环没有找到合格 skill | 5 |
| cover | 封面与标题 | 这一环没有找到合格 skill | 5 |
| review | 数据复盘 | 这一环没有找到合格 skill | 5 |

并有单测固定该契约：`test_gap_stages_stay_visible_with_marker_but_absent_from_article`（6 个阶段全部可见、5 个带
`gap_reason = 这一环没有找到合格 skill`）、`test_wide_coverage_map_still_searches_tail_stages`（12 个空阶段全部带 gap 标记）。

### 收录结果（14 条：GitHub 1、skills.sh 13）

| source | source_id | name | stage | installs | skill_path |
|---|---|---|---|---|---|
| skills.sh | op7418/youtube-clipper-skill/youtube-clipper | youtube-clipper | 选题 | 3787 | SKILL.md |
| skills.sh | pluviobyte/rnskill/ra | ra-人话 | 选题 | 1004 | SKILL.md |
| GitHub | CatherineHasAGun/absurdity | absurdity | 脚本与分镜 | 0 | SKILL.md |
| skills.sh | alchaincyf/huashu-skills/huashu-douyin-script | huashu-douyin-script | 脚本与分镜 | 955 | SKILL.md |
| skills.sh | elementsix/elementsix-skills/seedance-storyboard | seedance-storyboard | 素材与拍摄 | 847 | SKILL.md |
| skills.sh | mediastormdev/dream-to-video-skill/dream-to-video | dream-to-video | 素材与拍摄 | 225 | SKILL.md |
| skills.sh | agentchengfeng/chengfeng-videocut-skills/videocut | videocut:剪口播 | 剪辑 | 1186 | SKILL.md |
| skills.sh | yizhiyanhua-ai/media-downloader/media-downloader | media-downloader | 剪辑 | 593 | SKILL.md |
| skills.sh | cclank/lanshu-waytovideo/jianying-video-gen | jianying-video-gen | 剪辑 | 541 | SKILL.md |
| skills.sh | yzfly/douyin-mcp-server/douyin-video | douyin-video | 配音与字幕 | 1857 | SKILL.md |
| skills.sh | cat-xierluo/legal-skills/douyin-batch-download | douyin-batch-download | 配音与字幕 | 610 | SKILL.md |
| skills.sh | zrt-ai-lab/opencode-skills/auto-douyin | auto-douyin | 封面与标题 | 509 | SKILL.md |
| skills.sh | white0dew/xiaohongshuskills/redbookskills | redbookskills | 发布与分发 | 450 | SKILL.md |
| skills.sh | wind-alice/alicemarket/post-market-debrief | post-market-debrief | 数据复盘 | 670 | SKILL.md |

`skill_path` 全部是 `SKILL.md`（这些仓库的根布局），说明新加入的嵌套布局没有引入额外开销，只在根布局失败时才启用。


---

## 5. BEFORE / AFTER 对比

| 维度 | BEFORE | AFTER |
|---|---|---|
| 计划阶段 / 可见阶段 | 8 / 3（丢 5 个） | 8 / 8（另一次真实运行 8 / 8，其中 3 个带 gap 标记） |
| 收录 skill | 3（GitHub 3） | 14（skills.sh 13、GitHub 1） |
| 每个阶段都检索到 | 否（`review` 0 次请求） | 是（每阶段 10–16 次请求） |
| 抓取预算 | 80/80 打满后饿死尾部 | 102/160，公平切片，另有 95 次"阶段份额用完"跳过 |
| `degraded` 原因 | 只有 `True`，无法归因 | `['github:RuntimeError']`（skills.sh 正常；GitHub 403 限流） |
| 质量门槛拒绝理由 | 不可见（布尔） | `rejected[]`：6 条，`no_skill_md` / `installs<100` |
| 空阶段 | 静默删除 | `gap: true` + `gap_reason` |
| 质量门槛 | 仅 stars/freshness | skills.sh 用安装量（≥100）；GitHub 规则不变 |
| SKILL.md 布局 | 仅根目录 | 根 + `.agents/skills/` + `.claude/skills/` + `skills/` + `.cursor/skills/` |

结论：B1（检索面拓宽到 skills.sh）与 B4（缺口可见）都已在真实网络下生效；
BEFORE 里 8 个环节有 5 个（62.5%）是空且不可见的，AFTER 同一需求下 8 个环节全部命中，且 GitHub 限流时 skills.sh 独立撑住结果，
失败原因、预算消耗、被拒候选全部可观测。

### AFTER 运行中观察到、并据此调整的两点

1. `skills.sh` 在连续请求下会变慢：第一版 10s 超时把 13 次检索里的 9 次直接掐断（响应 10.7–12.2s）。
   已把 `_default_skills_sh_get` 默认超时提到 **20s**，之后 8/8 次检索全部成功（1.9–3.0s）。
2. 三个检索通道现在**先全部发检索请求、再统一做候选抓取**：否则一次 skills.sh 的 10 条候选抓取
   会把同阶段的 web 检索挤掉（实测 `search_calls=0`）。调整后 `search_calls=8`，三个通道都真实发出。


---

## 6. 决策记录：为什么用 skills.sh 的免费公开端点（D6）

- **选用的端点**：`GET https://skills.sh/api/search?q=<urlencoded>`（200，`application/json`）。
- **为什么**：免费 + 匿名（无 key、无账号、无付费）；skills.sh 官方没有文档化/计价的 API
  （第三方 `mastra-ai/skills-api` 的存在正说明这一点），而该端点的返回足以做覆盖检索：
  实测单次约 1.0–3.0s、约 16KB、`视频制作` 返回 100 条真实中文视频制作 skill；连续 8 次快速请求全部 200，未见限流。
- **不用的端点**：`GET /api/skill/<id>` → 401 `{"error":"Unauthorized"}`，不用。
- **降级策略**：任何超时 / 403 / 429 / 不可达 / JSON 解析失败 / `skills` 非数组 →
  仅 `_LOG.debug` 记录 + 设置 `degraded=True` 并写入 `degraded_reasons`（如 `skills.sh:RuntimeError`、`skills.sh:unparseable`），
  **保留全部 GitHub 结果**，skills.sh 侧返回空列表而非抛出；预算与 GitHub 共用同一 `_FetchBudget`（含公平切片）。

---

## 7. 需要其他工作流配合的改动（本工作流无权修改的文件）

### 7.1 `docs/skill_finder/COVERAGE.md:35`（覆盖地图提示词，与本版行为冲突）

现状：

```
仍为空的阶段从对外输出里删除：不要标记 `gap: true`，不要发明「缺口 skill」，不要在介绍文里写缺失阶段。展示给用户的 coverage 只含已收录 skill 的阶段。
```

B4 之后空阶段必须可见（`gap: true` + `gap_reason`），介绍文 prompt 仍然只含已收录阶段（`_filled_stages()`，这一条不变）。
建议把该行替换为：

```
仍为空的阶段必须保留在对外输出里：标记 `gap: true` 并给出短原因（默认「这一环没有找到合格 skill」），不要发明「缺口 skill」，不要在介绍文里写缺失阶段。展示给用户的 coverage 含全部阶段，其中 `gap: true` 表示这一环没找到合格 skill。
```

### 7.2 `src/autoposting/web.py` 相关的 3 个既有测试失败（非本工作流引入）

`tests/test_skill_finder.py::SkillFinderApiTests` 里 3 个断言 `INDEX_HTML` 的用例当前失败，
它们只读 `web.py` 的 HTML，与本工作流的改动无关（HEAD 的 `web.py` 满足它们，工作区的 `web.py` 正在被并行改写）：

| 用例 | 期望 | 工作区现状 |
|---|---|---|
| `test_skills_lane_shows_studio_gallery_and_autorender` | `searchSkills()` 里出现 `skills_ready` | 已改为 `skillArticleReady(data)`，无 `skills_ready` 字面量 |
| `test_poll_skill_snapshot_continues_seven_search_phases` | `pollSkillSnapshot()` 里出现 `renderSkillImages()` | 已改为 `queueSkillImagesForRender()` |
| `test_skills_lane_keeps_article_summary_above_gallery` | `#skills-article` 在 `#skills-gallery` 之前 | 工作区 `web.py:1049` gallery、`web.py:1050` article（HEAD 是 1024 article / 1033 gallery） |

好消息：工作区 `web.py:4147` 已经在渲染 `stage.gap` / `gap_reason`（`gapReason()` 读 `gap_reason` / `reason` / `note`），
与本版 snapshot 契约完全一致，不需要为本工作流再改前端。

### 7.3 本工作流顺带修好的跨模块契约（都在 `skill_finder.py` 内）

- `render_skill_package(..., copy_writer=...)`：`web.py:5811`（B5）已经在传，之前会 `TypeError: unexpected keyword argument 'copy_writer'`；
  现已支持（`copy_writer(repos) -> repos`，`LLMError` 时回退模板文案，与 weekly 同策略）。
- 删除了 `render_skill_package(..., generate_image=...)`：`visual/weekly.render_weekly_visual` 已不接受该参数，
  保留会在生产路径（`visual_renderer is None`）抛 `TypeError`；`web.py` 也不再传它。

---

## 附录 A：诊断脚本全文（`/tmp/skill_finder_diag.py`）

```python
"""Reproducible coverage diagnostic for the skill finder.

Runs a REAL search for 视频制作 through src/autoposting/skill_finder.py using the
production HTTP path (GitHub API + DuckDuckGo web search + skills.sh when present)
and records ①–⑤ required by docs/skill_finder/COVERAGE_DIAGNOSTIC_1.3.0.md.

Usage:
    cd /Users/youngi/Documents/MiniWork/AutoPosting-development
    PYTHONPATH=src python3 /tmp/skill_finder_diag.py before /tmp/diag-before.json [max_fetches]
    PYTHONPATH=src python3 /tmp/skill_finder_diag.py after  /tmp/diag-after.json  [max_fetches]

The only non-network part is the planner: `complete` is fixed to the 8-stage
coverage map below (COVERAGE.md shape, 6–12 stages) because no model credential is
configured in this environment. Every search/fetch call is real.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.environ.get("SKILL_FINDER_SRC", "src"))

from autoposting import skill_finder as sf  # noqa: E402

PLAN = {
    "goal": "从零做出一支可发布的视频（选题到复盘）",
    "title": "从零做视频技能包",
    "theme": "视频制作从选题到复盘",
    "chain": [
        {"id": "c1", "name": "选题", "role": "入口"},
        {"id": "c2", "name": "脚本与分镜", "role": "准备"},
        {"id": "c3", "name": "素材与拍摄", "role": "制作"},
        {"id": "c4", "name": "剪辑", "role": "制作"},
        {"id": "c5", "name": "配音与字幕", "role": "装配"},
        {"id": "c6", "name": "封面与标题", "role": "装配"},
        {"id": "c7", "name": "发布与分发", "role": "交付"},
        {"id": "c8", "name": "数据复盘", "role": "交付"},
    ],
    "stages": [
        {"id": "topic", "name": "选题", "queries": ["视频选题", "video topic ideas"], "required": True},
        {"id": "script", "name": "脚本与分镜", "queries": ["视频脚本", "video script storyboard"], "required": True},
        {"id": "footage", "name": "素材与拍摄", "queries": ["视频素材", "video footage b-roll"], "required": True},
        {"id": "edit", "name": "剪辑", "queries": ["视频剪辑", "video editing"], "required": True},
        {"id": "voice", "name": "配音与字幕", "queries": ["视频配音", "video voiceover subtitles"], "required": True},
        {"id": "cover", "name": "封面与标题", "queries": ["视频封面", "video thumbnail"], "required": True},
        {"id": "publish", "name": "发布与分发", "queries": ["视频发布", "video publishing"], "required": True},
        {"id": "review", "name": "数据复盘", "queries": ["视频数据复盘", "video analytics"], "required": True},
    ],
}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "before"
    out_path = Path(sys.argv[2] if len(sys.argv) > 2 else f"/tmp/diag-{mode}.json")
    max_fetches = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    query = "视频制作"

    calls: list[dict[str, object]] = []
    stage_rows: list[dict[str, object]] = []
    rejections: list[dict[str, object]] = []
    budgets: list[object] = []

    def complete(prompt: str) -> str:
        if "打包结果" in prompt:
            return json.dumps(
                {"title": "从零做视频技能包", "body": "视频制作全流程。\nAIGC"},
                ensure_ascii=False,
            )
        return json.dumps(PLAN, ensure_ascii=False)

    orig_github_search = sf._github_search

    def github_search(terms, getter):
        t0 = time.monotonic()
        items = orig_github_search(terms, getter)
        calls.append(
            {
                "source": "github-search",
                "terms": terms,
                "hits": len(items),
                "ms": int((time.monotonic() - t0) * 1000),
            }
        )
        return items

    sf._github_search = github_search

    skills_sh_search = getattr(sf, "_skills_sh_search", None)
    if skills_sh_search is not None:

        def wrapped_skills_sh(terms, getter, **kwargs):
            t0 = time.monotonic()
            items = skills_sh_search(terms, getter, **kwargs)
            calls.append(
                {
                    "source": "skills.sh",
                    "terms": terms,
                    "hits": len(items),
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            )
            return items

        sf._skills_sh_search = wrapped_skills_sh

    orig_quality_fn = getattr(sf, "quality_reason", None)
    orig_passes_fn = sf.passes_quality

    def quality(hit, now=None):
        if orig_quality_fn is not None:
            ok, reason = orig_quality_fn(hit, now=now)
        else:
            ok = orig_passes_fn(hit, now=now)
            reason = "passes" if ok else "rejected"
        if not ok:
            rejections.append(
                {
                    "source": str(hit.get("source") or ""),
                    "source_id": str(hit.get("source_id") or ""),
                    "stars": int(hit.get("stars") or 0),
                    "installs": int(hit.get("installs") or 0),
                    "skill_path": str(hit.get("skill_path") or ""),
                    "skill_md_len": len(str(hit.get("skill_md") or "")),
                    "pushed_at": str(hit.get("pushed_at") or ""),
                    "reason": reason,
                }
            )
        return ok, reason

    sf.passes_quality = lambda hit, now=None: quality(hit, now=now)[0]
    if orig_quality_fn is not None:
        sf.quality_reason = quality

    orig_budget = sf._FetchBudget

    class RecordingBudget(orig_budget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            budgets.append(self)

    sf._FetchBudget = RecordingBudget

    orig_names = sf._names_from_web

    def names(results):
        out = orig_names(results)
        calls.append(
            {
                "source": "web-refs",
                "terms": "(extracted)",
                "hits": len(out),
                "rows": len(results) if isinstance(results, list) else -1,
                "sources": sorted({str(item.get("source") if isinstance(item, dict) else item) for item in out}),
            }
        )
        return out

    sf._names_from_web = names

    orig_search_stage = sf._search_stage

    def search_stage(stage, getter, searcher, collected, seen, now, **kwargs):
        row = {
            "stage_id": str(stage.get("id")),
            "name": str(stage.get("name")),
            "queries": list(stage.get("queries") or []),
            "second_pass": bool(kwargs.get("second_pass")),
            "collected_before": len(collected),
            "call_index_before": len(calls),
        }
        stage_rows.append(row)
        orig_search_stage(stage, getter, searcher, collected, seen, now, **kwargs)
        row["collected_after"] = len(collected)
        row["calls"] = calls[row["call_index_before"] :]

    sf._search_stage = search_stage

    t0 = time.monotonic()
    error = ""
    result: dict[str, object] = {}
    try:
        result = sf.find_skills(
            query,
            complete=complete,
            github_get=None,
            web_search=None,
            now=datetime.now(timezone.utc),
            output_root=Path(f"/tmp/diag-{mode}/skills"),
            max_fetches=max_fetches,
            fetch_deadline_seconds=240,
        )
    except Exception as exc:  # diagnostic must still report partial evidence
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    elapsed = round(time.monotonic() - t0, 1)

    budget = budgets[-1] if budgets else None
    report = {
        "mode": mode,
        "query": query,
        "elapsed_s": elapsed,
        "max_fetches": max_fetches,
        "error": error,
        "plan_stages": [stage["id"] for stage in PLAN["stages"]],
        "emitted_stage_ids": [str(stage.get("id")) for stage in (result.get("stages") or [])],
        "emitted_stages": result.get("stages") or [],
        "skill_count": len(result.get("skills") or []),
        "skills": [
            {
                "source": skill.get("source"),
                "source_id": skill.get("source_id"),
                "name": skill.get("name"),
                "stage_id": skill.get("stage_id"),
                "stage_name": skill.get("stage_name"),
                "skill_path": skill.get("skill_path"),
                "installs": skill.get("installs"),
                "url": skill.get("url"),
            }
            for skill in (result.get("skills") or [])
        ],
        "degraded": bool(result.get("degraded")),
        "degraded_reasons": result.get("degraded_reasons") or result.get("degraded_reason") or [],
        "budget_report": getattr(budget, "report", lambda: None)() if budget is not None else None,
        "budget_calls": getattr(budget, "calls", None) if budget is not None else None,
        "stage_rows": stage_rows,
        "calls": calls,
        "rejections": rejections[:80],
        "rejection_count": len(rejections),
        "groups": result.get("groups") or [],
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[{mode}] elapsed={elapsed}s error={error!r} degraded={report['degraded']}")
    print(f"[{mode}] plan stages={len(PLAN['stages'])} emitted={len(report['emitted_stage_ids'])} -> {report['emitted_stage_ids']}")
    print(f"[{mode}] skills={report['skill_count']} budget={report['budget_report']}")
    for row in result.get("skills") or []:
        print(f"[{mode}]   {row.get('source')} | {row.get('source_id')} | {row.get('stage_name')} | {row.get('skill_path')}")
    print(f"[{mode}] calls:")
    for call in calls:
        print(f"[{mode}]   {call}")
    print(f"[{mode}] rejections={report['rejection_count']}")
    print(f"[{mode}] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

```

## 附录 B：验收脚本全文（`/tmp/skill_finder_verify.py`）

```python
"""Verification #2 for the skill-finder skills.sh workstream.

Two modes, both real network for GitHub:

    PYTHONPATH=src python3 /tmp/skill_finder_verify.py real
        A small real 视频制作 run; prints every collected row and asserts at least
        one has source == "skills.sh".

    PYTHONPATH=src python3 /tmp/skill_finder_verify.py degrade
        Same run but the skills.sh adapter is pointed at an unreachable URL and
        then at a URL that answers a real HTTP error; asserts the GitHub rows
        survive and the snapshot is degraded with a skills.sh reason.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "src")

from autoposting import skill_finder as sf  # noqa: E402

PLAN = {
    "goal": "从零做出一支可发布的视频",
    "title": "视频制作技能包",
    "theme": "视频制作",
    "chain": [{"id": "c1", "name": "剪辑", "role": "制作"}],
    # decompose_coverage enforces COVERAGE.md's 6-12 stage contract.
    "stages": [
        {"id": "pick", "name": "选题", "queries": ["视频选题"], "required": True},
        {"id": "draft", "name": "脚本", "queries": ["视频脚本"], "required": True},
        {"id": "shoot", "name": "素材", "queries": ["视频素材"], "required": True},
        {"id": "edit", "name": "剪辑", "queries": ["视频剪辑"], "required": True},
        {"id": "voice", "name": "配音", "queries": ["视频配音"], "required": True},
        {"id": "ship", "name": "发布", "queries": ["视频发布"], "required": True},
    ],
}


def complete(prompt: str) -> str:
    if "打包结果" in prompt:
        return json.dumps({"title": "视频制作技能包", "body": "视频制作流程。\nAIGC"}, ensure_ascii=False)
    return json.dumps(PLAN, ensure_ascii=False)


def run(label: str, out: str, max_fetches: int = 60) -> dict[str, object]:
    t0 = time.monotonic()
    result = sf.find_skills(
        "视频制作",
        complete=complete,
        github_get=None,  # real default HTTP path
        web_search=None,  # real DuckDuckGo path
        now=datetime.now(timezone.utc),
        output_root=Path(f"/tmp/verify-{out}/skills"),
        max_fetches=max_fetches,
        fetch_deadline_seconds=150,
    )
    elapsed = round(time.monotonic() - t0, 1)
    print(f"\n===== {label} =====")
    print(f"elapsed={elapsed}s degraded={result['degraded']} reasons={result.get('degraded_reasons')}")
    print(f"budget={json.dumps({k: v for k, v in result['budget'].items() if k != 'stages'}, ensure_ascii=False)}")
    print(f"stages={[(s['id'], s.get('gap'), s.get('fetches')) for s in result['stages']]}")
    print(f"skills={len(result['skills'])} rejected={len(result.get('rejected') or [])}")
    for row in result["skills"]:
        print(
            "  ROW source={source} source_id={source_id} name={name} installs={installs} "
            "stage={stage_name} path={skill_path}".format(**row)
        )
    return result


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "real"
    if mode == "real":
        result = run("real 视频制作 search (GitHub + skills.sh + web)", "real")
        sh_rows = [row for row in result["skills"] if row["source"] == "skills.sh"]
        print(f"\nskills.sh rows: {len(sh_rows)}")
        assert sh_rows, "expected at least one row with source == 'skills.sh'"
        print("ASSERT OK: at least one source == 'skills.sh' row")
        return 0

    if mode == "degrade":
        original = sf.SKILLS_SH_SEARCH_URL
        try:
            # 1) unreachable: nothing is listening on port 9 (discard)
            sf.SKILLS_SH_SEARCH_URL = "http://127.0.0.1:9/api/search"
            unreachable = run("degrade: skills.sh URL unreachable (connection refused)", "unreachable")
            assert unreachable["degraded"], "unreachable skills.sh must degrade"
            assert any("skills.sh" in str(r) for r in unreachable["degraded_reasons"]), unreachable[
                "degraded_reasons"
            ]
            github_rows = [row for row in unreachable["skills"] if row["source"] == "GitHub"]
            print(f"GitHub rows surviving the skills.sh outage: {len(github_rows)}")
            assert github_rows, "GitHub results must survive a skills.sh outage"

            # 2) real HTTP error: /api/skill/<id> answers 401 (the documented dead end)
            sf.SKILLS_SH_SEARCH_URL = "https://skills.sh/api/skill"
            errored = run("degrade: skills.sh URL answers a real HTTP error (401)", "http-error")
            assert errored["degraded"], "HTTP error from skills.sh must degrade"
            assert any("skills.sh" in str(r) for r in errored["degraded_reasons"]), errored[
                "degraded_reasons"
            ]
            assert [row for row in errored["skills"] if row["source"] == "GitHub"], (
                "GitHub results must survive a skills.sh HTTP error"
            )
            print("\nASSERT OK: degraded with skills.sh reason, GitHub rows survived in both cases")
            return 0
        finally:
            sf.SKILLS_SH_SEARCH_URL = original

    raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())

```
