# AutoPosting 更新开发指导（Agent 默认执行规范）

> 适用基线：AutoPosting `v3.0.0+`  
> 文档目的：当用户只描述“我想增加、修改、删除什么功能”时，Agent 自动承担架构、兼容性、可维护性、错误处理、测试和验收责任。  
> 核心原则：**用户负责产品意图，Agent 负责把需求放进正确的位置，并保证项目不会因为一次次小修改重新走向不可维护。**

---

## 0. 如何使用本文件

本文件应作为 AutoPosting 后续开发的常驻指导文档，建议放在项目根目录，并在 AGENTS.md 或 Agent 启动提示中声明：

> 修改 AutoPosting 前，必须先阅读本文件。除非用户明确要求改变这些原则，否则所有代码修改都应遵循本文件。

本文件不是一次性的重构计划，也不是要求每次需求都进行大规模重构。它规定的是：

- 新需求默认应该落在哪一层；
- 什么行为不能继续新增；
- 什么兼容性必须保留；
- 什么测试必须补；
- 什么失败必须可观测；
- 什么技术债在“碰到时”应顺手向正确方向推进；
- Agent 最终应该如何向用户交付结果。

---

# 1. 当前项目基线认知

AutoPosting 已经不是一个简单脚本，而是至少包含以下五个相对独立的子系统：

1. **信息采集与热点雷达**
2. **内容工作台与文章生成**
3. **视觉渲染**
4. **Skill Finder**
5. **发布与调度**

后续修改时，不能再把所有功能都视为“WebApp 里的一个新方法”或“web.py 里再加一个分支”。

## 1.1 v3.0.0 已经建立的安全底座

以下能力已经在 `3.0.0` 建立，后续修改必须继续沿用，不能绕开或退回旧模式：

- Scheduler 异常可观测、运行中任务保护、过期任务对账和失败终态闭合。
- Web 未知 500、采集、GitHub、Reader、图片、Skill、WeChat 等失败具有稳定、脱敏、可关联的诊断。
- GitHub Weekly、Skill Finder、favorite、scheduled result 的持久化 JSON 已引入版本化 envelope / schema version。
- SQLite 已使用 `PRAGMA user_version` 事务迁移序列。
- HTTP 已停止对未知对象使用无条件 `default=str`。
- WeChat 外部响应的非法 JSON、错误形状、缺字段、未知错误码和网络失败已统一进入稳定错误边界。
- 产品版本已经统一到 `3.0.0` 的维护方式。

### 禁止回退

任何新功能都不得：

- 重新把损坏 JSON 当成 `{}` 或 `[]` 静默吞掉；
- 重新把未知 Python 对象自动 `str()` 后塞进 API；
- 重新使用 `except Exception: pass` / `continue` 吞掉关键错误；
- 重新把原始异常、token、Authorization、请求 body、敏感 URL 直接返回给客户端或写入持久化诊断；
- 绕开 `PRAGMA user_version` 自建第二套迁移机制；
- 绕开现有稳定错误和诊断模型另造一套不兼容格式。

## 1.2 v3.0.0 尚未完成的治理

以下问题仍然存在，后续需求修改时要避免继续扩大：

- `platform / status / lane / phase` 等全局值域尚未完全集中；
- `web.py` 仍承担静态前端、HTTP、依赖容器和多个业务域；
- `find_skills()` 仍然过大；
- `Database` 仍然混合部分领域决策；
- `Workbench` 仍跨热点、文章、发布多个业务域；
- 单项/融合文章、发布 payload、视觉参数解析等仍存在重复管线；
- legacy 与产品规则治理仍未完成。

因此后续开发采用一个重要策略：

> **不要求一次性重构全部旧代码，但禁止新需求继续增加同类结构债。凡新需求正好碰到这些高风险区域，应优先沿正确边界做“小切片式改善”。**

---

# 2. 用户与 Agent 的职责边界

## 2.1 用户只需要描述产品需求

用户可以只说：

- “这里增加一个按钮。”
- “这个结果要保存。”
- “希望增加一种状态。”
- “这个页面再加一种筛选方式。”
- “失败后希望自动重试。”
- “新增一个平台。”
- “把这里的逻辑改成……”
- “这个功能不要了。”

用户**不需要**额外说明：

- 代码应该放哪个文件；
- 是否要拆模块；
- 数据库怎么迁移；
- 是否要加 schema version；
- 是否需要测试；
- 异常怎么记录；
- 是否需要兼容旧数据；
- 是否应该抽公共函数；
- 是否应该处理并发；
- 是否需要做回归验证。

这些属于 Agent 的默认职责。

## 2.2 Agent 不应把架构问题反抛给用户

以下问题原则上不要询问用户：

- “要不要顺便重构？”
- “要不要拆成 service？”
- “要不要加测试？”
- “要不要兼容旧数据？”
- “这个状态要不要集中定义？”
- “是否需要迁移脚本？”
- “错误要不要记录？”

这些都应由本指南直接决定。

只有当**产品行为本身存在无法安全推断的多种含义**，且不同选择会明显影响用户数据或最终体验时，才需要提出产品层问题。

如果能够做出安全、兼容、可逆的合理解释，应直接实施，并在最终报告中写明采用的假设。

---

# 3. 每次修改必须遵循的执行流程

任何需求，无论大小，都按以下顺序处理。

---

## Step 1：把用户语言翻译成“可验收行为”

先明确：

- 用户触发什么？
- 系统输入是什么？
- 成功时用户看到什么？
- 失败时用户看到什么？
- 是否写入数据库或文件？
- 页面刷新/服务重启后是否仍应保留？
- 是否会影响已有功能？
- 是否涉及外部服务？
- 是否涉及异步任务、调度或并发？

不要一上来就改代码。

### 输出应形成内部验收条件

例如：

> 点击“重新渲染”后，只更新指定图片；其他图片不变；失败后保留原图并返回稳定错误；刷新后数据库中的 visual snapshot 保持一致。

---

## Step 2：先画清影响范围，再修改

至少检查以下层级：

```text
用户界面
  ↓
HTTP / CLI 边界
  ↓
Application / Domain Service
  ↓
数据契约 / 状态机
  ↓
Database / 文件
  ↓
外部服务 Adapter
  ↓
Scheduler / 后台任务（如有）
```

不是每个需求都会涉及所有层，但必须先判断。

### 重要规则

如果某个概念跨越两个以上层级，必须先寻找或建立其**单一契约源**，不能直接在每一层各写一份字符串和判断。

---

## Step 3：找到“唯一应该拥有这条规则的地方”

每个业务规则原则上只能有一个主定义位置。

例如：

- HTTP 层：负责请求格式、安全边界、错误映射。
- Application Service：负责业务编排。
- Domain：负责业务规则和状态变化。
- Database：负责存取、事务、迁移，不负责热点分类等产品决策。
- Adapter：负责第三方协议差异。
- Renderer：负责视觉 schema → HTML → PNG。
- Scheduler：负责 claim、执行、终态闭合，不负责重新实现发布规则。

### 判断标准

如果修改一条规则时需要全项目搜索并手工同步 4～5 处，说明规则位置不对。

这时不要继续复制，而应先建立共享契约或 helper。

---

## Step 4：选择最小、但架构正确的修改切片

遵循：

> **Minimal Diff ≠ 把所有逻辑塞进原文件。**  
> 最小修改应理解为“最小完整责任单元”。

可以局部修改时，不做无关大重构。

但如果需求会迫使以下中心继续膨胀：

- `web.py`
- `find_skills()`
- `Database`
- `Workbench`

则优先把**本次新增逻辑**放到独立 service / contract / adapter / helper 中，让中心模块只保留调用。

### 高风险模块的“零新增复杂度”原则

后续修改默认要求：

- `web.py` 不新增新的业务算法；
- `do_GET / do_POST / do_DELETE` 不新增复杂业务流程；
- `find_skills()` 不继续吸收新的搜索源、文章逻辑或持久化逻辑；
- `Database` 不新增分类、排序、展示语义；
- `Workbench` 不新增与现有职责无关的新业务域。

旧代码可以暂时存在，但新代码应向边界清晰的方向生长。

---

## Step 5：先定义成功、失败、降级，再实现

每个新流程必须至少考虑：

1. 正常成功；
2. 用户输入不合法；
3. 外部服务失败；
4. 外部服务返回错误形状；
5. 数据损坏或版本不兼容；
6. 业务允许的 fallback；
7. 编程错误或未知错误；
8. 中途退出/取消；
9. 重复执行；
10. 服务重启后的恢复行为。

不要只实现 happy path。

---

## Step 6：实现后进行“局部验证 → 全量回归”

顺序：

1. 新增/修改功能的 targeted tests；
2. 相关模块测试；
3. 失败路径测试；
4. 数据兼容/迁移测试（如涉及）；
5. 全量测试；
6. self-check；
7. UI/HTTP/loopback 行为验收（如涉及）；
8. `git diff --check`。

---

## Step 7：最终向用户交付“结果”，而不是代码过程

最终报告必须告诉用户：

- 用户要求的功能是否完成；
- 实际增加/修改/删除了什么；
- 是否影响原有功能；
- 是否修改数据结构或迁移；
- 做了哪些测试；
- 哪些环境限制导致无法真实验证；
- 是否有任何功能被删除、替换或行为改变；
- 本次是否顺带减少了技术债；
- 仍有哪些明确未完成项。

不要只回复“已修改”。

---

# 4. 架构边界规则

---

## 4.1 HTTP / web.py

### HTTP 层只做四件事

```text
解析请求
→ 边界校验
→ 调用 Application Service
→ 映射响应 / 稳定错误
```

### 禁止

不要在 HTTP handler 中新增：

- 数据库查询组合逻辑；
- 外部 API 调用；
- 复杂 fallback；
- 排名计算；
- 发布状态判断；
- Skill 聚合；
- 文件复制流程；
- 视觉参数业务规则。

### 新增 API 时

优先使用：

```text
handler
  ↓
request contract / validator
  ↓
application service
  ↓
domain / repository / adapter
```

如果只是为了新增一条 API 就在 `do_POST` 增加十几到几十行业务分支，视为架构回退。

---

## 4.2 Application / WebApp

WebApp 可以继续作为 façade，但应该越来越“薄”。

允许：

- 组合已有服务；
- 生命周期控制；
- 调用一个明确的业务入口；
- 将业务结果交给 HTTP 层。

不允许继续承担：

- 新领域规则；
- 新数据修复策略；
- 新外部协议解析；
- 新持久化格式；
- 新视觉规则。

如果一个新功能有自己的输入、状态和失败语义，应创建独立 application service，而不是继续扩 WebApp。

---

## 4.3 Database

Database 的职责：

- schema；
- migration；
- CRUD；
- transaction；
- 基础查询；
- 数据完整性；
- 持久化 contract。

Database 不负责：

- hotspot 分类；
- 展示层过滤语义；
- 热度规则；
- UI phase 推导；
- 文章生成规则；
- Skill 业务分组；
- 外部 API fallback。

### 数据库返回的数据不应包含“偷偷修好的业务结论”

如果数据库中的 payload 损坏：

错误做法：

```text
解析失败 → {}
```

正确方向：

```text
解析失败
→ 明确 diagnostics / degraded / corruption
→ 上层决定如何展示或恢复
```

---

## 4.4 Skill Finder

`find_skills()` 是已知高风险中心。

以后新增 Skill Finder 功能时，按以下边界组织：

```text
Coverage / Query Planning
        ↓
Search Orchestration
        ↓
Source Adapters
        ↓
Aggregation / Dedupe / Ranking
        ↓
Article Generation
        ↓
Package Generation
        ↓
Persistence / Progress Events
```

### 禁止

- 再把一个新搜索源直接写进 `find_skills()` 主函数；
- 搜索失败后静默返回“看起来正常”的空技能；
- 在搜索函数中直接承担文章模板和文件落盘；
- 读取旧 snapshot 时自动重写状态但不标明恢复来源。

### 修改旧逻辑时

如果本次需求正好触碰一个明显混合职责块，应优先将**本次涉及的那一块**抽出，而不是一次性重写整个 Skill Finder。

---

## 4.5 Workbench

Workbench 当前跨多个业务域，后续避免继续扩张。

新的热点逻辑 → hotspot/radar service  
新的文章逻辑 → article/content service  
新的发布逻辑 → publishing service / adapter  
新的外部读取 → reader/background adapter

如果旧入口必须保留，可以保留兼容 façade，但内部调用新的模块。

---

## 4.6 Visual

保持目前稳定方向：

```text
Content Schema
→ Visual Rules / Tokens
→ HTML/CSS
→ Browser Screenshot
→ PNG
```

### 要求

- theme / palette / template 只能有一个统一解析入口；
- 单图渲染和整包渲染使用同一套参数语义；
- 文件路径 → HTTP URL 的转换只保留一份实现；
- 不得为了局部功能再复制一套视觉参数兼容逻辑；
- 不得静默替换现有 HTML/CSS + 浏览器截图路径为另一套渲染机制。

如用户明确要求改变渲染技术路线，再作为独立能力变更处理。

---

## 4.7 Publishing

发布相关逻辑必须继续区分：

```text
业务校验
→ archive/package
→ platform adapter
→ publication attempt
→ stable result
```

### 必须保留的现有能力边界

- Xiaohongshu 当前是手工发布包；
- WeChat live 路径是草稿语义；
- 不应擅自调用 `freepublish`；
- X 当前有采集/配置能力，但不能假设已经存在发布 adapter。

如果用户未来要求增加 X 发布，这属于**新增发布平台能力**，必须完整增加 contract、adapter、幂等、状态和测试，不能简单复用一个字符串分支。

### 发布前置校验

不要在 `publish()`、`archive_draft()`、WebApp、Scheduler 中分别复制同一规则。

应共享“已验证发布上下文”或统一校验函数。

---

## 4.8 Scheduler

Scheduler 必须满足：

```text
PENDING
→ claim
→ RUNNING
→ DONE / FAILED / CANCELLED
```

任何任务都必须能到达可解释的终态。

### 禁止

- 未预期异常后直接 `continue`；
- publisher 返回错误形状仍标记成功；
- RUNNING 永久悬挂；
- 失败只保存不可分类的原始 `str(exc)`。

### 新增调度功能时

必须考虑：

- 重复 claim；
- 重启；
- stale RUNNING；
- publisher exception；
- DB exception；
- 非法 publisher result；
- 幂等。

---

# 5. 单一契约源原则

项目最重要的后续维护目标之一，是让“同一个概念只定义一次”。

## 5.1 必须优先集中化的概念

尤其包括：

- `platform`
- `lane`
- `status`
- `phase`
- draft/event/publication/scheduled 状态
- visual theme/template/palette
- error code
- API payload field
- Skill workflow stage
- 产品规则阈值

`3.0.0` 尚未完成全局值域集中化，因此从现在开始：

> **任何新值不得继续以裸字符串形式散落到多个模块。**

如果本次需求新增：

```text
platform = "xxx"
phase = "xxx"
status = "xxx"
lane = "xxx"
```

必须先找到共享定义位置；如不存在，则创建契约模块，再让各层引用。

---

## 5.2 产品规则与技术常量分开

例如：

- Skill 分类词表；
- 搜索权重；
- 截断长度；
- poster density；
- 搜索关键词数量；
- 视觉阈值。

这些不是“随便的 magic number”，而是产品规则。

产品规则应：

- 集中；
- 命名；
- 有注释；
- 必要时版本化；
- 能在测试中明确验证。

不要在多个函数里直接写 `5 / 2 / 1`、`28`、`42` 等而没有语义名称。

---

# 6. 持久化与数据契约

---

## 6.1 任何持久化 JSON 都必须是显式契约

新持久化 payload 必须：

- 有 schema version；
- 有明确结构；
- 区分合法空值、真实无结果、降级结果和损坏数据；
- 读取时严格校验形状；
- 支持旧版本兼容或迁移；
- 未知版本不得静默当作当前版本。

### 注意

项目已经有 3.0.0 的 envelope 机制。

新代码应复用现有实现，**不要另造第二种 envelope 格式**。

---

## 6.2 空结果不等于失败

必须区分：

```text
正常执行，无结果
外部来源失败后的空结果
部分来源失败后的降级结果
持久化数据损坏
输入本身为空
```

用户界面可以选择把它们展示得很简洁，但系统内部不能把它们压成同一个 `{}` / `[]`。

---

## 6.3 Migration

所有 schema 变化必须通过现有 `PRAGMA user_version` 迁移体系。

必须满足：

- 单向；
- 事务；
- 幂等；
- 失败不推进版本；
- 可以从旧版本逐级升级；
- 不依赖“猜 SQL 字符串长什么样”来判断版本；
- 迁移后执行必要完整性检查；
- 有中断/重试测试。

### 禁止

- 启动时发现字段不存在就直接 `ALTER`，但不记录迁移版本；
- 迁移失败后继续启动并把新版本写入；
- 为了省事删除旧表/旧数据；
- 将旧 payload 读取失败直接重置为空。

---

# 7. API 与序列化

HTTP 输出只能序列化项目明确支持的类型。

如果出现未知类型：

```text
未知对象
→ 稳定内部错误
→ 服务端脱敏诊断
→ 客户端稳定错误码
```

不要重新使用：

```python
json.dumps(..., default=str)
```

来掩盖契约错误。

### API contract 改动

若修改已有字段：

优先：

- 增加字段；
- 保留旧字段一段兼容期；
- 在 contract 层完成兼容。

不要：

- 在前端和后端分别写两套兼容 fallback；
- 无通知地改变字段类型；
- 把 `null`、`[]`、`{}`、缺字段混为一谈。

---

# 8. Fallback、错误与可观测性

---

## 8.1 所有 fallback 必须“可解释”

允许 fallback，但必须知道：

- 为什么触发；
- 原始来源哪里失败；
- 使用了什么替代来源；
- 当前结果质量是否降级。

推荐内部至少具备这些语义：

```text
status
error_code / cause_code
fallback_reason
source / stage
degraded
request_id / job_id / correlation id
```

字段名以项目现有 diagnostics 契约为准，不要为了本指南再创造第二套格式。

---

## 8.2 业务允许的降级与数据损坏必须分开

例如：

### 合法降级

```text
原站页面读取失败
→ 使用 feed 内容
→ result = degraded
→ reason = page_read_failed
```

### 非法隐藏

```text
数据库 JSON 损坏
→ {}
→ 前端显示“暂无内容”
```

后者禁止。

---

## 8.3 未知异常

未知异常应该：

- 客户端：稳定、脱敏；
- 服务端：有 route/stage/request id/context；
- 日志：保留可调试信息；
- 不泄露 secret。

不能同时出现：

```text
客户端看到了内部异常
+
服务端却没有任何诊断日志
```

---

# 9. 安全与隐私边界

任何日志、diagnostics、DB error、API error 中不得直接写入：

- API key；
- token；
- Authorization；
- AppSecret；
- cookie；
- 请求 body 中的敏感字段；
- 原始敏感 URL；
- URL query 中的凭据；
- 环境变量 secret。

如果错误文本来自第三方异常，必须先通过项目的诊断脱敏边界。

### 新外部 adapter

必须测试：

- 网络失败；
- 非 JSON；
- JSON 数组；
- JSON 标量；
- 缺必要字段；
- 已知 error code；
- 未知 error code；
- secret 不泄漏。

---

# 10. 状态、并发与恢复

---

## 10.1 状态必须是状态机，不是随手写字符串

新增状态时必须回答：

- 可以从哪些状态进入？
- 能进入哪些终态？
- 是否可取消？
- 是否可重试？
- 服务重启后如何解释？
- 重复请求是否幂等？

不要只在前端加一个新 `phase`，然后数据库、后台线程和 API 各自猜含义。

---

## 10.2 revision / lock

当前部分并发保护仍然是应用层、单进程语义。

因此：

- 不要因为有 `revision` 就默认已经具备跨进程安全；
- 新并发修改必须明确单进程还是多进程；
- 更新 snapshot 前应重新读取最新状态；
- 网络请求尽量放在锁外；
- 持久化 commit 前再做 revision / current-state 校验。

如果没有实现跨进程能力，文档和代码都不要暗示已经支持。

---

# 11. 重复逻辑治理

修改需求时如果发现“复制旧代码最快”，先检查是否属于以下已知重复：

- 发布前置校验；
- publication package / archive payload；
- 单 item / fused article 公共尾段；
- HTTP 错误映射；
- theme/palette/template 解析；
- 图片文件复制与 URL 转换；
- hotspot 参数校验；
- Skill snapshot 字段处理。

### 默认规则

同一产品规则允许：

- 信任边界再校验一次；

但不允许：

- 每一层各维护一份略有不同的规则。

例如：

HTTP 层可以验证输入类型；Domain 层可以验证业务合法性。

但 HTTP、Service、DB 三层不应各自维护一套 `"hotspots" / "viewpoints"` 白名单。

---

# 12. Legacy 与旧路径

不要因为看起来旧就直接删除。

删除前至少确认：

- 静态调用者；
- CLI；
- Web；
- tests；
- 外部 import 兼容面；
- README / AGENTS；
- 用户已有数据。

### decompose_coverage 类 legacy

如果只有测试调用但可能是外部兼容面：

正确做法：

1. 标记 deprecated；
2. 指向新入口；
3. 设置明确兼容期；
4. 增加删除条件；
5. 最终再删除。

### 禁止

- 留两套逻辑永久并行；
- 旧函数名字保留，但内部悄悄改变语义；
- 删除后不在交付说明中告知用户。

---

# 13. 前端与真实行为验收

静态 HTML / JS 字符串存在不等于功能可用。

如果修改：

- 点击；
- 弹窗；
- 刷新；
- 恢复；
- 异步状态；
- 取消；
- 长文本；
- 图片渲染；
- 页面运行时 JavaScript；

则不能只写：

```python
assertIn("button", html)
```

还应至少完成一种真实行为验证：

- 浏览器运行时；
- faithful loopback fixture；
- JS runtime；
- 可复现 UI integration test。

### 环境受限时

如果当前环境不能启动真实浏览器或外部服务：

- 使用可用的 faithful fixture；
- 明确记录限制；
- 不得写“已完成真实浏览器验收”。

---

# 14. 测试规范

---

## 14.1 每个需求至少覆盖

### 正常路径

用户要求的功能能够成功。

### 边界输入

空值、非法值、过长值、未知枚举等。

### 失败路径

外部服务错误、DB 错误、文件错误或 LLM 错误。

### 兼容路径

旧数据、旧 schema、旧 API payload。

### 重复执行

刷新、重试、重复提交、幂等。

### 安全

错误中不泄露 secret。

---

## 14.2 触碰这些区域时的额外测试

### Database

- migration from old version；
- migration interruption；
- migration retry；
- malformed JSON；
- wrong shape；
- legal empty；
- foreign key check。

### Scheduler

- publisher throws；
- invalid result shape；
- stale RUNNING；
- claim conflict；
- DONE / FAILED terminal closure。

### Skill Finder

- cancel；
- stale revision；
- manual skill merge；
- required stage gap；
- LLM fallback；
- degraded source；
- persistence restore。

### Publishing

- duplicate attempt；
- invalid draft state；
- dry-run；
- live boundary；
- adapter bad response。

### UI

- runtime JS；
- refresh；
- async state；
- error display；
- long content；
- retained state。

---

## 14.3 标准回归命令

在环境允许时，完成：

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

然后：

```bash
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
```

以及：

```bash
git diff --check
```

如本次改动涉及 HTTP / loopback / UI，应再执行对应 targeted behavior test。

### 不要默认调用真实发布

除非用户明确要求并授权：

- 不调用真实微信发布；
- 不调用真实 X 发布；
- 不调用真实小红书发布。

测试优先使用 fixture / dry-run / loopback。

---

# 15. 修改高风险中心时的具体策略

---

## 15.1 web.py

如果需求必须改 web.py：

优先：

```text
新增薄 route
→ 调用独立 service
```

而不是：

```text
在 web.py 再写 100 行流程
```

如果新增前端代码较多，优先向静态资源拆分方向推进，不继续无限扩充内嵌 HTML/CSS/JS。

---

## 15.2 find_skills()

如果需求改变搜索：

新增/修改 search adapter。

如果改变 coverage：

改 coverage planner。

如果改变文章：

改 article writer。

如果改变 package：

改 package writer。

如果改变 snapshot：

改 contract / persistence。

不要把五种变化一起塞回 `find_skills()`。

---

## 15.3 Database

如果业务查询需要新字段：

可以扩 repository query。

如果需求是：

- “按 lane 分类”
- “判断热点 tier”
- “展示什么状态”

优先放 domain/service，不放 DB。

---

## 15.4 Workbench

如果新增一个清晰的新领域功能，优先建立独立 service。

Workbench 可以作为兼容入口调用它，但不应继续成为新的业务实现位置。

---

# 16. “碰到即治理”的技术债策略

不要为了一个小需求暂停开发去做全项目重构。

但当本次需求刚好触碰以下问题时，应顺手做一个可验证的小切片。

## 16.1 优先级 A：全局值域

如果新增/修改：

- platform；
- status；
- lane；
- phase；

则本次修改必须优先使用共享 contract。

不要新增更多散落字符串。

## 16.2 优先级 B：web.py

如果本次需要加入明显业务逻辑：

抽 service。

## 16.3 优先级 C：Skill Finder

如果本次修改落在 `find_skills()` 的某一阶段：

抽出该阶段。

## 16.4 优先级 D：Database

如果发现 DB 正在做产品决策：

本次相关逻辑尽量移到 service。

## 16.5 优先级 E：重复管线

如果修改恰好需要同步两份同类逻辑：

先抽公共逻辑，再改一次。

---

# 17. 禁止的“快速修复”模式

以下做法默认视为不合格：

### 1. 在多个文件复制同一判断

```text
web.py 一份
workbench.py 一份
db.py 一份
frontend 再一份
```

### 2. 新增未知状态字符串但不定义 contract

### 3. 用 `except Exception: pass`

### 4. 用 `{}` / `[]` 掩盖解析失败

### 5. 用 `default=str` 掩盖 API contract 问题

### 6. 修改数据库但没有 migration

### 7. 修改 migration 但没有旧库测试

### 8. 修改 UI 只验证字符串存在

### 9. 修改外部 API 只测正常 JSON

### 10. 新增 fallback 但用户无法区分降级结果

### 11. 为“快速上线”把新业务直接塞进 web.py / find_skills()

### 12. 重写大量无关代码，扩大回归面

### 13. 删除旧能力但不明确说明

### 14. 测试环境没有真实验证，却在报告中写成“全部端到端通过”

---

# 18. 修改决策树

```text
收到需求
│
├─ 只是文案/纯视觉静态改动？
│   └─ 最小修改 + UI targeted test
│
├─ 改 API 输入/输出？
│   └─ 先改 contract → service → handler → compatibility test
│
├─ 新增 status/platform/lane/phase？
│   └─ 先共享值域 → 状态机/规则 → 各层引用
│
├─ 改持久化结构？
│   └─ schema contract → PRAGMA user_version migration → old DB tests
│
├─ 接第三方 API？
│   └─ adapter → response validation → stable error → redaction tests
│
├─ 改异步/调度？
│   └─ state machine → idempotency → terminal closure → recovery tests
│
├─ 改 Skill Finder？
│   └─ 定位阶段 → 在对应阶段实现，不扩 find_skills 主函数
│
├─ 改发布？
│   └─ shared validation → package/archive → adapter → attempt
│
└─ 正好触碰已知重复/中心模块？
    └─ 做一个“小切片式抽离”，但不要顺便重写全项目
```

---

# 19. Agent 的默认完成标准（Definition of Done）

一个需求只有同时满足以下条件才算完成：

- [ ] 用户要求的可见行为已实现。
- [ ] 没有无意删除原有功能。
- [ ] 新逻辑放在合理责任层。
- [ ] 没有新增同一规则的多份实现。
- [ ] 新状态/值域没有继续散落裸字符串。
- [ ] 持久化结构有明确 contract。
- [ ] schema 变化使用正式 migration。
- [ ] 旧数据仍可读取或明确迁移。
- [ ] 空结果、降级、失败、损坏可区分。
- [ ] 未知异常不会静默吞掉。
- [ ] 诊断信息经过脱敏。
- [ ] 外部 API 错误形状已覆盖。
- [ ] 并发/重试/幂等已按需要处理。
- [ ] targeted tests 通过。
- [ ] 相关失败路径测试通过。
- [ ] 全量测试通过，或明确说明无法执行的原因。
- [ ] self-check 通过，或明确说明不适用。
- [ ] UI 行为改动有真实行为级验证，而不只是字符串测试。
- [ ] `git diff --check` 通过。
- [ ] 最终报告列出新增、修改、删除和未验证内容。

---

# 20. Agent 最终交付报告模板

每次修改完成后，用以下结构向用户汇报。

```md
# 本次更新结果

## 1. 用户需求
简述本次要求。

## 2. 已完成
- …
- …
- …

## 3. 实现方式
说明功能落在哪个模块、为什么这样放。
只解释关键架构决策，不需要把低层代码全部展开。

## 4. 数据与兼容性
- 是否修改数据库
- 是否新增 migration
- 是否修改 API contract
- 是否兼容旧数据

## 5. 新增 / 修改 / 删除
### 新增
- …

### 修改
- …

### 删除
- 无
或明确列出删除内容及原因。

## 6. 验证
- targeted tests：
- full tests：
- self-check：
- UI / loopback：
- git diff --check：

## 7. 未验证与环境限制
如实说明。

## 8. 剩余技术债
只列与本次修改直接相关、且尚未处理的内容。
```

---

# 21. 对 Agent 的简版强制指令

如果上下文窗口有限，至少保留以下规则：

> 1. 用户只负责描述想要的功能，你负责架构、兼容、测试和维护。  
> 2. 不要继续把业务逻辑堆进 `web.py`、`find_skills()`、`Database`、`Workbench`。  
> 3. 同一业务规则只允许一个主定义位置。  
> 4. 新增 `platform/status/lane/phase` 时必须使用共享 contract，不能继续散落裸字符串。  
> 5. 持久化 JSON 必须沿用版本化 envelope；损坏、正常空值、无结果、降级必须可区分。  
> 6. 数据库变化必须走 `PRAGMA user_version` 事务迁移，并覆盖旧库升级、中断和重试。  
> 7. 允许 fallback，但 fallback 必须可解释、可诊断、可区分质量等级。  
> 8. 不得吞掉未知异常，不得向客户端或持久化日志泄露 secret、token、Authorization、body 或敏感 URL。  
> 9. 外部 adapter 必须验证 JSON 类型、字段、未知错误码和网络失败。  
> 10. Scheduler 的任务必须闭合到明确终态，并考虑重复执行、重启、stale RUNNING 和幂等。  
> 11. UI 行为变化不能只做 HTML 字符串测试，必须尽量进行真实行为级验收。  
> 12. 修改先做 targeted tests，再做全量 tests、自检和 diff 检查。  
> 13. 不进行与用户需求无关的大重构；但如果本次正好碰到已知结构问题，做一个小而完整的治理切片。  
> 14. 任何被删除、替换或行为发生变化的功能，都必须在最终报告中明确写出。  
> 15. 环境没有真实验证时，不得把 fixture / 静态检查描述成真实端到端验收。

---

# 22. 本指南的长期目标

后续开发不应再依赖：

> “每次修改之前，让用户先提醒模型注意架构、不要写乱、不要破坏旧功能。”

理想状态应该是：

```text
用户：
“我想让这里增加一个功能。”

Agent：
理解需求
→ 自动检查影响范围
→ 找到正确责任层
→ 复用已有 contract
→ 必要时补迁移
→ 实现成功/失败/降级
→ 写测试
→ 回归
→ 告知用户结果和变化
```

也就是说：

> **维护性不是用户每次提需求时额外提出的要求，而是任何修改默认必须满足的产品质量属性。**

---

## 文档依据

本指南根据以下项目材料整理：

- `PROJECT_READABILITY_AUDIT_2026-09-19.md`
- `PATCH_3.0.0.md`

其中 `PATCH 3.0.0` 已完成的 P0/P1 安全底座被视为后续开发必须保持的基线；尚未完成的全局值域集中化、中心模块拆分、重复管线合并、legacy/规则治理，则采用“禁止继续扩大 + 需求触碰时小切片治理”的方式逐步推进。
