# AutoPosting 可读性审查修复实施计划

日期：2026-09-19  
状态：实施计划（本文件不等同于已完成修复）  
产品版本基线：2.0.0  
输入材料：

- PROJECT_READABILITY_AUDIT_2026-09-19.md
- 仓库约束：AGENTS.md

编排时参考了两份临时内部分析，但它们不是权威输入、不是复现依赖，也不纳入计划归档；权威依据仅为诊断报告和仓库约束。

## 0. 文档边界：用户请求、诊断建议与本计划

本次用户请求是：根据附件诊断报告编排一份具体、可执行、可验证的修复实施计划 Markdown 文档，并在完成后说明是否涉及 prompt 文件。本次请求没有授权立即实施业务修复。

附件诊断报告是只读审查材料。报告中的结论、证据行号、优先级和建议是本计划的输入，不是本轮对源码、数据库、测试、配置或 prompt 的直接修改指令。本文件把已核验的问题转换为带依赖、边界、验收、测试和回滚条件的工作包；真正实施时仍须按工作包确认基线和变更范围。

本轮交付边界：

- 生成本计划文件。
- 描述不覆盖既有补丁记录的文档更新记录及限定范围归档方式。
- 本文件本身只做文档内容校验；代码修复阶段才执行对应归档和 checksum。
- 不修改 src/、tests/、config/、数据库、运行数据、服务行为或任何 prompt 文件。
- 不把计划中的建议描述成已经完成的修复或验收。

## 1. 目标、非目标与完成判定

### 1.1 目标

1. 优先修复不可观测故障、错误信息泄露、损坏数据被伪装成正常空结果等 P0 风险。
2. 为 GitHub Weekly、Skill Finder、favorite、scheduled result 等持久化快照建立可演进的 JSON 契约，并为 SQLite 建立可追踪、可重跑的迁移账本。
3. 在保持现有入口、字段、事件和产物兼容的前提下，逐步降低 web.py、find_skills、Database 和 Workbench 的职责耦合；Workbench 明确拆为热点/来源健康、文章/背景/相关文章、WeChat 发布三域，并由兼容 façade 过渡。
4. 只合并已确认的重复校验、payload 构造、视觉参数归一化、图片发布和文章生成公共尾段；不为消除数字而增加抽象层。
5. 用少量高价值的真实行为、失败路径、异步竞态和迁移矩阵测试证明修复，而不是继续堆叠静态 HTML 字符串断言。

### 1.2 非目标

- 不新增平台、发布渠道、外部服务或第三方运行时依赖。
- 不一次性重写 web.py、skill_finder.py、workbench.py 或 db.py；每个切片必须有兼容入口和独立回滚边界。
- 不删除仍有生产入口的 CLI hotspot、CLI facts-first、HTML/CSS visual render 等并行链路。
- 不直接删除 decompose_coverage 或 docs/skill_finder/COVERAGE.md；本计划只安排兼容期限和废弃评估。
- 不删除所有 fallback。只区分业务允许的降级和数据损坏/编程错误，并补充已有 degraded、degraded_reasons、cause_code 等可观测信号。
- 不改变文章、Skill Finder、GitHub Weekly、发布包或视觉产物的业务语义。
- 不把页面布局、视觉规则或新的调教文案塞回 prompt Markdown。
- 不在本计划阶段执行源码修复、schema 迁移、服务启动、真实外部平台调用或正式版本升级。

### 1.3 完成判定

整体修复只有满足下列条件才可宣称完成：

- Scheduler 未预期异常可按 job_id、阶段和异常类型定位，任务不会静默停留在 PENDING/RUNNING；若 finish 写入失败无法立即闭合，下一 tick 必须执行有界 stale-running 对账/修复，或产生可关联的 operator repair 告警。不能把数据库不可用时的终态闭合表述成无条件保证。
- HTTP 未知异常对客户端只返回稳定的脱敏错误码和 request id；服务端日志可关联 route、method、request id 和 traceback，且不含 token、AppSecret、Authorization 或敏感查询参数。
- 损坏 JSON、形状错误、业务允许的空值和真实无结果可区分；相关快照至少能沿用 degraded/degraded_reasons 或已有 cause_code 语义表达原因。
- 已知旧库可重复初始化并完成前向迁移；迁移失败不推进版本号、不删除业务数据；新写入快照带 schema_version。
- /api/collect、/api/hotspots、文章生成/编辑、Skill 搜索/粘贴/渲染、GitHub Weekly、发布、定时发布和已有 CLI 的业务成功行为保持兼容；有意的安全例外是 WP-02 规定的未知 HTTP 500 从原始异常文本改为稳定脱敏错误契约，既有 4xx/403/404/502 仍保持兼容。
- 完成 targeted tests、全量测试、离线 self-check；在具备 TCP 权限的环境中完成真实 HTTP/浏览器行为验证。若验收环境仍限制 TCP，必须把环境错误与业务断言分开记录，不能以静态页面断言替代行为验收。

## 2. 现状校正与证据基线

### 2.1 诊断报告的事实边界

| 主题 | 已确认事实 | 对实施计划的影响 |
|---|---|---|
| 测试数量 | 报告记录发现 595 项；其中 106 个错误主要是受限沙箱禁止绑定本地 TCP，未观察到业务断言失败 | 不能把没有测试作为修复目标；先重放基线并在可绑定 TCP 的环境补行为测试 |
| 既有外部验收记录 | 既有 docs/PATCH_2.0.0.md 记录过另一执行环境的 666 passed、1 skipped | 两次运行的时间/环境不同，不互相覆盖；最终实施需重新记录当前基线 |
| 不监听端口子集 | 报告记录 DB、配置、内容工作流、发布子集 29 项全部通过 | P0/P1 可先用无监听测试推进，避免把环境限制误判为业务失败 |
| 测试缺口 | 主要缺真实浏览器点击/刷新、异步竞态、scheduler 失败路径、损坏 JSON 和完整迁移矩阵 | 采用少量行为/失败/迁移测试，不盲目扩充静态断言 |
| 死代码 | 没有证据证明大量绝对死代码；decompose_coverage 是明确 legacy 候选，其他并行入口仍被生产使用 | 不在本计划中直接删除旧链路 |
| Database/WebApp 模式 | 集中 SQLite 访问和 façade 模式本身可接受；问题是领域规则越界、文件规模和生命周期职责过多 | 先抽边界和兼容 façade，不为了模式名称重写 |
| fallback | 多数 fallback 有真实产品价值；缺口是原因、质量等级和来源不统一 | 复用现有 degraded/cause_code 语义，不把降级全部移除 |
| 密钥 | 报告未发现 API key/AppSecret 明文写入 SQLite，已有 Keychain 和不回显测试 | 日志和错误边界改造必须继续保持此约束 |
| 视觉 lane | GitHub Weekly 与 Skill Finder 已走 HTML/CSS + 浏览器截图，image_gen.py 只生成页面计划 | 不新增图片模型调用点，不把视觉布局恢复成 prompt |

### 2.2 严重问题到工作包的映射

| 报告结论 | 主要工作包 |
|---|---|
| scheduler 静默吞异常 | WP-01 |
| HTTP 500 暴露原始异常、无服务端诊断链 | WP-02 |
| JSON 损坏与正常空状态混淆、自动恢复写回不透明 | WP-03、WP-04 |
| 快照缺 schema_version、SQLite 缺迁移账本 | WP-04、WP-05 |
| platform/status/phase、default=str 和 WeChat 响应契约分散 | WP-06、WP-07 |
| web.py、find_skills、Database/Workbench 领域边界过宽 | WP-08、WP-09、WP-10、WP-11（含 Workbench 三域切片） |
| 发布、文章、视觉参数和图片发布重复 | WP-12、WP-13、WP-14 |
| legacy、规则散布、测试缺口 | WP-15、WP-16、WP-17 |

报告中的行号用于本计划当前基线定位；代码移动后以路径 + 符号名为主，重新生成行号只作为辅助证据。

## 3. 工程约束与总体原则

### 3.1 工程约束

- Python 3.11 标准库 + SQLite；不新增必需第三方运行时依赖。
- 全量测试：PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
- 离线自检：PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
- Web 服务默认绑定 127.0.0.1:8080；真实行为验收应使用本地服务或 faithful fixture。
- 每个实际代码补丁仍须遵守 AGENTS.md：写 docs/PATCH_{version}.md、生成限定范围的 tar.gz 和 .sha256。
- 密钥、AppSecret、access token、Authorization header、原始授权 URL 和请求 body 不进入日志、响应或计划归档。

### 3.2 Ponytail 最小化原则

1. 先复用已有概念：skill_finder.py 已有 degraded/degraded_reasons，相关文章已有 cause_code/detail，发布产物已有 schema_version；不要各自新造等价字段或框架。
2. 先查现有常量、校验器、parse_palette、normalize_poster_template、rank_events、cluster_candidates、PublicationError 和兼容读取器，再决定是否需要新文件。
3. 只有没有可复用来源时，才新增一个小模块/纯函数；不为了架构完整新增工厂、接口层、仓储层或全量 dataclass。
4. 任何新 envelope、常量或 helper 都必须有一个消费者和一条最小失败测试；否则不新增。
5. 结构拆分优先保留旧函数签名、字段、事件顺序和兼容导出；先移动职责，再改变语义。
6. PRAGMA user_version、快照 schema_version 和 revision 解决的是不同问题，不合并成重复状态。
7. 新日志只增加异常路径可观测性；不引入新队列、重试系统或常驻服务。

## 4. 总体执行顺序、依赖与变更闸门

~~~text
基线/表征测试（WP-00）
        ↓
P0：可观测性与边界安全（WP-01～03）
        ↓
P1：JSON/迁移/状态/API/外部响应契约（WP-04～07）
        ↓
P2：中心模块边界拆解（WP-08～11）
        ↓
P3：重复管线合并（WP-12～14）
        ↓
P4：legacy、规则治理与行为矩阵（WP-15～17）
        ↓
最终验收、补丁记录与归档
~~~

依赖规则：

- WP-00 必须先于所有代码工作包；新增表征测试不能由新实现定义。
- WP-01～03 不依赖大结构拆分，先恢复故障可见性。
- WP-04/05 应先于 WP-09/11，避免中心模块拆分同时改变数据形状。
- WP-06/07 应先于 WP-08/12，避免协议和外部响应契约在拆分中漂移。
- WP-08～11 可分开合并，但每一个小切片都必须通过自身回归闸门。
- WP-12～14 只在相应入口有基线快照后进行；文章尾段不得改变 ARTICLE_PROMPT_VERSION、prompt_style 或现有 prompt_version 参与的 LLM cache key。
- WP-17 贯穿各阶段，最终负责真实行为和失败矩阵验收。

每个代码工作包的合并闸门：

1. 先写/运行最小表征测试。
2. 只改变该工作包列出的文件和符号。
3. 运行 targeted tests + git diff --check。
4. 运行离线 self-check；涉及 HTTP/浏览器时使用真实本地服务或 faithful fixture。
5. 检查敏感信息和兼容字段。
6. 记录 patch note 和限定归档后才可宣称该代码补丁完成。

## 5. 工作包实施明细

### WP-00：基线与保护网（P0 前置）

**根因与证据**

诊断报告只读检查已经记录了 TCP 沙箱限制和 29 项无监听通过子集，但后续结构修改若没有可重放基线，容易把环境失败、既有失败和新回归混在一起。报告方法与限制见第 2 节。

**范围与精确文件/符号**

- tests/test_web.py：WebApiTests._request、IndexPageContractTests。
- tests/test_scheduler.py：既有时间解析、成功发布和未来任务测试。
- tests/test_db.py、tests/test_skill_finder.py、tests/test_github_weekly.py、tests/test_publishing.py：对应行为基线。
- 只记录 pyproject.toml 当前版本 2.0.0，不改版本文件。

**最小实施步骤**

1. 记录 commit、版本、测试发现数、TCP 环境错误分类、29 项无监听子集和 self-check 输出。
2. 为未知 HTTP 异常、scheduler publisher 异常、损坏快照、取消/旧 revision、旧库重复初始化各补一条最小表征测试。
3. 对每条表征测试注明修复前预期失败/暴露缺口或修复前已有通过，不能删除既有契约测试。

**验收标准**

- 同一环境可重放基线；测试报告区分环境错误、业务断言失败和跳过项。
- 所有后续工作包都能引用相同基线，不用重构后的行为反推旧行为。

**测试与回归**

- 运行当前 targeted tests、无监听 29 项子集和 offline self-check。
- 不在此阶段启动服务，不把 TCP 权限不足修成产品代码。

**风险与回滚**

- 风险：当前环境的 TCP 限制使完整测试无法完成。
- 回滚：只撤回新增表征测试；保留原测试输出和环境说明，不修改产品实现。

### WP-01：Scheduler 异常可观测与终态闭合（P0）

**根因与证据**

src/autoposting/scheduler.py:57-62 的 PublishScheduler._loop 对 tick 使用 except Exception: continue；_run_job（scheduler.py:74-94）只保存 publisher 的 str(exc)，成功结果还使用 default=str。数据库 finish_scheduled_job 位于 src/autoposting/db.py:1696-1711。因此异常可能既没有 traceback，也不能证明 claim 后的任务会离开 RUNNING。

**范围与精确文件/符号**

- src/autoposting/scheduler.py：PublishScheduler.__init__（现有 self.interval_seconds 及新增可选 self.stale_after_seconds）、PublishScheduler._active_job_ids（claim 后加入、publisher/finish 完成后由 _run_job 的 finally 移除）、PublishScheduler._loop、tick、_run_job；不把 active-job tracking 抽成新类。
- src/autoposting/db.py：claim_scheduled_job、finish_scheduled_job、get_scheduled_job、due_scheduled_jobs；必要时新增最小 reconcile_stale_running_jobs（仅在确认没有等价现有 helper 时）。
- tests/test_scheduler.py；必要时 tests/test_db.py 的 scheduled job 用例。

**最小实施步骤**

1. 复用标准库 logging，只在异常路径记录 job_id、draft_id（必要时脱敏）、platform、阶段、异常类型和 traceback。
2. 区分 publisher 失败、finish_scheduled_job 失败和 _loop 未预期失败；publisher 失败必须写 FAILED，写失败本身必须显式记录，不能伪造 DONE。
3. 采用具体默认阈值 stale_after_seconds = max(60 秒, 4 × interval_seconds)，其中 interval_seconds 是现有 PublishScheduler 构造参数（默认 15 秒）；如本地运维确需调整，只通过新增的可选 stale_after_seconds 构造参数传入，不新增环境变量、数据库配置或重试系统。
4. 为 finish 写失败定义有界闭合策略：下一 tick 先执行 stale-running 对账；仅当 job_id 不在当前 scheduler 的 _active_job_ids、数据库行仍是 RUNNING 且 updated_at 早于 cutoff 时，才以稳定 scheduler_finish_failed 原因原子标为 FAILED，并记录 repair 来源；数据库仍不可用时不自动重发，持续输出含 job_id 的 operator repair 告警，恢复后再对账。
5. 迟到的 publisher finish 必须使用 WHERE job_id=? AND status='RUNNING' 的条件更新；若 repair 先成功变为 FAILED，迟到 finish 的更新行数为 0、只记录 late_finish_ignored，不能覆盖 FAILED；若 finish 先变为 DONE/FAILED，repair 同样不得覆盖终态。当前进程 active job 不参与 repair，避免把仍在执行的发布标成失败。
6. 让 _loop 记录一次失败后继续下一 tick；保持单进程锁、15 秒轮询和已有 DONE/FAILED/SKIPPED 语义，不引入新队列、无限重试或新状态值。
7. 结果 JSON 进入 WP-04/WP-06 的严格序列化边界，不继续用 default=str 吞掉未知对象。

**验收标准**

- publisher 抛异常时任务进入 FAILED，错误原因稳定且日志可按 job/阶段定位。
- tick 的单次未知异常不终止后续轮询；数据库 finish 失败可观察且不返回伪造成功。
- 默认 interval_seconds=15 秒时 stale_after_seconds=60 秒；显式构造参数可覆盖，且测试能证明阈值来源。
- finish 失败后，下一 tick 的 stale-running 对账、稳定 FAILED 修复或 operator repair 告警至少有一条可验证路径；active job 被跳过，不发生无人知晓的永久 RUNNING，也不因修复机制重复发布。
- repair 与迟到 finish 的先后两种竞态都满足条件更新，终态只由第一个成功写入者保留，迟到写入可观测但不能覆盖。
- 成功、未来任务、claim 失败、停止线程和重复调度行为不变。

**测试与回归**

- tests/test_scheduler.py 补 publisher、due_scheduled_jobs、finish_scheduled_job、默认/显式 stale threshold、active job 跳过、stale-running 对账和 _loop 异常用例；必要时 tests/test_db.py 覆盖 repair 与迟到 finish 条件更新的原子性。
- 先跑 scheduler/db targeted tests，再跑全量测试和 self-check。
- 通过日志捕获断言 traceback、job id 存在且敏感信息不存在。

**风险与回滚**

- 风险：stale 阈值过短会把仍在运行的任务误标失败，过长又延迟修复；finish 写失败期间还存在重复发布风险。
- 回滚：保留原错误字段和单进程 claim 防线，单独回退 scheduler/repair 逻辑；不重写历史任务状态，不执行数据库 destructive reset。

### WP-02：HTTP 500 脱敏边界与请求关联（P0）

**根因与证据**

src/autoposting/web.py 的 _make_handler/ApiHandler.do_GET、do_POST、do_DELETE（约 7474-7819）分别处理异常；GET/POST/DELETE 的通用异常路径约在 7585、7773、7813 直接返回 str(exc)，log_message（7816-7817）为空实现。Origin、Content-Type 和热点参数校验也存在重复。

**范围与精确文件/符号**

- src/autoposting/web.py：_make_handler、ApiHandler._send_json、_read_json_body、do_GET、do_POST、do_DELETE、log_message。
- tests/test_web.py：WebApiTests._request、GET/POST/DELETE 异常及安全边界测试。
- 不改变 route 名、成功 payload、Origin 防护和已定义 400/404/403/502 语义。

**最小实施步骤**

1. 复用现有 handler 工厂和 _send_json，生成短 request id，并将 method、route、status、异常类型和 request id 送到服务端 logger。
2. 未知异常统一返回 ok=false、稳定 error_code=internal_error、兼容的 error_type 和 request id；不向客户端返回 str(exc)、文件路径、SQL 或外部 URL。
3. 保留已知 ValueError、KeyError、LLMError、PublicationError 的稳定状态映射；只抽共享参数解析/错误 helper，不削弱 HTTP 层和服务层双重信任边界。
4. log_message 只记录脱敏的请求摘要；过滤 Authorization、token、AppSecret、请求 body 和敏感 query。

**验收标准**

- GET/POST/DELETE 各有一个未知异常用例：HTTP 500、含 request id、不含底层异常文本，服务端日志可关联。
- 非法 Origin、非 JSON body、未知路由、已知 4xx/502 与成功路径不回归。

**测试与回归**

- 用真实 ThreadingHTTPServer 或现有 faithful fixture 发出 GET/POST/DELETE 请求，不只调用 handler 字符串。
- 检查响应和日志中不出现密钥、token、内部路径或原始授权 URL。
- 运行 tests/test_web.py 相关类、HTTP targeted tests、全量测试和 self-check。

**风险与回滚**

- 风险：前端可能依赖原始异常文本。
- 回滚：保留 error/error_type 兼容字段，只移除内部细节；可单独恢复 handler 错误映射，无数据库影响。

### WP-03：损坏 JSON、fallback 与跨边界日志分离（P0）

**根因与证据**

src/autoposting/db.py:530-562 将 GitHub Weekly 的 repos_json、images_json、visual_json 解码失败转成 []/{}；584-598 的 Skill payload 和 645-673 的 favorite payload 也会变成空对象。src/autoposting/web.py:6532-6605 的 _merge_latest_skill_fields、_restore_skill_article_from_package 可能在 GET 读取时自动合并并写回。另有采集错误原样写入 sources.last_error、GitHub 失败降为空结果、Reader/图片/发布降级和 skills.sh adapter 日志上下文不一致的问题（诊断报告第 5.12 节）。合法空状态、业务允许 fallback 与损坏/恢复后的状态因此不易区分。

**范围与精确文件/符号**

- src/autoposting/db.py：get_github_weekly、get_skill_finder、list_hotspot_favorites 及对应 save 方法。
- src/autoposting/web.py：_merge_latest_skill_fields、_restore_skill_article_from_package、skills_snapshot、GitHub Weekly 读取入口。
- src/autoposting/ingestion.py：RSSCollector.collect、SitemapNewsCollector.collect、AggregateCollector.collect、XCollector.collect。
- src/autoposting/reddit.py：RedditCollector.collect；src/autoposting/pipeline.py：collect_sources、内部 _run。
- src/autoposting/github_weekly.py：fetch_weekly_ai_repos、default_github_get、_http_fetch_error、_network_fetch_error、GitHubFetchError。
- src/autoposting/reader.py：ReaderService.read、read_payload、_render_fallback、_document；src/autoposting/images.py：collect_article_images、probe_candidates。
- src/autoposting/workbench.py：HotspotService.collect、WeChatPublisher._upload_permanent_image、_upload_image、_resolve_thumb_media_id；src/autoposting/skill_finder.py：_skills_sh_search、_skills_sh_items。
- sources.last_error 的实际 owner/边界：src/autoposting/db.py 的 record_fetch_result 是唯一数据库写入口；src/autoposting/pipeline.py 的 collect_sources/_run 和 ingestion.py 的 RSSCollector.collect、SitemapNewsCollector.collect、AggregateCollector.collect、XCollector.collect、reddit.py 的 RedditCollector.collect 是调用边界；src/autoposting/workbench.py 的 HotspotService.source_health、_source_health_entry、src/autoposting/db.py 的 get_source 是读取/状态映射边界；WebApp.status 通过 source_health 输出 API。
- tests/test_db.py、tests/test_web.py、tests/test_github_weekly.py、tests/test_skill_finder.py。
- tests/test_config_and_pipeline.py、tests/test_ingestion.py、tests/test_reader.py、tests/test_images.py、tests/test_reddit.py、tests/test_workbench.py。

**最小实施步骤**

1. 建立一个小型 decode 结果约定（正常值、缺失、损坏/形状错误），优先复用相关文章的 cause_code/detail 和 Skill 已有 degraded/degraded_reasons，不引入通用错误框架。
2. 保留可用外层记录和原始 JSON；损坏字段补稳定的 degraded reason，合法空列表/空字典仍表示正常空状态。
3. 明确 GET 恢复是读取展示还是写回修复：默认不把损坏静默写成正常；若必须写回，带来源/修复标记并保证幂等。
4. 对跨边界 fallback 统一策略：logger 记录结构化 code/source/stage/traceback 的脱敏诊断；record_fetch_result 作为唯一写 owner，把 sources.last_error 规范为稳定 code + 脱敏短 detail，不保存 token、Authorization、请求 body 或原始敏感 URL；get_source → HotspotService.source_health → _source_health_entry → WebApp.status 是唯一读取/API映射链路。API/快照沿用 degraded/degraded_reasons 或 cause_code/detail。GitHub 失败空结果、Reader failure_reason、图片 ImageError/空 asset bundle、WeChat PublicationError 和 skills.sh 的 degrade reason 均必须带来源。
5. 历史 sources.last_error 的迁移落在 WP-05 的 user_version 数据迁移步骤：用同一 sanitizer 将旧 raw 文本一次性归一为 legacy_source_error + 脱敏 detail，迁移幂等且不把 raw 文本重新暴露；之后所有写入仍只经过 record_fetch_result，读取仍只经过 get_source/source_health/_source_health_entry。
6. 不改变 GitHub、Reader、Skill 和图片等业务允许的 fallback，仅补充来源、质量和原因。

**验收标准**

- 损坏 JSON、非预期顶层类型、缺字段和合法空值在 DB/API 结果中可区分。
- Skill 快照损坏时不会抹掉 user_title、package 中可恢复的文章或已有 visual 信息。
- 正常 GitHub/Skill/favorite 读写和旧客户端字段保持兼容。
- 采集失败、GitHub 失败、Reader/图片/WeChat/skills.sh 降级均可按 source/stage/code 关联；sources.last_error 和 API 不含敏感信息，正常空结果不伪装成失败。
- sources.last_error 的写入只经过 record_fetch_result，读取/API 只经过 get_source → HotspotService.source_health → _source_health_entry → WebApp.status；WP-05 迁移后旧 raw 文本不会再次出现在响应。

**测试与回归**

- 用 SQLite fixture 构造各 JSON 字段损坏、数组/字符串顶层、合法空值和缺字段。
- 走 DB → WebApp → HTTP 的真实读取链路至少一次；断言 degraded/degraded_reasons/cause_code 只在相应场景出现。
- 为 RSS/sitemap/web/X/Reddit、GitHub search/trending、Reader static/dynamic、图片探测、WeChat 上传和 skills.sh adapter 各补一个脱敏失败 fixture；检查 sources.last_error、failure_reason、PublicationError 和 degrade reason 的形状。
- 对 record_fetch_result 的成功/失败写入、get_source/source_health/_source_health_entry 的 status 映射、WebApp.status API 脱敏，以及 WP-05 legacy_source_error 迁移/重跑各补最小 fixture。
- 回归 tests/test_db.py、test_web.py、GitHub Weekly、Skill Finder、ingestion、pipeline、reader、images、reddit、workbench 相关测试。

**风险与回滚**

- 风险：统一错误字段改变已有 source health 展示，或 logger 过滤过度导致诊断不足。
- 回滚：新增字段只增不删，保留原始列和旧字段；保留已有 code/cause_code 映射，单独恢复某个适配器的读取/日志逻辑，不删除坏数据。

### WP-04：持久化 JSON envelope 与 schema_version（P1）

**根因与证据**

GitHub Weekly、Skill Finder、favorite 和 scheduled result 主要以自由 JSON 存储：db.py:485-598、645-673、1704-1711。已有发布包/归档在 src/autoposting/publishing.py:96-107,143-153 使用 schema_version，可复用其命名/读取思路；不应另造不兼容的版本体系。

**范围与精确文件/符号**

- src/autoposting/db.py：save_github_weekly/get_github_weekly、save_skill_finder/get_skill_finder、add_hotspot_favorite/list_hotspot_favorites、finish_scheduled_job/get_scheduled_job。
- src/autoposting/web.py：GitHub Weekly、Skill snapshot、schedule payload 组装。
- src/autoposting/scheduler.py：result JSON 写入边界。
- tests/test_db.py、tests/test_web.py、tests/test_scheduler.py。

**契约矩阵（实施前冻结）**

所有 envelope 只包住持久化 JSON 列的内容，不改变现有数据库行和 API 展开形状。唯一兼容读取 owner 是 src/autoposting/db.py 的统一私有 decode/normalize helper；WebApp 和 scheduler 不各自解析版本。

| 存储列 | 当前写入 envelope（目标） | 旧形状转换 | 未知版本/坏形状 | DB/API 外部形状 |
|---|---|---|---|---|
| github_weekly.repos_json | {"schema_version":"github-weekly-repos-v1","data":[...]} | 无版本但顶层 list 视为 legacy-v0，归一化为 data list，正常读取并在下一次写入升级 | 保留原始列，返回 degraded reason，不伪装为正常空 list | 继续输出 repos list |
| github_weekly.images_json | {"schema_version":"github-weekly-images-v1","data":[...]} | 无版本但顶层 list 视为 legacy-v0 | 保留原始列，返回 degraded reason；可用部分不丢 | 继续输出 images list |
| github_weekly.visual_json | {"schema_version":"github-weekly-visual-v1","data":{...}} | 无版本但顶层 object 视为 legacy-v0 | 保留原始列并返回 degraded reason | 继续输出 visual object |
| skill_finder.payload_json | {"schema_version":"skill-finder-snapshot-v1","data":{...}} | 无版本但顶层 object 按旧 snapshot 字段归一化 | 不把坏 payload 变成空 snapshot；沿用 degraded/degraded_reasons | 继续输出 snapshot object |
| hotspot_favorites.payload_json | {"schema_version":"favorite-payload-v1","data":{...}} | 无版本但顶层 object 按旧 favorite payload 读取 | 返回该 favorite 的稳定 degraded reason，保留 item/title | 继续输出 favorite item object |
| scheduled_jobs.result_json | {"schema_version":"scheduled-result-v1","data":{...}} 或 null | 无版本但顶层 object 视为 legacy-v0 | 任务结果标为结果降级，不改变已完成/失败状态；禁止 default=str | 继续输出 result object/null |

schema_version 是 envelope 顶层字段，data 是原业务形状；不把 schema_version 塞进 repos、images、skills 等业务数组/对象。未知版本不得自动按最新版本猜测；只有明确的 legacy-v0 转换可自动执行。

**最小实施步骤**

1. 先按矩阵盘点每个消费者真正读取的字段，为当前版本写最小 normalize；不把所有历史字段一次性类型化。
2. 新写入带矩阵规定的 schema_version；旧无版本数据通过唯一 DB-side 兼容读取器转换；未知版本、错误顶层类型、缺关键字段进入稳定 degraded/fallback，不变成正常空结果。
3. scheduled result 先确保是可序列化 JSON 对象或 null，再写入；将版本和结果错误与任务状态分开。
4. 为每一行 envelope 保留原始列、转换后 data、reason 的最小诊断，不在 handler、前端或 scheduler 复制转换规则。

**验收标准**

- 当前版本写入带版本；旧无版本 fixture 可读；未知版本不会破坏原始数据。
- 非对象、非法 JSON、缺字段得到稳定结果，不抛未分类 AttributeError/KeyError。
- 重复读取/写入幂等；现有 package/visual 内容和字段兼容。
- 每一项都能按矩阵证明 schema_version 所在位置、legacy-v0 转换目标、未知版本行为、唯一 owner 和 API 展开形状。

**测试与回归**

- 为 GitHub Weekly、Skill、favorite、scheduled result 覆盖当前/旧/未知/缺字段/类型错误五类 fixture。
- 比较 envelope 迁移前后关键业务字段、content hash、图片清单和 phase/revision。
- 运行 DB/Web/Scheduler targeted tests、全量测试和 self-check。

**风险与回滚**

- 风险：旧客户端不认识新 envelope 字段，或错误版本导致快照不可读。
- 回滚：读取器保持旧形状兼容；新写入可暂时停止升级但不删除旧列，恢复上一阶段代码即可。

### WP-05：SQLite PRAGMA user_version 前向迁移账本（P1）

**根因与证据**

src/autoposting/db.py:257-270 的 Database.initialize 每次顺序执行多个 _ensure_*；273-334 以 sqlite_master SQL 文本探测 sitemap/reddit，337-415 通过 SQL 重建 model purpose，418-437 通过 PRAGMA table_info 探测 weekly images/visual。报告未发现 PRAGMA user_version 或统一 ledger。

**范围与精确文件/符号**

- src/autoposting/db.py：SCHEMA、Database.initialize、_ensure_sitemap_source_type、_ensure_reddit_source_type、_ensure_translation_model_purpose、_ensure_leaderboard_model_purpose、_ensure_image_model_purpose、_ensure_github_weekly_images、_ensure_github_weekly_visual、_ensure_skill_finder、_ensure_hotspot_favorites、_ensure_engagement_snapshots。
- src/autoposting/db.py：复用 Database.initialize 的 user_version 迁移账本，新增/复用最小 sources.last_error 归一化步骤（实际 helper 名称遵循现有迁移 helper 约定），该步骤的 owner 是 Database.initialize 的迁移序列，而非运行时采集器。
- tests/test_db.py、tests/test_config_and_pipeline.py：sources.last_error 的 legacy/raw/sanitized fixture、迁移中断/重试和重复初始化用例。
- 不在本工作包拆分所有仓储类，不自动删除列或重写业务数据。

**最小实施步骤**

1. 盘点已发布 schema 形态和现有 _ensure_* 顺序，定义单向版本号；旧探测逻辑作为一次性迁移步骤，不继续作为长期版本判断。
2. 在事务内执行每个迁移；成功后更新 user_version，重复 initialize 不重复执行；失败时不推进版本号。
3. 在同一迁移序列中由 Database.initialize 负责 sources.last_error 历史归一化：复用 WP-03 的 sanitizer，把旧 raw 文本转为稳定的 legacy_source_error + 脱敏 detail；已归一化行不重复改写，运行时新写入仍只经 record_fetch_result，读取仍只经 get_source/source_health 链路。
4. 对空库、当前库和已知旧库先在临时副本运行；迁移前要求备份 SQLite 文件。
5. 以 PRAGMA foreign_key_check 和数据行计数/关键字段快照证明未丢数据。

**验收标准**

- 空库、当前库、每个已知旧库、迁移中断/重试均可验证。
- 连续两次 initialize 结果相同；sitemap/reddit、model purpose、weekly、Skill、favorite、engagement 迁移矩阵通过。
- 含 raw/legacy sources.last_error 的旧库迁移后只保留稳定 code + 脱敏 detail；迁移在任意步骤中断后重跑可完成，连续重复 initialize 不产生重复改写或重复版本推进，并与 WP-03 的 record_fetch_result 写入、get_source/source_health 读取边界一致。
- 失败迁移不推进版本、不删除既有业务数据。

**测试与回归**

- 为每个历史形态生成最小 SQLite fixture；其中包含 raw/legacy sources.last_error，模拟迁移中断后再次 initialize，并验证 sanitizer、user_version 和重复初始化幂等性。
- tests/test_db.py 验证迁移 owner/版本推进、旧 source error 归一化、失败重试和两次 initialize；tests/test_config_and_pipeline.py 验证迁移后采集失败仍由 record_fetch_result 写入同一稳定形状。
- 运行 tests/test_db.py、相关 Skill/GitHub/engagement/publishing 测试和 self-check。
- 在临时 DB 上执行 foreign_key_check，不使用真实用户库作为破坏性试验对象。

**风险与回滚**

- 风险最高：迁移执行中断、旧库存在未列出的形态或事务边界错误。
- 回滚：停止服务，恢复备份 DB 和上一阶段代码；不执行 git reset --hard、不手工删除表/列。

### WP-06：状态、平台、lane/phase 值域与 API 序列化（P1）

**根因与证据**

src/autoposting/db.py:117-164,201-211 仅部分表有 CHECK；items/events、publication_attempts 等状态/平台约束不对称。状态字符串和 platform/lane/phase 分散在 content.py、publishing.py、scheduler.py、skill_finder.py、web.py。web.py:7476-7482 和 scheduler.py:89-94 使用 json.dumps(..., default=str)，llm.py:662-675 的 settings_payload 类型标注与实际 bool 字段不一致。

**范围与精确文件/符号**

- src/autoposting/db.py：SCHEMA、insert_item、update_event_status、upsert_draft、record_publication_attempt、scheduled job 方法。
- src/autoposting/content.py：ContentWorkflow.save_fact_list、draft 状态方法。
- src/autoposting/publishing.py：PublicationService.publish、archive_draft。
- src/autoposting/scheduler.py、src/autoposting/skill_finder.py、src/autoposting/web.py：状态/lane/phase 使用点。
- src/autoposting/web.py：ApiHandler._send_json；src/autoposting/llm.py：settings_payload。
- 只有在现有源码无单一来源时，才新增一个只含常量/Literal/小校验函数的模块；不默认新增 contracts.py。

**最小实施步骤**

1. 先列出 platform、lane、draft/event/publication/scheduled/skill phase 的全集、合法迁移和外部兼容拼写。
2. 复用已有来源集中定义值域；调用方逐步改用同一来源，对外字符串不变。
3. 对已确认安全的新增/迁移字段增加 DB CHECK/FK；旧库已有合法值不因集中而被破坏。
4. HTTP 只显式序列化 JSON 基本类型和已知 Path/date 等类型；未知对象返回稳定错误，不静默字符串化。修正 settings_payload 类型但不暴露凭据。

**验收标准**

- 非法 platform/status/phase 在 DB 和 HTTP 层稳定失败；历史合法值和前端选项可读。
- Path/date/bool 等已知类型有稳定 JSON 形状；未知对象不会变成不可追踪字符串。
- 状态迁移、发布幂等、scheduled job 和 Skill phase 不回归。

**测试与回归**

- tests/test_content_workflow.py、tests/test_publishing.py、tests/test_scheduler.py、tests/test_web.py 增加非法值/已知值和 serializer boundary 用例。
- 对迁移后的旧库运行状态读取矩阵；运行全量测试和 self-check。

**风险与回滚**

- 风险：历史数据库含未记录但实际可用的字符串，或客户端依赖字符串化日期。
- 回滚：先只做服务层共享校验和兼容 encoder；DB CHECK 通过迁移逐字段开启，任何发现的旧值先记录兼容映射，不直接删除。

### WP-07：WeChat 外部响应稳定适配（P1）

**根因与证据**

src/autoposting/workbench.py:1271-1288 的 WeChatPublisher._api 直接 json.loads 后调用 payload.get；非法 JSON 会抛 JSONDecodeError，数组/标量会导致 AttributeError，token/draft 缺字段在 publish（1415-1453）调用处可能变成 KeyError。上传 media_id/url 缺失才有专门 PublicationError（1332-1353）；已知 errcode 映射在 1238-1247 应保留。

**范围与精确文件/符号**

- src/autoposting/workbench.py：WeChatPublisher._api、_upload_permanent_image、_upload_image、_resolve_thumb_media_id、publish。
- src/autoposting/publishing.py：PublicationError 及调用边界只做兼容验证，不改变 dry-run/live 规则。
- tests/test_workbench.py 或现有 WeChat publisher 测试、tests/test_publishing.py、tests/test_web.py。

**最小实施步骤**

1. 在 _api 统一检查网络响应、JSON 解析、顶层对象形状和已知 errcode；映射为不泄密的稳定 PublicationError code。
2. 为 access_token、draft/add、upload media_id/url 写一个最小字段读取 helper；缺字段、错误类型和未知 errcode 使用稳定 code，不回传原始响应或 token。
3. 保留既有已知错误映射、网络错误、dry-run、live=true 才写微信草稿的边界。

**验收标准**

- 非法 JSON、JSON 数组/标量、缺 access_token、缺 media_id/url、未知 errcode 和网络失败均转为稳定 PublicationError。
- 正常 token → 上传 → draft/add 仍可完成；Xiaohongshu 仍仅手工包。

**测试与回归**

- 用注入 opener fixture，不访问真实微信；覆盖所有异常响应形状。
- 验证日志/异常不含 app secret、access token、原始授权 URL。
- 运行 WeChat、publishing、Web publish targeted tests 和全量回归。

**风险与回滚**

- 风险：外部接口偶发返回新增字段或成功响应形状差异。
- 回滚：保留已知 errcode 映射和旧成功字段读取；只恢复 adapter，数据库和 prompt 无影响。

### WP-08：web.py 静态资源、HTTP 协议与应用门面拆分（P2）

**根因与证据**

src/autoposting/web.py 总计约 7,837 行：约 94-5683 为内嵌 HTML/CSS/JavaScript，5686-7471 为 WebApp，7474-7819 为 handler。WebApp.__init__（5687-5761）同时接受约 15 个依赖并内部创建多个服务；do_POST（7588-7774）承载路由、协议校验和错误映射。

**范围与精确文件/符号**

- src/autoposting/web.py：INDEX_HTML、WebApp、_make_handler、ApiHandler、create_server、run_web。
- 可新增 src/autoposting/web_assets.py 保存静态资源；可新增最小 src/autoposting/http_api.py 保存协议/错误 helper，但必须先确认现有导入模式。
- tests/test_web.py 全部页面契约、路由、API 和服务生命周期测试。

**最小实施步骤**

1. 先移动静态资源，保留 web.INDEX_HTML 兼容别名，使用字节/字符串契约证明页面未变。
2. 再抽 body 读取、Content-Type/Origin、request id、响应和异常映射；路由表集中但 endpoint 名不变。
3. 最后才评估按领域移动少量 WebApp 方法；保留 WebApp façade 和 create_server，不同时重写服务生命周期。
4. 每个移动切片都先通过页面/API targeted tests，再使用真实 HTTP/浏览器验收。

**验收标准**

- 页面内容、所有 endpoint、成功 payload、既有 4xx/403/404/502 payload、Origin 防护和 scheduler 生命周期不变；唯一有意变化是 WP-02 已定义的未知 500 脱敏稳定错误契约，不再返回原始异常文本。
- web.py 不再同时承载大段静态资源和协议细节；新模块能独立测试，旧导入仍可用。

**测试与回归**

- tests/test_web.py 页面解析、JS parser、真实 HTTP endpoint 和服务启动/停止测试。
- 实际验证 /api/status、/api/collect、/api/hotspots、文章、Skill、周榜、发布、调度；不以 assertIn 单独作为验收。

**风险与回滚**

- 风险：资源路径、导入顺序、闭包捕获和错误映射变化；若将 WP-02 的未知 500 例外误扩散到既有 4xx/502，视为回归。
- 回滚：每次只移动一个模块，保留兼容别名；发现问题时只回退该切片，不迁移数据库、不改变 prompt。

### WP-09：find_skills 搜索/聚合/文章包三阶段拆分（P2）

**根因与证据**

src/autoposting/skill_finder.py:700-997 的 find_skills 同时处理关键词覆盖、FetchBudget、stage 搜索、旧/手工 skill 复用、degraded、LLM 文章、fallback、写包和 progress event。WebApp.skills_search（web.py:6798-6925）还负责线程持久化、取消、revision 和 render 字段。

**范围与精确文件/符号**

- src/autoposting/skill_finder.py：find_skills、keyword_coverage、_search_stage、_reuse_previous、aggregate_skills、write_article、_fallback_article、write_package、snapshot、render_skill_package。
- src/autoposting/web.py：skills_search、内部 persist/mark_failed/run、_skill_lock、_skill_revision、_save_skill_render_fields、skills_snapshot。
- src/autoposting/db.py：save_skill_finder、get_skill_finder。
- tests/test_skill_finder.py、tests/test_web.py、tests/test_skill_package.py。
- 不新增图像模型 prompt 或调用点；render_skill_package 继续 HTML/CSS + browser screenshot。

**最小实施步骤**

1. 先保持当前 snapshot dict、degraded/degraded_reasons、budget、rejected、phase、event 名称和事件顺序，抽出三个阶段：coverage/search orchestration、result aggregation、article/package。
2. find_skills 只协调和收口错误；搜索阶段不写 package，文章阶段不决定搜索覆盖，写包阶段不重新解释 query。
3. 保持 max fetch/deadline、previous/manual skills、cancel、on_event、revision 和 fallback article 语义；不先引入大套 dataclass。
4. WebApp/DB 继续持有锁、持久化和 revision 保护；阶段只接收最小输入输出。

**验收标准**

- 相同 fixture 下候选去重、stage gap、手工粘贴、无模型 fallback、文章/包文件、事件进度与基线一致。
- cancel 能在三个阶段终止；旧 revision 不覆盖新搜索/粘贴；重复请求不丢 manual skills。
- 各阶段可注入 fake GitHub/web/LLM；find_skills 原签名仍是兼容 façade。

**测试与回归**

- 阶段级测试覆盖预算耗尽、来源失败、degraded reasons、旧快照复用、manual merge 和 package 写入。
- 真实调用 WebApp.skills_search，检查进度、取消和 revision；对 index.json/article.md/图片输出做文件级行为验证。
- 运行 Skill、Web、package、visual 相关测试和 self-check。

**风险与回滚**

- 风险：线程竞态、事件顺序变化、旧 revision 覆盖新结果。
- 回滚：保留原 find_skills 签名和事件 payload；pipeline 只作为内部实现，可切回旧实现，不删除已有 package/snapshot。

### WP-10：Database 与热点领域排序解耦（P2）

**根因与证据**

src/autoposting/db.py:1327-1438 的 list_hotspot_candidates 在 SQL 后执行 lane 过滤、来源判断、should_hide_candidate、engagement/tier 计算和排序，并在 1374 反向导入 .ingestion。HotspotService.list_window（workbench.py:582-688）同时编排时间窗口、翻译和 radar。

**范围与精确文件/符号**

- src/autoposting/db.py：list_hotspot_candidates，及其内部 SQL/行读取部分。
- src/autoposting/workbench.py：HotspotService.collect、_persist_radar_snapshots、list_window。
- src/autoposting/radar.py：rank_events、cluster_candidates。
- tests/test_db.py、tests/test_radar.py、tests/test_web.py 及 CLI hotspot 测试。
- 不在本工作包拆分 Database 为多个仓储文件。

**最小实施步骤**

1. 为当前 DB 查询和 Web/CLI radar 输出保存候选集合、排序、lane、来源、隐藏和 engagement/tier 快照。
2. 保留 SQL 的数据选择/分页能力，把领域分类、lane/来源解释、隐藏、engagement/tier 和排序移回 HotspotService/radar，复用现有 rank_events/cluster_candidates。
3. 保留旧 DB 方法作为兼容薄层，待结果稳定后再评估是否有必要按表域拆文件。

**验收标准**

- CLI hotspot-check 和 Web /api/hotspots 的候选集合、排序、隐藏、heat/time/radar 输出及 snapshot 写入与基线一致。
- db.py 不再反向导入 ingestion；DB 单测无需网络即可运行。

**测试与回归**

- 使用同一 fixture 做迁移前/后 golden 对比；覆盖来源类型、lane、空结果、隐藏规则、engagement/tier。
- 运行 DB/radar/workbench/Web/CLI targeted tests、全量测试和 self-check。

**风险与回滚**

- 风险：SQL 的过滤顺序和 Python 排序顺序变化，导致热点排名漂移。
- 回滚：保留旧方法和兼容输出；先以 feature-local helper 切换，不删除旧逻辑，golden 不一致立即回退。

### WP-11：WebApp 依赖组合与 Skill snapshot 适配边界（P2）

**根因与证据**

web.py:5687-5761 的 WebApp.__init__ 是依赖容器和业务门面，接收约 15 个参数并继续内部构造 LLMService、HotspotService、Workbench、ContentWorkflow、PublishScheduler。Skill snapshot 在 db.py:564-598 以 JSON dict 存储，skill_finder.py:777-816 构造，web.py:6532-6564 直接修改同一组键。

**范围与精确文件/符号**

- src/autoposting/web.py：WebApp.__init__、_restore_*、skills_snapshot、skills_search、_merge_latest_skill_fields、_save_skill_render_fields。
- src/autoposting/db.py：Skill snapshot save/get。
- src/autoposting/skill_finder.py：snapshot、aggregate_skills、apply_skill_selection。
- Workbench 三域边界切片（同属 P2，但独立小提交）：
  - 热点/来源健康目标模块：src/autoposting/workbench_hotspots.py，承载 HotspotService、_source_health_entry、_radar_row、_radar_event_candidate、event_ids_for；workbench.py 保留兼容导出。
  - 文章/背景/相关文章目标模块：src/autoposting/workbench_articles.py，承载 Workbench.generate_article、_generate_fused_article、related_search、find_hooks、original、rescan_images 所需的应用编排；workbench.py 保留薄 façade。
  - WeChat 发布目标模块：src/autoposting/wechat_publisher.py，承载 WeChatPublisher、WechatConfigurationRequired 及其 _api/upload/publish 方法；workbench.py 保留兼容导入。
  - 若实施前确认现有等价模块或导入拓扑更适合其他名称，必须在切片记录中固定唯一目标路径，不得同时保留两套实现。
- tests/test_web.py、tests/test_skill_finder.py、tests/test_skill_package.py。
- 不新增多层 DI 容器；只有构造顺序确实需要时才引入一个内部 composition context/工厂。

**最小实施步骤**

1. 先记录现有依赖创建顺序和可注入 fake service 组合；保留公共 WebApp 构造签名。
2. 将服务创建/恢复集中到一个最小内部组合边界，不改变服务生命周期和 keychain 读取。
3. 为 Skill snapshot 建一个单一 normalize/version adapter；可用 TypedDict/小 dataclass，但不改变外部 JSON 字段或 revision/phase。
4. WebApp 只通过 adapter 读写字段，旧字段、缺字段和包恢复仍由一个入口处理。
5. 在 Workbench 三域切片中先抽热点服务，再抽文章/背景编排，最后抽 WeChat publisher；每一步都由 workbench.py 的兼容 façade 转发，禁止在一次提交中重命名所有调用方。

**验收标准**

- 最小 fake 依赖可启动 WebApp；文章、热点、Skill、渲染、调度可分别注入替身。
- Skill 旧字段、缺字段、并发 revision、package 恢复和用户标题均保持行为。
- 组合边界减少构造器理解成本，但不改变公共 API。
- 热点/来源健康、文章/背景/相关文章、WeChat 发布三域均有唯一实现；旧 workbench.py 导入和 Workbench/HotspotService/WeChatPublisher 公共调用仍可用。

**测试与回归**

- 增加 WebApp composition smoke test 和 Skill snapshot normalization fixture。
- 复用现有 degraded/degraded_reasons/schema_version，测试未知字段保留策略、缺字段默认和 revision 冲突。
- 为三个 Workbench 目标模块各增加导入兼容、最小 fake 依赖和跨域调用 smoke test；对热点输出、文章 draft/source/assets、WeChat dry-run payload 做 golden 对比。
- 运行 Web/Skill/package 全量相关测试和 self-check。

**风险与回滚**

- 风险：初始化顺序、keychain 恢复、跨域循环导入或测试 fake 缺属性；WorkBench 拆分还可能改变发布/文章共享 helper 的生命周期。
- 回滚：保留旧构造器参数、workbench.py 兼容导出和旧字段 adapter；三个目标模块逐个切换，可单独恢复任一域。

### WP-12：发布校验与 package/archive payload 去重（P3）

**根因与证据**

src/autoposting/publishing.py:41-83 的 PublicationService.publish 校验 current draft、draft/event APPROVED 后调用 archive_draft；127-136 再次校验。publication package 与 article archive 在 96-107、143-153 重复读取 title/body/tags/assets/source ids/content hash，但两个产物的 schema_version 语义不同。

**范围与精确文件/符号**

- src/autoposting/publishing.py：PublicationService.publish、archive_draft、私有验证/context/payload helper。
- src/autoposting/web.py：_publish_locked、publish。
- tests/test_publishing.py、tests/test_web.py。
- 不合并两种产物 schema；不改变 dry-run/live、Xiaohongshu 手工包和 WeChat 真实草稿边界。

**最小实施步骤**

1. 抽取内部 approved/current draft/event 的已验证 context；公开 archive_draft 仍独立防御，内部 publish 传递已验证 context。
2. 抽取共享文章字段读取和 payload builder；在末端分别添加 publication-package-v1、article-archive-v1 的特有字段。
3. 保持 publish lock、幂等 attempt、dry-run、live=true、密钥边界。

**验收标准**

- publish dry-run、archive 单独调用、未批准/superseded/event-not-approved、重复发布和 live/manual 边界与基线一致；HTTP 未知 500 的改变仅按 WP-02 的安全契约处理，不算业务兼容性回归。
- 两种 JSON 内容 hash、字段兼容和文件位置不变；内部不重复查库。

**测试与回归**

- 运行 publishing/Web 定向测试；比较生成的 package/archive JSON golden。
- 走一次不触发外部平台的实际 publish dry-run；确认日志和归档无凭据。

**风险与回滚**

- 风险：把两个 schema 误合并，或公开 archive 失去独立防御。
- 回滚：保留旧 public method；只回退内部 context/payload helper，不删除产物。

### WP-13：单项/融合文章生成公共尾段（P3）

**根因与证据**

src/autoposting/workbench.py:808-1008 的 Workbench.generate_article 与 1010-1207 的 _generate_fused_article 输入语义不同，但在图片收集、source records、framing、Draft 构造、Database.upsert_draft 和返回 payload 处重复。二者都在 844/1025 加载 prompt_style，并在 915-926/1100-1111 调用同一 LLMService.generate_article。

**范围与精确文件/符号**

- src/autoposting/workbench.py：Workbench.generate_article、_generate_fused_article、rescan_images。
- src/autoposting/content.py：ContentWorkflow.save_fact_list、edit_draft（只复用经过验证的 draft/asset 边界）。
- src/autoposting/llm.py：LLMService.generate_article 调用契约，不移入 prompt 文案。
- 文章、Workbench、LLM、Content 测试。

**最小实施步骤**

1. 先保持单项/fused 各自准备 FactList、source refs、background、radar、hooks、pasted/related context。
2. 抽最小私有 finalize_article_draft 类似尾段：文章结果 → 图片资产 → framing → Draft → upsert_draft → 返回 payload。
3. 保持 ARTICLE_PROMPT_VERSION、prompt_style、现有 prompt_version 参与的 LLM cache key、平台差异和图片失败语义。
4. CLI facts-first 链路保持独立；只共享已验证的 Draft/asset helper。

**验收标准**

- 单项、双项融合、无图、部分图片失败、WeChat/Xiaohongshu、chosen hooks、pasted/related/radar 和幂等缓存输出与基线一致。
- ARTICLE_PROMPT_VERSION、prompt_style 和现有 prompt_version/cache key 语义未改变；LLM 失败原因和 source ids 不回归。若测试通过 HTTP 未知异常路径，按 WP-02 的稳定 500 契约核对，不要求保留原始异常文本。

**测试与回归**

- 运行 tests/test_workbench.py、tests/test_content_workflow.py、tests/test_llm.py 相关测试。
- 对单项/fused draft 的 body/title/assets/source records/event status 做 golden/字段对比；不修改任何 prompt 文件。

**风险与回滚**

- 风险：把输入准备逻辑误合并，造成融合文章事实边界或 prompt 输入变化。
- 回滚：尾段 helper 可由两个旧方法分别调用；发现 ARTICLE_PROMPT_VERSION 或现有 prompt_version/cache key 变化立即回退，不清理缓存。

### WP-14：热点参数、视觉参数与 Skill 图片发布动作去重（P3）

**根因与证据**

热点 hours/lane 校验重复在 web.py:7530-7545,7617-7640 和 workbench.py:518-523,582-590；theme/palette/template 解析重复在 web.py:6477-6484,6973-6980,7125-7144；Skill 图片复制/URL 转换重复在 web.py:7017-7028,7089-7097。

**范围与精确文件/符号**

- src/autoposting/web.py：github_weekly_render、skills_render、github_weekly_image、publish_images、on_image。
- src/autoposting/workbench.py：HotspotService.collect、list_window 的参数边界。
- src/autoposting/visual/weekly.py：render_weekly_visual。
- 复用已有 parse_palette、normalize_poster_template、visual schema/tokens。
- tests/test_github_weekly.py、tests/test_visual.py、tests/test_web.py。

**最小实施步骤**

1. 抽一个纯 normalize/validate helper，统一 theme/palette/template 默认、合并和错误；HTTP 层仍保留类型/信任边界，服务层仍做业务校验。
2. 抽一次性的 image file → HTTP URL 发布 helper；保留 Skill revision lock、增量回调和 snapshot 写入顺序。
3. 对热点 hours/lane 只共享解析规则和错误码，不去掉服务层防御。
4. 继续 HTML/CSS + browser screenshot；不新增 image-model call site。

**验收标准**

- Weekly cover/poster、Skill cover/poster、单图重渲染、无截图环境和错误路径的 theme/palette/template、文件、URL、snapshot、progress 与基线一致。
- /api/hotspots、/api/collect 的合法/非法 hours/lane 语义一致；渲染/图片失败的未知 500 仅按 WP-02 的脱敏稳定错误契约改变，既有 4xx/502 和成功路径不变。

**测试与回归**

- 真实 HTTP GET/POST hotspot、GitHub render/re-render、Skill full/cover-only render。
- 检查 image 文件存在性、URL 内容、增量 progress、revision 防覆盖和 visual schema 校验。
- 运行 visual/GitHub/Skill/Web targeted tests、全量测试和 self-check。

**风险与回滚**

- 风险：默认合并顺序变化、重复复制导致 URL 或文件清单不同、异步回调丢失。
- 回滚：保留旧入口 helper；只在默认策略 golden 一致后删除重复代码，异常时恢复单入口调用。

### WP-15：decompose_coverage legacy 兼容期限（P4）

**根因与证据**

src/autoposting/skill_finder.py:1127-1211 明确标记 decompose_coverage 为 Legacy；graft callers 仅命中 tests/test_skill_finder.py:937-950，而主链路使用 keyword_coverage（832-849）。docs/PATCH_1.4.0.md:148-149,270-271 说明 COVERAGE.md 和函数仍保留兼容面，但静态调用图不能证明外部导入者不存在。

**范围与精确文件/符号**

- src/autoposting/skill_finder.py：decompose_coverage、keyword_coverage。
- tests/test_skill_finder.py：legacy 用例。
- docs/skill_finder/COVERAGE.md 仅作为只读 prompt/兼容依据；可能的归档/脚本/外部调用约定只做检索。
- 本计划不删除函数、不修改 COVERAGE.md，也不新增 prompt 文案。

**最小实施步骤**

1. 全项目、归档脚本和公开入口搜索调用约定；记录外部兼容风险。
2. 若确认需要，只在 skill_finder.py、tests/test_skill_finder.py 或非 prompt 的迁移记录中加入低噪声 deprecation 注释/测试和迁移说明，规定至少一个兼容期限。
3. 不修改 docs/skill_finder/COVERAGE.md；只有另立 prompt/文档专项、提供迁移错误信息并完成版本说明后，才评估对它的任何修改或删除。

**验收标准**

- 生产主链路仍走 keyword_coverage；legacy 测试用途和兼容期限清楚。
- 当前阶段没有删除行为，也不改变 Skill prompt。

**测试与回归**

- 运行 Skill Finder legacy/主链路测试、全项目调用检索和 package 行为验证。

**风险与回滚**

- 风险：外部脚本或用户仍直接 import legacy 函数。
- 回滚：保留函数和文档；只撤回 deprecation 注释，不删除兼容代码。

### WP-16：规则、阈值和版本来源治理（P4）

**根因与证据**

Skill 产品规则散落在 skill_finder.py:28-47,61-70,1000-1003,2621-2644；视觉规则同时存在 src/autoposting/visual/rules.py:31-48,55-68,79-88 和 docs/image_prompts/rendering/rules.json；产品版本分散在 pyproject.toml:7、src/autoposting/__init__.py:3、web.py:66、llm.py:42 和测试断言。问题是规则来源不可见，不是所有数字都应配置化。

**范围与精确文件/符号**

- src/autoposting/skill_finder.py：SKILL_KIND_RULES、WORKFLOW_STAGES、KEYWORD_LIMIT、_best_stage 等分类/权重逻辑。
- src/autoposting/visual/rules.py：load_rules、apply_project_rules 等阈值。
- docs/image_prompts/rendering/rules.json：已有可配置视觉规则。
- 版本五处联动文件和测试；不改变产品版本作为本计划的一部分。

**最小实施步骤**

1. 列出现有规则的 owner、版本、消费者和边界测试；先复用已有常量/JSON。
2. 只把跨模块且真正会独立变化的值集中；不为一处使用的阈值建 config 或 factory。
3. 视觉规则迁移只做一组，保持 schema/HTML 输出兼容；不要把产品规则迁移成 prompt 文案。
4. 任何未来版本 bump 必须遵守 AGENTS.md 五处同步清单。

**验收标准**

- Skill 分类、关键词上限、visual 空/超长/8 条密度边界和现有 output 不变。
- 规则 owner、版本和解释性输出可定位；rules.json 仍是视觉规则 JSON，不被误称为 prompt。

**测试与回归**

- 运行现有 visual/Skill tests，增加最小边界值测试。
- 若实际修改版本，检查五处生产位置和测试断言；本计划不改版本。

**风险与回滚**

- 风险：规则集中后默认值、浮点阈值或视觉 schema 漂移。
- 回滚：先保留旧读取 fallback；任何 golden 不一致恢复原 Python/JSON 来源。

### WP-17：行为、失败、迁移和敏感信息最终矩阵（P4）

**根因与证据**

报告第 5.11 节确认项目不是缺测试，而是 tests/test_web.py:1924-3069 主要静态契约，tests/test_scheduler.py:42-59 缺失败路径，迁移测试未覆盖 GitHub/Skill/favorite/engagement 全矩阵；第 5.12 节还指出 scheduler、HTTP、采集和部分降级日志上下文不一致。

**范围与精确文件/符号**

- tests/test_web.py：真实 HTTP/浏览器路径、异常展示、refresh/revision。
- tests/test_scheduler.py：publisher、finish、loop 失败。
- tests/test_db.py：JSON envelope、旧库迁移、中断/重试、foreign_key_check。
- tests/test_workbench.py/WeChat、tests/test_publishing.py：外部响应与发布契约。
- tests/test_skill_finder.py、tests/test_skill_package.py、tests/test_github_weekly.py、tests/test_visual.py：Skill/GitHub/视觉行为。
- 生产观测检查点：src/autoposting/ingestion.py 的各 Collector.collect、pipeline.py 的 collect_sources/_run、github_weekly.py 的 fetch_weekly_ai_repos/default_github_get、reader.py 的 ReaderService.read/_render_fallback、images.py 的 collect_article_images/probe_candidates、workbench.py 的 WeChatPublisher 上传方法、skill_finder.py 的 _skills_sh_search。

**最小实施步骤**

1. 真实 HTTP/浏览器：证明 collect 后候选可见、refresh 后仍在、长文章确实使编辑面板变长、Skill 异步搜索/取消/旧 revision 不覆盖。
2. Scheduler：覆盖 publisher 异常、finish 失败、loop 未预期异常、状态查询和错误展示。
3. 持久化：覆盖损坏 JSON、正常空值、旧/当前/未知版本、迁移中断重试、重复 initialize。
4. HTTP/WeChat：覆盖 GET/POST/DELETE 脱敏 500、request id、非法 body、非法 WeChat JSON/非对象/关键字段缺失。
5. 文章/发布/视觉：单项/fused 尾段、package/archive、palette/template、图片 URL/文件复制和幂等。
6. 逐项检查采集/fallback 观测：source.last_error 只含稳定 code 与脱敏 detail；GitHub 空结果带 degraded reason；Reader failure_reason、图片/WeChat 错误和 skills.sh degrade reason 可按 source/stage 关联，日志不含敏感 URL/凭据。
7. 只保留能证明行为的最小测试；静态页面断言作为契约辅助，不能作为功能验收唯一证据。

**验收标准**

- targeted tests 全部通过后再跑全量；测试报告明确环境限制。
- PYTHONPATH=src:tests python3 -m unittest discover -s tests -v、离线 self-check 和真实本地服务/浏览器路径均有记录。
- 日志、响应、数据库和归档不含 token/AppSecret/Authorization。

**测试与回归**

最终顺序：

1. 相关 targeted tests。
2. 全量 unittest。
3. offline self-check。
4. 真实本地 HTTP/浏览器验收。
5. 数据库 migration/snapshot version 检查。
6. git diff --check、文档/归档清单和 checksum。

**风险与回滚**

- 风险：测试环境缺 TCP、浏览器或外部凭据；浏览器测试可能产生 output/data 变化。
- 回滚：使用临时 DB/临时 output/faithful fixture；不得为通过测试修改产品代码绕过限制。环境不可用时标记未完成，不伪造验收。

## 6. 里程碑与建议切片

| 里程碑 | 包含工作包 | 可交付物 | 进入条件 | 退出条件 |
|---|---|---|---|---|
| M0 基线 | WP-00 | 可重放测试/环境基线、表征测试 | 当前代码/版本已记录 | 环境错误与业务失败分开 |
| M1 P0 安全与可观测 | WP-01～03 | scheduler 日志、HTTP 脱敏边界、JSON 降级诊断 | M0 通过 | 失败路径可定位，正常空值不误判 |
| M2 P1 契约 | WP-04～07 | snapshot version、migration ledger、值域/serializer、WeChat adapter | M1 通过 | 旧库/旧快照/外部坏响应矩阵通过 |
| M3 P2 边界拆解 | WP-08～11 | web 资源/协议边界、Skill pipeline、热点领域边界、Workbench 三域、composition/snapshot adapter | M2 通过 | 入口/事件/字段/排序 golden 不变；未知 500 按 WP-02 安全例外 |
| M4 P3 去重 | WP-12～14 | 发布/文章/视觉公共 helper | 相应 M3 切片稳定 | 产物 hash、ARTICLE_PROMPT_VERSION、现有 prompt_version/cache key、图片/URL 行为不变；未知 500 按 WP-02 安全例外 |
| M5 P4 收尾 | WP-15～17 | legacy 期限、规则 owner、行为/失败/迁移矩阵 | M1～M4 通过 | 全量测试、自检、真实行为和敏感检查完成 |
| M6 记录归档 | 版本记录、计划归档 | patch note、限定 tar.gz、sha256 | M5 或本次文档交付 | 归档可列出且 checksum 通过 |

建议每个代码切片保持单一主题：

- P0：scheduler；HTTP；DB decode，各自独立提交。
- P1：JSON envelope；migration ledger；值域/serializer；WeChat adapter，各自独立提交。
- P2：web assets；HTTP protocol；Skill stage；hotspot domain；composition/snapshot，各自独立提交。
- P3：publication；article finalize；visual/image publish，各自独立提交。

## 7. 统一风险与回滚策略

| 风险 | 预防 | 回滚 |
|---|---|---|
| SQLite 迁移/快照损坏 | 停服务、复制 DB、临时副本矩阵、事务内更新 user_version | 停服务后恢复备份 DB 与上一阶段代码；不 destructive reset |
| HTTP 响应兼容性 | 保留 ok/error/error_type，只增加 request id/稳定 code，先补契约测试 | 回退 handler/serializer；无需清理数据库 |
| Scheduler 状态异常 | 先测 claim/finish 原子性，保留旧状态值 | 回退 scheduler；人工检查 RUNNING，禁止自动重复发布 |
| Skill 异步竞态 | revision/cancel 表征测试、WebApp 持锁、单写入点 | 回退 pipeline 内部实现；不删除 package/snapshot |
| 大文件拆分导入错误 | 兼容别名、逐文件切片、页面字节测试 | 回退单个模块移动，数据库/输出不变 |
| 日志泄漏 | 脱敏 helper、token/secret regression test | 立即停用新增日志 handler；若确有暴露，按密钥轮换流程处理 |
| 视觉/文章语义漂移 | golden、ARTICLE_PROMPT_VERSION/现有 prompt_version/cache key、schema 校验、HTML/PNG 行为检查 | 恢复旧 helper；不修改 prompt 以掩盖回归 |

## 8. Prompt 文件影响结论

### 8.1 本次计划的结论

**本次修复计划不需要修改任何 prompt 文件。**

原因：诊断问题位于可观测性、数据契约、SQLite 迁移、模块边界、状态/校验重复、外部响应适配和测试覆盖；没有把 prompt 内容判定为本次可读性问题的根因。WP-13 明确要求保持现有 prompt 输入、ARTICLE_PROMPT_VERSION、prompt_style 以及已有 prompt_version 参与的 LLM cache key 不变；本计划只引用这些代码中已有的标识。

### 8.2 实施时必须保持不变的 prompt/提示词相关文件

本计划不修改以下文件：

- docs/ARTICLE_BASE_PROMPT.md
- docs/article_styles/AI专家.md
- docs/article_styles/经验分享.md
- docs/article_styles/klinkenborg.md
- docs/skill_finder/PACKAGE_ARTICLE.md
- docs/skill_finder/COVERAGE.md
- docs/image_prompts/copy.md
- docs/image_prompts/copy_skill.md
- docs/image_prompts/legacy/STYLE.original.md

docs/RELATED_SEARCH_PROMPT.md 虽是 prompt 命名文件，但当前源码没有加载引用；它不是本计划的活跃 prompt 输入，本计划不修改它，也不把它纳入运行契约。

其中：

- 文章 prompt 仍由现有 article prompt loader/LLM 调用链读取；文章公共尾段只复用调用契约。
- PACKAGE_ARTICLE.md 和 COVERAGE.md 是 Skill Finder 的文案/覆盖约束；只有未来主动改变 Skill 文章或 coverage JSON 语义时才另立专项变更。
- docs/image_prompts/copy.md、copy_skill.md 属于文字内容层，不是恢复图像模型整页 prompt 的理由；视觉仍遵循 HTML/CSS + browser screenshot。
- STYLE.original.md 是历史备份，不应被重新接回运行链路。

若实施期间发现 prompt 内容本身导致功能断言失败，应停止该工作包，补充证据并另立 prompt 专项计划；不得在可读性修复中顺手改文案。

## 9. 交付清单与完成定义

### 9.1 代码修复阶段的交付

- 每个工作包的变更文件、符号和行为差异记录。
- targeted tests、全量测试、offline self-check 和真实 HTTP/浏览器验收记录。
- schema/migration/snapshot 兼容矩阵。
- 日志/响应/归档敏感信息检查记录。
- 每个实际版本补丁的 docs/PATCH_{version}.md、限定范围 tar.gz 和 .sha256。

### 9.2 本次文档交付的完成定义

- docs/PROJECT_READABILITY_REMEDIATION_PLAN_2026-09-19.md 存在，关键章节齐全：目标/非目标、现状校正、原则、依赖、工作包、里程碑、完成定义、回滚、Prompt 结论。
- 本次不修改产品代码或 prompt 文件。
- 既有 docs/PATCH_2.0.0.md 和 archives/autoposting-patch-2.0.0-update-20260919.tar.gz 不被覆盖；本次最终文档记录与归档采用将生成的 r3 命名。
- 本次文档更新使用独立修订标识记录并归档；归档只包含 AGENTS.md 允许的路径，.sha256 通过。

### 9.3 归档命名决策

当前产品版本为 2.0.0，同名 PATCH_2.0.0.md 与基础 tar.gz 已存在；工作区最终交付的本次文档记录和归档采用 r3 后缀。为满足“每次更新记录并归档”而不进行无谓产品版本升级、不覆盖既有补丁记录，本次文档更新使用：

- 修订记录：docs/PATCH_2.0.0-r3.md
- 归档：archives/autoposting-patch-2.0.0-update-20260919-r3.tar.gz
- 校验：archives/autoposting-patch-2.0.0-update-20260919-r3.tar.gz.sha256

2.0.0-r3 是文档/补丁记录修订标识，不是 pyproject.toml 的产品版本；不触发五处产品版本同步。不得再同时创建另一套 readability-plan 命名的 PATCH 或归档。
