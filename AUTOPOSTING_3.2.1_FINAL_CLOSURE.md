# AutoPosting 3.2.1 — Iteration 2 Final Closure

> 日期：2026-09-23
> 当前版本：AutoPosting `3.2.0`
> 任务性质：**Iteration 2 Final Closure / 两轮重构最终收口**
> 本任务不是 Iteration 3，不新增新的产品主线。
> 执行基线：`README.md`、`AUTPOSTING_UPDATE_DEVELOPMENT_GUIDE.md`、`AUTPOSTING_TWO_ITERATION_REFACTOR_PLAN_2026-09-22.md`、`docs/PATCH_3.2.0.md`、当前 git 状态。

---

# 0. Control Position

```text
Project: AutoPosting
Current Version: 3.2.0
Target Version: 3.2.1
Refactor Program: Two-Iteration Refactor
Iteration 1: COMPLETED
Iteration 2 Main Body: COMPLETED
Current Task: FINAL CLOSURE
Expected Result: REFACTOR CLOSED
```

本任务完成后：

> **AutoPosting 两轮重构正式结束，不再继续拆分为 Iteration 3 / 4。**

后续新增能力恢复为正常产品迭代，不再继续以本次重构名义扩展。

---

# 1. 当前已完成状态

3.2.0 已完成以下主体：

- Resource Discovery 统一资源发现域；
- GitHub / Skill / Web 三类 Adapter；
- 两轮 Decision / Jev 判断；
- `github_weekly` preset；
- DB `user_version 4` 与 `resource_sets`；
- 新资源页；
- 新创作页；
- 新发布页；
- 旧四栏目主 UI 退场；
- 新 IA；
- GitHub Weekly 从一级产品退为资源能力；
- Skill Finder 从一级产品退为资源能力；
- 发布中心接入真实草稿 / 审批 / 发布 / 调度；
- 真实浏览器验收通过；
- 全量测试通过。

3.2.0 当前唯一需要继续处理的主体遗留为：

1. 文章生成仍然存在旧 `hooks/find` / `/api/related-search` 素材链依赖；
2. lane 时代的部分隐藏 Skill 渲染 DOM 仍被内部写入；
3. 少量 lane compatibility 仍同时承担产品逻辑与兼容语义，需要分离。

以下内容 **不属于本次 Final Closure**：

- 资源候选直接出图；
- 新增新的 Search Adapter；
- 新增更多 Subscription Source；
- 调整 Jev 排序权重；
- Topic Dossier 关系图 / 时间线高级可视化；
- 新视觉主题；
- 新发布平台；
- UI 美化类非阻塞优化。

---

# 2. 本次核心目标

本任务只解决两个问题。

## Goal A — Article Generation 真正消费 Topic Dossier

新的正式链路必须成为：

```text
Information Discovery
        ↓
Event Cluster
        ↓
Topic Dossier
        ↓
Creation
        ↓
Article Generation
```

而不能继续存在：

```text
Topic Dossier
        ↓
Creation
        ↓
hooks/find
        ↓
related-search
        ↓
重新找一轮素材
```

文章生成不应再在内部偷偷开启另一套研究流程。

## Goal B — 清理两轮重构已被替代的 Legacy

本次必须完成：

- `hooks/find` 产品链退场；
- `/api/related-search` 产品链退场；
- lane 产品 / UI 机制清场；
- Skill Finder 隐藏 DOM 写入目标迁移或删除；
- 对应重复 JS / CSS / handler / tests / README 文案清理。

原则：

> **删除业务实现，保留真正必要的兼容面。**

禁止为了“保险”继续永久保留两套业务算法。

---

# 3. 执行前要求

Agent 开始修改前必须：

1. 阅读：
   - `README.md`
   - `AUTPOSTING_UPDATE_DEVELOPMENT_GUIDE.md`
   - `AUTPOSTING_TWO_ITERATION_REFACTOR_PLAN_2026-09-22.md`
   - `docs/PATCH_3.2.0.md`
2. 检查当前分支、`git status`、`origin/main`、当前最新 commit，以及 3.2.0 是否完整存在于当前工作区。
3. 首先做一次静态调用扫描，确认以下对象的真实调用者：

```text
hooks/find
/api/related-search
find_hooks
related_search
lane
body.lane-*
#skill-search-row
#skill-source-bar
#skill-degraded-note
#skill-list
```

4. 在代码修改前记录哪些属于真实业务调用、哪些只属于测试、哪些属于 CLI / external import compatibility、哪些属于旧 UI、哪些属于 persisted payload compatibility。

不要先删再猜。

---

# 4. Article Input Contract

本轮需要建立或明确一个统一的文章生成输入契约，建议语义：

```text
ArticleGenerationContext
```

至少应覆盖：

```text
source_type
primary_subject
research_summary
evidence[]
source_refs[]
selected_images[]
resource_refs[]
metadata
```

字段名称根据当前项目实际 contract 调整，不要求机械照抄。

## 4.1 source_type

至少允许：

```text
topic_dossier
resource_set
manual
```

若现有架构已有等价概念，复用，不要再造第二套。

## 4.2 Topic Dossier → ArticleGenerationContext

Topic Dossier 必须能稳定映射为文章生成上下文，至少包含：

- 主题标题；
- 主题摘要；
- 主要 Event Cluster；
- Evidence Source；
- Evidence relation；
- 来源信息；
- 已选择图片；
- 用户最终选择保留的研究内容。

禁止文章生成阶段重新调用旧“找切口 / 相关检索”。

## 4.3 Resource Set → ArticleGenerationContext

如果当前 Creation 已支持 Resource Set，则保持已有正式链路。

若当前 Resource Set 只是通过旧 skill / weekly bridge 进入文章，允许本次只做最小必要适配，不扩新产品能力。

本次主要目标仍是：

> **修复 Topic Dossier → Article。**

---

# 5. Article Service 职责边界

Article Service 负责：

```text
接收已整理好的研究上下文
    ↓
组装事实 / 来源 / 写作上下文
    ↓
调用文章模型
    ↓
验证文章结果
    ↓
生成 Draft
```

Article Service 不再负责：

- 自动找切口；
- 自动调用旧 related search；
- 再次自行扩展主题；
- 自己维护第二套研究证据；
- 在文章生成流程中直接做搜索编排。

---

# 6. 研究职责统一

最终唯一正式研究能力为：

```text
Research Service
        ↓
Topic Dossier
```

其中：

- SearchPlan 由 Planner 产生；
- Search Adapter 获取候选；
- Jev / Decision Service 负责判断；
- Evidence Source 落入 Topic Dossier；
- Creation 只消费结果。

禁止：

```text
Research Service
+
Article 内部隐藏 Research
```

两套并存。

---

# 7. hooks/find 清理策略

当前状态：

```text
POST /api/hooks/find
```

已经被产品层弃用，但文章素材链仍在使用。

本次处理顺序：

```text
先完成 Topic Dossier → Article
        ↓
替换全部正常业务调用
        ↓
扫描调用者
        ↓
确认无真实兼容依赖
        ↓
删除 hooks/find
```

## 7.1 删除对象

若确认无真实外部兼容依赖，应删除：

- HTTP route；
- handler；
- service / helper；
- 对应 JS；
- UI contract；
- tests；
- README API 文档；
- WEB_FUNCTIONAL_TEST 旧路径；
- PATCH / 注释中将其描述为正式能力的内容。

## 7.2 如果存在 external import compatibility

如果静态扫描确认某函数仍是明确公共 Python import 面，不要保留旧业务实现。

改成：

```text
deprecated thin façade
        ↓
new Research / Topic Dossier service
```

并在 Completion Report 中列出：

- 保留对象；
- 调用者；
- 为什么不能本次删除；
- 何时允许删除。

禁止：

> “可能有人用，所以先全部留着。”

必须有可证明的真实兼容理由。

---

# 8. /api/related-search 清理策略

处理逻辑与 `hooks/find` 相同。

顺序：

```text
Topic Dossier 正式承接关联研究
        ↓
Article 不再依赖
        ↓
确认 Web / JS / CLI / tests / docs 无真实调用
        ↓
删除 route
```

本任务结束后：

> 产品中不再存在“相关检索”作为独立概念。

关联搜索只是 Research Service 内部能力。

---

# 9. lane 清理策略

不要粗暴全局删除 `lane`。

必须先将 lane 分为三类。

## 9.1 A 类：旧 UI / 产品 lane

例如：

- `body.lane-*`
- 旧栏目显示 / 隐藏逻辑；
- lane 决定 Skill / Weekly / Hotspot / Viewpoint 页面；
- 旧 JS routing。

### 处理

**删除。**

新的 IA 已经不依赖 lane。

## 9.2 B 类：旧 payload compatibility

如果历史持久化数据中仍含 `lane`，而读取旧数据需要识别：

### 处理

允许保留兼容 parser。

但：

- 不允许 lane 继续参与新的业务路由；
- 不允许新的持久化对象继续依赖 lane；
- 不允许新 UI 根据 lane 工作。

## 9.3 C 类：CLI / external API compatibility

如果 CLI / external import 仍明确接收 lane：

可以保留薄 compatibility translation：

```text
legacy lane
    ↓
translate
    ↓
new source_type / resource_type / page contract
```

不得保留旧 lane 业务算法。

---

# 10. Skill Finder 隐藏 DOM 清理

当前仍存在：

```text
#skill-search-row
#skill-source-bar
#skill-degraded-note
#skill-list
```

它们已经不是用户可见产品入口，但仍作为技能包渲染写入目标。

本次必须解决这个结构债。

## 10.1 正确方向

Visual / Skill Package 需要的数据，应由：

```text
Resource Set / Skill Package Contract
        ↓
Visual Service
```

直接传递。

不要继续依赖隐藏 DOM 作为中间状态容器。

## 10.2 本次处理

优先：

1. 找到真实写入逻辑；
2. 明确哪些数据真正用于 Skill 海报、介绍文、来源说明、degraded note；
3. 将数据改为正式 JS state / server response / render contract；
4. 修改 visual / package 调用；
5. 删除隐藏 DOM；
6. 删除对应 CSS；
7. 更新 tests。

禁止：

> 把 hidden DOM 改名后继续隐藏使用。

---

# 11. 不新增第三套前端状态系统

如果前端目前已有统一 state / data model，复用。

如果没有，也只做本次功能所需的最小状态对象。

不要为了删除 hidden DOM 顺手引入：

- Redux；
- 新前端框架；
- 新状态库；
- 大规模前端迁移。

---

# 12. API 兼容与删除原则

本轮允许：

```text
删除已经完成迁移、无真实调用者的旧 API
```

这次不再要求“再 deprecated 一个版本”。

因为：

- 第一轮已经完成 deprecated；
- 第二轮主体已经完成新业务；
- 本任务就是到期清理。

## 12.1 删除前必须搜索

至少检查：

```text
src/
tests/
docs/
README.md
AGENTS.md
config/
scripts/
CLI
Web JS
external import surface
```

## 12.2 删除后必须确保

旧 API：

- 不出现在推荐文档；
- 不出现在 UI；
- 不出现在 JS；
- 不出现在正常测试；
- 不存在第二套业务实现。

---

# 13. 数据迁移

本任务原则上 **不应新增 DB schema**。

如果 ArticleGenerationContext 仅作为运行时 contract：

不要为了形式完整新增表。

如果 Topic Dossier → Draft 现有持久化确实缺少必要引用字段，并且无法安全复用，才允许做最小 migration。

如发生：

- 必须使用现有 `PRAGMA user_version`；
- 事务迁移；
- 幂等；
- 有迁移测试；
- 失败不推进版本；
- 不破坏 3.2.0 数据。

---

# 14. Jev / Planner

本次不改变：

- Jev 权重；
- Jev rubric；
- SearchPlan 设计；
- Planner provider 行为；
- Resource Judge。

只允许为了 Topic Dossier → Article 传递上下文做必要适配。

不要借本次 Final Closure 重新调模型效果。

---

# 15. Settings

3.2.0 已完成所有 API / Provider / Token 配置集中化。

本轮不得：

- 把配置重新塞回 Creation；
- 把配置重新塞回 Resource；
- 把配置重新塞回 Information；
- 增加临时 token 输入框。

如果迁移旧 JS 时发现残留配置入口：

直接清理。

---

# 16. UI 最终目标

本轮完成后，产品一级导航保持：

```text
首页
资讯
资源
创作
发布

信息源
运行记录
设置
```

禁止恢复：

```text
热点候选
观点候选
GitHub 周榜
Skill Finder
```

作为一级页面。

---

# 17. Creation 页面验收

至少完成以下真实行为。

## Case 1 — Topic Dossier → Article

```text
资讯
→ 选择事件
→ 研究
→ 得到 Topic Dossier
→ 进入创作
→ 生成文章
```

确认：

- 不调用 `hooks/find`；
- 不调用 `/api/related-search`；
- 文章使用 Dossier evidence；
- 来源 ID / 引用仍合法；
- AIGC 与现有文章校验不退化。

## Case 2 — Existing Draft

```text
发布页 / 创作页
→ 打开已有草稿
→ 编辑
→ 保存
```

不能因为新 Context contract 破坏旧草稿。

## Case 3 — Manual

如果当前产品支持手工创作输入：

继续可用。

不要强制所有文章都必须来自 Topic Dossier。

---

# 18. Publishing 不得改变

本轮不改发布业务。

必须保持：

```text
approve
→ publish
→ schedule
```

现有安全边界不变：

- WeChat live 只写公众号草稿；
- 不调用 `freepublish`；
- Xiaohongshu 继续 `manual_required`；
- secret 不泄漏；
- scheduler 行为不变化。

如果 Article contract 改动影响 Draft：

必须执行 Publishing regression。

---

# 19. 测试要求

## 19.1 Targeted — Research → Article

必须新增 / 更新：

- Topic Dossier → ArticleGenerationContext；
- dossier with official evidence；
- dossier with community evidence；
- dossier with mixed evidence；
- dossier empty / invalid；
- evidence missing required field；
- source refs；
- image refs；
- article generation success；
- article model failure；
- no second search side effect。

必须有测试明确断言：

> Article generation 不调用旧 hooks / related-search。

## 19.2 Legacy deletion

必须验证：

- `/api/hooks/find` 已删除或明确仅剩有证明的 external façade；
- `/api/related-search` 已删除；
- 正常 JS 不引用；
- README 不推荐；
- Web functional test 不再调用。

## 19.3 Skill rendering

必须验证：

- 无 `#skill-search-row` 依赖；
- 无 `#skill-source-bar` 依赖；
- 无 `#skill-degraded-note` 依赖；
- 无 `#skill-list` 依赖；
- Skill 海报仍可生成；
- Skill package 数据正确；
- degraded 信息仍能正确显示在正式 UI / contract 中。

## 19.4 lane

必须验证：

- `[data-lane]` 仍为 0；
- `body.lane-*` 不参与新 UI；
- 新业务对象不写入 lane；
- 如果保留旧 payload compatibility，旧数据仍能读取；
- CLI compatibility（如存在）仍可用。

## 19.5 Full Regression

执行：

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

然后：

```bash
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
```

然后：

```bash
git diff --check
```

以及必要的：

- `py_compile`；
- secret scan；
- migration tests（如本次涉及 schema）。

---

# 20. 真实 UI 验收

必须启动真实本地服务并使用真实浏览器或当前项目已有 faithful browser runtime。

至少验证：

1. 首页正常；
2. 资讯页正常；
3. Topic Dossier 可进入；
4. Dossier 可进入 Creation；
5. Article 成功生成；
6. Network / server diagnostics 中无 `hooks/find`；
7. Network / server diagnostics 中无 `/api/related-search`；
8. Resource 页面正常；
9. Skill Package / Skill 海报正常；
10. Creation 页面无隐藏旧 Skill Finder 产品 UI；
11. Publishing 页面正常；
12. Settings 正常；
13. 浏览器刷新后关键状态符合现有 contract；
14. JS 无 runtime error。

---

# 21. 静态清场检查

最终必须执行一次全项目检索。

目标关键词：

```text
hooks/find
related-search
find_hooks
related_search
热点候选
观点候选
Skill查找器
GitHub 周榜
legacy-lane-tabs
legacy-ui-contract
body.lane-
skill-search-row
skill-source-bar
skill-degraded-note
skill-list
```

注意：

`GitHub 周榜` 作为 **Preset / Studio 名称** 可以合法存在。

`Skill` 作为 **Resource Type / Skill Package / Visual Studio** 可以合法存在。

不要因为关键词命中就机械删除。

需要判断：

> 它是旧一级产品语义，还是新架构中的合法能力？

---

# 22. 文档更新

本轮至少更新：

- `README.md`
- `docs/WEB_FUNCTIONAL_TEST.md`
- `docs/PATCH_3.2.1.md`
- 版本号相关文件

README 必须明确：

```text
Two-Iteration Refactor: CLOSED
```

同时移除：

- `hooks/find` 推荐说明；
- `/api/related-search` 推荐说明；
- 旧 lane UI；
- 隐藏 Skill Finder UI 作为产品能力的说明。

---

# 23. 版本

目标版本：

```text
3.2.1
```

不要升级成：

```text
3.3.0
4.0.0
```

本次是收口补丁，不是新功能大版本。

---

# 24. Git 与交付

完成代码与测试后：

1. 检查 `git status`；
2. 确认没有无关文件；
3. 检查 `git diff`；
4. `git diff --check`；
5. 创建 PATCH 文档；
6. 按现有项目规则生成源码归档；
7. 生成 `.sha256`；
8. commit；
9. push 当前目标分支 / main（按当前仓库实际开发流程）。

不要修改其他 worktree 的 branch pointer，除非当前流程明确要求。

---

# 25. Completion Report 格式

最终必须按以下格式报告：

```text
# Task Completion Report — AutoPosting 3.2.1 Final Closure

## Control Position
Previous: Iteration 2 Main Body COMPLETED
Current: Two-Iteration Refactor CLOSED

## Completed

## Changed

## Deleted

## Compatibility Kept

## Migration

## Tests

## Real UI Verification

## Static Legacy Scan

## Known Limitations

## Remaining Work
```

---

# 26. Remaining Work 规则

本次完成后：

`Remaining Work` 中 **不得再出现本次重构主体遗留**，包括：

- Topic Dossier → Article；
- hooks/find；
- related-search；
- legacy Skill hidden DOM；
- product lane routing。

允许出现的只能是后续普通增强，例如：

- Resource 候选直接出图；
- 新 Subscription Source；
- Jev 权重调优；
- Topic Dossier 图谱；
- 新 Preset；
- UI polish。

这些不属于本次重构未完成。

---

# 27. Definition of Done

本任务只有在以下条件全部满足时才算完成。

- [ ] Topic Dossier 可以直接进入 Creation。
- [ ] Article Generation 使用 Topic Dossier Evidence。
- [ ] Article 正常流程不再触发 `hooks/find`。
- [ ] Article 正常流程不再触发 `/api/related-search`。
- [ ] `hooks/find` 的旧业务实现已删除。
- [ ] `/api/related-search` 的旧业务实现已删除。
- [ ] 如有 external compatibility façade，有明确真实调用证据且只调用新 service。
- [ ] Skill 渲染不再依赖旧隐藏 DOM。
- [ ] `#skill-search-row` 等旧隐藏目标已删除。
- [ ] lane 不再参与新产品路由。
- [ ] 旧 payload / CLI 如需 lane，已降级为薄兼容层。
- [ ] 新 UI 主导航保持稳定。
- [ ] Resource Discovery 不退化。
- [ ] Skill 海报不退化。
- [ ] GitHub Weekly preset / studio 不退化。
- [ ] Draft 编辑不退化。
- [ ] Publishing 不退化。
- [ ] Settings 不退化。
- [ ] 全量 tests 通过。
- [ ] self-check 通过。
- [ ] `git diff --check` 通过。
- [ ] 真实 UI 核心链路通过。
- [ ] README / PATCH / functional test 已更新。
- [ ] 归档与 sha256 已生成。
- [ ] 3.2.1 已 commit / push。
- [ ] Completion Report 明确写明：`Two-Iteration Refactor CLOSED`。

---

# 28. 明确禁止

本任务中不要顺手：

- 做 Resource → Visual 一键出图；
- 新增第三轮需求；
- 重写 Resource Discovery；
- 调整 Jev 排名策略；
- 新增 Source；
- 换前端框架；
- 换视觉技术路线；
- 重构整个 Database；
- 重构整个 `web.py`；
- 重构整个 `find_skills()`；
- 新增发布平台；
- 修改微信发布语义；
- 修改 Xiaohongshu 发布语义；
- 将本任务扩大成“顺手清理全项目”。

原则：

> **只清理本轮重构已经明确到期的旧链路。**

---

# 29. 最终状态

完成后项目应满足：

```text
发现
  ↓
研究
  ↓
Topic Dossier / Resource Set
  ↓
创作
  ↓
视觉
  ↓
审批 / 发布
```

系统中不再存在另一套隐藏的：

```text
找切口
→ 相关检索
→ 旧 lane
→ 隐藏 Skill Finder DOM
```

作为实际业务链路。

---

# 30. Final Closure Statement

本任务完成后，应在 `PATCH_3.2.1.md` 和最终 Completion Report 中明确写入：

> **AutoPosting Two-Iteration Refactor is CLOSED.**
>
> 资讯探索、资源探索、研究、创作、视觉与发布已进入统一产品结构；本轮重构所替代的旧热点 / 观点 / GitHub Weekly / Skill Finder 一级产品结构及旧研究链路已经退出正式业务路径。后续需求按正常产品迭代处理，不再继续扩展本次重构任务。
