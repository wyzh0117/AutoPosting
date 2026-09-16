# AutoPosting 0.4.5

## 变更

- 将所有文章共用的事实、输出、优先级和平台规则迁移到 `docs/ARTICLE_BASE_PROMPT.md`，旧 `docs/ARTICLE_PROMPT.md` 不再作为入口。
- 将文章类型从 7 个数字占位扩展为 8 个具名选项：消息、通讯、新闻特写、新闻专访、调查报道、深度报道、评论/述评、AI专家。
- 1—7 类分别定义选材、结构、证据门槛、受控降级和防串味规则；第 8 类承接原有“专业嗅觉”和 `chosen_hooks` 写法。
- 下拉框、Web API、Workbench 和直接文章模型调用均默认选择“8 AI专家”，保持升级前的默认写稿行为。
- Prompt 明确按“设置页写作风格 → 基础硬约束 → 文章类型 → 平台呈现”处理冲突；程序返回后的 JSON、来源 ID 和 AIGC 校验保持不变。
- 同步更新 `AGENTS.md`、README、文章 Prompt 版本 `article-v7` 和应用版本 `0.4.5`。
- 同步更新项目内 `autoposting` 技能的文章调教入口说明，避免后续维护继续引用旧文件。

## 边界

- 设置页写作风格是 Prompt 内最高优先级；若它要求的输出无法通过程序校验，稿件仍会被现有校验器拒收。
- 材料不足时不会停止生成：专访降为观点综述，调查报道降为公开信息核对，其他类型按各自文件收窄结论。
- 本次仍只保存每个 item/platform 的当前草稿，不新增八份并行草稿历史。
- 未新增第三方运行时依赖，也未改变发布安全边界。
- 未经额外授权，没有把现有候选内容发送到外部模型批量生成八篇验收稿；使用本地忠实夹具验证了八类 Prompt 的完整组装与隔离。

## 验证

- `PYTHONPATH=src:tests python3 -m unittest discover -s tests -v`：210 项通过。
- `PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check`：全部检查通过。
- 本地忠实夹具以同一素材组装 1—8 类最终 Prompt：八类身份、降级规则、来源/AIGC 约束及设置页风格后置全部通过断言；“一般人容易看漏的角度”仅出现在第 8 类。
- 经 `ServiceController` 重启后，`/api/status` 返回 `0.4.5`。
- 真实 Chromium 页面显示 8 个完整名称，默认选中“8 AI专家”。
- `git diff --check`、独立 review 和补丁归档校验在交付前执行。
