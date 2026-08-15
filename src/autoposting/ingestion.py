from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin

from .db import Database
from .engagement import extract_hn_id
from .images import BROWSER_UA, extract_image_urls_from_feed_fields, is_chrome_image_url
from .models import CollectionResult, FeedItem, Source
from .normalize import canonicalize_url, normalize_text

AI_CONTENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:"
    r"ai|a\.i\.|gpt-?\d*|chatgpt|llm|llms|claude|gemini|grok|"
    r"openai|anthropic|deepseek|qwen|copilot|transformer|nvidia"
    r")(?![a-z0-9])"
    r"|人工智能|大模型|智能体|生成式|机器学习|深度学习|神经网络"
)
DIGEST_PATTERN = re.compile(r"早报|晚报|午报|一周回顾")
COMMERCE_PATTERN = re.compile(
    r"开售|优惠价|限时优惠|新配色|以旧换新|首发价|预售|累计销量|万元起|元起|"
    r"京东.{0,12}元|购机即赠|刚需好价"
)
GADGET_PATTERN = re.compile(
    r"耳机|头戴|耳夹|手机|扫拖机器人|全景相机|轿车|纯电轿车|汽车：|点阵背屏"
)
FEATURE_AI_PATTERN = re.compile(
    r"AI\s*(超级)?(夜景|变声|成片|降噪|美颜|修图|跟拍|字幕|配音|一键)"
)


MIXED_FEED_IDS = {"ithome"}


def is_retail_or_gadget_noise(title: str) -> bool:
    text = title or ""
    if DIGEST_PATTERN.search(text):
        return True
    feature_only = FEATURE_AI_PATTERN.search(text) and not (
        AI_CONTENT_PATTERN.search(FEATURE_AI_PATTERN.sub(" ", text))
    )
    if feature_only and (COMMERCE_PATTERN.search(text) or GADGET_PATTERN.search(text)):
        return True
    if AI_CONTENT_PATTERN.search(text) and not feature_only:
        return False
    return bool(COMMERCE_PATTERN.search(text) or GADGET_PATTERN.search(text))


def should_hide_candidate(title: str, source_ids: list[str] | tuple[str, ...] = ()) -> bool:
    if is_retail_or_gadget_noise(title):
        return True
    mixed = any(source_id in MIXED_FEED_IDS for source_id in source_ids)
    if mixed and AI_CONTENT_PATTERN.search(title or "") is None:
        return True
    return False


def item_matches_filter(item: FeedItem, content_filter: str) -> bool:
    if content_filter != "ai":
        return True
    title = item.title or ""
    if is_retail_or_gadget_noise(title):
        return False
    return AI_CONTENT_PATTERN.search(title) is not None


Fetcher = Callable[[str], bytes]


def _default_fetcher(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, *names: str) -> str:
    for child in element:
        if _local_name(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def _entry_media_urls(element: ET.Element) -> list[str]:
    urls: list[str] = []
    for child in element.iter():
        name = _local_name(child.tag)
        if name == "enclosure":
            url = (child.attrib.get("url") or "").strip()
            if url:
                urls.append(url)
        elif name in {"content", "thumbnail"}:
            url = (child.attrib.get("url") or "").strip()
            if url:
                urls.append(url)
    return urls


def _entry_link(element: ET.Element) -> str:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        if child.text and child.text.strip():
            return child.text.strip()
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel in {"alternate", ""}:
            return href
    return ""


def _iso_date(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_feed(payload: bytes) -> list[FeedItem]:
    root = ET.fromstring(payload)
    entries = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    parsed: list[FeedItem] = []
    for entry in entries:
        try:
            title = normalize_text(_child_text(entry, "title"))
            url = _entry_link(entry)
            if not title or not url:
                continue
            content = _child_text(entry, "content", "encoded", "summary", "description")
            external_id = _child_text(entry, "guid", "id") or None
            published = _child_text(entry, "pubDate", "published", "updated")
            enclosure_url = ""
            enclosure_type = ""
            for child in entry:
                if _local_name(child.tag) == "enclosure":
                    enclosure_url = (child.attrib.get("url") or "").strip()
                    enclosure_type = (child.attrib.get("type") or "").strip()
                    break
            image_urls = extract_image_urls_from_feed_fields(
                page_url=url,
                content_html=content,
                enclosure_url=enclosure_url,
                enclosure_type=enclosure_type,
                media_urls=_entry_media_urls(entry),
            )
            comments = _child_text(entry, "comments")
            hn_id = extract_hn_id(url, comments, external_id)
            parsed.append(
                FeedItem(
                    external_id=external_id,
                    canonical_url=canonicalize_url(url),
                    title=title,
                    content_text=normalize_text(content),
                    published_at=_iso_date(published),
                    raw_data={
                        "external_id": external_id,
                        "link": url,
                        "published": published or None,
                        "title": title,
                        "image_urls": image_urls,
                        "comments": comments or None,
                        "hn_id": hn_id or None,
                    },
                )
            )
        except (TypeError, ValueError):
            continue
    return parsed


class _AggregatePageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.items: list[FeedItem] = []
        self.current: dict[str, str] | None = None
        self.capture: str | None = None
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        item_id = values.get("data-item-id")
        if item_id and self.current is None:
            self.current = {"external_id": item_id, "depth": str(self.depth)}
        if self.current is not None:
            if "timeline-title" in classes:
                self.capture = "title"
                self.current["url"] = urljoin(self.base_url, values.get("href") or "")
            elif "timeline-summary" in classes:
                self.capture = "summary"
            elif "timeline-source" in classes:
                self.capture = "source"
            elif "uc-body-p" in classes:
                self.capture = "summary"
            elif tag == "a" and "uc-body" in classes:
                self.current["url"] = urljoin(self.base_url, values.get("href") or "")
        self.depth += 1

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] = self.current.get(self.capture, "") + data

    def handle_endtag(self, tag: str) -> None:
        self.depth -= 1
        if tag in {"a", "p", "span"}:
            self.capture = None
        if self.current is not None and self.depth == int(self.current["depth"]):
            title = normalize_text(self.current.get("title", ""))
            summary = normalize_text(self.current.get("summary", ""))
            source = normalize_text(self.current.get("source", ""))
            url = self.current.get("url") or urljoin(
                self.base_url, f"items/{self.current['external_id']}"
            )
            if not title:
                title = summary.split("。", 1)[0].strip()
            if title and url:
                content = "\n".join(part for part in (source, summary) if part)
                self.items.append(
                    FeedItem(
                        external_id=self.current["external_id"],
                        canonical_url=canonicalize_url(url),
                        title=title,
                        content_text=content or title,
                        published_at=None,
                        raw_data={
                            "external_id": self.current["external_id"],
                            "link": url,
                            "published": None,
                            "title": title,
                        },
                    )
                )
            self.current = None


def parse_aggregate_page(payload: bytes, base_url: str) -> list[FeedItem]:
    parser = _AggregatePageParser(base_url)
    parser.feed(payload.decode("utf-8", errors="replace"))
    return parser.items


class RSSCollector:
    def __init__(self, database: Database, fetcher: Fetcher | None = None) -> None:
        self.database = database
        self.fetcher = fetcher or _default_fetcher

    def collect(self, source: Source) -> CollectionResult:
        if source.type != "rss":
            raise ValueError(f"RSSCollector cannot collect source type: {source.type}")
        try:
            items = [
                item
                for item in parse_feed(self.fetcher(source.url_or_handle))
                if item_matches_filter(item, source.content_filter)
            ]
            items = enrich_hn_engagement(items, self.fetcher)
            inserted = sum(self.database.insert_item(source, item) for item in items)
        except Exception as exc:
            self.database.record_fetch_result(source.source_id, str(exc))
            raise
        self.database.record_fetch_result(source.source_id)
        return CollectionResult(inserted=inserted, duplicates=len(items) - inserted)


SITEMAP_NEWS_LIMIT = 8
NEWS_SLUG_RE = re.compile(r"/news/(news\d+)/?$")


def parse_sitemap_locs(payload: bytes) -> list[str]:
    root = ET.fromstring(payload)
    locs: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "loc" or not element.text:
            continue
        url = element.text.strip()
        if url:
            locs.append(url)
    return locs


def news_slug_published_at(url: str) -> str | None:
    match = NEWS_SLUG_RE.search(url)
    if not match:
        return None
    digits = match.group(1)[4:]
    try:
        if len(digits) == 6:
            published = datetime(
                2000 + int(digits[:2]), int(digits[2:4]), int(digits[4:6]), tzinfo=timezone.utc
            )
        elif len(digits) == 4:
            published = datetime(2024, int(digits[:2]), int(digits[2:4]), tzinfo=timezone.utc)
        else:
            return None
    except ValueError:
        return None
    return published.isoformat()


class _OfficialNewsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self._in_title = False
        self._in_p = False
        self._chunk: list[str] = []
        self._paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            prop = found.get("property") or found.get("name")
            if prop in {"description", "og:description"} and found.get("content"):
                self.description = found["content"]
        elif tag == "p":
            self._in_p = True
            self._chunk = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = normalize_text("".join(self._chunk))
            if text:
                self._paragraphs.append(text)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._in_p:
            self._chunk.append(data)

    def summary(self) -> str:
        if self.description:
            return normalize_text(self.description)
        return normalize_text(" ".join(self._paragraphs[:3]))


def parse_official_news_page(payload: bytes, url: str) -> FeedItem | None:
    parser = _OfficialNewsParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    title = parser.title.split("|", 1)[0].strip()
    if not title:
        return None
    slug = NEWS_SLUG_RE.search(url)
    return FeedItem(
        external_id=slug.group(1) if slug else url,
        canonical_url=canonicalize_url(url),
        title=title,
        content_text=parser.summary() or title,
        published_at=news_slug_published_at(url),
        raw_data={"kind": "official_docs"},
    )


def collect_sitemap_news(fetcher: Fetcher, sitemap_url: str, limit: int = SITEMAP_NEWS_LIMIT) -> list[FeedItem]:
    locs = parse_sitemap_locs(fetcher(sitemap_url))
    news_urls = [url for url in locs if NEWS_SLUG_RE.search(url)]
    news_urls.sort(key=lambda url: news_slug_published_at(url) or "", reverse=True)
    items: list[FeedItem] = []
    for url in news_urls[:limit]:
        parsed = parse_official_news_page(fetcher(url), url)
        if parsed is not None:
            items.append(parsed)
    return items


class SitemapNewsCollector:
    def __init__(self, database: Database, fetcher: Fetcher | None = None) -> None:
        self.database = database
        self.fetcher = fetcher or _default_fetcher

    def collect(self, source: Source) -> CollectionResult:
        if source.type != "sitemap":
            raise ValueError(f"SitemapNewsCollector cannot collect source type: {source.type}")
        try:
            items = [
                item
                for item in collect_sitemap_news(self.fetcher, source.url_or_handle)
                if item_matches_filter(item, source.content_filter)
            ]
            inserted = sum(self.database.insert_item(source, item) for item in items)
        except Exception as exc:
            self.database.record_fetch_result(source.source_id, str(exc))
            raise
        self.database.record_fetch_result(source.source_id)
        return CollectionResult(inserted=inserted, duplicates=len(items) - inserted)


class AggregateCollector:
    def __init__(self, database: Database, fetcher: Fetcher | None = None) -> None:
        self.database = database
        self.fetcher = fetcher or _default_fetcher

    def collect(self, source: Source) -> CollectionResult:
        if source.type != "web":
            raise ValueError(f"AggregateCollector cannot collect source type: {source.type}")
        try:
            items = parse_aggregate_page(
                self.fetcher(source.url_or_handle), source.url_or_handle
            )
            inserted = sum(self.database.insert_item(source, item) for item in items)
        except Exception as exc:
            self.database.record_fetch_result(source.source_id, str(exc))
            raise
        self.database.record_fetch_result(source.source_id)
        return CollectionResult(inserted=inserted, duplicates=len(items) - inserted)


def enrich_hn_engagement(items: list[FeedItem], fetcher: Fetcher, limit: int = 8) -> list[FeedItem]:
    enriched: list[FeedItem] = []
    probed = 0
    for item in items:
        raw = dict(item.raw_data or {})
        hn_id = extract_hn_id(
            raw.get("hn_id"),
            raw.get("comments"),
            item.canonical_url,
            item.external_id,
        )
        if not hn_id or raw.get("score") or probed >= limit:
            enriched.append(item)
            continue
        probed += 1
        try:
            payload = json.loads(
                fetcher(f"https://hacker-news.firebaseio.com/v0/item/{hn_id}.json")
            )
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            enriched.append(item)
            continue
        if not isinstance(payload, dict):
            enriched.append(item)
            continue
        raw = dict(item.raw_data)
        raw["hn_id"] = hn_id
        if payload.get("score") is not None:
            raw["score"] = str(payload.get("score"))
        if payload.get("descendants") is not None:
            raw["comments"] = str(payload.get("descendants"))
        enriched.append(
            FeedItem(
                external_id=item.external_id,
                canonical_url=item.canonical_url,
                title=item.title,
                content_text=item.content_text,
                published_at=item.published_at,
                raw_data=raw,
            )
        )
    return enriched


def normalize_x_handle(value: str) -> str:
    text = (value or "").strip()
    if "://" in text:
        path = text.split("://", 1)[1]
        text = path.split("/", 1)[1] if "/" in path else ""
    return text.lstrip("@").split("/")[0].split("?")[0].strip()


def _tweet_is_retweet(tweet: dict[str, object]) -> bool:
    refs = tweet.get("referenced_tweets")
    if not isinstance(refs, list):
        return False
    return any(isinstance(row, dict) and row.get("type") == "retweeted" for row in refs)


def _media_by_key(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    includes = payload.get("includes")
    if not isinstance(includes, dict):
        return {}
    media = includes.get("media")
    if not isinstance(media, list):
        return {}
    index: dict[str, dict[str, object]] = {}
    for row in media:
        if isinstance(row, dict) and row.get("media_key"):
            index[str(row["media_key"])] = row
    return index


def tweet_image_urls(
    tweet: dict[str, object], media_by_key: dict[str, dict[str, object]]
) -> list[str]:
    attachments = tweet.get("attachments") if isinstance(tweet.get("attachments"), dict) else {}
    keys = attachments.get("media_keys") if isinstance(attachments, dict) else None
    urls: list[str] = []
    for key in keys or []:
        row = media_by_key.get(str(key)) or {}
        url = str(row.get("url") or row.get("preview_image_url") or "")
        if url.startswith(("http://", "https://")) and not is_chrome_image_url(url):
            urls.append(url)
    return list(dict.fromkeys(urls))


def x_user_tweets_url(user_id: str) -> str:
    return (
        f"https://api.x.com/2/users/{user_id}/tweets"
        "?max_results=20&exclude=replies"
        "&tweet.fields=created_at,public_metrics,attachments,referenced_tweets"
        "&expansions=attachments.media_keys"
        "&media.fields=url,preview_image_url,type,width,height"
    )


class XCollector:
    def __init__(
        self,
        database: Database,
        fetcher: Fetcher | None = None,
        bearer_token: str = "",
    ) -> None:
        self.database = database
        self.bearer_token = bearer_token.strip()
        self.fetcher = fetcher or self._authorized_fetcher

    def _authorized_fetcher(self, url: str) -> bytes:
        if not self.bearer_token:
            raise ValueError("x_bearer_token_required")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": BROWSER_UA,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()

    def collect(self, source: Source) -> CollectionResult:
        if source.type != "x":
            raise ValueError(f"XCollector cannot collect source type: {source.type}")
        if not self.bearer_token and self.fetcher is self._authorized_fetcher:
            raise ValueError("x_bearer_token_required")
        handle = normalize_x_handle(source.url_or_handle)
        if not handle:
            raise ValueError("x_handle_required")
        try:
            user_payload = json.loads(
                self.fetcher(f"https://api.x.com/2/users/by/username/{handle}")
            )
            user = user_payload.get("data") if isinstance(user_payload, dict) else None
            if not isinstance(user, dict) or not user.get("id"):
                raise ValueError("x_user_not_found")
            user_id = str(user["id"])
            username = str(user.get("username") or handle)
            tweets_payload = json.loads(self.fetcher(x_user_tweets_url(user_id)))
            tweets = tweets_payload.get("data") if isinstance(tweets_payload, dict) else []
            media_by_key = _media_by_key(tweets_payload) if isinstance(tweets_payload, dict) else {}
            items: list[FeedItem] = []
            for tweet in tweets or []:
                if not isinstance(tweet, dict) or not tweet.get("id"):
                    continue
                if _tweet_is_retweet(tweet):
                    continue
                metrics = tweet.get("public_metrics") if isinstance(tweet.get("public_metrics"), dict) else {}
                text = normalize_text(str(tweet.get("text") or ""))
                url = f"https://x.com/{username}/status/{tweet['id']}"
                if not text:
                    continue
                image_urls = tweet_image_urls(tweet, media_by_key)
                raw_data: dict[str, object] = {
                    "external_id": str(tweet["id"]),
                    "link": url,
                    "title": text,
                    "username": username,
                    "likes": str(metrics.get("like_count") or 0),
                    "reposts": str(metrics.get("retweet_count") or 0),
                    "replies": str(metrics.get("reply_count") or 0),
                    "image_urls": image_urls,
                }
                items.append(
                    FeedItem(
                        external_id=str(tweet["id"]),
                        canonical_url=canonicalize_url(url),
                        title=text.split("\n", 1)[0][:180],
                        content_text=text,
                        published_at=_iso_date(str(tweet.get("created_at") or "")),
                        raw_data=raw_data,  # type: ignore[arg-type]
                    )
                )
            inserted = sum(self.database.insert_item(source, item) for item in items)
        except Exception as exc:
            self.database.record_fetch_result(source.source_id, str(exc))
            raise
        self.database.record_fetch_result(source.source_id)
        return CollectionResult(inserted=inserted, duplicates=len(items) - inserted)