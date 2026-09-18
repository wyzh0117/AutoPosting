from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from .content import Claim, ContentWorkflow, Draft, FactList, FactValidationError
from .article_fuse import fuse_hotspot_items
from .article_prompt import DEFAULT_ARTICLE_PROMPT_STYLE, load_article_style_prompt
from .xhs_cards import build_xhs_cards
from .db import Database
from .engagement import (
    better_tier,
    effective_tier,
    engagement_from_raw,
    engagement_kind,
    frame_article_body,
    source_citation_id,
)
from .images import (
    build_asset_bundle,
    collect_article_images,
    cover_image,
    default_page_fetcher,
    figure_html,
    guess_image_content_type,
    normalize_assets,
    selected_body_images,
    strip_image_credits,
    _safe_external_url,
)
from .title_poster import render_title_poster
from .background import (
    RELATED_SEARCH_CAUSE_NO_RESULT,
    RELATED_SEARCH_CAUSE_TRANSPORT,
    RelatedSearchError,
    derive_related_query,
    gather_background,
    redact_related_detail,
    related_article_context,
    related_search_reason,
    search_related_news,
    search_web,
)
from .hooks import EMPTY_HOOKS_MESSAGE, find_hooks as collect_hooks, sanitize_chosen_hooks
from .ingestion import should_hide_candidate
from .llm import ARTICLE_PROMPT_VERSION, LLMError, LLMService
from .pipeline import collect_sources
from .publishing import PublicationError
from .models import Source

LOGGER = logging.getLogger(__name__)

HOTSPOT_HOUR_PRESETS = (4, 6, 24, 72, 168, 720)
HOTSPOT_HOURS = HOTSPOT_HOUR_PRESETS
HOTSPOT_HOUR_MIN = 1
HOTSPOT_HOUR_MAX = 720
HOTSPOT_LIMIT = 10
HOTSPOT_POOL = 40
PASTED_PAGE_LIMIT = 20000
RELATED_SEARCH_ITEM_LIMIT = 8


def _with_pasted_page(
    material: str, background: dict[str, object], pasted_text: object
) -> tuple[str, dict[str, object]]:
    text = str(pasted_text or "").strip()
    if not text:
        return material, background
    clipped = text[:PASTED_PAGE_LIMIT]
    updated = dict(background)
    updated["pasted_page"] = clipped
    prefix = f"{material}\n\n" if str(material or "").strip() else ""
    return f"{prefix}用户粘贴的原文页面：\n{clipped}", updated


def _with_related_news(
    material: str, background: dict[str, object], related_items: object
) -> tuple[str, dict[str, object]]:
    if not isinstance(related_items, list) or not related_items:
        return material, background
    rows: list[str] = []
    for item in related_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or item.get("source_url") or "").strip()
        snippet = str(
            item.get("snippet") or item.get("summary") or item.get("description") or ""
        ).strip()
        if not title and not snippet:
            continue
        block = title
        if snippet:
            block = f"{block}\n{snippet}" if block else snippet
        if url:
            block = f"{block}\n{url}"
        rows.append(block)
    if not rows:
        return material, background
    clipped = ("相关新闻补充：\n" + "\n\n".join(rows))[:PASTED_PAGE_LIMIT]
    updated = dict(background)
    updated["related_news"] = clipped
    prefix = f"{material}\n\n" if str(material or "").strip() else ""
    return f"{prefix}{clipped}", updated


def xhs_fields(
    title: str,
    caption: str,
    assets: dict[str, object],
    handle: str = "",
) -> dict[str, object]:
    cover = cover_image(assets)
    cover_url = str(cover.get("url") or "") if cover else ""
    built = build_xhs_cards(
        title,
        str(assets.get("manuscript") or ""),
        cover_url=cover_url,
        handle=handle,
    )
    return {
        "manuscript": str(assets.get("manuscript") or ""),
        "caption": caption,
        "cards": built["cards"],
        "card_size": {"width": built["width"], "height": built["height"]},
    }


XIAOHONGSHU_CREATOR_URL = "https://creator.xiaohongshu.com/publish/publish"
DEFAULT_WECHAT_BASE_URL = "https://api.weixin.qq.com"
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", re.I)
_HTML_LINK_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>",
    re.I | re.DOTALL,
)


def _download_image(url: str) -> bytes:
    parsed = urlsplit(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(10_000_000)


def _wechat_image_payload(data: bytes, stem: str) -> tuple[bytes, str]:
    mapping = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
    suffix = mapping.get(guess_image_content_type(data))
    if suffix:
        return data, stem + suffix
    converted = _convert_to_jpeg(data)
    if converted:
        return converted, stem + ".jpg"
    return data, stem + ".jpg"


_safe_article_url = _safe_external_url


def _article_line_html(line: str) -> str:
    """Escape article text while preserving safe Markdown and HTML links."""
    token_re = re.compile(
        rf"({_MARKDOWN_LINK_RE.pattern}|{_HTML_LINK_RE.pattern})", re.I | re.DOTALL
    )
    rendered: list[str] = []
    cursor = 0
    for match in token_re.finditer(line):
        rendered.append(html.escape(line[cursor : match.start()]))
        token = match.group(0)
        markdown = _MARKDOWN_LINK_RE.fullmatch(token)
        if markdown:
            url = _safe_article_url(markdown.group(2))
            if url:
                rendered.append(
                    f'<a href="{html.escape(url, quote=True)}">'
                    f"{html.escape(markdown.group(1))}</a>"
                )
            else:
                rendered.append(html.escape(token))
        else:
            anchor = _HTML_LINK_RE.fullmatch(token)
            url = _safe_article_url(anchor.group(1)) if anchor else ""
            if url and anchor:
                rendered.append(
                    f'<a href="{html.escape(url, quote=True)}">'
                    f"{html.escape(re.sub(r'<[^>]+>', '', anchor.group(2)))}</a>"
                )
            else:
                rendered.append(html.escape(token))
        cursor = match.end()
    rendered.append(html.escape(line[cursor:]))
    return "".join(rendered)


def _convert_to_jpeg(data: bytes) -> bytes:
    import subprocess
    import tempfile
    from pathlib import Path

    try:
        with tempfile.TemporaryDirectory(prefix="autoposting-img-") as raw:
            source = Path(raw) / "in.bin"
            dest = Path(raw) / "out.jpg"
            source.write_bytes(data)
            result = subprocess.run(
                ["sips", "-s", "format", "jpeg", str(source), "--out", str(dest)],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode == 0 and dest.is_file():
                payload = dest.read_bytes()
                if payload:
                    return payload
    except Exception:
        return b""
    return b""


def _summary(content_text: str, max_chars: int = 200) -> str:
    text = " ".join(content_text.split()).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def extract_wechat_digest(body: str, title: str = "", max_chars: int = 120) -> str:
    """Choose a substantive article paragraph for the WeChat draft digest."""
    text = strip_image_credits(str(body or ""))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    candidates: list[str] = []
    short_candidates: list[str] = []
    for raw in re.split(r"\n\s*\n|\n", text):
        paragraph = " ".join(html.unescape(raw).split()).strip(" -*#")
        if not paragraph or paragraph == title.strip():
            continue
        lowered = paragraph.casefold()
        if lowered.startswith(
            (
                "来源", "原文", "原文链接", "信息源", "信息来源", "参考来源", "来源索引",
                "图片来源", "配图来源", "事实说明", "aigc", "讨论提示", "讨论说明", "讨论观察", "配图", "图片", "封面",
            )
        ):
            continue
        if len(paragraph) < 8:
            if lowered.rstrip("：:") in {"导语", "核心信息", "正文", "结语"}:
                continue
            short_candidates.append(paragraph)
            continue
        candidates.append(paragraph)
    if not candidates:
        if not short_candidates:
            return ""
        return max(short_candidates, key=len)[:max_chars]
    fact_markers = ("发布", "宣布", "确认", "上线", "推出", "达到", "增长", "更新", "获得")
    chosen = max(
        candidates,
        key=lambda value: (any(marker in value for marker in fact_markers), len(value)),
    )
    return chosen[:max_chars]


def _normalize_article_item_ids(
    item_id: str, item_ids: list[str] | None
) -> list[str]:
    if item_ids is None:
        return [item_id] if item_id else []
    if not isinstance(item_ids, (list, tuple)):
        raise ValueError("item_ids must be a list of strings")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in item_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("item_ids must be a list of strings")
        value = raw.strip()
        if value not in seen:
            seen.add(value)
            cleaned.append(value)
    if not cleaned and item_id:
        return [item_id]
    return cleaned


def _parse_fetched_at(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_health_entry(
    source: Source, row: dict[str, object], now: datetime
) -> dict[str, object]:
    last_error = row.get("last_error")
    last_fetched = _parse_fetched_at(row.get("last_fetched_at"))
    interval = int(row.get("fetch_interval_minutes") or source.fetch_interval_minutes or 15)
    if not source.enabled:
        status = "disabled"
    elif last_error:
        status = "error"
    elif last_fetched is None:
        status = "never"
    elif now >= last_fetched + timedelta(minutes=max(interval, 1) * 2):
        status = "stale"
    else:
        status = "ok"
    return {
        "source_id": source.source_id,
        "name": source.name,
        "type": source.type,
        "tier": source.tier,
        "enabled": source.enabled,
        "fetch_interval_minutes": interval,
        "last_fetched_at": last_fetched.isoformat() if last_fetched else "",
        "last_error": str(last_error or ""),
        "status": status,
    }


def _decorate_candidate(
    candidate: dict[str, object], translated: dict[str, dict[str, str]]
) -> dict[str, object]:
    item_id = str(candidate["item_id"])
    entry: dict[str, object] = {
        "item_id": item_id,
        "title": candidate["title"],
        "summary": _summary(str(candidate["content_text"])),
        "observed_at": candidate["observed_at"],
        "source_ids": candidate["source_ids"],
        "source_urls": candidate["source_urls"],
        "source_count": len(candidate["source_ids"]),
        "engagement": candidate.get("engagement") or {},
        "engagement_weight": candidate.get("engagement_weight") or 0,
        "base_tier": candidate.get("base_tier") or "",
        "effective_tier": candidate.get("effective_tier") or "",
        "promoted": bool(candidate.get("promoted")),
        "discussion": bool(candidate.get("discussion")),
    }
    translation = translated.get(item_id)
    if translation is None:
        entry["title_zh"] = ""
        entry["summary_zh"] = ""
        entry["translation_status"] = "unavailable"
        entry["translation_error"] = "translation_unavailable"
    else:
        entry["title_zh"] = translation["title_zh"]
        entry["summary_zh"] = translation["summary_zh"]
        entry["translation_status"] = translation["status"]
        entry["translation_error"] = translation["error"]
        if translation.get("error_code") or translation.get("error_reason"):
            entry["translation_error_code"] = translation.get("error_code", "")
            entry["translation_error_reason"] = translation.get("error_reason", "")
    return entry


class HotspotService:
    def __init__(
        self,
        database: Database,
        sources: tuple[Source, ...],
        fetcher=None,
        llm_service: LLMService | None = None,
    ) -> None:
        self.database = database
        self.sources = tuple(sources)
        self.fetcher = fetcher
        self.llm_service = llm_service
        self.x_bearer_token = ""

    def source_health(self, now: datetime | None = None) -> list[dict[str, object]]:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        health: list[dict[str, object]] = []
        for source in self.sources:
            if source.type == "reddit":
                continue
            self.database.upsert_source(source)
            row = self.database.get_source(source.source_id)
            health.append(_source_health_entry(source, row, moment))
        return health

    def _sources_for_lane(self, lane: str) -> tuple[Source, ...]:
        if lane not in {"hotspots", "viewpoints"}:
            raise ValueError("lane must be hotspots or viewpoints")
        if lane == "viewpoints":
            return tuple(source for source in self.sources if source.type == "reddit")
        return tuple(source for source in self.sources if source.type != "reddit")

    def collect(
        self,
        hours: int = 4,
        *,
        force: bool = True,
        source_id: str | None = None,
        lane: str = "hotspots",
    ) -> dict[str, object]:
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise ValueError("hours must be an integer")
        if hours < HOTSPOT_HOUR_MIN or hours > HOTSPOT_HOUR_MAX:
            raise ValueError(
                f"hours must be between {HOTSPOT_HOUR_MIN} and {HOTSPOT_HOUR_MAX}"
            )
        only = (source_id,) if source_id else None
        batch = collect_sources(
            self.database,
            self._sources_for_lane(lane),
            fetcher=self.fetcher,
            force=force,
            only_source_ids=only,
            x_bearer_token=self.x_bearer_token,
        )
        payload = self.list_window(hours, translate=False, lane=lane)
        payload["collection"] = {
            "inserted": batch.inserted,
            "duplicates": batch.duplicates,
            "failed": batch.failed,
            "skipped_disabled": batch.skipped_disabled,
            "skipped_not_due": batch.skipped_not_due,
            "errors": batch.errors,
            "forced": force,
            "source_id": source_id,
        }
        return payload

    def list_window(
        self, hours: int = 4, *, translate: bool = True, lane: str = "hotspots"
    ) -> dict[str, object]:
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise ValueError("hours must be an integer")
        if hours < HOTSPOT_HOUR_MIN or hours > HOTSPOT_HOUR_MAX:
            raise ValueError(
                f"hours must be between {HOTSPOT_HOUR_MIN} and {HOTSPOT_HOUR_MAX}"
            )
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        time_rows = [
            row
            for row in self.database.list_hotspot_candidates(
                since.isoformat(),
                limit=HOTSPOT_LIMIT,
                sort="time",
                pool=HOTSPOT_POOL,
                lane=lane,
            )
            if not should_hide_candidate(str(row["title"]), row.get("source_ids") or ())
        ]
        heat_rows = [
            row
            for row in self.database.list_hotspot_candidates(
                since.isoformat(),
                limit=HOTSPOT_LIMIT,
                sort="heat",
                pool=HOTSPOT_POOL,
                lane=lane,
            )
            if not should_hide_candidate(str(row["title"]), row.get("source_ids") or ())
        ]
        by_id = {str(row["item_id"]): row for row in (*time_rows, *heat_rows)}
        translated: dict[str, dict[str, str]] = {}
        if self.llm_service is not None:
            translated = self.llm_service.translate_batch(
                [
                    {
                        "item_id": item_id,
                        "title": str(row["title"]),
                        "summary": _summary(str(row["content_text"])),
                    }
                    for item_id, row in by_id.items()
                ],
                cache_only=not translate,
            )
        time_candidates = [_decorate_candidate(row, translated) for row in time_rows]
        heat_candidates = [_decorate_candidate(row, translated) for row in heat_rows]
        return {
            "hours": hours,
            "candidates": time_candidates,
            "rankings": {"time": time_candidates, "heat": heat_candidates},
            "sources": self.source_health(),
        }


class Workbench:
    def __init__(
        self,
        database: Database,
        llm_service: LLMService | None = None,
        page_fetcher=None,
        image_fetcher=None,
    ) -> None:
        self.database = database
        self.llm_service = llm_service
        self.page_fetcher = page_fetcher
        self.image_fetcher = image_fetcher

    def find_hooks(self, item_id: str, searcher=None) -> dict[str, object]:
        item = self.database.get_item(item_id)
        page_url = ""
        for pair in self.database.get_item_sources(item_id):
            if pair.get("source_url"):
                page_url = str(pair["source_url"])
                break
        if not page_url:
            page_url = str(item.get("canonical_url") or "")
        notes = gather_background(
            self.database,
            str(item["title"]),
            str(item["content_text"]),
            item_id=item_id,
            page_url=page_url,
            fetcher=self.page_fetcher,
        )
        excerpt = notes.source_excerpt or str(item["content_text"])
        extra = (
            self.llm_service.extract_hook_candidates(excerpt)
            if self.llm_service is not None
            else []
        )
        if searcher is None:
            searcher = lambda query: search_web(query, fetcher=self.page_fetcher)
        hooks = collect_hooks(str(item["title"]), excerpt, searcher, extra)
        return {
            "ok": True,
            "hooks": hooks,
            "message": EMPTY_HOOKS_MESSAGE if not hooks else "",
        }

    def related_search(
        self, draft_id: str, selection: str, selection_start: int, selection_end: int
    ) -> dict[str, object]:
        draft = self.database.get_draft(draft_id)
        related_article_context(str(draft["body"]), selection, selection_start, selection_end)
        # The raw selection stays the source of truth for context; only the
        # query handed to the search engines is derived and bounded.
        derived = derive_related_query(selection)
        try:
            search_fetcher = None if self.page_fetcher is default_page_fetcher else self.page_fetcher
            query, results = search_related_news(
                selection, fetcher=search_fetcher, limit=RELATED_SEARCH_ITEM_LIMIT
            )
        except RelatedSearchError as exc:
            # Full cause (including the upstream exception text) stays in the
            # server log; the API only ever sees the graded Chinese reason.
            # Every logged detail goes through _redact_related_detail(), the
            # single boundary that keeps credentials out of the log.
            LOGGER.warning(
                "related search failed: cause=%s sources=%s detail=%s",
                exc.cause_code,
                ",".join(exc.sources) or "-",
                redact_related_detail(exc.detail),
            )
            raise LLMError(
                "related_search_failed",
                exc.reason,
                cause_code=exc.cause_code,
            ) from None
        except Exception as exc:
            # Same boundary as above: the crash text is logged only redacted.
            LOGGER.warning(
                "related search crashed: %s: %s",
                type(exc).__name__,
                redact_related_detail(str(exc)),
            )
            raise LLMError(
                "related_search_failed",
                related_search_reason(RELATED_SEARCH_CAUSE_TRANSPORT),
                cause_code=RELATED_SEARCH_CAUSE_TRANSPORT,
            ) from None
        items: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for result in results:
            url = str(result.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(
                {
                    "title": str(result.get("title") or "").strip(),
                    "url": url,
                    "snippet": str(result.get("snippet") or "").strip()[:280],
                }
            )
        # A reachable search that qualified nothing is a success with an empty
        # result set, not a failure: the caller keeps search_status="empty".
        cause_code = "" if items else RELATED_SEARCH_CAUSE_NO_RESULT
        return {
            "ok": True,
            "query": query,
            "items": items[:RELATED_SEARCH_ITEM_LIMIT],
            "search_status": "ok" if items else "empty",
            "cause_code": cause_code,
            "query_degraded": bool(derived.degraded),
            "query_strategy": derived.strategy,
            "query_source_chars": len(derived.selection or selection),
        }

    def generate_article(
        self,
        item_id: str,
        platform: str,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        style: str = "",
        chosen_hooks: object = None,
        prompt_style: str = DEFAULT_ARTICLE_PROMPT_STYLE,
        xhs_handle: str = "",
        item_ids: list[str] | None = None,
        pasted_text: str | None = None,
        related_items: object = None,
    ) -> dict[str, object]:
        if platform not in {"wechat", "xiaohongshu"}:
            raise ValueError(f"unsupported platform: {platform}")
        ids = _normalize_article_item_ids(item_id, item_ids)
        if len(ids) >= 2:
            return self._generate_fused_article(
                ids,
                platform,
                model=model,
                provider=provider,
                effort=effort,
                style=style,
                chosen_hooks=chosen_hooks,
                prompt_style=prompt_style,
                xhs_handle=xhs_handle,
                pasted_text=pasted_text,
                related_items=related_items,
            )
        if ids:
            item_id = ids[0]
        preset_style = load_article_style_prompt(prompt_style)
        item = self.database.get_item(item_id)
        item_sources = self.database.get_item_sources(item_id)
        source_ids = tuple(
            sorted({str(pair["source_id"]) for pair in item_sources})
        )
        source_refs: list[dict[str, str]] = []
        source_occurrences: dict[str, int] = {}
        seen_source_refs: set[str] = set()
        for pair in item_sources:
            source_id = str(pair["source_id"])
            source_url = str(pair.get("source_url") or "")
            ref_key = f"{source_id}|{source_url}"
            if ref_key in seen_source_refs:
                continue
            seen_source_refs.add(ref_key)
            source_occurrences[source_id] = source_occurrences.get(source_id, 0) + 1
            source_refs.append(
                {
                    "source_id": source_id,
                    "citation_id": source_citation_id(
                        source_id, source_occurrences[source_id]
                    ),
                    "source_url": source_url,
                }
            )
        summary = _summary(str(item["content_text"])) or str(item["title"])
        facts = FactList(
            event=str(item["title"]),
            claims=(Claim(summary, source_ids, "medium"),),
            unknowns=(),
            conflicts=(),
        )
        event_id = self.database.create_event(
            str(item["title"]),
            str(item["topic"]),
            "HOTSPOT_ITEM",
            f"hotspot-item:{item_id}:{platform}",
        )
        workflow = ContentWorkflow(self.database)
        validation = workflow.save_fact_list(event_id, facts)
        if not validation.valid:
            raise FactValidationError(",".join(validation.errors))
        if self.llm_service is None:
            raise LLMError("article_generation_failed")
        settings = self.llm_service.resolve_settings(model, provider, effort)
        page_url = ""
        for pair in self.database.get_item_sources(item_id):
            if pair.get("source_url"):
                page_url = str(pair["source_url"])
                break
        if not page_url:
            page_url = str(item.get("canonical_url") or "")
        notes = gather_background(
            self.database,
            str(item["title"]),
            str(item["content_text"]),
            item_id=item_id,
            page_url=page_url,
            fetcher=self.page_fetcher,
        )
        material = notes.source_excerpt or str(item["content_text"])
        hooks = sanitize_chosen_hooks(chosen_hooks)
        background = notes.as_prompt()
        background["source_refs"] = source_refs
        if hooks:
            background["chosen_hooks"] = hooks
        material, background = _with_pasted_page(material, background, pasted_text)
        material, background = _with_related_news(material, background, related_items)
        article = self.llm_service.generate_article(
            platform,
            str(item["title"]),
            material,
            source_ids,
            model=settings.model,
            provider=settings.provider,
            effort=settings.effort,
            style=style,
            background=background,
            preset_style=preset_style,
        )
        records: list[dict[str, str]] = []
        discussion = False
        promoted = False
        best_base = "S4"
        has_x = False
        engagement = engagement_from_raw(item.get("raw_json"))
        citation_occurrences: dict[str, int] = {}
        for pair in item_sources:
            source = self.database.get_source(str(pair["source_id"]))
            kind = engagement_kind(str(source["source_id"]), str(pair["source_url"]), str(source["type"]))
            base = str(source["tier"])
            best_base = better_tier(best_base, base)
            if effective_tier(base, engagement, kind) != base:
                promoted = True
            if str(source["type"]) == "x":
                has_x = True
            source_id = str(source["source_id"])
            citation_occurrences[source_id] = citation_occurrences.get(source_id, 0) + 1
            records.append(
                {
                    "source_id": source_id,
                    "citation_id": source_citation_id(
                        source_id, citation_occurrences[source_id]
                    ),
                    "name": str(source["name"]),
                    "source_url": str(pair["source_url"]),
                }
            )
        discussion = best_base == "S4" or has_x
        images = collect_article_images(
            item,
            item_sources,
            page_fetcher=self.page_fetcher,
            probe_fetcher=self.image_fetcher,
        )
        assets = build_asset_bundle(images, item_id=item_id)
        manuscript = article["body"]
        source_body = article.get("caption") if platform == "xiaohongshu" else manuscript
        framed = frame_article_body(
            str(source_body),
            records,
            discussion=discussion,
            promoted=promoted,
            inline_sources=True,
        )
        body = strip_image_credits(framed)
        if platform == "xiaohongshu":
            assets["manuscript"] = manuscript
        draft = Draft(
            draft_id=hashlib.sha256(
                f"{event_id}:{platform}:{ARTICLE_PROMPT_VERSION}".encode()
            ).hexdigest()[:24],
            event_id=event_id,
            platform=platform,
            title=article["title"],
            body=body,
            tags=(),
            source_ids=source_ids,
            generator=(
                f"{settings.provider}/{settings.model}:{settings.effort}:{ARTICLE_PROMPT_VERSION}"
            ),
            processing_version=ARTICLE_PROMPT_VERSION,
            assets=assets,
        )
        outcome = self.database.upsert_draft(draft)
        if outcome != "unchanged":
            self.database.update_event_status(event_id, "DRAFTED")
        payload: dict[str, object] = {
            "event_id": event_id,
            "draft_id": draft.draft_id,
            "platform": platform,
            "status": self.database.get_draft(draft.draft_id)["status"],
            "title": draft.title,
            "body": draft.body,
            "source_ids": list(source_ids),
            "assets": assets,
            "prompt_style": prompt_style,
            "llm": self.llm_service.settings_payload(settings),
        }
        if platform == "xiaohongshu":
            payload.update(xhs_fields(draft.title, draft.body, assets, xhs_handle))
        return payload

    def _generate_fused_article(
        self,
        item_ids: list[str],
        platform: str,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        style: str = "",
        chosen_hooks: object = None,
        prompt_style: str = DEFAULT_ARTICLE_PROMPT_STYLE,
        xhs_handle: str = "",
        pasted_text: str | None = None,
        related_items: object = None,
    ) -> dict[str, object]:
        preset_style = load_article_style_prompt(prompt_style)
        prepared: list[dict[str, object]] = []
        source_ids_set: set[str] = set()
        items_by_id: dict[str, dict[str, object]] = {}
        source_refs: list[dict[str, str]] = []
        source_occurrences: dict[str, int] = {}
        seen_source_refs: set[str] = set()
        for item_id in item_ids:
            item = dict(self.database.get_item(item_id))
            pairs = self.database.get_item_sources(item_id)
            item["source_ids"] = [str(pair["source_id"]) for pair in pairs]
            item["source_urls"] = [str(pair.get("source_url") or "") for pair in pairs]
            prepared.append(item)
            items_by_id[item_id] = item
            source_ids_set.update(str(pair["source_id"]) for pair in pairs)
            for pair in pairs:
                source_id = str(pair["source_id"])
                source_url = str(pair.get("source_url") or "")
                ref_key = f"{source_id}|{source_url}"
                if ref_key in seen_source_refs:
                    continue
                seen_source_refs.add(ref_key)
                source_occurrences[source_id] = source_occurrences.get(source_id, 0) + 1
                source_refs.append(
                    {
                        "source_id": source_id,
                        "citation_id": source_citation_id(
                            source_id, source_occurrences[source_id]
                        ),
                        "source_url": source_url,
                    }
                )
        fused = fuse_hotspot_items(prepared)
        source_ids = tuple(sorted(source_ids_set | set(fused.get("source_ids") or [])))
        fused_title = str(fused["title"])
        fused_content = str(fused.get("content_text") or fused.get("summary") or "")
        summary = _summary(fused_content) or fused_title
        facts = FactList(
            event=fused_title,
            claims=(Claim(summary, source_ids, "medium"),),
            unknowns=(),
            conflicts=(),
        )
        stable_key = f"hotspot-item:{','.join(sorted(item_ids))}:{platform}"
        event_id = self.database.create_event(
            fused_title,
            str(prepared[0].get("topic") or "unclassified"),
            "HOTSPOT_ITEM",
            stable_key,
        )
        workflow = ContentWorkflow(self.database)
        validation = workflow.save_fact_list(event_id, facts)
        if not validation.valid:
            raise FactValidationError(",".join(validation.errors))
        if self.llm_service is None:
            raise LLMError("article_generation_failed")
        settings = self.llm_service.resolve_settings(model, provider, effort)
        notes = gather_background(
            self.database,
            fused_title,
            fused_content,
            item_id="",
            page_url="",
            fetcher=self.page_fetcher,
        )
        material = fused_content or notes.source_excerpt
        hooks = sanitize_chosen_hooks(chosen_hooks)
        background = notes.as_prompt()
        background["source_refs"] = source_refs
        if hooks:
            background["chosen_hooks"] = hooks
        material, background = _with_pasted_page(material, background, pasted_text)
        material, background = _with_related_news(material, background, related_items)
        article = self.llm_service.generate_article(
            platform,
            fused_title,
            material,
            source_ids,
            model=settings.model,
            provider=settings.provider,
            effort=settings.effort,
            style=style,
            background=background,
            preset_style=preset_style,
        )
        records: list[dict[str, str]] = []
        discussion = False
        promoted = False
        best_base = "S4"
        has_x = False
        seen_urls: set[str] = set()
        citation_occurrences: dict[str, int] = {}
        images: list[dict[str, object]] = []
        for item_id in item_ids:
            item = items_by_id[item_id]
            pairs = self.database.get_item_sources(item_id)
            engagement = engagement_from_raw(item.get("raw_json"))
            images.extend(
                collect_article_images(
                    item,
                    pairs,
                    page_fetcher=self.page_fetcher,
                    probe_fetcher=self.image_fetcher,
                )
            )
            for pair in pairs:
                source = self.database.get_source(str(pair["source_id"]))
                kind = engagement_kind(
                    str(source["source_id"]), str(pair["source_url"]), str(source["type"])
                )
                base = str(source["tier"])
                best_base = better_tier(best_base, base)
                if effective_tier(base, engagement, kind) != base:
                    promoted = True
                if str(source["type"]) == "x":
                    has_x = True
                source_url = str(pair["source_url"])
                record_key = f"{source['source_id']}|{source_url}"
                if record_key in seen_urls:
                    continue
                seen_urls.add(record_key)
                source_id = str(source["source_id"])
                citation_occurrences[source_id] = citation_occurrences.get(source_id, 0) + 1
                records.append(
                    {
                        "source_id": source_id,
                        "citation_id": source_citation_id(
                            source_id, citation_occurrences[source_id]
                        ),
                        "name": str(source["name"]),
                        "source_url": source_url,
                    }
                )
        discussion = best_base == "S4" or has_x
        assets = build_asset_bundle(images, item_id=",".join(sorted(item_ids)))
        manuscript = article["body"]
        source_body = article.get("caption") if platform == "xiaohongshu" else manuscript
        framed = frame_article_body(
            str(source_body),
            records,
            discussion=discussion,
            promoted=promoted,
            inline_sources=True,
        )
        body = strip_image_credits(framed)
        if platform == "xiaohongshu":
            assets["manuscript"] = manuscript
        draft = Draft(
            draft_id=hashlib.sha256(
                f"{event_id}:{platform}:{ARTICLE_PROMPT_VERSION}".encode()
            ).hexdigest()[:24],
            event_id=event_id,
            platform=platform,
            title=article["title"],
            body=body,
            tags=(),
            source_ids=source_ids,
            generator=(
                f"{settings.provider}/{settings.model}:{settings.effort}:{ARTICLE_PROMPT_VERSION}"
            ),
            processing_version=ARTICLE_PROMPT_VERSION,
            assets=assets,
        )
        outcome = self.database.upsert_draft(draft)
        if outcome != "unchanged":
            self.database.update_event_status(event_id, "DRAFTED")
        payload: dict[str, object] = {
            "event_id": event_id,
            "draft_id": draft.draft_id,
            "platform": platform,
            "status": self.database.get_draft(draft.draft_id)["status"],
            "title": draft.title,
            "body": draft.body,
            "source_ids": list(source_ids),
            "assets": assets,
            "prompt_style": prompt_style,
            "llm": self.llm_service.settings_payload(settings),
        }
        if platform == "xiaohongshu":
            payload.update(xhs_fields(draft.title, draft.body, assets, xhs_handle))
        return payload

    def rescan_images(self, draft_id: str) -> dict[str, object]:
        draft = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        bundle = normalize_assets(draft["assets_json"])
        item_id = str(bundle.get("item_id") or "")
        if not item_id:
            event = self.database.get_event(str(draft["event_id"]))
            item_id = self.database.find_item_id_by_title(str(event["name"]))
        if not item_id:
            raise ValueError("source_item_not_found")
        item = self.database.get_item(item_id)
        images = collect_article_images(
            item,
            self.database.get_item_sources(item_id),
            page_fetcher=self.page_fetcher,
            probe_fetcher=self.image_fetcher,
        )
        bundle = build_asset_bundle(images, item_id=item_id)
        manuscript = normalize_assets(draft["assets_json"]).get("manuscript")
        if isinstance(manuscript, str) and manuscript.strip():
            bundle["manuscript"] = manuscript
        return bundle


class WechatConfigurationRequired(PublicationError):
    pass


WECHAT_ERRCODES = {
    40001: "invalid_credential",
    40013: "invalid_appid",
    40014: "invalid_access_token",
    41001: "access_token_required",
    42001: "access_token_expired",
    45009: "api_frequency_limit",
    53501: "draft_not_found",
    53010: "publish_not_allowed",
}


class WeChatPublisher:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        thumb_media_id: str,
        author: str = "",
        base_url: str = DEFAULT_WECHAT_BASE_URL,
        opener=None,
        image_fetcher=None,
        poster_renderer=None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.thumb_media_id = thumb_media_id
        self.author = author
        self.base_url = base_url.rstrip("/")
        self.opener = opener
        self.image_fetcher = image_fetcher
        self.poster_renderer = poster_renderer

    def _api(self, method: str, path: str, query: str = "", body: str | bytes | None = None) -> dict[str, object]:
        data = body.encode("utf-8") if isinstance(body, str) else body
        headers = {}
        if isinstance(data, bytes) and data.startswith(b"------AutoPosting"):
            boundary = data.split(b"\r\n", 1)[0][2:].decode("ascii")
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(
            f"{self.base_url}{path}{query}", data=data, method=method, headers=headers
        )
        try:
            with (self.opener or urllib.request.urlopen)(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError) as exc:
            raise PublicationError("wechat_api_unavailable") from exc
        errcode = payload.get("errcode", 0)
        if errcode:
            raise PublicationError(WECHAT_ERRCODES.get(errcode, f"wechat_api_error:{errcode}"))
        return payload

    def _asset_bundle(self, draft: dict[str, object]) -> dict[str, object]:
        raw = draft.get("assets")
        if raw is None and draft.get("assets_json") is not None:
            raw = draft["assets_json"]
        return normalize_assets(raw)

    def _draft_assets(self, draft: dict[str, object]) -> list[dict[str, object]]:
        return selected_body_images(self._asset_bundle(draft))

    def _selected_cover_assets(self, draft: dict[str, object]) -> list[dict[str, object]]:
        bundle = self._asset_bundle(draft)
        ordered: list[dict[str, object]] = []
        seen: set[str] = set()
        cover = cover_image(bundle)
        if cover is not None:
            key = str(cover.get("id") or cover.get("url") or "")
            ordered.append(cover)
            if key:
                seen.add(key)
        for image in selected_body_images(bundle):
            key = str(image.get("id") or image.get("url") or "")
            if key and key in seen:
                continue
            ordered.append(image)
            if key:
                seen.add(key)
        return ordered

    def _upload_permanent_image(self, access_token: str, image_bytes: bytes, filename: str) -> str:
        boundary = "----AutoPostingBoundary"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = self._api(
            "POST",
            "/cgi-bin/material/add_material",
            f"?access_token={access_token}&type=image",
            header + image_bytes + footer,
        )
        media_id = payload.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise PublicationError("wechat_thumb_upload_failed")
        return media_id

    def _upload_image(self, access_token: str, image_bytes: bytes, filename: str) -> str:
        boundary = "----AutoPostingBoundary"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = self._api(
            "POST",
            "/cgi-bin/media/uploadimg",
            f"?access_token={access_token}",
            header + image_bytes + footer,
        )
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            raise PublicationError("wechat_image_upload_failed")
        return url

    def _filled_content(self, draft: dict[str, object], access_token: str) -> str:
        body = strip_image_credits(str(draft["body"]))
        paragraphs = "".join(
            f"<p>{_article_line_html(line)}</p>" for line in body.splitlines()
        )
        figures: list[str] = []
        fetcher = self.image_fetcher or _download_image
        for index, asset in enumerate(self._draft_assets(draft), start=1):
            published = None
            if fetcher is not None:
                try:
                    payload = fetcher(str(asset["url"]))
                    if isinstance(payload, (bytes, bytearray)) and payload:
                        image_bytes, filename = _wechat_image_payload(
                            bytes(payload), f"image-{index}"
                        )
                        published = self._upload_image(access_token, image_bytes, filename)
                except Exception:
                    published = None
            figures.append(figure_html(asset, published))
        return "".join(figures) + paragraphs

    def _poster_bytes(self, title: str) -> bytes:
        renderer = self.poster_renderer if self.poster_renderer is not None else render_title_poster
        try:
            payload = renderer(title)
        except Exception:
            return b""
        if isinstance(payload, (bytes, bytearray)) and payload:
            return bytes(payload)
        return b""

    def _upload_selected_cover(self, asset: dict[str, object], access_token: str) -> str:
        fetcher = self.image_fetcher or _download_image
        payload = fetcher(str(asset.get("url") or ""))
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return ""
        image_bytes, filename = _wechat_image_payload(bytes(payload), "cover")
        return self._upload_permanent_image(access_token, image_bytes, filename)

    def _resolve_thumb_media_id(self, draft: dict[str, object], access_token: str) -> str:
        selected = self._selected_cover_assets(draft)
        if selected:
            for asset in selected:
                try:
                    media_id = self._upload_selected_cover(asset, access_token)
                except Exception:
                    media_id = ""
                if media_id:
                    return media_id
            return self.thumb_media_id
        poster = self._poster_bytes(str(draft.get("title") or ""))
        if poster:
            try:
                return self._upload_permanent_image(access_token, poster, "cover.png")
            except Exception:
                pass
        return self.thumb_media_id

    def publish(self, draft: dict[str, object]) -> dict[str, object]:
        token = self._api(
            "GET",
            "/cgi-bin/token",
            "?"
            + urlencode(
                {
                    "grant_type": "client_credential",
                    "appid": self.app_id,
                    "secret": self.app_secret,
                }
            ),
        )
        access_token = str(token["access_token"])
        content = self._filled_content(draft, access_token)
        thumb_media_id = self._resolve_thumb_media_id(draft, access_token)
        if not thumb_media_id:
            raise PublicationError("wechat_cover_required")
        digest = extract_wechat_digest(str(draft["body"]), str(draft.get("title") or ""))
        article = {
            "title": draft["title"],
            "author": self.author,
            "digest": digest,
            "content": content,
            "thumb_media_id": thumb_media_id,
            "need_open_comment": 1,
            "only_fans_can_comment": 0,
        }
        draft_result = self._api(
            "POST",
            "/cgi-bin/draft/add",
            f"?access_token={access_token}",
            json.dumps({"articles": [article]}, ensure_ascii=False),
        )
        media_id = str(draft_result["media_id"])
        return {
            "status": "DRAFT_SAVED",
            "reference": media_id,
        }
