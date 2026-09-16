from __future__ import annotations

from pathlib import Path

from autoposting.image_gen import image_prompt_dir


def visual_root() -> Path:
    return image_prompt_dir()


def tokens_path() -> Path:
    return visual_root() / "design-system" / "tokens.json"


def schema_path(kind: str) -> Path:
    return visual_root() / "content-schemas" / f"{kind}.schema.json"


def example_path(kind: str) -> Path:
    return visual_root() / "examples" / f"{kind}.example.json"


def rules_path() -> Path:
    return visual_root() / "rendering" / "rules.json"
