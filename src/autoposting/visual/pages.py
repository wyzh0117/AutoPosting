from __future__ import annotations

from html import escape
from typing import Any

from . import components as c
from .css import stylesheet
from .rules import apply_project_rules, apply_ranking_rules
from .tokens import load_tokens


def render_html(payload: dict[str, Any], tokens: dict[str, Any] | None = None) -> str:
    resolved = tokens or load_tokens()
    meta = resolved.get("meta") if isinstance(resolved.get("meta"), dict) else {}
    variant = str(meta.get("template_variant") or "").strip()
    page = payload.get("page")
    if page == "ranking-overview" and variant == "cover":
        body = smart_cover(payload)
        page_class = "page page-ranking"
    elif page == "project-detail" and variant in {"poster-a", "poster-b"}:
        # Keep the geometric outer plate, but use the complete, stable detail layout.
        body = project_detail(payload)
        page_class = "page page-project"
    elif page == "ranking-overview":
        body = ranking_overview(payload)
        page_class = "page page-ranking"
    elif page == "project-detail":
        body = project_detail(payload)
        page_class = "page page-project"
    else:
        raise ValueError(f"unsupported page: {page}")
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width={resolved['canvas']['width']}\">"
        f"<title>{_title(payload)}</title><style>{stylesheet(resolved)}</style></head>"
        f"<body><div class=\"{page_class}{(' template-' + variant) if variant else ''}\">{body}</div></body></html>"
    )


def smart_cover(payload: dict[str, Any]) -> str:
    data = apply_ranking_rules(payload)
    items = []
    # The weekly cover is the overview of the whole ranking, not a teaser.
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
        items.append(
            "<li class=\"c-smart-cover-item\">"
            f"<span class=\"c-smart-cover-rank\">{int(item.get('rank') or 0):02d}</span>"
            f"<span class=\"c-smart-cover-name\">{escape(str(item.get('name') or ''))}</span>"
            f"<span class=\"c-smart-cover-desc\">{escape(str(item.get('description') or ''))}</span>"
            f"<span class=\"c-smart-cover-metric\">{escape(str(metric.get('display') or metric.get('value') or ''))}</span>"
            "</li>"
        )
    tags = "".join(
        f"<span>{escape(str(tag))}</span>"
        for tag in (data.get("tags") or [])
        if str(tag).strip()
    )
    page_number = str(data.get("pageNumber") or f"01 / {len(items) + 1:02d}")
    kicker = str(data.get("kicker") or "")
    kicker_block = f'<p class="c-smart-cover-kicker">{escape(kicker)}</p>' if kicker else ""
    return (
        '<div class="c-smart-cover-shell">'
        '<div class="c-smart-cover-topline">'
        f"<span>{escape(str(data.get('brand') or ''))}</span>"
        f"<span>{escape(str(data.get('issue') or ''))}</span>"
        "</div>"
        '<div class="c-smart-cover-heading">'
        f"{kicker_block}"
        f"<h1>{escape(str(data.get('title') or ''))}</h1>"
        f"<p>{escape(str(data.get('subtitle') or ''))}</p>"
        "</div>"
        f'<ol class="c-smart-cover-list">{"".join(items)}</ol>'
        f'<div class="c-smart-cover-tags">{tags}</div>'
        '<div class="c-smart-cover-footer">'
        f"<span>{escape(str(data.get('footer') or data.get('source') or ''))}</span>"
        f"<span>{escape(page_number)}</span>"
        "</div>"
        "</div>"
    )


def geometric_poster(payload: dict[str, Any], variant: str) -> str:
    data = apply_project_rules(payload)
    features = _poster_values(data, "features")
    how = _poster_values(data, "how")
    audience = _poster_values(data, "audience")
    scene = _poster_values(data, "scene")
    name = escape(str(data.get("name") or ""))
    rank = int(data.get("rank") or 0)
    key_message = escape(str(data.get("keyMessage") or data.get("description") or ""))
    if variant == "poster-a":
        lines = "".join(
            f'<li><span>{index:02d}</span><b>{escape(value)}</b></li>'
            for index, value in enumerate((features + how)[:4], start=1)
        )
        return (
            '<div class="c-geometric-poster c-poster-a-shell">'
            f'<div class="c-poster-a-number">{rank:02d}</div>'
            f'<div class="c-poster-a-title"><h1>{name}</h1><p>{key_message}</p></div>'
            f'<ol class="c-poster-a-lines">{lines}</ol>'
            f'<div class="c-poster-a-footer">{escape(str(data.get("pageNumber") or ""))}</div>'
            "</div>"
        )
    info = features[:1] + how[:1] + audience[:1] + scene[:1]
    rows = "".join(
        f'<li><span>{index:02d}</span><p>{escape(value)}</p></li>'
        for index, value in enumerate(info[:4], start=1)
    )
    mark = escape("".join(ch for ch in str(data.get("name") or "") if ch.isalnum())[:4])
    return (
        '<div class="c-geometric-poster c-poster-b-shell">'
        f'<div class="c-poster-b-media"><span>{mark}</span></div>'
        '<div class="c-poster-b-chevron" aria-hidden="true"></div>'
        f'<div class="c-poster-b-title"><h1>{name}</h1><p>{key_message}</p></div>'
        f'<ol class="c-poster-b-info">{rows}</ol>'
        f'<div class="c-poster-b-footer">{escape(str(data.get("pageNumber") or ""))}</div>'
        "</div>"
    )


def _poster_values(data: dict[str, Any], key: str) -> list[str]:
    values = data.get(key) if isinstance(data.get(key), list) else []
    return [str(value).strip() for value in values if str(value).strip()]


def ranking_overview(payload: dict[str, Any]) -> str:
    data = apply_ranking_rules(payload)
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    page_number = str(data.get("pageNumber") or f"01 / {len(items) + 1:02d}")
    return "".join(
        [
            c.brand_header(
                str(data.get("brand") or ""),
                str(data.get("issue") or ""),
                str(data.get("avatar") or ""),
            ),
            c.hero_title(str(data.get("title") or ""), str(data.get("subtitle") or "")),
            c.issue_meta(
                str(data.get("issue") or ""),
                str(data.get("dateRange") or ""),
                str(data.get("kicker") or ""),
            ),
            c.ranking_list(data.get("items") or []),
            c.topic_tags(data.get("tags") or []),
            c.footer(str(data.get("footer") or data.get("source") or ""), page_number),
        ]
    )


def project_detail(payload: dict[str, Any]) -> str:
    data = apply_project_rules(payload)
    name = str(data.get("name") or "")
    return "".join(
        [
            c.brand_header(
                str(data.get("brand") or ""),
                str(data.get("issue") or ""),
                str(data.get("avatar") or ""),
            ),
            '<div class="c-project-shell">',
            '<div class="c-masthead">',
            '<div class="c-masthead-copy">',
            '<div class="c-title-row">'
            f"{c.rank_pill(int(data.get('rank') or 1))}"
            f"{c.category_tag(str(data.get('category') or ''))}"
            "</div>",
            c.hero_title(name, str(data.get("tagline") or "")),
            c.quote_card("一句话看懂", str(data.get("keyMessage") or ""), compact=True),
            "</div>",
            '<div class="c-masthead-logo">'
            f"{c.project_logo(name, str(data.get('logo') or ''), str(data.get('category') or ''), name, str(data.get('markSvg') or ''))}"
            "</div></div>",
            c.bullet_panel("做什么？", data.get("features") or []),
            '<div class="c-project-pair">',
            c.bullet_panel("如何实现？", data.get("how") or []),
            c.bullet_panel(
                "适合哪些人群",
                data.get("audience") or [],
                row_icons=False,
                title_icon="people",
            ),
            "</div>",
            c.bullet_panel(
                "使用场景",
                data.get("scene") or [],
                row_icons=False,
                title_icon="pin",
            ),
            "</div>",
            c.footer(
                str(data.get("footer") or data.get("source") or ""),
                str(data.get("pageNumber") or ""),
            ),
        ]
    )


def _title(payload: dict[str, Any]) -> str:
    return str(payload.get("name") or payload.get("title") or "visual")
