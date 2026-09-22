# PATCH 2.0.0-r1 — 可读性审查修复实施计划文档记录

版本基线：`2.0.0`（`pyproject.toml` 当前版本）。本次仅记录可读性审查及其修复实施计划，不是产品版本升级，也没有执行业务修复。

## 改了什么

- 保留并归档本轮文档交付：`docs/PROJECT_READABILITY_AUDIT_2026-09-19.md` 与 `docs/PROJECT_READABILITY_REMEDIATION_PLAN_2026-09-19.md`。
- 新增本记录文件，说明文档交付的边界、验收状态和快照归档位置。
- 未修改 `src/`、`tests/`、`config/`、数据库、运行数据、服务行为或任何 prompt 文件。

## 命名与边界

- `docs/PATCH_2.0.0.md` 已存在，因此没有覆盖它；本记录使用最小且可追溯的 `-r1` 后缀。
- `archives/autoposting-patch-2.0.0-update-20260919.tar.gz` 及其 `.sha256` 已存在，因此本次快照使用对应的 `-r1` 后缀；原归档保持不变。
- 归档仅包含 `src/`、`tests/`、`docs/`、`config/` 及根目录允许的项目文件；不包含 `data/`、`output/`、`archives/`、`.git/`、缓存、`__pycache__`、`.venv/`、`.env` 或 secrets。

## 验证

- 版本核对：`pyproject.toml` → `2.0.0`。
- 冲突核对：标准补丁记录和标准归档目标均已存在，均未覆盖。
- 测试、离线 self-check、服务重启及浏览器行为验证：未运行；本次是文档/归档记录，不改变运行行为。
- 归档生成后检查成员路径无禁入项，并使用 `sha256 -c` 验证通过。

## 归档

- 快照：`archives/autoposting-patch-2.0.0-update-20260919-r1.tar.gz`
- 校验：`archives/autoposting-patch-2.0.0-update-20260919-r1.tar.gz.sha256`
