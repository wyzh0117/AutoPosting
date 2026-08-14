from __future__ import annotations

import hashlib
import html
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .content import Claim, ContentWorkflow, Draft, FactList, FactValidationError
from .db import Database
from .engagement import (
    better_tier,
    effective_tier,
    engagement_from_raw,
    engagement_kind,
    frame_article_body,
)
from .images import (
    append_image_credits,
    build_asset_bundle,
    collect_article_images,
    cover_image,
    figure_html,
    normalize_assets,
    selected_body_images,
)
from .background import gather_background, search_web
from .hooks import EMPTY_HOOKS_MESSAGE, find_hooks as collect_hooks, sanitize_chosen_hooks
from .ingestion import should_hide_candidate
from .llm import ARTICLE_PROMPT_VERSION, LLMError, LLMService
from .pipeline import collect_sources
from .publishing import PublicationError
from .models import Source

HOTSPOT_HOUR_PRESETS = (4, 6, 24, 72, 168, 720)
HOTSPOT_HOURS = HOTSPOT_HOUR_PRESETS
HOTSPOT_HOUR_MIN = 1
HOTSPOT_HOUR_MAX = 720
HOTSPOT_LIMIT = 10
HOTSPOT_POOL = 40
XIAOHONGSHU_CREATOR_URL = "https://creator.xiaohongshu.com/publish/publish"
DEFAULT_WECHAT_BASE_URL = "https://api.weixin.qq.com"


def _download_image(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(2_000_000)


def _summary(content_text: str, max_chars: int = 200) -> str:
    text = " ".join(content_text.split()).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


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
            self.database.upsert_source(source)
            row = self.database.get_source(source.source_id)
            health.append(_source_health_entry(source, row, moment))
        return health

    def collect(
        self,
        hours: int = 4,
        *,
        force: bool = True,
        source_id: str | None = None,
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
            self.sources,
            fetcher=self.fetcher,
            force=force,
            only_source_ids=only,
            x_bearer_token=self.x_bearer_token,
        )
        payload = self.list_window(hours, translate=False)
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

    def list_window(self, hours: int = 4, *, translate: bool = True) -> dict[str, object]:
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
                since.isoformat(), limit=HOTSPOT_LIMIT, sort="time", pool=HOTSPOT_POOL
            )
            if not should_hide_candidate(str(row["title"]), row.get("source_ids") or ())
        ]
        heat_rows = [
            row
            for row in self.database.list_hotspot_candidates(
                since.isoformat(), limit=HOTSPOT_LIMIT, sort="heat", pool=HOTSPOT_POOL
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

    def generate_article(
        self,
        item_id: str,
        platform: str,
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        style: str = "",
        chosen_hooks: object = None,
    ) -> dict[str, object]:
        if platform not in {"wechat", "xiaohongshu"}:
            raise ValueError(f"unsupported platform: {platform}")
        item = self.database.get_item(item_id)
        source_ids = tuple(
            sorted({str(pair["source_id"]) for pair in self.database.get_item_sources(item_id)})
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
        if hooks:
            background["chosen_hooks"] = hooks
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
        )
        item_sources = self.database.get_item_sources(item_id)
        records: list[dict[str, str]] = []
        discussion = False
        promoted = False
        best_base = "S4"
        has_x = False
        engagement = engagement_from_raw(item.get("raw_json"))
        for pair in item_sources:
            source = self.database.get_source(str(pair["source_id"]))
            kind = engagement_kind(str(source["source_id"]), str(pair["source_url"]), str(source["type"]))
            base = str(source["tier"])
            best_base = better_tier(best_base, base)
            if effective_tier(base, engagement, kind) != base:
                promoted = True
            if str(source["type"]) == "x":
                has_x = True
            records.append(
                {
                    "source_id": str(source["source_id"]),
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
        framed = frame_article_body(
            article["body"], records, discussion=discussion, promoted=promoted
        )
        body = append_image_credits(
            framed, selected_body_images(assets), cover_image(assets)
        )
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
        return {
            "event_id": event_id,
            "draft_id": draft.draft_id,
            "platform": platform,
            "status": self.database.get_draft(draft.draft_id)["status"],
            "title": draft.title,
            "body": draft.body,
            "source_ids": list(source_ids),
            "assets": assets,
            "llm": self.llm_service.settings_payload(settings),
        }

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
        return build_asset_bundle(images, item_id=item_id)


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
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.thumb_media_id = thumb_media_id
        self.author = author
        self.base_url = base_url.rstrip("/")
        self.opener = opener
        self.image_fetcher = image_fetcher

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

    def _upload_thumb(self, access_token: str, image_bytes: bytes, filename: str) -> str:
        boundary = "----AutoPostingBoundary"
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = self._api(
            "POST",
            "/cgi-bin/media/upload",
            f"?access_token={access_token}&type=thumb",
            header + image_bytes + footer,
        )
        media_id = payload.get("media_id") or payload.get("thumb_media_id")
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
        paragraphs = "".join(
            f"<p>{html.escape(line)}</p>" for line in str(draft["body"]).splitlines()
        )
        figures: list[str] = []
        fetcher = self.image_fetcher or _download_image
        for index, asset in enumerate(self._draft_assets(draft), start=1):
            published = None
            if fetcher is not None:
                try:
                    payload = fetcher(str(asset["url"]))
                    if isinstance(payload, (bytes, bytearray)) and payload:
                        published = self._upload_image(
                            access_token, bytes(payload), f"image-{index}.jpg"
                        )
                except Exception:
                    published = None
            figures.append(figure_html(asset, published))
        return "".join(figures) + paragraphs

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
        thumb_media_id = self.thumb_media_id
        cover = cover_image(self._asset_bundle(draft))
        fetcher = self.image_fetcher or _download_image
        if cover is not None and fetcher is not None:
            try:
                payload = fetcher(str(cover["url"]))
                if isinstance(payload, (bytes, bytearray)) and payload:
                    thumb_media_id = self._upload_thumb(
                        access_token, bytes(payload), "cover.jpg"
                    )
            except Exception:
                thumb_media_id = self.thumb_media_id
        digest = " ".join(str(draft["body"]).split())[:120]
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
        submit_result = self._api(
            "POST",
            "/cgi-bin/freepublish/submit",
            f"?access_token={access_token}",
            json.dumps({"media_id": media_id}),
        )
        return {
            "status": "SUBMITTED",
            "reference": str(submit_result.get("publish_id", media_id)),
        }
