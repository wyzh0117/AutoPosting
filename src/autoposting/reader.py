"""Small, dependency-free article reader used by on-demand collection.

The reader deliberately keeps fetching and extraction together behind one
stable contract.  It is not a crawler: callers decide which URL to read and
may provide content already present in an RSS item to avoid a second request.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .images import BROWSER_UA
from .normalize import canonicalize_url, content_hash, normalize_text


ReaderFetcher = Callable[[str], Any]
RenderFetcher = Callable[[str], Any]

FULL = "full"
PARTIAL = "partial"
BLOCKED = "blocked"
PAYWALLED = "paywalled"
EMPTY = "empty"
FAILED = "failed"
READ_STATUSES = frozenset({FULL, PARTIAL, BLOCKED, PAYWALLED, EMPTY, FAILED})

_BLOCKED_MARKERS = (
    "access denied",
    "access forbidden",
    "checking your browser",
    "enable javascript",
    "enable cookies",
    "verify you are human",
    "verify that you are human",
    "captcha",
    "robot check",
    "unusual traffic",
    "request blocked",
    "forbidden",
    "访问被拒绝",
    "请启用 javascript",
    "请验证您是人类",
)
_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscription required",
    "sign in to continue reading",
    "login to continue reading",
    "continue reading with a subscription",
    "already a subscriber",
    "premium content",
    "paywall",
    "members only",
    "订阅后继续阅读",
    "登录后继续阅读",
    "付费内容",
)
_FAILURE_MARKERS = (
    "bad gateway",
    "internal server error",
    "service unavailable",
    "temporarily unavailable",
    "page not found",
    "404 not found",
    "error 500",
    "服务器错误",
    "页面不存在",
)
_CONTENT_CONTAINER_RE = re.compile(
    r"(?:^|[-_])(?:article|content|post|story|entry|正文|文章)(?:$|[-_])",
    re.I,
)
_JSON_LD_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>"
    r"(.*?)</script\s*>",
    re.I | re.S,
)
_JS_SHELL_RE = re.compile(
    r"(?:<script\b|(?:id|class)\s*=\s*['\"](?:root|app|__next|__nuxt|react-root|app-root)\b)",
    re.I,
)


@dataclass(frozen=True)
class ArticleDocument:
    """Normalized output of :class:`ReaderService`.

    ``text`` is retained for partial/blocked documents so callers can inspect
    what the page actually returned.  Consumers must use ``status`` before
    treating it as a complete article.
    """

    url: str
    canonical_url: str | None
    title: str | None
    author: str | None
    published_at: str | None
    text: str
    excerpt: str | None
    source_domain: str | None
    source_type: str | None
    fetch_method: str
    extractor: str
    status: str
    quality_score: float
    failure_reason: str | None
    fetched_at: datetime
    content_hash: str | None

    def __post_init__(self) -> None:
        if self.status not in READ_STATUSES:
            raise ValueError(f"unsupported article status: {self.status}")
        if not 0.0 <= self.quality_score <= 1.0:
            raise ValueError("quality_score must be between 0 and 1")


@dataclass(frozen=True)
class _Extracted:
    title: str | None
    author: str | None
    published_at: str | None
    canonical_url: str | None
    text: str
    extractor: str
    paragraphs: tuple[str, ...]
    link_chars: int


@dataclass(frozen=True)
class QualityResult:
    score: float
    status: str
    failure_reason: str | None


class ArticleQualityGate:
    """Score extracted text and classify obvious non-article responses."""

    def evaluate(
        self,
        title: str | None,
        text: str,
        *,
        paragraphs: tuple[str, ...] | list[str] = (),
        link_chars: int = 0,
    ) -> QualityResult:
        cleaned = normalize_text(text)
        if not cleaned:
            return QualityResult(0.0, EMPTY, "empty_content")

        lower = cleaned.casefold()
        # A marker is only treated as a gate failure when it is a meaningful
        # portion of the response.  Long articles can legitimately mention
        # subscriptions or JavaScript in their body.
        if len(cleaned) < 900:
            if any(marker in lower for marker in _FAILURE_MARKERS):
                return QualityResult(
                    self._score(title, cleaned, paragraphs, link_chars),
                    FAILED,
                    "failure_page",
                )
            if any(marker in lower for marker in _BLOCKED_MARKERS):
                return QualityResult(self._score(title, cleaned, paragraphs, link_chars), BLOCKED, "blocked_page")
            if any(marker in lower for marker in _PAYWALL_MARKERS):
                return QualityResult(
                    self._score(title, cleaned, paragraphs, link_chars),
                    PAYWALLED,
                    "paywall_or_truncated",
                )

        blocks = tuple(normalize_text(item) for item in paragraphs if normalize_text(item))
        if not blocks:
            blocks = tuple(part.strip() for part in re.split(r"\n{2,}", cleaned) if part.strip())
        score = self._score(title, cleaned, blocks, link_chars)
        if len(cleaned) >= 80 and score >= 0.55:
            return QualityResult(score, FULL, None)
        return QualityResult(score, PARTIAL, "low_quality")

    @staticmethod
    def _score(
        title: str | None,
        text: str,
        paragraphs: tuple[str, ...] | list[str],
        link_chars: int,
    ) -> float:
        if not text:
            return 0.0
        blocks = tuple(item for item in paragraphs if item)
        chars = len(text)
        paragraph_count = max(len(blocks), 1)
        average_length = chars / paragraph_count
        length_score = min(chars / 400.0, 1.0)
        paragraph_score = min(paragraph_count / 3.0, 1.0)
        average_score = min(average_length / 120.0, 1.0)
        link_ratio = min(max(link_chars, 0) / max(chars, 1), 1.0)
        link_score = max(0.0, 1.0 - link_ratio * 2.0)
        duplicate_score = 1.0
        if blocks:
            duplicate_score = len(set(blocks)) / len(blocks)
        relevance = _title_relevance(title or "", text)
        score = (
            length_score * 0.35
            + paragraph_score * 0.20
            + average_score * 0.15
            + link_score * 0.15
            + duplicate_score * 0.10
            + relevance * 0.05
        )
        return round(max(0.0, min(score, 1.0)), 3)


class _BodyParser(HTMLParser):
    _BLOCKS = {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
    }
    _SKIP = {"script", "style", "nav", "header", "footer", "aside", "svg", "noscript", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_tags: list[str] = []
        self.focus_depth = 0
        self._focus_tags: list[str] = []
        self._open_blocks: list[tuple[str, list[str], int]] = []
        self.blocks: list[tuple[bool, str, int]] = []
        self.loose_parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.canonical: str | None = None
        self._in_title = False
        self._link_depth = 0
        self._script_json_depth = 0
        self.json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {str(key).lower(): (value or "") for key, value in attrs}
        if self.skip_tags:
            if tag in self._SKIP:
                self.skip_tags.append(tag)
            return
        if tag in self._SKIP:
            if tag == "script" and "application/ld+json" in values.get("type", "").lower():
                self._script_json_depth = 1
            else:
                self.skip_tags.append(tag)
            return
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = (values.get("name") or values.get("property") or "").lower().strip()
            value = values.get("content", "").strip()
            if key and value:
                self.meta[key] = value
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical = values.get("href", "").strip() or None
        elif tag == "a":
            self._link_depth += 1
        if tag in {"article", "main"} or self._is_content_container(values):
            self.focus_depth += 1
            self._focus_tags.append(tag)
        if tag in self._BLOCKS:
            self._open_blocks.append((tag, [], 0))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._script_json_depth:
            if tag == "script":
                self._script_json_depth = 0
            return
        if self.skip_tags:
            if tag == self.skip_tags[-1]:
                self.skip_tags.pop()
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_depth:
            self._link_depth -= 1
        if tag in self._BLOCKS:
            for index in range(len(self._open_blocks) - 1, -1, -1):
                open_tag, parts, link_chars = self._open_blocks[index]
                if open_tag != tag:
                    continue
                del self._open_blocks[index]
                text = normalize_text(" ".join(parts))
                if text:
                    self.blocks.append((self.focus_depth > 0, text, link_chars))
                break
        if self._focus_tags and self._focus_tags[-1] == tag:
            self._focus_tags.pop()
            self.focus_depth = max(0, self.focus_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._script_json_depth:
            self.json_ld_parts.append(data)
            return
        if self.skip_tags:
            return
        if self._in_title:
            self.title_parts.append(data)
        if not self._open_blocks:
            if not self._in_title:
                self.loose_parts.append(data)
            return
        self._open_blocks[-1][1].append(data)
        if self._link_depth:
            tag, parts, link_chars = self._open_blocks[-1]
            self._open_blocks[-1] = (tag, parts, link_chars + len(data))

    @staticmethod
    def _is_content_container(attrs: Mapping[str, str]) -> bool:
        value = f"{attrs.get('id', '')} {attrs.get('class', '')}".strip()
        if not value:
            return False
        return any(_CONTENT_CONTAINER_RE.search(part) for part in value.split())


def normalize_url(value: str) -> str:
    """Normalize a URL using the project's canonical URL policy."""

    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        return canonicalize_url(value)
    except ValueError:
        return ""


def _title_relevance(title: str, text: str) -> float:
    title = normalize_text(title).casefold()
    if not title:
        return 0.5
    text = text.casefold()
    if title and title in text:
        return 1.0
    words = [part for part in re.findall(r"[\w\u3400-\u9fff]+", title) if len(part) > 1]
    if not words:
        return 0.5
    return min(sum(word in text for word in words) / len(words) + 0.2, 1.0)


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_text(value) or None
    if isinstance(value, Mapping):
        return _first_string(value.get("name") or value.get("text"))
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_string(item)
            if found:
                return found
    return None


def _json_ld_articles(value: Any) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if isinstance(value.get("articleBody"), str):
            found.append(value)
        graph = value.get("@graph")
        if graph is not None:
            found.extend(_json_ld_articles(graph))
    elif isinstance(value, list):
        for item in value:
            found.extend(_json_ld_articles(item))
    return found


def _extract_json_ld(html: str) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    best: Mapping[str, Any] | None = None
    best_text = ""
    for raw in _JSON_LD_RE.findall(html):
        try:
            value = json.loads(html_lib.unescape(raw).strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for candidate in _json_ld_articles(value):
            body = normalize_text(str(candidate.get("articleBody") or ""))
            if len(body) > len(best_text):
                best = candidate
                best_text = body
    if best is None or not best_text:
        return None, None, None, None, None
    author = _first_string(best.get("author"))
    title = _first_string(best.get("headline") or best.get("name"))
    published = _first_string(best.get("datePublished") or best.get("dateCreated"))
    canonical = _first_string(best.get("url"))
    return best_text, title, author, published, canonical


def _extract_html(payload: str) -> _Extracted:
    parser = _BodyParser()
    try:
        parser.feed(payload)
        parser.close()
    except (TypeError, ValueError):
        # HTMLParser is intentionally forgiving, but malformed surrogate
        # input should still leave callers with a useful partial document.
        pass

    title = normalize_text(parser.meta.get("og:title") or parser.meta.get("twitter:title") or "")
    if not title:
        title = normalize_text(" ".join(parser.title_parts)) or None
    author = normalize_text(
        parser.meta.get("author") or parser.meta.get("article:author") or ""
    ) or None
    published = normalize_text(
        parser.meta.get("article:published_time")
        or parser.meta.get("datePublished")
        or parser.meta.get("date")
        or ""
    ) or None
    json_text, json_title, json_author, json_published, json_canonical = _extract_json_ld(payload)
    if json_title:
        title = json_title
    if json_author:
        author = json_author
    if json_published:
        published = json_published
    canonical = parser.canonical or parser.meta.get("og:url") or json_canonical

    focused = [item for item in parser.blocks if item[0]]
    chosen = focused if sum(len(item[1]) for item in focused) >= 200 else parser.blocks
    unique: list[str] = []
    link_chars = 0
    for _focused, text, links in chosen:
        if text and text not in unique:
            unique.append(text)
            link_chars += links
    html_text = "\n\n".join(unique)
    loose_text = normalize_text(" ".join(parser.loose_parts))
    if loose_text and (
        not html_text
        or any(marker in loose_text.casefold() for marker in _BLOCKED_MARKERS + _PAYWALL_MARKERS)
    ):
        unique.append(loose_text)
        html_text = "\n\n".join(unique)
    if json_text and (len(json_text) >= len(html_text) or not html_text):
        return _Extracted(
            title=title,
            author=author,
            published_at=published,
            canonical_url=canonical,
            text=json_text,
            extractor="json-ld",
            paragraphs=tuple(part for part in re.split(r"\n{2,}", json_text) if part),
            link_chars=0,
        )
    return _Extracted(
        title=title,
        author=author,
        published_at=published,
        canonical_url=canonical,
        text=html_text,
        extractor="html",
        paragraphs=tuple(item for item in unique if item),
        link_chars=link_chars,
    )


def _looks_like_js_shell(payload: str, document: ArticleDocument) -> bool:
    """Recognize a mostly empty app shell without penalizing static pages."""

    if not payload or "<script" not in payload.casefold():
        return False
    lower = payload.casefold()
    if any(marker in lower for marker in ("enable javascript", "javascript required", "请启用 javascript")):
        return True
    if document.status != FULL and len(document.text) < 160:
        return True
    if _JS_SHELL_RE.search(payload) is None:
        return False
    return document.status != FULL or len(document.text) < 160


def _extract_rss(value: Any) -> _Extracted:
    if isinstance(value, bytes):
        raw = value.decode("utf-8-sig", errors="replace")
    else:
        raw = str(value or "")
    # RSS content:encoded is normally HTML.  A plain text item is still a
    # valid full-content candidate and should not trigger a duplicate fetch.
    if "<" in raw and ">" in raw:
        extracted = _extract_html(raw)
        if extracted.text:
            return _Extracted(
                title=extracted.title,
                author=extracted.author,
                published_at=extracted.published_at,
                canonical_url=extracted.canonical_url,
                text=extracted.text,
                extractor="rss",
                paragraphs=extracted.paragraphs,
                link_chars=extracted.link_chars,
            )
    text = normalize_text(raw)
    return _Extracted(
        title=None,
        author=None,
        published_at=None,
        canonical_url=None,
        text=text,
        extractor="rss",
        paragraphs=tuple(part for part in re.split(r"\n{2,}", text) if part),
        link_chars=0,
    )


def _coerce_fetch_result(result: Any) -> tuple[bytes, int | None]:
    if isinstance(result, bytes):
        return result, None
    if isinstance(result, bytearray):
        return bytes(result), None
    if isinstance(result, str):
        return result.encode("utf-8"), None
    if isinstance(result, tuple) and len(result) >= 2:
        first, second = result[0], result[1]
        if isinstance(first, int):
            payload = second
            status = first
        else:
            payload = first
            status = second if isinstance(second, int) else None
        return _coerce_fetch_result(payload)[0], status
    read = getattr(result, "read", None)
    if callable(read):
        payload = read()
        status = getattr(result, "status", None) or getattr(result, "code", None)
        return _coerce_fetch_result(payload)[0], status if isinstance(status, int) else None
    raise TypeError("fetcher must return bytes, text, or a response-like value")


def playwright_render_fetcher(url: str) -> str:
    """Render one URL with an optional Playwright installation.

    Playwright is intentionally imported lazily.  The normal reader path has
    no browser dependency; callers opt into this renderer explicitly when the
    host application has Playwright and a browser available.
    """

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("dynamic_renderer_unavailable") from exc
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError("invalid_url")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(normalized, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                # A page with a never-ending analytics request is still useful
                # once DOMContentLoaded has fired.
                pass
            return page.content()
        finally:
            browser.close()


class ReaderService:
    """Read one URL and return a stable :class:`ArticleDocument`."""

    def __init__(
        self,
        fetcher: ReaderFetcher | None = None,
        *,
        render_fetcher: RenderFetcher | None = None,
        dynamic_fetcher: RenderFetcher | None = None,
        quality_gate: ArticleQualityGate | None = None,
    ) -> None:
        self.fetcher = fetcher or self._default_fetcher
        self.render_fetcher = render_fetcher or dynamic_fetcher
        self.quality_gate = quality_gate or ArticleQualityGate()

    @staticmethod
    def _default_fetcher(url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=20) as response:
            return response.read()

    def read(
        self,
        url: str,
        *,
        payload: bytes | str | None = None,
        rss_content: bytes | str | None = None,
        rss_item: Any = None,
        feed_item: Any = None,
        title_hint: str | None = None,
        author: str | None = None,
        published_at: str | None = None,
        source_type: str | None = None,
        **options: Any,
    ) -> ArticleDocument:
        """Read a URL, reusing supplied RSS content before making HTTP.

        ``feed_item``/``rss_item`` accept either a ``FeedItem``-like object or
        a mapping.  The extra aliases in ``options`` keep integrations simple
        without changing this contract when a collector calls the argument
        ``body`` or ``content``.
        """

        requested_url = str(url or "").strip()
        normalized = normalize_url(requested_url)
        now = datetime.now(timezone.utc)
        if not normalized:
            return self._document(
                requested_url,
                None,
                None,
                None,
                "",
                None,
                "",
                "failed",
                0.0,
                "invalid_url",
                now,
            )

        item = rss_item if rss_item is not None else feed_item
        if item is None:
            item = options.get("rss_entry") or options.get("item")
        rss_value = rss_content
        if item is not None:
            item_value, item_title, item_author, item_published = self._feed_fields(item)
            if rss_value is None:
                rss_value = item_value
            title_hint = title_hint or item_title
            author = author or item_author
            published_at = published_at or item_published
        if rss_value is None:
            rss_value = (
                options.get("rss_text")
                or options.get("rss_body")
                or options.get("rss_payload")
                or options.get("rss_html")
                or options.get("feed_content")
                or options.get("content")
            )

        if rss_value is not None and str(rss_value or "").strip():
            extracted = _extract_rss(rss_value)
            document = self._from_extracted(
                requested_url,
                normalized,
                extracted,
                title_hint=title_hint,
                author=author,
                published_at=published_at,
                source_type=source_type or "rss",
                fetch_method="rss",
                now=now,
            )
            if document.status == FULL or payload is None and self.fetcher is None:
                return document
            # RSS summaries are useful partial evidence, but a configured
            # fetcher may be able to upgrade them to the article page.
            if document.status == FULL:
                return document

        if payload is None:
            try:
                fetched = self.fetcher(normalized)
                raw_payload, response_status = _coerce_fetch_result(fetched)
            except HTTPError as exc:
                status = BLOCKED if exc.code in {401, 403, 429} else FAILED
                reason = "blocked_http" if status == BLOCKED else "fetch_failed"
                return self._document(
                    requested_url,
                    normalized,
                    title_hint,
                    author,
                    "",
                    None,
                    "http",
                    status,
                    0.0,
                    reason,
                    now,
                    published_at=published_at,
                )
            except (OSError, TimeoutError, ValueError, TypeError):
                return self._document(
                    requested_url,
                    normalized,
                    title_hint,
                    author,
                    "",
                    None,
                    "http",
                    FAILED,
                    0.0,
                    "fetch_failed",
                    now,
                    published_at=published_at,
                )
        else:
            try:
                raw_payload, response_status = _coerce_fetch_result(payload)
            except (TypeError, ValueError):
                return self._document(
                    requested_url,
                    normalized,
                    title_hint,
                    author,
                    "",
                    None,
                    source_type or "http",
                    FAILED,
                    0.0,
                    "invalid_payload",
                    now,
                    published_at=published_at,
                )

        if response_status is not None and response_status >= 400:
            status = BLOCKED if response_status in {401, 403, 429} else FAILED
            return self._document(
                requested_url,
                normalized,
                title_hint,
                author,
                "",
                None,
                source_type or "http",
                status,
                0.0,
                "blocked_http" if status == BLOCKED else "http_error",
                now,
                published_at=published_at,
            )
        html = raw_payload.decode("utf-8-sig", errors="replace")
        extracted = _extract_html(html)
        document = self._from_extracted(
            requested_url,
            normalized,
            extracted,
            title_hint=title_hint,
            author=author,
            published_at=published_at,
            source_type=source_type or "html",
            fetch_method="http" if payload is None else "payload",
            now=now,
        )
        renderer = (
            options.get("render_fetcher")
            or options.get("dynamic_fetcher")
            or self.render_fetcher
        )
        if not callable(renderer):
            return document
        if document.status == FULL and not _looks_like_js_shell(html, document):
            return document
        return self._render_fallback(
            requested_url,
            normalized,
            document,
            renderer,
            title_hint=title_hint,
            author=author,
            published_at=published_at,
            source_type=source_type or "html",
            now=now,
        )

    def read_payload(
        self,
        payload: bytes | str,
        *,
        url: str = "",
        title_hint: str | None = None,
        source_type: str | None = None,
        render_fetcher: RenderFetcher | None = None,
    ) -> ArticleDocument:
        """Parse an already fetched page without changing old call sites."""

        return self.read(
            url or "https://local.invalid/",
            payload=payload,
            title_hint=title_hint,
            source_type=source_type or "html",
            render_fetcher=render_fetcher,
        )

    def read_feed_item(self, item: Any) -> ArticleDocument:
        """Read a FeedItem-like value while reusing its body."""

        value, title, author, published = self._feed_fields(item)
        url = self._field(item, "canonical_url") or self._field(item, "url") or ""
        return self.read(
            url,
            rss_content=value,
            title_hint=title,
            author=author,
            published_at=published,
            source_type="rss",
        )

    # Friendly aliases for callers that use an imperative name.
    read_url = read
    read_html = read_payload

    def _render_fallback(
        self,
        requested_url: str,
        normalized: str,
        static_document: ArticleDocument,
        renderer: RenderFetcher,
        *,
        title_hint: str | None,
        author: str | None,
        published_at: str | None,
        source_type: str,
        now: datetime,
    ) -> ArticleDocument:
        """Try one rendered page, preserving a non-full result on failure."""

        try:
            rendered = renderer(normalized)
            raw_payload, response_status = _coerce_fetch_result(rendered)
        except RuntimeError as exc:
            reason = str(exc).strip()
            return replace(
                static_document,
                fetch_method="dynamic",
                failure_reason=(
                    "dynamic_renderer_unavailable"
                    if reason == "dynamic_renderer_unavailable"
                    else "dynamic_fetch_failed"
                ),
            )
        except Exception:
            return replace(
                static_document,
                fetch_method="dynamic",
                failure_reason="dynamic_fetch_failed",
            )
        if response_status is not None and response_status >= 400:
            return replace(
                static_document,
                fetch_method="dynamic",
                failure_reason="dynamic_http_error",
            )
        rendered_html = raw_payload.decode("utf-8-sig", errors="replace")
        rendered_extracted = _extract_html(rendered_html)
        rendered_document = self._from_extracted(
            requested_url,
            normalized,
            rendered_extracted,
            title_hint=title_hint,
            author=author,
            published_at=published_at,
            source_type=source_type,
            fetch_method="dynamic",
            now=now,
        )
        if rendered_document.status == FULL:
            return rendered_document
        if static_document.status == FULL:
            return static_document
        if rendered_document.quality_score >= static_document.quality_score:
            return rendered_document
        return replace(
            static_document,
            fetch_method="dynamic",
            failure_reason="dynamic_low_quality",
        )

    def _from_extracted(
        self,
        requested_url: str,
        normalized: str,
        extracted: _Extracted,
        *,
        title_hint: str | None,
        author: str | None,
        published_at: str | None,
        source_type: str,
        fetch_method: str,
        now: datetime,
    ) -> ArticleDocument:
        title = extracted.title or normalize_text(title_hint or "") or None
        text = normalize_text(extracted.text)
        quality = self.quality_gate.evaluate(
            title,
            text,
            paragraphs=extracted.paragraphs,
            link_chars=extracted.link_chars,
        )
        canonical_candidate = extracted.canonical_url or normalized
        canonical = normalize_url(canonical_candidate) or normalized
        return self._document(
            requested_url,
            canonical,
            title,
            author or extracted.author,
            text,
            extracted.extractor,
            source_type,
            quality.status,
            quality.score,
            quality.failure_reason,
            now,
            published_at=published_at or extracted.published_at,
            fetch_method=fetch_method,
        )

    @staticmethod
    def _field(item: Any, name: str) -> str:
        if isinstance(item, Mapping):
            return str(item.get(name) or "").strip()
        return str(getattr(item, name, "") or "").strip()

    @classmethod
    def _feed_fields(cls, item: Any) -> tuple[str, str, str, str]:
        value = cls._field(item, "content_text") or cls._field(item, "content")
        title = cls._field(item, "title")
        author = cls._field(item, "author")
        published = cls._field(item, "published_at")
        raw = item.get("raw_data") if isinstance(item, Mapping) else getattr(item, "raw_data", None)
        if isinstance(raw, Mapping):
            value = value or str(
                raw.get("content_html") or raw.get("content") or raw.get("description") or ""
            ).strip()
            author = author or str(raw.get("author") or "").strip()
            published = published or str(raw.get("published") or "").strip()
        return value, title, author, published

    @staticmethod
    def _document(
        url: str,
        canonical_url: str | None,
        title: str | None,
        author: str | None,
        text: str,
        extractor: str | None,
        source_type: str | None,
        status: str,
        quality_score: float,
        failure_reason: str | None,
        fetched_at: datetime,
        *,
        published_at: str | None = None,
        fetch_method: str = "http",
    ) -> ArticleDocument:
        cleaned = normalize_text(text)
        return ArticleDocument(
            url=url,
            canonical_url=canonical_url,
            title=normalize_text(title or "") or None,
            author=normalize_text(author or "") or None,
            published_at=normalize_text(published_at or "") or None,
            text=cleaned,
            excerpt=cleaned[:500] or None,
            source_domain=(urlsplit(canonical_url or url).hostname or "").lower() or None,
            source_type=source_type,
            fetch_method=fetch_method,
            extractor=extractor or "none",
            status=status,
            quality_score=round(max(0.0, min(float(quality_score), 1.0)), 3),
            failure_reason=failure_reason,
            fetched_at=fetched_at,
            content_hash=content_hash(title or "", cleaned) if cleaned else None,
        )


__all__ = [
    "ArticleDocument",
    "ArticleQualityGate",
    "BLOCKED",
    "EMPTY",
    "FAILED",
    "FULL",
    "PARTIAL",
    "PAYWALLED",
    "QualityResult",
    "ReaderService",
    "RenderFetcher",
    "normalize_url",
    "playwright_render_fetcher",
]
