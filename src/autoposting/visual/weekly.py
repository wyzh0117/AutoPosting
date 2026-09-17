from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from autoposting.github_weekly import week_window_start

from .adapter import project_from_repo, ranking_from_repos
from .components import AUTHOR_BRAND, author_avatar_data_uri
from .marks import is_author_avatar, resolve_mark
from .media import fetch_bytes, fetch_data_uri
from .pipeline import render_page
from .story import find_readme_logo, load_readme
from .tokens import default_weekly_palette, normalize_poster_template, resolve_tokens


def render_weekly_visual(
    repos: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    out_dir: Path,
    theme: str = "dark",
    palette: dict[str, str] | None = None,
    screenshot: bool = True,
    screenshot_fn: Callable[..., Path] | None = None,
    *,
    brand: str | None = None,
    series: str | None = None,
    source: str | None = None,
    footer: str | None = None,
    kicker: str | None = None,
    issue: str | None = None,
    poster_template: str = "1",
) -> list[dict[str, Any]]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    catalog_dir = target.parent / "visual-marks"
    date_range = week_window_start()
    resolved_brand = AUTHOR_BRAND if brand is None else brand
    avatar = author_avatar_data_uri() if resolved_brand == AUTHOR_BRAND else ""
    total = max(min(len(repos), 10), 1)
    rendered: list[dict[str, Any]] = []
    for job in jobs:
        kind = str(job.get("kind") or "")
        stem = str(job.get("id") or kind or "page")
        render_rank = 0
        if kind == "cover":
            payload = ranking_from_repos(
                repos,
                brand=brand,
                series=series,
                issue=issue,
                date_range=date_range,
                avatar=avatar,
                source=source,
                footer=footer,
                kicker=kicker,
            )
            if job.get("visual_title"):
                payload["title"] = str(job["visual_title"])
            if job.get("visual_subtitle"):
                payload["subtitle"] = str(job["visual_subtitle"])
            if job.get("visual_kicker"):
                payload["kicker"] = str(job["visual_kicker"])
            if job.get("visual_footer"):
                payload["footer"] = str(job["visual_footer"])
            for item in payload.get("items") or []:
                if isinstance(item, dict):
                    _apply_mark(item, catalog_dir)
        else:
            repo = _match_repo(repos, job)
            if repo is None:
                rendered.append(dict(job))
                continue
            render_rank = int(repo.get("rank") or 1)
            payload = project_from_repo(
                repo,
                brand=brand,
                series=series,
                issue=issue,
                page_number=f"{render_rank + 1:02d} / {total + 1:02d}",
                avatar=avatar,
                source=source,
                footer=footer,
            )
            _apply_project_mark(payload, repo, catalog_dir)
        weekly_palette = default_weekly_palette(theme)
        weekly_palette.update(palette or {})
        job_tokens = resolve_tokens(theme, weekly_palette)
        if kind == "cover":
            variant = "cover"
            job_tokens["canvas"] = {"width": 1080, "height": 1440, "aspect": "3:4"}
        else:
            variant = "poster-a" if render_rank % 2 else "poster-b"
            job_tokens["canvas"] = {"width": 1080, "height": 1440, "aspect": "3:4"}
        job_tokens["meta"] = dict(job_tokens.get("meta") or {})
        job_tokens["meta"]["template_variant"] = variant
        job_tokens["meta"]["poster_template"] = normalize_poster_template(poster_template)
        result = render_page(
            payload,
            out_dir=target,
            screenshot=screenshot,
            tokens=job_tokens,
            file_stem=stem,
            screenshot_fn=screenshot_fn,
        )
        row = dict(job)
        if result.get("png") or result.get("html"):
            filename = f"{stem}.png"
            if result.get("png"):
                row["file"] = filename
            row["url"] = f"/api/generated/github-weekly/{filename}?v={time.time_ns()}"
        row["selected"] = True
        rendered.append(row)
    return rendered


def _apply_mark(item: dict[str, Any], catalog_dir: Path) -> None:
    logo = str(item.get("logo") or "")
    if logo and not is_author_avatar(logo):
        embedded = fetch_data_uri(logo)
        item["logo"] = embedded or ""
        item["markSvg"] = ""
        if item["logo"]:
            return
    item["logo"] = ""
    mark = resolve_mark(
        name=str(item.get("name") or ""),
        kind=str(item.get("category") or ""),
        description=str(item.get("description") or ""),
        catalog_dir=catalog_dir,
    )
    item["markSvg"] = str(mark.get("svg") or "")


def _apply_project_mark(payload: dict[str, Any], repo: dict[str, Any], catalog_dir: Path) -> None:
    owner = str(repo.get("owner") or "")
    name = str(repo.get("name") or "")
    readme = load_readme(owner, name, fetch_bytes) if owner and name else ""
    found = find_readme_logo(readme, owner=owner, repo=name) if readme else ""
    if found:
        embedded = fetch_data_uri(found)
        if embedded:
            payload["logo"] = embedded
            payload["markSvg"] = ""
            return
    logo = str(payload.get("logo") or "")
    if logo and not is_author_avatar(logo):
        embedded = fetch_data_uri(logo)
        if embedded:
            payload["logo"] = embedded
            payload["markSvg"] = ""
            return
    payload["logo"] = ""
    mark = resolve_mark(
        name=name,
        kind=str(repo.get("kind") or payload.get("category") or ""),
        topics=list(repo.get("topics") or []),
        description=str(repo.get("description") or repo.get("summary") or ""),
        catalog_dir=catalog_dir,
    )
    payload["markSvg"] = str(mark.get("svg") or "")


def _match_repo(repos: list[dict[str, Any]], job: dict[str, Any]) -> dict[str, Any] | None:
    rank = int(job.get("rank") or 0)
    title = str(job.get("title") or "")
    for repo in repos:
        if rank and int(repo.get("rank") or 0) == rank:
            return repo
    for repo in repos:
        if title and str(repo.get("name") or "") == title:
            return repo
    return None
