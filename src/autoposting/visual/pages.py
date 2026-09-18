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
    poster_template = str(meta.get("poster_template") or "1").strip()
    page = payload.get("page")
    if page == "ranking-overview" and variant == "cover":
        body = toc_cover(payload) if poster_template == "2" else smart_cover(payload)
        page_class = "page page-ranking"
    elif page == "project-detail" and variant in {"poster-a", "poster-b"}:
        # Keep the geometric outer plate, but use the complete, stable detail layout.
        body = project_detail(payload)
        if poster_template == "2":
            body = _toc_chrome(body)
        page_class = "page page-project"
    elif page == "ranking-overview":
        body = ranking_overview(payload)
        page_class = "page page-ranking"
    elif page == "project-detail":
        body = project_detail(payload)
        page_class = "page page-project"
    else:
        raise ValueError(f"unsupported page: {page}")
    skin = " skin-toc" if poster_template == "2" else ""
    fit = ""
    if page == "project-detail":
        fit = (
            "<script>function overflowing(el){"
            "return !!(el&&el.scrollHeight>el.clientHeight+1);"
            "}"
            "function fitPosterCopy(){"
            "document.querySelectorAll('.c-bullet-panel,.c-quote-card').forEach(function(box){"
            "var size=parseFloat(window.getComputedStyle(box).fontSize)||16;"
            "var list=box.querySelector('.c-feature-list');"
            "var guard=0;"
            "while((overflowing(box)||overflowing(list))&&size>10&&guard<32){"
            "size-=0.5;guard+=1;box.style.fontSize=size+'px';"
            "}});"
            "}"
            "fitPosterCopy();"
            "if(document.fonts&&document.fonts.ready){document.fonts.ready.then(fitPosterCopy);}</script>"
        )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width={resolved['canvas']['width']}\">"
        f"<title>{_title(payload)}</title><style>{stylesheet(resolved)}</style></head>"
        f"<body><div class=\"{page_class}{(' template-' + variant) if variant else ''}{skin}\">{body}</div>{fit}</body></html>"
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


def toc_cover(payload: dict[str, Any]) -> str:
    """Parchment directory cover: same 10 items, skill_directory_10 chrome."""
    data = apply_ranking_rules(payload)
    cards = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        cards.append(
            "<article class=\"toc-item\">"
            "<div class=\"toc-rule\"></div>"
            f"<p class=\"toc-num\">{int(item.get('rank') or 0):02d}</p>"
            f"<h2 class=\"toc-skill\">{escape(str(item.get('name') or ''))}</h2>"
            f"<p class=\"toc-desc\">{escape(str(item.get('description') or ''))}</p>"
            "</article>"
        )
    title = str(data.get("title") or data.get("subtitle") or "Directory")
    inner = (
        '<section class="toc-content">'
        f'<h1 class="toc-title">{escape(title)}</h1>'
        f'<div class="toc-grid">{"".join(cards)}</div>'
        "</section>"
    )
    return _toc_chrome(inner)


def _toc_chrome(inner: str) -> str:
    """Double-line frame and corner plates from toc_background_only.html."""
    return (
        '<div class="toc-stage">'
        '<div class="toc-frame h top-a"></div><div class="toc-frame h soft top-b"></div>'
        '<div class="toc-frame h bottom-a"></div><div class="toc-frame h soft bottom-b"></div>'
        '<div class="toc-frame v left-a"></div><div class="toc-frame v soft left-b"></div>'
        '<div class="toc-frame v right-a"></div><div class="toc-frame v soft right-b"></div>'
        '<div class="toc-inner-line"></div>'
        '<div class="toc-corner tl"></div><div class="toc-corner tr"></div>'
        '<div class="toc-corner bl"></div><div class="toc-corner br"></div>'
        f"{inner}"
        "</div>"
    )


def _title(payload: dict[str, Any]) -> str:
    return str(payload.get("name") or payload.get("title") or "visual")
