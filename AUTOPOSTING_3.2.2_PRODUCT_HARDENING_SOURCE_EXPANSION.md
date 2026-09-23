# AutoPosting 3.2.2 — Product Hardening & Source Expansion

> 日期：2026-09-23
> 当前基线：AutoPosting `3.2.1`
> 项目状态：**Two-Iteration Refactor CLOSED**
> 本任务性质：正常产品开发，不再属于重构 Iteration。
> 目标：完成 Git 正式收口、清理死 JS、扩展 Subscription Source，并检修 Planner / Jev / Reader 当前频繁出现的降级与未配置问题。

## 0. Control Position

```text
Current Version: 3.2.1
Target Version: 3.2.2
Refactor Status: CLOSED
Current Phase: Normal Product Development
Task Type: Hardening + Source Expansion
```

本任务不重新设计产品架构，不开启新的重构轮次。

## 1. 本次任务范围

本次只处理四类工作：

1. **Git 正式收口**
   - 将已完成的 3.2.1 工作合并到正式 `main`
   - 推送远端
   - 确认本地 / 远端分支一致

2. **清理 3.2.1 报告中剩余的死 JS**
   - 删除“只有定义、没有消费者”的前端函数
   - 删除相应无效调用入口、注释与测试残留
   - 不扩大为全项目前端重构

3. **扩展 Subscription Source**
   - 增加长期抓取源
   - 强化来源分层、健康状态与抓取可观测性
   - 不把临时 Evidence Source 自动升级为 Subscription Source

4. **检修 Planner / Jev / Reader**
   当前频繁出现以下诊断：

```text
planner · plan · planner_failed · not_configured
jev · assess · decision_failed · not_configured
reader · evidence_read · evidence_read_degraded · fetch_failed
reader · evidence_read · evidence_read_degraded · blocked_http
```

需要判断这些诊断分别属于真正未配置、配置存在但未正确加载、Provider purpose 未绑定、请求链没有使用统一设置、Reader 对网络失败分类过粗，还是 Reader 遇到 HTTP 阻断时缺少替代读取路径。

## 2. Git 正式收口

执行前检查：

```bash
git status
git branch -vv
git log --oneline --decorate -n 20
git fetch origin
```

确认：

- `origin/codex/development` 是否包含 3.2.1 最终提交；
- `origin/main` 当前指向；
- main 与 development 是否存在额外分叉；
- 工作区是否干净。

优先使用 fast-forward。若无法 fast-forward，先检查分叉来源，只合并真实需要的 3.2.1 提交，不丢失 main 上合法提交，不使用强制 push。

完成后：

```bash
git push origin main
```

并确认：

```text
local main == origin/main
```

最终报告记录最终 commit hash。

## 3. 清理 Dead JS

3.2.1 报告已确认存在约 8 处“只有定义、没有消费”的 JS，例如 `loadResearchDossier` 等。

对每个候选函数：

1. 全项目搜索定义与调用；
2. 检查 inline HTML/JS、event listener、dynamic string reference、tests、docs；
3. 确认只有定义、没有实际消费者；
4. 删除函数；
5. 删除对应 dead state、无效 DOM query、无效 CSS hook、无效测试断言与旧注释。

不要借本次任务重写整个 Web 前端、换框架、重做 state 管理或清理所有“看起来旧”的 JS。只删除已确认无消费者的死代码。

## 4. Subscription Source 扩展

本轮目标不是简单增加 URL 数量，而是提高：

```text
一手信息覆盖
+
社区反馈覆盖
+
工程项目覆盖
+
来源稳定性
```

### Subscription Source

长期、周期性抓取，进入 Source Registry，具备 source_id、enabled、tier、source_type、adapter_type、fetch_interval、health、last_attempt、last_success、failure diagnostics。

### Evidence Source

只属于某次 Research / Topic Dossier，不自动写入长期抓取源。若某个 Evidence Source 持续高价值，后续可以由用户显式选择“加入长期订阅”，但本轮不要求实现该按钮。

## 5. Source Expansion 第一批建议范围

### Tier A — 官方原始来源

优先扩展：

- OpenAI
- Anthropic
- Google DeepMind
- Google AI
- Meta AI
- Hugging Face
- NVIDIA AI / Technical Blog
- DeepSeek
- 重点模型 / Agent 项目 GitHub Releases

优先抓：

- News / Blog
- Research
- Product Update / Changelog
- 官方文档更新页
- sitemap
- GitHub Releases

国内厂商优先官方站点、官方文档、官方 GitHub / Gitee、公开 sitemap，不优先依赖转载媒体。

### Tier B — 开发者 / 工程社区

继续加强：

- Hacker News
- Reddit AI / LLM / Local LLM / Agent 社区
- GitHub Search / Trending / Releases
- Hugging Face 社区
- 已验证开发者账号

### Tier C — 可靠媒体

主要补充：

- 商业背景
- 采访
- 行业影响
- 跨公司比较

媒体不能替代 Tier A 官方原始来源。

## 6. Source Registry 要求

新增长期源不得只往配置里加 URL，必须明确：

```text
name
source_type
tier
adapter_type
enabled
fetch_interval
identity_verified
topic_scope
language
```

并复用现有 Source Registry。

如需要 schema 变化：

- 使用现有 `PRAGMA user_version`
- 事务迁移
- 幂等
- 有旧库升级测试

## 7. Collection Run 验收

扩源后必须真实验证：

```text
requested_sources
attempted_sources
successful_sources
failed_sources
items_fetched
items_inserted
items_after_dedupe
clusters_created_or_updated
```

同时确认 UI 可以看见本次到底抓了哪些源、哪些成功、哪些失败、每个源抓到了多少内容、是否有 degraded。

## 8. Planner / Jev / Reader 诊断

当前诊断：

```text
planner · plan · planner_failed · not_configured
jev · assess · decision_failed · not_configured
reader · evidence_read · evidence_read_degraded · fetch_failed
reader · evidence_read · evidence_read_degraded · blocked_http
```

本轮必须逐类定位。

## 9. Planner `not_configured`

检查链路：

```text
Settings
→ Provider persistence
→ purpose binding
→ application service
→ planner adapter
```

重点排查：

- 设置页已配置模型，但 Planner purpose 没绑定；
- Planner 仍读取旧配置路径；
- 配置写入 SQLite，但 runtime 没恢复；
- API Key 在 Keychain，但 Planner 没正确取；
- purpose 名字不一致；
- provider 配置正常但 `not_configured` 判断条件过早。

如果系统已有可复用 LLM Provider，建议默认策略：

```text
专用 Planner 已配置
→ 使用专用 Planner

否则通用可用 LLM 已配置
→ 复用通用 LLM

否则
→ planner not_configured
```

是否采用该 fallback，以当前模型配置架构为准。不要新增第二套 API 配置入口。

## 10. Jev `not_configured`

检查：

```text
base_url
api_key
model / endpoint
purpose
enabled state
runtime restore
```

重点排查：

- UI 显示配置成功，但实际没有持久化 purpose；
- API Key 已存但 runtime adapter 取不到；
- Jev Adapter 使用不同 config key；
- restart 后 metadata 恢复、secret 未恢复；
- decision service 没收到 provider；
- `not_configured` 与真实网络错误混淆。

正确分类应区分：

```text
not_configured
timeout
unreachable
rate_limited
provider_error
invalid_response
auth_failed
```

具体错误码复用现有 diagnostics contract。

## 11. Planner / Jev 设置统一

所有配置仍必须位于“设置”。

禁止在资讯页、资源页、研究页、创作页增加临时 API Key / Provider 输入框。

设置页应能明确展示：

```text
Planner: configured / unconfigured
Jev: configured / unconfigured
Article Model: configured / unconfigured
Chat Model: configured / unconfigured
```

但不得回显 secret。

## 12. Reader `fetch_failed`

当前 `fetch_failed` 不一定是 bug。本轮需要先把真实原因分类。

至少区分：

```text
dns_failure
timeout
connection_reset
tls_failure
http_error
invalid_content
unsupported_content
too_large
reader_internal_error
```

如果现有 diagnostics contract 已有对应 code，直接复用。

## 13. Reader `blocked_http`

确认它是否实际包含：

- 403
- 401
- 429
- robots / anti-bot
- Cloudflare
- 登录墙
- User-Agent 拦截
- 重定向到挑战页

不要通过模拟登录、Cookie 绕过、私有接口或绕过网站访问限制来“修复”。

## 14. Reader 合法降级链

建议检查并建立：

```text
Primary page fetch
    ↓ 失败
Feed / original collected content
    ↓ 不足
Search snippet / metadata
    ↓
degraded evidence
```

对于存在合法替代来源的情况：

```text
blocked_http
→ fallback
→ degraded=true
→ fallback_reason=blocked_http
```

不要让单个网页无法读取导致整个 Topic Dossier / Resource Search 失败。

## 15. Reader 与 Evidence Source

Evidence Source 需要保留：

```text
original_url
source_type
read_status
read_method
degraded
cause_code
fallback_source
```

字段名称按现有 contract 为准。

核心要求：用户最终看到的研究结果必须能区分“完整读取”与“降级读取”。

## 16. 本轮不做

明确排除：

- Resource → Visual 一键出图
- Jev 排名权重调优
- Topic Dossier 图谱
- 新发布平台
- 新前端框架
- 全项目 web.py 重构
- 全项目 JS 重构
- 全项目 Reader 重写
- 绕过站点访问控制
- 扩大为第三轮架构重构

## 17. 测试要求

### Git / Dead JS
- dead function 无消费者
- 删除后 JS runtime 无 error
- 页面核心功能不退化

### Subscription Source
- 新 source config
- enabled / disabled
- source type
- adapter type
- collect success
- collect failure
- collection run
- dedupe / cluster

### Planner
- configured
- not_configured
- provider fallback
- restart restore
- timeout
- invalid response

### Jev
- configured
- not_configured
- auth failure
- timeout
- rate limit
- invalid response
- decision success
- objective-only fallback

### Reader
- normal page
- 403 / blocked
- timeout
- DNS / connection failure
- invalid content
- fallback to collected content
- degraded evidence

## 18. 真实验收

至少完成：

1. 设置页真实配置 Planner / Jev；
2. 重启服务；
3. 确认配置恢复；
4. 发起一次资讯 Research；
5. Planner 不再错误报 `not_configured`；
6. Jev 不再错误报 `not_configured`；
7. 有可访问 Evidence 时 Reader 正常读取；
8. 人为选择一个 blocked / failed URL；
9. Reader 正确 degraded；
10. Topic Dossier 仍能完成；
11. Resource Discovery 仍正常；
12. Article Generation 仍正常；
13. Publishing 不退化；
14. 新 Subscription Sources 可在 Collection Run 中看到真实抓取记录。

## 19. 全量回归

执行：

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
git diff --check
```

并完成：

- py_compile
- secret scan
- 真实浏览器 / faithful runtime 验收

## 20. 版本与交付

目标版本：

```text
3.2.2
```

完成后：

- 更新 README
- 更新 WEB_FUNCTIONAL_TEST
- 新增 `docs/PATCH_3.2.2.md`
- 生成 archive + sha256
- commit
- merge / fast-forward 到 main
- push `origin/main`
- 确认工作区干净

## 21. Completion Report

最终报告按：

```text
# AutoPosting 3.2.2 Completion Report

## Git Closure
## Dead JS Removed
## Subscription Sources Added
## Planner Diagnosis
## Jev Diagnosis
## Reader Diagnosis
## Changed
## Deleted
## Migration
## Tests
## Real UI Verification
## Known Limitations
## Remaining Work
## Final Git State
```

## 22. Definition of Done

- [ ] 3.2.1 最终代码已经进入 `main`
- [ ] `origin/main` 已推送
- [ ] 本地 main 与远端一致
- [ ] 已确认的 dead JS 已删除
- [ ] JS runtime 无新增错误
- [ ] 新 Subscription Source 已接入
- [ ] Source Registry 正确展示新增源
- [ ] Collection Run 能看到实际抓取结果
- [ ] Planner 配置链已检修
- [ ] 已配置时 Planner 不再报 `not_configured`
- [ ] Jev 配置链已检修
- [ ] 已配置时 Jev 不再报 `not_configured`
- [ ] 网络 / auth / provider 错误不再误分类为 `not_configured`
- [ ] Reader `fetch_failed` 已细化或保留足够 diagnostics
- [ ] Reader `blocked_http` 有合法 degraded / fallback 语义
- [ ] 单一 Evidence Source 失败不拖死整个 Research
- [ ] Settings 仍是所有 API / Provider 的唯一配置入口
- [ ] Resource Discovery 不退化
- [ ] Topic Dossier 不退化
- [ ] Article Generation 不退化
- [ ] Publishing 不退化
- [ ] 全量测试通过
- [ ] self-check 通过
- [ ] `git diff --check` 通过
- [ ] 真实 UI / 服务验收通过
- [ ] 3.2.2 文档、归档与 sha256 完成
- [ ] 最终提交已推送到 `origin/main`

## 23. 本轮最终目标

本任务结束后，AutoPosting 应从：

```text
架构已经稳定
但：
- main 尚未正式收口
- 有少量 dead JS
- 长期信息源覆盖不足
- Planner / Jev 经常显示 not_configured
- Reader 降级诊断噪音较大
```

进入：

```text
正式 main 已收口
+
前端旧死代码已清理
+
Subscription Source 覆盖扩展
+
Planner / Jev 配置链稳定
+
Reader 失败可解释、可降级
+
正常产品开发状态
```
