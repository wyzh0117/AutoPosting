"""Gather recent background for a hotspot before drafting.

Uses the local SQLite window plus a DuckDuckGo HTML search. Failures stay
empty notes so drafting can still run on the original item.
"""

from __future__ import annotations

import base64
import binascii
import json
from multiprocessing import get_all_start_methods, get_context
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from time import monotonic
from typing import Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

from .db import Database
from .images import BROWSER_UA
from .linked_works import fetch_linked_works
from .normalize import canonicalize_url, normalize_text

Fetcher = Callable[[str], bytes]
WEB_RESULT_LIMIT = 5
LOCAL_RESULT_LIMIT = 5
PAGE_TEXT_LIMIT = 1800
ORIGINAL_TEXT_LIMIT = 50_000
RELATED_SELECTION_LIMIT = 2_000
RELATED_CONTEXT_LIMIT = 12_000
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
_RELATED_BLOCKED_HOSTS = {
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "tiktok.com",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "reddit.com",
    "github.com", "gitlab.com", "wikipedia.org", "zh.wikipedia.org",
    "news.ycombinator.com", "stackoverflow.com", "stackexchange.com", "quora.com",
    "discord.com", "t.me", "telegram.me", "zhihu.com", "bilibili.com", "douyin.com",
}
_RELATED_BLOCKED_SEARCH_HOSTS = {"google.com", "bing.com", "duckduckgo.com", "news.google.com"}
_RELATED_TRAILING = re.compile(r"[\s\"'‘’“”「」『』（）()【】\[\]《》。，！？!?；;：:,、]+$")
_RELATED_LEADING = re.compile(r"^[\s\"'‘’“”「」『』（）()【】\[\]《》]+")
_RELATED_TRAILING_PUNCT = set("。！？!?；;：:,、，.")
_RELATED_SEARCH_BUDGET_SECONDS = 14.0
_RELATED_SEARCH_REQUEST_TIMEOUT = 4.0
_RELATED_SEARCH_MAX_ATTEMPTS = 4
_RELATED_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.+][A-Za-z0-9]+)*")
# Keep the platform's configured proxy.  In the desktop runtime that proxy is
# what reaches Google News; forcing a direct connection makes every request
# wait for the socket timeout before the RSS fallback gets a chance.
_RELATED_SEARCH_OPENER = build_opener()
_RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS = _RELATED_SEARCH_BUDGET_SECONDS

# Keep the URL out of executable code and pass it as an argv item below.  The
# desktop's Foundation session can use the same network configuration as the
# browser when urllib's proxy/socket path is unavailable.
_MACOS_NATIVE_FETCH_SCRIPT = r'''ObjC.import("Foundation");
var NSClass = $.NSClassFromString;
var args = $.NSProcessInfo.processInfo.arguments;
var url = ObjC.unwrap(args.lastObject);
var request = NSClass("NSMutableURLRequest").alloc.initWithURL($.NSURL.URLWithString(url));
request.setTimeoutInterval(5);
request.setValueForHTTPHeaderField("Mozilla/5.0", "User-Agent");
request.setValueForHTTPHeaderField("application/rss+xml,application/xml;q=0.9,*/*;q=0.8", "Accept");
var done = false, body = "", errorMessage = "";
var completion = ObjC.block("void, NSData*, NSURLResponse*, NSError*", function(data, response, error) {
    if (data && data.length) {
        var bytes = $.NSData.dataWithData(data);
        var string = $.NSString.alloc.initWithDataEncoding(bytes, $.NSUTF8StringEncoding);
        if (string) body = ObjC.unwrap(string);
    }
    if (!body && error && error.code) errorMessage = ObjC.unwrap(error.localizedDescription);
    done = true;
});
var task = NSClass("NSURLSession").sharedSession.dataTaskWithRequestCompletionHandler(request, completion);
task.resume;
var started = Date.now();
while (!done && Date.now() - started < 5500) {
    $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
}
if (!done) { task.cancel; throw new Error("timeout"); }
if (errorMessage) throw new Error(errorMessage);
if (!body) throw new Error("empty response");
console.log(body);'''


def _javascript_offset(text: str, offset: int) -> int:
    """Convert a browser UTF-16 selection offset to a Python string offset."""
    units = 0
    for index, char in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > offset:
            raise ValueError("selection offset splits a Unicode character")
    if units == offset:
        return len(text)
    raise ValueError("selection offset is outside draft body")


def related_article_context(body: str, selection: str, start: int, end: int) -> str:
    """Return all selected paragraphs and at most one paragraph on either side."""
    if not isinstance(body, str) or not isinstance(selection, str):
        raise ValueError("body and selection must be strings")
    if not selection.strip() or len(selection) > RELATED_SELECTION_LIMIT:
        raise ValueError(f"selection must be 1-{RELATED_SELECTION_LIMIT} characters")
    if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
        raise ValueError("selection_start and selection_end must be integers")
    if start < 0 or end <= start:
        raise ValueError("selection does not match draft body")
    start = _javascript_offset(body, start)
    end = _javascript_offset(body, end)
    if end <= start or end > len(body):
        raise ValueError("selection does not match draft body")
    raw_selection = body[start:end]
    if raw_selection != selection:
        if raw_selection.strip() != selection:
            raise ValueError("selection does not match draft body")
        start += len(raw_selection) - len(raw_selection.lstrip())
        end = start + len(selection)

    spans: list[tuple[int, int]] = []
    cursor = 0
    for boundary in re.finditer(r"\r?\n[ \t]*\r?\n", body):
        if body[cursor : boundary.start()].strip():
            spans.append((cursor, boundary.start()))
        cursor = boundary.end()
    if body[cursor:].strip():
        spans.append((cursor, len(body)))
    selected_indexes = [
        index
        for index, (paragraph_start, paragraph_end) in enumerate(spans)
        if start < paragraph_end and end > paragraph_start
    ]
    if not selected_indexes:
        raise ValueError("selection is outside a paragraph")
    first_index, last_index = selected_indexes[0], selected_indexes[-1]
    selected_start, selected_end = spans[first_index][0], spans[last_index][1]
    selected = body[selected_start:selected_end].strip()
    left_neighbor = body[spans[first_index - 1][0] : spans[first_index - 1][1]].strip() if first_index else ""
    right_neighbor = (
        body[spans[last_index + 1][0] : spans[last_index + 1][1]].strip()
        if last_index + 1 < len(spans)
        else ""
    )
    context = "\n\n".join(piece for piece in (left_neighbor, selected, right_neighbor) if piece)
    if len(context) <= RELATED_CONTEXT_LIMIT:
        return context
    # Preserve the complete selected block before trimming optional neighbors.
    if len(selected) >= RELATED_CONTEXT_LIMIT:
        # The exact selection is bounded above, so retaining it is safer than
        # slicing away part of a cross-paragraph selection.
        return selection[:RELATED_CONTEXT_LIMIT]
    neighbors = [piece for piece in (left_neighbor, right_neighbor) if piece]
    remaining = max(0, RELATED_CONTEXT_LIMIT - len(selected) - 4 * len(neighbors))
    left_piece = right_piece = ""
    if left_neighbor and right_neighbor:
        share = remaining // 2
        left_piece, right_piece = left_neighbor[:share], right_neighbor[:remaining - share]
    elif left_neighbor:
        left_piece = left_neighbor[:remaining]
    elif right_neighbor:
        right_piece = right_neighbor[:remaining]
    pieces = []
    if left_piece:
        pieces.append(left_piece)
    pieces.append(selected)
    if right_piece:
        pieces.append(right_piece)
    return "\n\n".join(piece for piece in pieces if piece).strip()[:RELATED_CONTEXT_LIMIT]


@dataclass
class BackgroundNote:
    focus: str
    query: str
    local: list[dict[str, str]] = field(default_factory=list)
    web: list[dict[str, str]] = field(default_factory=list)
    source_excerpt: str = ""
    linked_works: list[dict[str, str]] = field(default_factory=list)

    def as_prompt(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "focus": self.focus,
            "query": self.query,
            "source_excerpt": self.source_excerpt,
            "local_related": self.local,
            "web_related": self.web,
        }
        if self.linked_works:
            payload["linked_works"] = self.linked_works
        return payload


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
            if href.startswith("/") and "uddg=" in href:
                href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
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


class _ArticleTextParser(HTMLParser):
    _BLOCKS = {"p", "h1", "h2", "h3", "li", "blockquote"}
    _SKIP = {"script", "style", "nav", "header", "footer", "aside", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_tags: list[str] = []
        self.focus_depth = 0
        self.block_depth = 0
        self.chunk: list[str] = []
        self.blocks: list[tuple[bool, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if self.skip_tags:
            if tag in self._SKIP:
                self.skip_tags.append(tag)
            return
        if tag in self._SKIP:
            self.skip_tags.append(tag)
            return
        if tag in {"article", "main"}:
            self.focus_depth += 1
        if tag in self._BLOCKS:
            if self.block_depth == 0:
                self.chunk = []
            self.block_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tags:
            if tag == self.skip_tags[-1]:
                self.skip_tags.pop()
            return
        if tag in self._BLOCKS and self.block_depth:
            self.block_depth -= 1
            if self.block_depth == 0:
                text = " ".join(" ".join(self.chunk).split()).strip()
                if text:
                    self.blocks.append((self.focus_depth > 0, text))
                self.chunk = []
        if tag in {"article", "main"} and self.focus_depth:
            self.focus_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip_tags and self.block_depth:
            self.chunk.append(data)

    def text(self) -> str:
        focused = [text for in_focus, text in self.blocks if in_focus]
        chosen = focused if sum(map(len, focused)) >= 200 else [text for _, text in self.blocks]
        return "\n\n".join(dict.fromkeys(chosen))[:ORIGINAL_TEXT_LIMIT]


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


def _related_query_tokens(query: str) -> tuple[set[str], set[str]]:
    english = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query)
        if token.casefold() not in STOPWORDS
    }
    chinese: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]{2,}", query):
        chinese.update(run[index : index + 2] for index in range(len(run) - 1))
    return english, chinese


def _related_result_relevant(result: dict[str, str], query: str) -> bool:
    english, chinese = _related_query_tokens(query)
    if not english and not chinese:
        return True
    haystack = normalize_text(
        f"{result.get('title') or ''} {result.get('snippet') or ''}"
    ).casefold()
    english_hits = sum(token in haystack for token in english)
    chinese_hits = sum(token in haystack for token in chinese)
    total_hits = english_hits + chinese_hits
    if english and len(english) >= 3 and english_hits < 2:
        return False
    if chinese and len(chinese) >= 4 and chinese_hits < 2:
        return False
    if english and chinese and total_hits < 2:
        return False
    return total_hits >= (2 if len(english) >= 3 or len(chinese) >= 4 else 1)


def _related_result_allowed(result: dict[str, str], query: str) -> bool:
    url = str(result.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if any(host == blocked or host.endswith("." + blocked) for blocked in _RELATED_BLOCKED_HOSTS):
        return False
    if any(host == blocked or host.endswith("." + blocked) for blocked in _RELATED_BLOCKED_SEARCH_HOSTS):
        return False
    path = (parsed.path or "").lower()
    if any(part in path for part in ("/search", "/tag/", "/category/", "/video", "/watch")):
        return False
    if path in {"", "/"}:
        return False
    return _related_result_relevant(result, query)


def _unwrap_related_url(value: str) -> str:
    """Extract the publisher URL from a Google News RSS article token."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "news.google.com" or not parsed.path.startswith("/rss/articles/"):
        return value.strip()
    params = parse_qs(parsed.query)
    for key in ("url", "u"):
        candidate = unquote(params.get(key, [""])[0]).strip()
        if candidate.startswith(("http://", "https://")):
            return candidate
    token = parsed.path.rsplit("/", 1)[-1]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, binascii.Error):
        return ""
    match = re.search(rb"https?://[^\x00-\x20\"'<>]+", decoded)
    return match.group(0).decode("utf-8", errors="ignore") if match else ""


def _parse_related_rss(payload: bytes) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, UnicodeError):
        return []
    results: list[dict[str, str]] = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1].lower() != "item":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1].lower(): normalize_text(child.text or "")
            for child in entry
        }
        title, url = fields.get("title", ""), fields.get("link", "")
        if title and url:
            results.append({"title": title, "url": url, "snippet": fields.get("description", "")[:280]})
    return results


def _clean_related_query(query: str) -> str:
    cleaned = normalize_text(query)
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = _RELATED_LEADING.sub("", cleaned)
        cleaned = _RELATED_TRAILING.sub("", cleaned).strip()
    return cleaned


def _related_only_trailing_cleanup(query: str, cleaned: str) -> bool:
    suffix = query[len(cleaned) :] if query.startswith(cleaned) else ""
    return bool(suffix) and all(char in _RELATED_TRAILING_PUNCT for char in suffix)


def _related_search_fetcher(url: str, timeout: float = _RELATED_SEARCH_REQUEST_TIMEOUT) -> bytes:
    request = Request(
        url,
        headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
    )
    parsed = urlsplit(url)
    is_google_rss = (
        (parsed.hostname or "").lower().removeprefix("www.") == "news.google.com"
        and parsed.path.startswith("/rss/")
    )
    native_attempted = False
    if sys.platform == "darwin" and is_google_rss:
        native_attempted = True
        try:
            return _related_search_native_fetcher(url, timeout)
        except Exception:
            pass
    try:
        # An explicit empty proxy map avoids urllib's macOS system-proxy lookup,
        # which can block before the request timeout starts.
        with _RELATED_SEARCH_OPENER.open(request, timeout=timeout) as response:
            return response.read(400_000)
    except Exception:
        if sys.platform != "darwin" or native_attempted:
            raise
        return _related_search_native_fetcher(url, timeout)


def _related_search_native_fetcher(url: str, timeout: float = _RELATED_SEARCH_REQUEST_TIMEOUT) -> bytes:
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _MACOS_NATIVE_FETCH_SCRIPT, "--", url],
            check=True,
            capture_output=True,
            shell=False,
            timeout=min(max(float(timeout), 0.1), 6.0),
        )
    except (OSError, subprocess.SubprocessError) as native_error:
        raise OSError("native related search unavailable") from native_error
    if not completed.stdout:
        raise OSError("native related search returned empty response")
    return completed.stdout


def _compress_related_query(query: str) -> str:
    """Keep only ASCII names/tokens present in the user's selection."""
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _RELATED_ASCII_TOKEN.finditer(query):
        token = match.group(0)
        key = token.casefold()
        if key not in seen:
            seen.add(key)
            tokens.append(token)
    return " ".join(tokens)[:160]


def _search_related_news_impl(
    selection: str, fetcher: Fetcher | None = None, limit: int = 1
) -> tuple[str, list[dict[str, str]]]:
    """Search the selected text directly and return the first qualified article."""
    query = normalize_text(selection)
    if not query:
        return "", []
    load = fetcher or _related_search_fetcher

    deadline = monotonic() + _RELATED_SEARCH_BUDGET_SECONDS
    attempts_used = 0

    attempts_by_name = {
        "google": ("https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en", _parse_related_rss),
        "bing": ("https://www.bing.com/search?format=rss&q={encoded}", _parse_related_rss),
        "ddg": ("https://html.duckduckgo.com/html/?q={encoded}", parse_search_html),
    }

    def search_once(current: str, sources: tuple[str, ...]) -> tuple[list[dict[str, str]], bool]:
        nonlocal attempts_used
        encoded = quote_plus(current)
        attempts = [(template.format(encoded=encoded), parser) for name in sources for template, parser in (attempts_by_name[name],)]
        seen: set[str] = set()
        qualified: list[dict[str, str]] = []
        fetched_any = False
        for url, parser in attempts:
            if attempts_used >= _RELATED_SEARCH_MAX_ATTEMPTS:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            attempts_used += 1
            try:
                if load is _related_search_fetcher:
                    payload = load(url, timeout=min(_RELATED_SEARCH_REQUEST_TIMEOUT, remaining))
                else:
                    payload = load(url)
                fetched_any = True
                parsed = parser(payload)
            except Exception:
                # Keep transport/parser details out of the API response.
                continue
            for result in parsed:
                try:
                    publisher_url = _unwrap_related_url(str(result.get("url") or ""))
                    if not publisher_url:
                        continue
                    canonical = canonicalize_url(publisher_url)
                except (TypeError, ValueError):
                    continue
                result = {
                    "title": normalize_text(str(result.get("title") or "")),
                    "url": canonical,
                    "snippet": normalize_text(str(result.get("snippet") or ""))[:280],
                }
                if canonical in seen or not _related_result_allowed(result, current):
                    continue
                seen.add(canonical)
                qualified.append(result)
                if len(qualified) >= limit:
                    return qualified, fetched_any
        if not fetched_any:
            raise OSError("related search unavailable")
        return qualified, fetched_any

    try:
        results, _ = search_once(query, ("google",))
    except OSError:
        # A transport failure should still leave room for the deterministic
        # compression and the non-Google fallbacks within the outer budget.
        results = []
    compressed = _compress_related_query(query)
    if not results and compressed and compressed != query:
        try:
            compressed_results, _ = search_once(compressed, ("google",))
        except OSError:
            compressed_results = []
        query = compressed
        results = compressed_results
    if not results:
        fallback_results, _ = search_once(query, ("bing", "ddg"))
        results = fallback_results
    cleaned = _clean_related_query(query)
    if not results and cleaned and cleaned != query and not _related_only_trailing_cleanup(query, cleaned):
        query = cleaned
        results, _ = search_once(cleaned, ("google", "bing", "ddg"))
    return query, results


def _related_search_worker(send_conn, selection: str, limit: int) -> None:
    try:
        send_conn.send(("ok", _search_related_news_impl(selection, limit=limit)))
    except Exception:
        send_conn.send(("error", "related search unavailable"))
    finally:
        send_conn.close()


def _search_related_news_bounded(
    selection: str, limit: int = 1
) -> tuple[str, list[dict[str, str]]]:
    """Run default network search in a process that can be terminated."""
    start_methods = get_all_start_methods()
    # The caller is the threaded local web server.  Forking from a live
    # request thread inherits locks and native networking state (notably the
    # macOS Foundation/JXA path), which can consume the entire search budget.
    # A fresh interpreter is safe; retain fork only for platforms without it.
    if "spawn" in start_methods:
        context = get_context("spawn")
    elif "fork" in start_methods:
        context = get_context("fork")
    else:
        context = get_context()
    receive_conn, send_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_related_search_worker, args=(send_conn, selection, limit), daemon=True
    )
    process.start()
    send_conn.close()
    try:
        process.join(_RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join()
            raise TimeoutError("related search timed out")
        if not receive_conn.poll():
            raise OSError("related search unavailable")
        status, payload = receive_conn.recv()
    finally:
        receive_conn.close()
    if status != "ok":
        raise OSError("related search unavailable")
    return payload


def search_related_news(
    selection: str, fetcher: Fetcher | None = None, limit: int = 1
) -> tuple[str, list[dict[str, str]]]:
    # Injected fetchers stay in-process for tests and the local workbench path.
    if fetcher is not None:
        return _search_related_news_impl(selection, fetcher=fetcher, limit=limit)
    return _search_related_news_bounded(selection, limit=limit)


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
    return article_page_text(payload)[:PAGE_TEXT_LIMIT]


def article_page_text(payload: bytes) -> str:
    parser = _ArticleTextParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    parser.close()
    return parser.text()


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
        try:
            note.linked_works = fetch_linked_works(f"{title}\n{summary}", fetcher=fetcher)
        except (OSError, TimeoutError, ValueError):
            note.linked_works = []
    return note
