"""Load GitHub weekly poster-copy instructions from docs/image_prompts/copy.md."""

from __future__ import annotations

from pathlib import Path

COPY_PROMPT_FILENAME = "copy.md"


def leaderboard_copy_prompt_paths() -> tuple[Path, ...]:
    here = Path(__file__).resolve()
    return (
        here.parents[2] / "docs" / "image_prompts" / COPY_PROMPT_FILENAME,
        Path.cwd() / "docs" / "image_prompts" / COPY_PROMPT_FILENAME,
    )


def load_leaderboard_copy_prompt() -> str:
    for candidate in leaderboard_copy_prompt_paths():
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    raise FileNotFoundError("leaderboard_copy_prompt_missing")
