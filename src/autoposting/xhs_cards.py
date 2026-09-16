"""Fixed 9:16 Xiaohongshu note cards. Layout is template-owned."""

from __future__ import annotations

import re

from .normalize import contains_source_id

XHS_CARD_WIDTH = 1080
XHS_CARD_HEIGHT = 1920
XHS_MAX_CARDS = 18
XHS_PAGE_CHARS = 380
XHS_PLACEHOLDER_ID = "@小红书号"
XHS_CAPTION_FULL_LIMIT = 500
_BLANK_RE = re.compile(r"\n\s*\n")
_CAPTION_DASH_RE = re.compile(r"(?m)^-\s+\S")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def first_paragraph(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    return _BLANK_RE.split(cleaned, maxsplit=1)[0].strip().split("\n", 1)[0].strip()


def paginate_text(text: str, limit: int = XHS_PAGE_CHARS) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if limit < 1:
        return [cleaned]
    paragraphs = [part.strip() for part in _BLANK_RE.split(cleaned) if part.strip()]
    pages: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for chunk in _chunks(paragraph, limit):
            candidate = chunk if not current else f"{current}\n\n{chunk}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                pages.append(current)
            current = chunk
    if current:
        pages.append(current)
    return pages


def build_xhs_cards(
    title: str,
    manuscript: str,
    *,
    cover_url: str = "",
    handle: str = "",
) -> dict[str, object]:
    label = handle.strip() or XHS_PLACEHOLDER_ID
    remainder = (manuscript or "").strip()
    cover: dict[str, object] = {
        "kind": "cover",
        "title": title,
        "handle": label,
        "avatar": "",
        "width": XHS_CARD_WIDTH,
        "height": XHS_CARD_HEIGHT,
        "image_url": "",
        "lede": "",
    }
    if cover_url.strip():
        cover["image_url"] = cover_url.strip()
    else:
        lede, remainder = _split_lede(remainder)
        cover["lede"] = lede
    pages = paginate_text(remainder)[: XHS_MAX_CARDS - 1]
    cards = [cover]
    for page in pages:
        cards.append(
            {
                "kind": "page",
                "text": page,
                "handle": label,
                "avatar": "",
                "width": XHS_CARD_WIDTH,
                "height": XHS_CARD_HEIGHT,
            }
        )
    return {
        "width": XHS_CARD_WIDTH,
        "height": XHS_CARD_HEIGHT,
        "handle": label,
        "cards": cards,
    }


def _split_lede(text: str) -> tuple[str, str]:
    lede = first_paragraph(text)
    if not lede:
        return "", text
    rest = text.strip()
    if rest.startswith(lede):
        rest = rest[len(lede) :].lstrip()
    return lede, rest


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def visible_len(text: str) -> int:
    return len("".join((text or "").split()))


def is_complete_caption(caption: object, source_ids: tuple[str, ...]) -> bool:
    if not isinstance(caption, str):
        return False
    if not _CJK_RE.search(caption) or "AIGC" not in caption:
        return False
    if any(not contains_source_id(caption, source_id) for source_id in source_ids):
        return False
    match = _CAPTION_DASH_RE.search(caption)
    if match is None:
        return False
    lead = caption[: match.start()].strip()
    return bool(lead) and bool(_CJK_RE.search(lead))


def fill_xhs_caption(
    body: str, source_ids: tuple[str, ...], caption: object = None
) -> str:
    manuscript = (body or "").strip()
    if visible_len(manuscript) <= XHS_CAPTION_FULL_LIMIT:
        return _ensure_disclosure(manuscript, source_ids)
    if is_complete_caption(caption, source_ids):
        return str(caption).strip()
    return _highlight_caption(manuscript, source_ids)


def _highlight_caption(body: str, source_ids: tuple[str, ...]) -> str:
    lead = _clip_line(first_paragraph(body) or body[:80], 80)
    paragraphs = [part.strip() for part in _BLANK_RE.split(body) if part.strip()]
    bullets: list[str] = []
    for paragraph in paragraphs[1:6]:
        line = paragraph.split("\n", 1)[0].strip()
        line = re.sub(r"^#+\s*", "", line)
        if line and line != lead:
            bullets.append(f"- {_clip_line(line, 48)}")
    if not bullets:
        sentences = [part.strip() for part in re.split(r"(?<=[。！？])", body) if part.strip()]
        for sentence in sentences[1:5]:
            if sentence != lead:
                bullets.append(f"- {_clip_line(sentence, 48)}")
    if not bullets:
        bullets = ["- 详见正文图片"]
    return _ensure_disclosure(f"{lead}\n" + "\n".join(bullets), source_ids)


def _ensure_disclosure(text: str, source_ids: tuple[str, ...]) -> str:
    body = text.strip()
    missing = [source_id for source_id in source_ids if not contains_source_id(body, source_id)]
    if missing:
        body = f"{body}\n来源：{', '.join(missing)}"
    if "AIGC" not in body:
        body = f"{body}\nAIGC 声明：内容由 AI 辅助整理，发布前需人工确认。"
    return body


def _clip_line(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
