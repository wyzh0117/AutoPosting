from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from typing import Any, Iterable

from .icons import feature_icon, named_icon
from .marks import family_from_kind, is_author_avatar, themed_svg

AUTHOR_BRAND = "EE_萤飞"
_AVATAR_PATH = Path(__file__).resolve().parent / "assets" / "author-avatar.png"
_AVATAR_URI = ""


def author_avatar_data_uri() -> str:
    global _AVATAR_URI
    if _AVATAR_URI:
        return _AVATAR_URI
    payload = _AVATAR_PATH.read_bytes()
    _AVATAR_URI = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    return _AVATAR_URI


def brand_header(brand: str, trailing: str = "", avatar: str = "") -> str:
    right = f'<div class="c-issue-badge">{escape(trailing)}</div>' if trailing else ""
    if avatar:
        mark = (
            f'<img class="c-brand-avatar" src="{escape(avatar, quote=True)}" alt="">'
            f"<span>{escape(brand)}</span>"
        )
        cls = "c-brand-mark c-brand-author"
    else:
        mark = f"<i></i>{escape(brand)}"
        cls = "c-brand-mark"
    return (
        f'<header class="c-brand-header">'
        f'<div class="{cls}">{mark}</div>'
        f"{right}</header>"
    )


def hero_title(title: str, subtitle: str = "") -> str:
    sub = f'<p class="c-hero-sub">{escape(subtitle)}</p>' if subtitle else ""
    return f'<h1 class="c-hero-title">{escape(title)}</h1>{sub}'


def issue_meta(issue: str, date_range: str, kicker: str = "") -> str:
    bits = [part for part in (kicker, issue, date_range) if part]
    return '<div class="c-meta-row">' + "".join(f"<span>{escape(bit)}</span>" for bit in bits) + "</div>"


def ranking_badge(rank: int) -> str:
    return f'<div class="c-ranking-badge">{int(rank):02d}</div>'


def project_logo(
    name: str,
    src: str = "",
    kind: str = "",
    seed: str = "",
    mark_svg: str = "",
) -> str:
    if mark_svg:
        return f'<div class="c-project-logo c-logo-3d">{mark_svg}</div>'
    if src and not is_author_avatar(src):
        return (
            f'<div class="c-project-logo"><img src="{escape(src, quote=True)}" alt=""></div>'
        )
    svg = themed_svg(name or kind, family_from_kind(kind, name), seed or name or kind)
    return f'<div class="c-project-logo c-logo-3d">{svg}</div>'


def ranking_item(item: dict[str, Any]) -> str:
    metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
    progress = float(item.get("progress") or 0)
    width = max(8, min(100, round(progress * 100)))
    return (
        f'<article class="c-ranking-item">'
        f"{ranking_badge(int(item.get('rank') or 0))}"
        f"{project_logo(str(item.get('name') or ''), str(item.get('logo') or ''), str(item.get('category') or ''), mark_svg=str(item.get('markSvg') or ''))}"
        f'<div class="c-item-copy">'
        f'<div class="c-item-name">{escape(str(item.get("name") or ""))}</div>'
        f'<div class="c-item-desc">{escape(str(item.get("description") or ""))}</div>'
        f"</div>"
        f'<div class="c-metric">'
        f'<div class="c-metric-value">{escape(str(metric.get("display") or metric.get("value") or ""))}</div>'
        f'<div class="c-metric-label">{escape(str(metric.get("label") or "Stars"))}</div>'
        f'<div class="c-progress-bar"><span style="width:{width}%"></span></div>'
        f"</div></article>"
    )


def ranking_list(items: Iterable[dict[str, Any]]) -> str:
    body = "".join(ranking_item(item) for item in items)
    return f'<section class="c-ranking-list">{body}</section>'


def topic_tags(tags: Iterable[str]) -> str:
    chips = "".join(f"<span>{escape(tag)}</span>" for tag in tags if tag)
    if not chips:
        return ""
    return f'<div class="c-topic-tags">{chips}</div>'


def footer(text: str, page_number: str = "") -> str:
    right = f'<span class="c-page-number">{escape(page_number)}</span>' if page_number else "<span></span>"
    return f'<footer class="c-footer"><span>{escape(text)}</span>{right}</footer>'


def category_tag(label: str) -> str:
    return f'<span class="c-category-tag">{escape(label)}</span>'


def rank_pill(rank: int) -> str:
    return f'<span class="c-rank-pill">NO.{int(rank):02d}</span>'


def quote_card(label: str, message: str, compact: bool = False) -> str:
    klass = "c-quote-card c-quote-lede" if compact else "c-quote-card"
    return (
        f'<section class="{klass}"><div class="label">{escape(label)}</div>'
        f"<p>{escape(message)}</p></section>"
    )


def bullet_panel(
    title: str,
    items: Iterable[str],
    *,
    row_icons: bool = True,
    title_icon: str = "",
) -> str:
    rows = []
    for index, item in enumerate(items):
        text = str(item).strip()
        if not text:
            continue
        if row_icons:
            rows.append(
                f"<li>{feature_icon(text, f'{title}-{index}')}<span>{escape(text)}</span></li>"
            )
        else:
            rows.append(f"<li><span>{escape(text)}</span></li>")
    if not rows:
        rows.append("<li><span>暂无</span></li>")
    heading = escape(title)
    if title_icon:
        heading = f"{named_icon(title_icon, f'title-{title_icon}')}<span>{heading}</span>"
    klass = "c-panel c-bullet-panel"
    if not row_icons:
        klass += " c-plain-list"
    return (
        f'<section class="{klass}"><h3>{heading}</h3>'
        f'<ul class="c-feature-list">{"".join(rows)}</ul></section>'
    )


