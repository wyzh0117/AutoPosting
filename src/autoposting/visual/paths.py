"""Filesystem resolution for the HTML visual asset root.

The asset root holds the design system, content schemas, examples and rendering
rules consumed by the HTML/CSS pipeline. It is not an image-model prompt
directory: the folder keeps its historical name because
``AUTOPOSTING_IMAGE_PROMPT_DIR`` is a public override used by tests.
"""

from __future__ import annotations

import os
from pathlib import Path

ASSET_ROOT_ENV = "AUTOPOSTING_IMAGE_PROMPT_DIR"
ASSET_DIRNAME = "image_prompts"


def asset_root() -> Path:
    """Resolve the visual asset root.

    Order: ``AUTOPOSTING_IMAGE_PROMPT_DIR`` override, then ``<repo>/docs/image_prompts``
    when that directory exists, then ``cwd()/docs/image_prompts``.
    """
    override = (os.environ.get(ASSET_ROOT_ENV) or "").strip()
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parents[3] / "docs" / ASSET_DIRNAME
    if packaged.is_dir():
        return packaged
    return Path.cwd() / "docs" / ASSET_DIRNAME


def visual_root() -> Path:
    """Backwards-compatible alias for :func:`asset_root`."""
    return asset_root()


def tokens_path() -> Path:
    return asset_root() / "design-system" / "tokens.json"


def schema_path(kind: str) -> Path:
    return asset_root() / "content-schemas" / f"{kind}.schema.json"


def example_path(kind: str) -> Path:
    return asset_root() / "examples" / f"{kind}.example.json"


def rules_path() -> Path:
    return asset_root() / "rendering" / "rules.json"
