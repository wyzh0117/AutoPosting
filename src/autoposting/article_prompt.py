"""Load article-generation instructions from docs/ARTICLE_PROMPT.md."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ARTICLE_PROMPT_FILENAME = "ARTICLE_PROMPT.md"
DEFAULT_ARTICLE_PROMPT_VERSION = "article-v4"
_SECTION_RE = re.compile(r"^##\s+(\S+)\s*$", re.MULTILINE)
_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
_PLATFORM_ALIASES = {
    "wechat": ("微信", "wechat"),
    "xiaohongshu": ("小红书", "xiaohongshu"),
    "shared": ("共用", "shared"),
}


def article_prompt_paths() -> tuple[Path, ...]:
    override = (os.environ.get("AUTOPOSTING_ARTICLE_PROMPT") or "").strip()
    here = Path(__file__).resolve()
    return tuple(
        path
        for path in (
            Path(override) if override else None,
            here.parents[2] / "docs" / ARTICLE_PROMPT_FILENAME,
            Path.cwd() / "docs" / ARTICLE_PROMPT_FILENAME,
        )
        if path is not None
    )


def load_article_prompt_doc(path: str | Path | None = None) -> str:
    if path is not None:
        return Path(path).read_text(encoding="utf-8")
    for candidate in article_prompt_paths():
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("article_prompt_doc_missing")


def parse_article_prompt_doc(text: str) -> dict[str, str]:
    match = _VERSION_RE.search(text or "")
    version = match.group(1) if match else DEFAULT_ARTICLE_PROMPT_VERSION
    parts = _SECTION_RE.split(text or "")
    sections = {"version": version, "共用": "", "微信": "", "小红书": ""}
    if len(parts) < 3:
        return sections
    for index in range(1, len(parts), 2):
        name = parts[index].strip()
        body = parts[index + 1].strip() if index + 1 < len(parts) else ""
        sections[name] = body
    return sections


def article_prompt_version(text: str | None = None) -> str:
    doc = text if text is not None else load_article_prompt_doc()
    return parse_article_prompt_doc(doc)["version"]


def _section(sections: dict[str, str], key: str) -> str:
    for name in _PLATFORM_ALIASES[key]:
        if sections.get(name):
            return sections[name]
    return ""


def build_article_prompt(
    platform: str,
    source_ids: tuple[str, ...] | list[str],
    payload: dict[str, object],
    style: str = "",
    doc_text: str | None = None,
) -> str:
    if platform not in {"wechat", "xiaohongshu"}:
        raise ValueError(f"unsupported platform: {platform}")
    sections = parse_article_prompt_doc(
        doc_text if doc_text is not None else load_article_prompt_doc()
    )
    shared = _section(sections, "shared")
    specific = _section(sections, platform)
    if not specific:
        raise ValueError(f"article_prompt_missing_section:{platform}")
    style_block = f"写作风格要求：\n{style.strip()}\n" if style.strip() else ""
    data = json.dumps(payload, ensure_ascii=False)
    values = {
        "{source_ids}": ", ".join(source_ids),
        "{style_block}": style_block,
        "{data}": data,
    }
    filled_shared = shared
    filled_specific = specific
    for token, value in values.items():
        filled_shared = filled_shared.replace(token, value)
        filled_specific = filled_specific.replace(token, value)
    return f"{filled_shared}\n{filled_specific}\n"


ARTICLE_PROMPT_VERSION = DEFAULT_ARTICLE_PROMPT_VERSION
try:
    ARTICLE_PROMPT_VERSION = article_prompt_version()
except (OSError, FileNotFoundError, KeyError):
    ARTICLE_PROMPT_VERSION = DEFAULT_ARTICLE_PROMPT_VERSION
