from __future__ import annotations

import re
from typing import Any

from autoposting.github_weekly import iso_week_issue

from .components import AUTHOR_BRAND, author_avatar_data_uri
from .marks import is_author_avatar

CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADING = re.compile(r"^#{1,6}\s+")
_MD_LIST = re.compile(r"^[-*+]\s+")
_PLACEHOLDER_LABELS = ("一句话看懂", "能做什么", "如何实现", "适合哪些人", "使用场景", "做什么？")


def _lane_text(value: str | None, default: str) -> str:
    """``None`` keeps the GitHub weekly default; an explicit ``""`` stays blank."""
    return default if value is None else value


def ranking_from_repos(
    repos: list[dict[str, Any]],
    *,
    brand: str | None = AUTHOR_BRAND,
    series: str | None = "GitHub 本周爆火 AI 项目榜",
    issue: str | None = None,
    date_range: str | None = "",
    avatar: str = "",
    source: str | None = "GitHub",
    footer: str | None = "专业 AI 科技媒体 · 每周更新",
    kicker: str | None = "WEEKLY RANKING",
    title: str | None = "GitHub",
    subtitle: str | None = "本周爆火 AI 项目榜",
    metric_label: str = "Stars",
) -> dict[str, Any]:
    items = []
    tags: list[str] = []
    for index, repo in enumerate(repos[:10], start=1):
        kind = str(repo.get("kind") or repo.get("category") or "")
        if kind and kind not in tags:
            tags.append(kind)
        items.append(
            {
                "rank": int(repo.get("rank") or index),
                "name": str(repo.get("name") or ""),
                "description": str(repo.get("summary") or repo.get("description") or ""),
                "category": kind,
                "logo": _safe_logo(repo),
                "url": str(repo.get("url") or ""),
                "metric": {
                    "label": metric_label,
                    "value": int(repo.get("stars") or 0),
                },
            }
        )
    resolved_brand = _lane_text(brand, AUTHOR_BRAND)
    return {
        "page": "ranking-overview",
        "brand": resolved_brand,
        "series": _lane_text(series, "GitHub 本周爆火 AI 项目榜"),
        "issue": iso_week_issue() if issue is None else issue,
        "dateRange": _lane_text(date_range, ""),
        "kicker": _lane_text(kicker, "WEEKLY RANKING"),
        "title": _lane_text(title, "GitHub"),
        "subtitle": _lane_text(subtitle, "本周爆火 AI 项目榜"),
        "source": _lane_text(source, "GitHub"),
        "footer": _lane_text(footer, "专业 AI 科技媒体 · 每周更新"),
        "tags": tags[:6],
        "items": items,
        "avatar": avatar or (author_avatar_data_uri() if resolved_brand == AUTHOR_BRAND else ""),
    }


def project_from_repo(
    repo: dict[str, Any],
    *,
    brand: str | None = AUTHOR_BRAND,
    series: str | None = "GitHub 本周爆火 AI 项目榜",
    issue: str | None = None,
    page_number: str = "",
    avatar: str = "",
    source: str | None = "GitHub",
    footer: str | None = "专业 AI 科技媒体 · 每周更新",
) -> dict[str, Any]:
    kind = str(repo.get("kind") or "AI 工具")
    features = to_bullets(repo.get("does") or "", max_len=48)
    how = to_bullets(repo.get("how") or "")
    audience = to_bullets(repo.get("audience") or "")
    scene = to_bullets(repo.get("scene") or "")
    name = str(repo.get("name") or "")
    key_message = _clip_line(strip_poster_markup(str(repo.get("summary") or "")), 72)
    tagline = str(repo.get("tagline") or "").strip()
    if (
        not tagline
        or tagline == name
        or tagline == key_message
        or tagline == str(repo.get("summary") or "").strip()
    ):
        tagline = ""
    resolved_brand = _lane_text(brand, AUTHOR_BRAND)
    return {
        "page": "project-detail",
        "brand": resolved_brand,
        "series": _lane_text(series, "GitHub 本周爆火 AI 项目榜"),
        "issue": iso_week_issue() if issue is None else issue,
        "avatar": avatar or (author_avatar_data_uri() if resolved_brand == AUTHOR_BRAND else ""),
        "pageNumber": page_number,
        "rank": int(repo.get("rank") or 1),
        "category": kind,
        "name": name,
        "tagline": tagline,
        "description": str(repo.get("description") or ""),
        "keyMessage": key_message,
        "logo": _safe_logo(repo),
        "markSvg": str(repo.get("markSvg") or ""),
        "features": features,
        "how": how,
        "audience": audience,
        "scene": scene,
        "url": str(repo.get("url") or ""),
        "source": _lane_text(source, "GitHub"),
        "footer": _lane_text(footer, "专业 AI 科技媒体 · 每周更新"),
    }


def to_bullets(text: Any, *, limit: int = 5, max_len: int = 28) -> list[str]:
    """Split poster fields into lines. Does not rewrite or filter wording."""
    if isinstance(text, (list, tuple)):
        parts = [strip_poster_markup(str(item)) for item in text if str(item).strip()]
    else:
        raw = strip_poster_markup(str(text or ""))
        if not raw:
            return []
        if "\n" in raw:
            parts = [line.strip(" -•\t") for line in raw.splitlines() if line.strip()]
        elif "。" in raw:
            parts = [f"{piece.strip()}。" for piece in raw.split("。") if piece.strip()]
        else:
            parts = _split(raw)
        if len(parts) == 1 and len(parts[0]) > 40 and "。" not in parts[0]:
            parts = [
                piece.strip()
                for piece in re.split(r"[；;]|(?<=[\u4e00-\u9fff])，(?=[\u4e00-\u9fff]{4,})", parts[0])
                if piece.strip()
            ]
    parts = _explode_cjk_spaces(parts)
    cleaned: list[str] = []
    for part in parts:
        piece = part.strip(" ，、；;")
        if piece and piece not in cleaned:
            cleaned.append(piece)
        if len(cleaned) >= limit:
            break
    return cleaned


def strip_poster_markup(text: str) -> str:
    """Drop markdown markers and prompt-placeholder labels before a poster prints."""
    value = str(text or "")
    value = _MD_BOLD.sub(r"\1", value)
    value = _MD_ITALIC.sub(r"\1", value)
    value = _MD_CODE.sub(r"\1", value)
    kept: list[str] = []
    for raw in value.splitlines():
        line = _MD_HEADING.sub("", raw)
        line = _MD_LIST.sub("", line)
        line = line.replace("**", "").replace("`", "").strip(" -•\t")
        if line in _PLACEHOLDER_LABELS:
            continue
        for label in _PLACEHOLDER_LABELS:
            if line.startswith(label):
                line = line[len(label) :].lstrip("：: ").strip()
                break
        if line:
            kept.append(line)
    return "\n".join(kept).strip()


def _safe_logo(repo: dict[str, Any]) -> str:
    url = str(repo.get("icon_url") or repo.get("logo") or "")
    if not url or is_author_avatar(url):
        return ""
    return url


def _clip_line(text: str, limit: int) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _split(text: str) -> list[str]:
    for separator in ("、", "，", ",", "；", ";"):
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text.strip()] if text.strip() else []


def _explode_cjk_spaces(parts: list[str]) -> list[str]:
    out: list[str] = []
    for part in parts:
        chunks = [item.strip() for item in CJK_SPACE_RE.split(part) if item.strip()]
        out.extend(chunks)
    return out
