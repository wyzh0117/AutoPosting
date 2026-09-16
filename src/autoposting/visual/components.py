from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from typing import Any, Iterable

from .icons import feature_icon, function_schematic, named_icon
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


def hero_visual(
    src: str = "",
    caption: str = "功能示意",
    steps: Iterable[str] | None = None,
    seed: str = "flow",
    story: Iterable[dict[str, Any]] | None = None,
) -> str:
    beats = [beat for beat in (story or []) if isinstance(beat, dict)]
    if beats:
        return story_board(beats, seed)
    if src:
        inner = f'<img src="{escape(src, quote=True)}" alt="">'
        return (
            f'<div class="c-hero-visual">{inner}'
            f'<div class="c-hero-caption">{escape(caption)}</div></div>'
        )
    inner = function_schematic(list(steps or []), seed)
    return (
        f'<div class="c-hero-visual">{inner}'
        f'<div class="c-hero-caption">{escape(caption)}</div></div>'
    )


def story_board(beats: Iterable[dict[str, Any]], seed: str = "story") -> str:
    from .story import snake_rows

    items = [beat for beat in beats if beat.get("caption") or beat.get("image")]
    rows = snake_rows(items)
    body = []
    for row_index, row in enumerate(rows):
        klass = "c-story-row" + (" c-story-reverse" if row_index % 2 else "")
        cells = "".join(_story_beat(beat, f"{seed}-{row_index}-{index}") for index, beat in enumerate(row))
        body.append(f'<div class="{klass}">{cells}</div>')
    return f'<section class="c-hero-visual c-story">{"".join(body)}</section>'


def _story_beat(beat: dict[str, Any], seed: str) -> str:
    caption = escape(str(beat.get("caption") or ""))
    image = str(beat.get("image") or "")
    if image:
        media = f'<img src="{escape(image, quote=True)}" alt="">'
    else:
        media = feature_icon(str(beat.get("caption") or seed), seed)
    return (
        f'<article class="c-story-beat"><div class="c-story-media">{media}</div>'
        f'<p class="c-story-caption">{caption}</p></article>'
    )


def quote_card(label: str, message: str, compact: bool = False) -> str:
    klass = "c-quote-card c-quote-lede" if compact else "c-quote-card"
    return (
        f'<section class="{klass}"><div class="label">{escape(label)}</div>'
        f"<p>{escape(message)}</p></section>"
    )


def feature_list(items: Iterable[str]) -> str:
    return bullet_panel("做什么？", items)


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


def workflow_diagram(steps: Iterable[str]) -> str:
    chips = "".join(f"<span>{escape(step)}</span>" for step in steps if step)
    return f'<section class="c-panel"><h3>工作流</h3><div class="c-workflow">{chips}</div></section>'


def metric_panel(metrics: Iterable[dict[str, Any]], kind: str = "") -> str:
    cells = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        label = escape(str(metric.get("label") or ""))
        display = escape(str(metric.get("display") or metric.get("value") or ""))
        if not label or not display:
            continue
        cells.append(
            f'<div class="c-metric-cell"><div class="c-metric-value">{display}</div>'
            f'<div class="c-metric-label">{label}</div></div>'
        )
    if kind:
        cells.append(
            f'<div class="c-metric-cell"><div class="c-metric-value">{escape(kind)}</div>'
            f'<div class="c-metric-label">类型</div></div>'
        )
    if not cells:
        cells.append('<div class="c-metric-cell"><div class="c-metric-value">—</div><div class="c-metric-label">数据</div></div>')
    return (
        f'<section class="c-panel c-metric-panel"><h3>核心指标</h3>'
        f'<div class="c-metric-grid">{"".join(cells)}</div></section>'
    )


def audience_card(people: Iterable[str]) -> str:
    text = "  ·  ".join(person for person in people if person)
    return (
        f'<section class="c-panel c-audience-card"><h3>适合人群</h3>'
        f"<p>{escape(text)}</p></section>"
    )


def url_card(url: str, display: str = "") -> str:
    shown = display or url
    return (
        f'<section class="c-panel c-url-card"><h3>项目地址</h3>'
        f"<p>{escape(shown)}</p></section>"
    )


