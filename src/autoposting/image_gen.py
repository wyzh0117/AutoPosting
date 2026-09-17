"""GitHub weekly render plan: pure job metadata for the HTML visual pipeline.

This module owns no image-model prompt text and never calls an image API. The
weekly and skill lanes are rendered by :mod:`autoposting.visual` (HTML/CSS plus
an optional browser screenshot); the plan built here only names the pages.
"""

from __future__ import annotations


def build_weekly_render_plan(repos: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build the cover + poster job list for a GitHub weekly visual package."""
    jobs: list[dict[str, object]] = [
        {
            "id": "cover",
            "kind": "cover",
            "rank": 0,
            "title": "本周爆火ai工具",
            "url": "",
            "file": "",
            "selected": True,
        }
    ]
    for repo in repos[:10]:
        rank = int(repo.get("rank") or len(jobs))
        name = str(repo.get("name") or f"repo-{rank}")
        jobs.append(
            {
                "id": f"{rank:02d}-{_safe_name(name)}",
                "kind": "poster",
                "rank": rank,
                "title": name,
                "url": "",
                "file": "",
                "selected": True,
            }
        )
    return jobs


def _safe_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-._" else "-" for char in name)
    return cleaned.strip("-._")[:40] or "repo"
