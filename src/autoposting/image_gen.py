"""GitHub weekly model images: load prompt .md files, then generate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

IMAGE_PROMPT_DIRNAME = "image_prompts"
IMAGE_PROMPT_FILES = {
    "style": "STYLE.md",
    "cover": "cover.md",
    "poster": "poster.md",
}
GenerateFn = Callable[[str], bytes]


class _TemplateMap(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def image_prompt_dir() -> Path:
    override = (os.environ.get("AUTOPOSTING_IMAGE_PROMPT_DIR") or "").strip()
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    packaged = here.parents[2] / "docs" / IMAGE_PROMPT_DIRNAME
    if packaged.is_dir():
        return packaged
    return Path.cwd() / "docs" / IMAGE_PROMPT_DIRNAME


def load_image_prompt(name: str) -> str:
    filename = IMAGE_PROMPT_FILES.get(name)
    if filename is None:
        raise ValueError(f"unknown image prompt: {name}")
    path = image_prompt_dir() / filename
    if not path.is_file():
        raise FileNotFoundError(f"image_prompt_missing:{name}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FileNotFoundError(f"image_prompt_empty:{name}")
    return text


def load_weekly_image_prompts() -> dict[str, str]:
    return {name: load_image_prompt(name) for name in IMAGE_PROMPT_FILES}


def _render_prompt(template: str, **values: object) -> str:
    mapping = _TemplateMap({key: str(value) for key, value in values.items()})
    return template.format_map(mapping).strip()


def build_weekly_image_jobs(repos: list[dict[str, object]]) -> list[dict[str, object]]:
    prompts = load_weekly_image_prompts()
    style = prompts["style"]
    names = [str(repo.get("name") or repo.get("full_name") or "") for repo in repos[:10]]
    ranking = "、".join(f"{index + 1}.{name}" for index, name in enumerate(names) if name)
    cover_prompt = _render_prompt(
        prompts["cover"],
        style=style,
        ranking=ranking or "ten unnamed AI tools",
    )
    jobs: list[dict[str, object]] = [
        {
            "id": "cover",
            "kind": "cover",
            "rank": 0,
            "title": "本周爆火ai工具",
            "prompt": cover_prompt,
            "url": "",
            "file": "",
            "selected": True,
        }
    ]
    for repo in repos[:10]:
        rank = int(repo.get("rank") or len(jobs))
        name = str(repo.get("name") or f"repo-{rank}")
        summary = str(repo.get("summary") or repo.get("description") or "")
        does = str(repo.get("does") or "")
        scene = str(repo.get("scene") or "")
        kind = str(repo.get("kind") or "")
        prompt = _render_prompt(
            prompts["poster"],
            style=style,
            name=name,
            rank=rank,
            rank02=f"{rank:02d}",
            summary=summary,
            does=does,
            scene=scene,
            kind=kind,
        )
        jobs.append(
            {
                "id": f"{rank:02d}-{_safe_name(name)}",
                "kind": "poster",
                "rank": rank,
                "title": name,
                "prompt": prompt,
                "url": "",
                "file": "",
                "selected": True,
            }
        )
    return jobs


def render_weekly_images(
    jobs: list[dict[str, object]],
    generate: GenerateFn,
    out_dir: Path,
) -> list[dict[str, object]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[dict[str, object]] = []
    for job in jobs:
        prompt = str(job.get("prompt") or "")
        payload = generate(prompt)
        if not payload:
            rendered.append(dict(job))
            continue
        filename = f"{job['id']}.png"
        (out_dir / filename).write_bytes(payload)
        row = dict(job)
        row["file"] = filename
        row["url"] = f"/api/generated/github-weekly/{filename}"
        row["selected"] = True
        rendered.append(row)
    return rendered


def _safe_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-._" else "-" for char in name)
    return cleaned.strip("-._")[:40] or "repo"
