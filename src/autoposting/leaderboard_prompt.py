"""Load poster-copy instructions from docs/image_prompts/copy*.md."""

from __future__ import annotations

from pathlib import Path

COPY_PROMPT_FILENAME = "copy.md"
SKILL_COPY_PROMPT_FILENAME = "copy_skill.md"


def leaderboard_copy_prompt_paths() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[2] / "docs" / "image_prompts" / COPY_PROMPT_FILENAME,
        Path.cwd() / "docs" / "image_prompts" / COPY_PROMPT_FILENAME,
    )


def load_leaderboard_copy_prompt() -> str:
    return _load(COPY_PROMPT_FILENAME, "leaderboard_copy_prompt_missing")


def skill_copy_prompt_paths() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[2] / "docs" / "image_prompts" / SKILL_COPY_PROMPT_FILENAME,
        Path.cwd() / "docs" / "image_prompts" / SKILL_COPY_PROMPT_FILENAME,
    )


def load_skill_copy_prompt() -> str:
    """Skill-lane copy instructions; falls back to the weekly prompt if absent."""
    return _load(SKILL_COPY_PROMPT_FILENAME, "leaderboard_copy_prompt_missing")


def _load(filename: str, missing_code: str) -> str:
    here = Path(__file__).resolve()
    for candidate in (
        here.parents[2] / "docs" / "image_prompts" / filename,
        Path.cwd() / "docs" / "image_prompts" / filename,
    ):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
            break
    if filename != COPY_PROMPT_FILENAME:
        return _load(COPY_PROMPT_FILENAME, missing_code)
    raise FileNotFoundError(missing_code)
