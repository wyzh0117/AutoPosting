"""Gather recent background for a hotspot before drafting.

Uses the local SQLite window plus a DuckDuckGo HTML search. Failures stay
empty notes so drafting can still run on the original item.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from .db import Database
from .images import BROWSER_UA
from .normalize import normalize_text

Fetcher = Callable[[str], bytes]
WEB_RESULT_LIMIT = 5
LOCAL_RESULT_LIMIT = 5
PAGE_TEXT_LIMIT = 1800
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "新闻",
    "消息",
    "发布",
    "今日",
    "报道",
    "称",
}


@dataclass
class BackgroundNote:
    focus: str
    query: str
    local: list[dict[str, str]] = field(default_factory=list)
    web: list[dict[str, str]] = field(default_factory=list)
    source_excerpt: str = ""

    def as_prompt(self) -> dict[str, object]:
        return {
            "focus": self.focus,
            "query": self.query,
            "source_excerpt": self.source_excerpt,
            "local_related": self.local,
            "web_related": self.web,
        }


def infer_focus(title: str, summary: str = "") -> str:
    text = normalize_text(title or "")
    if not text:
        return normalize_text(summary)[:80]
    return text[:120]


def search_query(title: str) -> str:
    focus = infer_focus(title)
    if "2026" not in focus and "2025" not in focus:
        return f"{focus} 2026"
    return focus


def _tokens(title: str) -> set[str]:
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", title or "")
    return {part.casefold() for part in parts if part.casefold() not in STOPWORDS}


class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._in_title = False
        self._in_snippet = False
        self._title: list[str] = []
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        classes = found.get("class", "")
        href = found.get("href", "")
        if tag == "a" and href and (
            "result__a" in classes or "result-link" in classes or found.get("rel") == "nofollow"
        ):
            if href.startswith("http") and "duckduckgo.com" not in href:
                self._href = href
                self._in_title = True
                self._title = []
        elif tag in {"a", "td", "div"} and (
            "result__snippet" in classes or "result-snippet" in classes
        ):
            self._in_snippet = True
            self._snippet = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
            title = normalize_text("".join(self._title))
            if self._href and title:
                self.results.append({"title": title, "url": self._href, "snippet": ""})
                self._href = ""
        if tag in {"a", "td", "div"} and self._in_snippet:
            self._in_snippet = False
            snippet = normalize_text("".join(self._snippet))
            if snippet and self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = snippet[:280]

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)
        elif self._in_snippet:
            self._snippet.append(data)


def parse_search_html(payload: bytes) -> list[dict[str, str]]:
    parser = _DDGParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    parser.close()
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in parser.results:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)
    return unique


def _default_search_fetcher(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=12) as response:
        return response.read(400_000)


def _parse_ddg_json(payload: bytes) -> list[dict[str, str]]:
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    results: list[dict[str, str]] = []
    abstract = str(parsed.get("AbstractText") or "").strip()
    abstract_url = str(parsed.get("AbstractURL") or "").strip()
    heading = str(parsed.get("Heading") or "").strip()
    if abstract and abstract_url:
        results.append({"title": heading or abstract[:80], "url": abstract_url, "snippet": abstract[:280]})
    related = parsed.get("RelatedTopics")
    if isinstance(related, list):
        for entry in related:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("Text") or "").strip()
            url = str(entry.get("FirstURL") or "").strip()
            if text and url:
                results.append({"title": text.split(" - ", 1)[0][:120], "url": url, "snippet": text[:280]})
    return results


def _parse_wikipedia(payload: bytes) -> list[dict[str, str]]:
    try:
        parsed = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list) or len(parsed) < 4:
        return []
    titles, snippets, urls = parsed[1], parsed[2], parsed[3]
    results: list[dict[str, str]] = []
    for title, snippet, url in zip(titles, snippets, urls):
        if title and url:
            results.append(
                {"title": str(title), "url": str(url), "snippet": str(snippet or "")[:280]}
            )
    return results


def search_web(query: str, fetcher: Fetcher | None = None, limit: int = WEB_RESULT_LIMIT) -> list[dict[str, str]]:
    if not query.strip():
        return []
    load = fetcher or _default_search_fetcher
    encoded = quote_plus(query.strip())
    attempts = (
        (f"https://html.duckduckgo.com/html/?q={encoded}", parse_search_html),
        (f"https://lite.duckduckgo.com/lite/?q={encoded}", parse_search_html),
        (f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1", _parse_ddg_json),
        (
            "https://en.wikipedia.org/w/api.php?action=opensearch&limit=3&format=json"
            f"&search={encoded}",
            _parse_wikipedia,
        ),
    )
    for url, parser in attempts:
        try:
            results = parser(load(url))
        except (OSError, TimeoutError, ValueError):
            continue
        if results:
            return results[:limit]
    return []


def search_local(database: Database, title: str, item_id: str = "", hours: int = 168) -> list[dict[str, str]]:
    tokens = _tokens(title)
    if not tokens:
        return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    scored: list[tuple[int, dict[str, str]]] = []
    for row in database.list_recent_items(since):
        other_id = str(row.get("item_id") or "")
        if item_id and other_id == item_id:
            continue
        other_title = str(row.get("title") or "")
        overlap = len(tokens & _tokens(other_title))
        if overlap < 1:
            continue
        scored.append(
            (
                overlap,
                {
                    "title": other_title,
                    "summary": normalize_text(str(row.get("content_text") or ""))[:240],
                    "url": str(row.get("canonical_url") or ""),
                    "source_id": str(row.get("source_id") or ""),
                },
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:LOCAL_RESULT_LIMIT]]


def excerpt_page(payload: bytes) -> str:
    return normalize_text(payload.decode("utf-8", errors="replace"))[:PAGE_TEXT_LIMIT]


def gather_background(
    database: Database,
    title: str,
    summary: str = "",
    item_id: str = "",
    page_url: str = "",
    fetcher: Fetcher | None = None,
) -> BackgroundNote:
    focus = infer_focus(title, summary)
    note = BackgroundNote(focus=focus, query=search_query(title), source_excerpt=normalize_text(summary)[:PAGE_TEXT_LIMIT])
    try:
        note.local = search_local(database, title, item_id=item_id)
    except (OSError, ValueError):
        note.local = []
    if fetcher is not None and page_url:
        try:
            excerpt = excerpt_page(fetcher(page_url))
            if excerpt:
                note.source_excerpt = excerpt
        except (OSError, TimeoutError, ValueError, UnicodeError):
            pass
    if fetcher is not None:
        try:
            note.web = search_web(note.query, fetcher=fetcher)
        except (OSError, TimeoutError, ValueError):
            note.web = []
    return note
