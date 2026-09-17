"""Shared poster-copy fixtures for tests. Not production fallback copy."""

from __future__ import annotations


def poster_copy(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "summary": "在终端里直接把提示词交给本地模型并回显结果。",
        "does": "把提示词发给本地模型。\n把模型输出打到终端。",
        "how": "读入命令行参数。\n调用已配置的模型接口。",
        "audience": "习惯在项目目录里试验提示词的人。",
        "scene": "需要对着仓库立刻问模型的时候。",
        "enriched": True,
    }
    row.update(overrides)
    return row


def with_poster_copy(repo: dict[str, object], **overrides: object) -> dict[str, object]:
    row = dict(repo)
    row.update(poster_copy(**overrides))
    return row


def stub_copy_writer(repos: list[dict[str, object]]) -> list[dict[str, object]]:
    updated: list[dict[str, object]] = []
    for repo in repos:
        name = str(repo.get("name") or "skill")
        updated.append(
            with_poster_copy(
                repo,
                summary=f"用{name}完成这一步并交出可用结果。",
                does=f"处理{name}相关材料。\n产出可用结果。",
            )
        )
    return updated


def attach_stub_poster_copy(app: object) -> None:
    app._leaderboard_model_configured = True  # type: ignore[attr-defined]
    app.leaderboard_service.enrich_github_repos = stub_copy_writer  # type: ignore[attr-defined]
    app.leaderboard_service.enrich_skill_repos = stub_copy_writer  # type: ignore[attr-defined]
