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
from typing import Callable, Iterator
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
_RELATED_SEARCH_BUDGET_SECONDS = 20.0
_RELATED_SEARCH_REQUEST_TIMEOUT = 3.5
_RELATED_SEARCH_MAX_ATTEMPTS = 5
# The parent process must outlive the child by a wide margin.  The join window
# covers the spawn/interpreter start (a fresh interpreter re-imports the whole
# package) plus the macOS Foundation native fallback, which can add one extra
# request timeout on top of a native attempt.  Worst-case child work is
# 5 x 3.5s = 17.5s < the 20s budget, and the parent still waits 10s longer than
# that budget; total wall clock stays under ~35s, which is acceptable for an
# interactive button.
_RELATED_SEARCH_PROCESS_MARGIN_SECONDS = 10.0
_RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS = (
    _RELATED_SEARCH_BUDGET_SECONDS + _RELATED_SEARCH_PROCESS_MARGIN_SECONDS
)
_RELATED_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.+][A-Za-z0-9]+)*")
# Keep the platform's configured proxy.  In the desktop runtime that proxy is
# what reaches Google News; forcing a direct connection makes every request
# wait for the socket timeout before the RSS fallback gets a chance.
_RELATED_SEARCH_OPENER = build_opener()

# --- related-search query derivation -------------------------------------
# A long selection is unusable as a search query (upstream engines time out on
# it), and ASCII-only compression turned Chinese selections into a single
# meaningless token such as "AI" or "skill".  The rules below keep whole CJK
# runs and ASCII tokens, drop function words, and bound the query length.
_RELATED_QUERY_MAX_CHARS = 48
_RELATED_QUERY_MIN_CHARS = 12
_RELATED_QUERY_VERBATIM_CHARS = 60
_RELATED_QUERY_DEGRADED_MAX_CHARS = 24
_RELATED_QUERY_DEGRADE_CHARS = 200
_RELATED_QUERY_DEGRADED_KEYWORDS = 2
_RELATED_QUERY_DEGRADED_MIN_CHARS = 4
_RELATED_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
# Chinese segmentation stays deliberately conservative: only punctuation,
# whitespace and *multi-character* function words split a run.  Splitting on
# single characters would destroy compounds such as 中间/中国/手机.
_RELATED_ZH_SPLIT_STOPWORDS = frozenset(
    {
        "新闻", "消息", "发布", "今日", "昨日", "明日", "今天", "昨天", "明天",
        "报道", "记者", "编辑", "来源", "责任编辑", "更多", "点击", "阅读", "原文",
        "链接", "分享", "评论", "相关", "进行", "通过", "关于", "对于", "根据",
        "表示", "认为", "指出", "介绍", "显示", "宣布", "透露", "目前", "近日",
        "近期", "时候", "但是", "因为", "所以", "如果", "可以", "已经", "正在",
        "我们", "他们", "这个", "那个", "一个", "一些", "以及", "并且", "而且",
        "然而", "不过", "同时", "此外", "总之", "据报", "消息称", "获悉",
        "与此", "以此", "上述", "例如", "其中",
        "推荐", "值得", "一起", "看看", "了解", "如下", "以上", "以下",
    }
)
# Only true particles are trimmed off fragment edges and rejected as n-gram
# boundaries.  Word-internal characters such as 会/中/上 must stay: trimming
# them turned 英伟达在大会上 into the junk keyphrase 英伟达在大.
_RELATED_ZH_CHAR_STOPWORDS = frozenset("的了和与及在是有也就而并或等被把从对这那我你他她它之其")
_RELATED_ZH_TRIM_CHARS = _RELATED_ZH_CHAR_STOPWORDS
_RELATED_EN_STOPWORDS = frozenset(STOPWORDS) | frozenset(
    {
        "a", "an", "is", "are", "was", "were", "be", "been", "being", "of", "in",
        "on", "at", "to", "by", "as", "or", "not", "it", "its", "his", "her",
        "their", "they", "we", "you", "he", "she", "will", "would", "can",
        "could", "should", "has", "have", "had", "do", "does", "did", "more",
        "most", "than", "then", "there", "here", "what", "which", "who", "when",
        "where", "why", "how", "about", "into", "over", "after", "before",
        "during", "between", "out", "up", "down", "also", "such", "into",
    }
)
_RELATED_QUERY_KEEP_FRAGMENT_CHARS = 6
_RELATED_QUERY_GRAM_SIZES = (2, 3, 4)
_RELATED_QUERY_FREQ_BONUS = 3
_RELATED_CLAUSE_BREAK = re.compile(r"[\r\n]+|[，。！？!?；;：:、,.…]+")
_RELATED_ZH_SPLIT_SIZES = tuple(
    sorted({len(word) for word in _RELATED_ZH_SPLIT_STOPWORDS}, reverse=True)
)

# Failure taxonomy.  ``no_qualified_result`` is reported (never raised) because
# a reachable-but-empty search is a successful result for the API contract.
RELATED_SEARCH_CAUSE_TIMEOUT = "timeout"
RELATED_SEARCH_CAUSE_TRANSPORT = "transport_unavailable"
RELATED_SEARCH_CAUSE_NO_RESULT = "no_qualified_result"
RELATED_SEARCH_CAUSE_PARSE = "parse_error"
RELATED_SEARCH_CAUSES = (
    RELATED_SEARCH_CAUSE_TIMEOUT,
    RELATED_SEARCH_CAUSE_TRANSPORT,
    RELATED_SEARCH_CAUSE_NO_RESULT,
    RELATED_SEARCH_CAUSE_PARSE,
)
# The Bing hosts answer this machine in well under a second while Google News
# and DuckDuckGo hang until their socket timeout, so a working Bing is enough
# to succeed inside the child budget.  The two Bing hosts serve different
# markets: cn.bing.com returns relevant results for Chinese queries, while
# www.bing.com is the better one for ASCII queries, so the order follows the
# query script.  Four sources, four attempts: 4 x 3.5s = 14s of worst-case
# socket time stays under the 20s budget.  Google News is deliberately *not*
# listed: it stopped answering from this machine entirely (each attempt cost a
# native plus a urllib timeout) and it is what consumed the old 14s budget; it
# can be re-added here together with the attempt cap.
_RELATED_SEARCH_SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "google",
        "Google 新闻",
        "https://news.google.com/rss/search?q={encoded}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    ),
    ("bing_news", "Bing 资讯", "https://www.bing.com/news/search?format=rss&q={encoded}"),
    ("bing_cn", "Bing 中国", "https://cn.bing.com/search?format=rss&q={encoded}"),
    ("bing", "Bing", "https://www.bing.com/search?format=rss&q={encoded}"),
    ("ddg", "DuckDuckGo", "https://html.duckduckgo.com/html/?q={encoded}"),
)
_RELATED_SEARCH_SOURCE_LABELS = {name: label for name, label, _ in _RELATED_SEARCH_SOURCES}
_RELATED_SEARCH_RSS_SOURCES = frozenset({"bing", "bing_cn", "bing_news", "google"})
# Google News RSS first (no API key). Other hosts fill in if Google is empty.
_RELATED_SEARCH_CJK_ORDER = ("google", "bing_news", "bing_cn", "bing", "ddg")
_RELATED_SEARCH_ASCII_ORDER = ("google", "bing", "bing_news", "bing_cn", "ddg")


def _related_search_sources(query: str) -> tuple[tuple[str, str, str], ...]:
    """Order sources for the query script; Google News RSS always comes first."""
    order = (
        _RELATED_SEARCH_CJK_ORDER
        if _RELATED_CJK_RUN.search(query)
        else _RELATED_SEARCH_ASCII_ORDER
    )
    by_name = {name: (name, label, template) for name, label, template in _RELATED_SEARCH_SOURCES}
    ordered = [by_name[name] for name in order if name in by_name]
    # A source that is not named in the order tables still gets tried last.
    ordered.extend(source for source in _RELATED_SEARCH_SOURCES if source[0] not in set(order))
    return tuple(ordered)

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
    # Hits from either script count, so a mixed Chinese/English query can still
    # qualify a result written in only one of the two languages.  Requiring a
    # per-script hit rejected every English article for a query such as
    # "OpenAI rogue agent swarm 黑进 HuggingFace".
    total_hits = sum(token in haystack for token in english) + sum(
        token in haystack for token in chinese
    )
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
    """Extract the publisher URL from a search-engine redirect wrapper."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "bing.com" and parsed.path.lower().startswith("/news/apiclick.aspx"):
        # Bing News RSS wraps the article in ?url=<percent-encoded publisher>.
        candidate = unquote(parse_qs(parsed.query).get("url", [""])[0]).strip()
        return candidate if candidate.startswith(("http://", "https://")) else ""
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


class _RelatedRssParseError(ValueError):
    """Raised when a search source answered with something that is not a feed."""


def _parse_related_rss(payload: bytes) -> list[dict[str, str]]:
    """Parse a search RSS feed; a malformed payload is a graded parse error.

    Only related search calls this, and it must be able to tell "reachable but
    unparseable" apart from "reachable and simply empty", so the old
    silent-`[]` behaviour now raises.
    """
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise _RelatedRssParseError(type(exc).__name__) from None
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
    # The Foundation/JXA path exists because the desktop's proxy configuration
    # is what reaches Google News.  Running it for every host doubled the cost
    # of each failed attempt (a DNS error cost 6.1s instead of 3.5s) and pushed
    # the per-source attempt sum past the child budget.
    if sys.platform == "darwin" and is_google_rss:
        try:
            return _related_search_native_fetcher(url, timeout)
        except Exception:
            pass
    with _RELATED_SEARCH_OPENER.open(request, timeout=timeout) as response:
        return response.read(400_000)


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


@dataclass(frozen=True)
class RelatedQuery:
    """A bounded search query derived from the raw selection.

    ``selection`` keeps the untouched raw text so callers never lose context,
    while ``text`` is what actually goes to the search engines.  ``degraded``
    marks the explicit long-selection strategy so the API can surface it.
    """

    text: str
    selection: str
    strategy: str = "keywords"
    degraded: bool = False
    keywords: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True)
class RelatedSearchReport:
    """Result of one related-search run, graded with a failure taxonomy.

    Unpacks as ``(query, results)`` for existing callers.  ``cause_code`` is
    empty on success, ``no_qualified_result`` when the search completed but
    nothing qualified, and the hard failures are raised as
    :class:`RelatedSearchError` instead of being flattened into one code.
    """

    query: str
    results: list[dict[str, str]] = field(default_factory=list)
    selection: str = ""
    strategy: str = ""
    degraded: bool = False
    cause_code: str = ""

    def __iter__(self) -> Iterator[tuple[str, list[dict[str, str]]]]:
        """Existing callers keep unpacking ``query, results = ...``."""
        return iter((self.query, self.results))


class RelatedSearchError(RuntimeError):
    """Graded related-search failure; never carries upstream payload text."""

    def __init__(self, cause_code: str, *, detail: str = "", sources: tuple[str, ...] = ()) -> None:
        self.cause_code = cause_code
        self.detail = detail
        self.sources = tuple(sources)
        super().__init__(cause_code)

    @property
    def reason(self) -> str:
        return related_search_reason(self.cause_code, self.sources)


def related_search_reason(cause_code: str, sources: tuple[str, ...] | list[str] = ()) -> str:
    """Human-readable Chinese reason the frontend can render directly."""
    labels = "、".join(
        _RELATED_SEARCH_SOURCE_LABELS.get(name, name) for name in sources
    ) or "所有检索源"
    if cause_code == RELATED_SEARCH_CAUSE_TIMEOUT:
        return f"检索超时：约 {_RELATED_SEARCH_BUDGET_SECONDS:.0f} 秒内没有源返回结果"
    if cause_code == RELATED_SEARCH_CAUSE_TRANSPORT:
        return f"网络不可达：{labels} 都没连上"
    if cause_code == RELATED_SEARCH_CAUSE_PARSE:
        return f"检索结果解析失败：{labels} 返回的内容不是可识别的搜索结果"
    if cause_code == RELATED_SEARCH_CAUSE_NO_RESULT:
        return "未找到相关新闻：检索已完成，但没有符合条件的报道"
    return "相关新闻检索失败"


# ---------------------------------------------------------------------------
# Log redaction boundary
# ---------------------------------------------------------------------------
#
# ``redact_related_detail`` is the ONLY thing that may turn upstream failure
# text into log text.  Every value that reaches a logger (or crosses the child
# process pipe on its way to one) goes through it, so the rules below are the
# complete statement of what a server log is allowed to contain.
_RELATED_DETAIL_LIMIT = 400
# Redaction must stay cheap and total even when a detail carries a whole
# upstream body, so the scan runs over a bounded prefix.  Whatever the window
# drops can never leak: the final cap discards it anyway.
_RELATED_DETAIL_SCAN_LIMIT = 4_000
_RELATED_DETAIL_PLACEHOLDER = "***"
_RELATED_DETAIL_SUPPRESSED = "<detail suppressed>"

# Credential-bearing parameter/header names, longest first so ``access_token``
# wins over ``token`` at the same offset.
_RELATED_CREDENTIAL_NAMES = (
    "authorization",
    "client_secret",
    "access_token",
    "signature",
    "password",
    "apikey",
    "api_key",
    "bearer",
    "secret",
    "token",
    "auth",
    "code",
    "key",
    "sig",
    "pwd",
    "passwd",
)

# ``token=abc``, ``"api_key": "abc"``, ``Authorization: Bearer abc`` …  The
# separator and any auth scheme are kept so the log stays diagnosable; only the
# credential value becomes ``***``.  Boundaries are alphanumeric-only (not
# ``\b``) so prefixed names such as ``auth_token``/``x-api-key`` are still
# caught, while ``keyword``/``author``/``zipcode`` are not.
_RELATED_CREDENTIAL_PAIR = re.compile(
    r"(?i)(?<![A-Za-z0-9])(" + "|".join(_RELATED_CREDENTIAL_NAMES) + r")(?![A-Za-z0-9])"
    r"(['\"]?\s*[:=]\s*)"
    r"((?:bearer|basic|digest)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|[^\s&,;)\]}>]+)"
)

# A bare ``Bearer <credential>`` / ``Basic <credential>`` scheme run with no
# parameter name in front of it.
_RELATED_CREDENTIAL_SCHEME = re.compile(
    r"(?i)\b(bearer|basic|digest)(\s+)([^\s,;]{4,})"
)

# A 32+ character hex/base64url blob: the shape of a raw API key or session
# token.  ``/`` is excluded so ordinary URL paths are not swallowed whole.
_RELATED_OPAQUE_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-+=]{32,}(?![A-Za-z0-9_\-+=])")

# Any hyphen/underscore-joined machine run; a run that names a credential
# segment (``SECRET-UPSTREAM-BODY``) or that mixes case+digits at token length
# is treated as an opaque credential.
_RELATED_MACHINE_RUN = re.compile(r"[A-Za-z0-9_\-]{6,}")
_RELATED_CREDENTIAL_SEGMENTS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "key",
        "passwd",
        "password",
        "pwd",
        "secret",
        "sig",
        "signature",
        "token",
    }
)

# ``//user:pass@host`` — userinfo in a URL is a credential.
_RELATED_URL_USERINFO = re.compile(r"//[^/\s@]+@")

# A parameter/header name followed by its separator; such a run is a NAME, so
# it stays readable (the pair rule already replaced its value).  Matched with
# ``Pattern.match(text, offset)``, which anchors at the offset itself.
_RELATED_PARAMETER_TAIL = re.compile(r"['\"]?\s*[:=]")

# Safety net: after a redaction placeholder, a separator can only introduce the
# credential that belonged to it — ``name=<blob>=`` shapes collapse here.  A
# ``://`` after the placeholder is a redacted URL scheme, not a credential.
_RELATED_PLACEHOLDER_VALUE = re.compile(
    r"(?<=\*\*\*)(['\"]?\s*[:=]\s*)(?!//)(\"[^\"]*\"|'[^']*'|[^\s&,;)\]}>]+)"
)


def _related_machine_run(match: re.Match[str]) -> str:
    """Redact one machine-looking run when it carries credential signals."""
    run = match.group(0)
    if _RELATED_PARAMETER_TAIL.match(match.string, match.end()) and (
        run.lower() in _RELATED_CREDENTIAL_NAMES
        or (run.islower() and not any(char.isdigit() for char in run) and len(run) <= 24)
    ):
        return run
    segments = re.split(r"[-_]", run)
    if len(segments) >= 2 and any(
        segment.lower() in _RELATED_CREDENTIAL_SEGMENTS for segment in segments
    ):
        return _RELATED_DETAIL_PLACEHOLDER
    if len(run) >= 20 and any(char.isdigit() for char in run) and any(
        char.isupper() for char in run
    ):
        return _RELATED_DETAIL_PLACEHOLDER
    return run


def redact_related_detail(detail: object) -> str:
    """Return ``detail`` in the only form allowed to reach a log.

    This function IS the boundary: every related-search failure detail written
    to a server log — and every detail handed to the child process that logs
    one — must pass through it.  It is therefore total by construction: it
    accepts anything (including ``None`` and non-strings), never raises, and
    never returns more than :data:`_RELATED_DETAIL_LIMIT` characters.

    Applied in order:

    * URL userinfo (``//user:pass@host``) is dropped;
    * credential query parameters and header pairs — ``token``,
      ``access_token``, ``api_key``/``apikey``, ``key``, ``secret``,
      ``client_secret``, ``password``/``passwd``/``pwd``, ``sig``,
      ``signature``, ``auth``, ``authorization``, ``bearer``, ``code`` — lose
      their value case-insensitively, keeping the name so the line stays
      diagnosable;
    * ``Bearer``/``Basic``/``Digest`` scheme credentials are replaced;
    * bare opaque tokens (32+ hex/base64url characters) and machine runs that
      name a credential segment (``SECRET-UPSTREAM-BODY``) are replaced.

    Everything else — the exception type, the host, the surrounding diagnostic
    prose — survives so a failure can still be triaged.
    """
    try:
        text = "" if detail is None else str(detail)
    except Exception:  # a broken __str__ must never break the log call
        return _RELATED_DETAIL_SUPPRESSED
    try:
        text = text[:_RELATED_DETAIL_SCAN_LIMIT]
        text = _RELATED_URL_USERINFO.sub("//", text)
        text = _RELATED_CREDENTIAL_PAIR.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}"
                f"{match.group(3) or ''}{_RELATED_DETAIL_PLACEHOLDER}"
            ),
            text,
        )
        text = _RELATED_CREDENTIAL_SCHEME.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}{_RELATED_DETAIL_PLACEHOLDER}"
            ),
            text,
        )
        text = _RELATED_OPAQUE_TOKEN.sub(_RELATED_DETAIL_PLACEHOLDER, text)
        text = _RELATED_MACHINE_RUN.sub(_related_machine_run, text)
        text = _RELATED_PLACEHOLDER_VALUE.sub(
            lambda match: f"{match.group(1)}{_RELATED_DETAIL_PLACEHOLDER}", text
        )
    except Exception:  # defensive: redaction must not be able to break logging
        return _RELATED_DETAIL_SUPPRESSED
    return text[:_RELATED_DETAIL_LIMIT]


def _split_related_cjk_run(run: str, offset: int) -> list[tuple[str, int]]:
    """Split one CJK run on multi-character function words, keeping order."""
    pieces: list[tuple[str, int]] = []
    index = 0
    while index < len(run):
        matched = 0
        for size in _RELATED_ZH_SPLIT_SIZES:
            if index + size <= len(run) and run[index : index + size] in _RELATED_ZH_SPLIT_STOPWORDS:
                matched = size
                break
        if matched:
            index += matched
            continue
        start = index
        index += 1
        while index < len(run):
            if any(
                index + size <= len(run)
                and run[index : index + size] in _RELATED_ZH_SPLIT_STOPWORDS
                for size in _RELATED_ZH_SPLIT_SIZES
            ):
                break
            index += 1
        pieces.append((run[start:index], offset + start))
    return pieces


def _trim_related_stopword_chars(piece: str) -> str:
    """Drop leading/trailing single-character function words from a fragment."""
    start, end = 0, len(piece)
    while start < end and piece[start] in _RELATED_ZH_TRIM_CHARS:
        start += 1
    while end > start and piece[end - 1] in _RELATED_ZH_TRIM_CHARS:
        end -= 1
    return piece[start:end]


def _related_gram_usable(gram: str) -> bool:
    """Reject n-grams that only glue function words onto a real word."""
    return (
        len(gram) >= 2
        and gram[0] not in _RELATED_ZH_CHAR_STOPWORDS
        and gram[-1] not in _RELATED_ZH_CHAR_STOPWORDS
    )


def _related_query_candidates(text: str) -> list[tuple[int, str, int]]:
    """Rank ``(score, keyword, first_position)`` for a selection.

    Short CJK fragments and ASCII tokens are kept whole.  Longer fragments are
    clauses (Chinese prose separates clauses with commas, not periods), so a
    clause is only a candidate when it repeats; otherwise its 2-4 character
    sub-terms are considered.  A sub-term is admitted only when it repeats and
    its neighbours vary, which rejects windows glued onto a longer word
    ("款芯片" always preceded by 这) while keeping real terms ("芯片").
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, tuple[str, int]] = {}
    base_scores: dict[str, int] = {}
    left_neighbours: dict[str, set[str]] = {}
    right_neighbours: dict[str, set[str]] = {}
    gram_lengths: dict[str, int] = {}

    def register(key: str, keyword: str, position: int, base: int, is_gram: bool) -> None:
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, (keyword, position))
        base_scores[key] = max(base_scores.get(key, 0), base)
        if is_gram:
            gram_lengths[key] = base

    for match in _RELATED_CJK_RUN.finditer(text):
        for piece, position in _split_related_cjk_run(match.group(0), match.start()):
            trimmed = _trim_related_stopword_chars(piece)
            if len(trimmed) < 2:
                continue
            if len(trimmed) <= _RELATED_QUERY_KEEP_FRAGMENT_CHARS:
                register(trimmed, trimmed, position, len(trimmed), False)
                continue
            # A clause is never a query term; only its sub-terms compete.
            for size in _RELATED_QUERY_GRAM_SIZES:
                for offset in range(len(trimmed) - size + 1):
                    gram = trimmed[offset : offset + size]
                    if not _related_gram_usable(gram):
                        continue
                    left = trimmed[offset - 1] if offset else ""
                    after = offset + size
                    right = trimmed[after] if after < len(trimmed) else ""
                    left_neighbours.setdefault(gram, set()).add(left)
                    right_neighbours.setdefault(gram, set()).add(right)
                    register(gram, gram, position + offset, len(gram), True)
    for match in _RELATED_ASCII_TOKEN.finditer(text):
        token = match.group(0)
        if token.casefold() in _RELATED_EN_STOPWORDS:
            continue
        register(token.casefold(), token, match.start(), min(len(token), 12), False)

    candidates: list[tuple[int, str, int]] = []
    for key, (keyword, position) in first_seen.items():
        if key in gram_lengths:
            # A real term repeats *and* sits between varying characters; a
            # window glued onto a longer word keeps one constant neighbour.
            varied = (
                len(left_neighbours.get(key, ())) >= 2
                and len(right_neighbours.get(key, ())) >= 2
            )
            if counts[key] < 2 or not varied:
                continue
        score = base_scores[key] + _RELATED_QUERY_FREQ_BONUS * min(counts[key] - 1, 3)
        candidates.append((score, keyword, position))
    candidates.sort(key=lambda item: (-item[0], item[2]))
    return candidates


def _select_related_keywords(text: str, max_chars: int, limit: int | None = None) -> list[str]:
    """Best keywords that fit ``max_chars``, returned in original text order."""
    chosen: list[tuple[int, str]] = []
    used = 0
    for _score, keyword, position in _related_query_candidates(text):
        if limit is not None and len(chosen) >= limit:
            break
        cost = len(keyword) if not chosen else len(keyword) + 1
        if used + cost > max_chars:
            continue
        chosen.append((position, keyword))
        used += cost
    chosen.sort(key=lambda item: item[0])
    return [keyword for _position, keyword in chosen]


def _compress_related_query(query: str, limit: int | None = None) -> str:
    """Keep the meaningful CJK runs and ASCII tokens of a selection.

    This is the CJK-aware replacement for the old ASCII-only compressor: a
    Chinese selection keeps its words instead of collapsing to ``AI``/``skill``.
    """
    return " ".join(_select_related_keywords(query, _RELATED_QUERY_MAX_CHARS, limit))


def _related_degraded_query(text: str, cap: int) -> str:
    """Explicit long-selection degradation: keep the leading (topic) clause.

    A paragraph cannot be searched verbatim, so it is cut down to its first
    clause, which is where Chinese/English prose states its subject.  The cut
    is reported through ``RelatedQuery.degraded``/``strategy`` and through the
    related-search API payload.
    """
    head = _RELATED_CLAUSE_BREAK.split(text.strip(), maxsplit=1)[0].strip()
    if len(head) < 2:
        head = text.strip()
    if len(head) <= cap:
        return head
    cut = head[:cap]
    if re.search(r"[A-Za-z0-9]$", cut) and re.match(r"[A-Za-z0-9]", head[cap : cap + 1]):
        space = cut.rfind(" ")
        if space > 0:
            cut = cut[:space]
    return cut.strip()


def derive_related_query(selection: str) -> RelatedQuery:
    """Turn a raw selection into a bounded, engine-friendly search query.

    * punctuation/whitespace and function words are dropped;
    * the query is capped at 48 characters (24 for degraded input);
    * selections longer than 200 characters use an explicit degradation
      strategy (the leading clause, i.e. where prose states its subject) so the
      engine sees a real query instead of a paragraph, and the
      ``degraded``/``strategy`` fields make that visible to the caller and to
      the related-search API response.

    The raw selection is preserved on the result for context.
    """
    raw = normalize_text(selection)
    if not raw:
        return RelatedQuery(text="", selection="", strategy="empty", degraded=False)
    if len(raw) > _RELATED_QUERY_DEGRADE_CHARS:
        cap = _RELATED_QUERY_DEGRADED_MAX_CHARS
        prefix = _related_degraded_query(raw, cap)
        strategy = "degraded_leading_clause"
        degraded = True
        if len(prefix) >= _RELATED_QUERY_DEGRADED_MIN_CHARS:
            keywords = (prefix,)
        else:
            keywords = tuple(
                _select_related_keywords(raw, cap, _RELATED_QUERY_DEGRADED_KEYWORDS)
            )
    else:
        keywords = _select_related_keywords(raw, _RELATED_QUERY_MAX_CHARS)
        strategy = "keywords" if keywords else "verbatim"
        degraded = False
        cap = _RELATED_QUERY_MAX_CHARS
        if len(raw) > _RELATED_QUERY_VERBATIM_CHARS and len(" ".join(keywords)) < _RELATED_QUERY_MIN_CHARS:
            # Prose whose keywords are too thin to search with: the leading
            # clause still names the subject, which beats a one-word query.
            prefix = _related_degraded_query(raw, cap)
            if len(prefix) >= _RELATED_QUERY_DEGRADED_MIN_CHARS:
                keywords = (prefix,)
                strategy = "leading_clause"
                degraded = True
    text = " ".join(keywords)[:cap]
    if not text:
        text = (_clean_related_query(raw) or raw)[:cap]
    return RelatedQuery(
        text=text,
        selection=raw,
        strategy=strategy,
        degraded=degraded,
        keywords=tuple(keywords),
    )


def _related_payload_loaded(payload: object) -> bool:
    """True when a fetch returned actual bytes rather than an empty body."""
    try:
        return bool(payload and payload.strip())
    except Exception:
        return bool(payload)


def _search_related_news_impl(
    selection: str, fetcher: Fetcher | None = None, limit: int = 8
) -> RelatedSearchReport:
    """Search the derived query and return the first qualified article.

    Every source is graded: transport failures, unparseable payloads, an
    exhausted budget and a reachable-but-empty search stay distinguishable
    instead of collapsing into a single opaque failure.
    """
    derived = derive_related_query(selection)
    if not derived.text:
        return RelatedSearchReport(query="", selection="")
    load = fetcher or _related_search_fetcher

    deadline = monotonic() + _RELATED_SEARCH_BUDGET_SECONDS
    attempts_used = 0
    attempted: list[str] = []
    reachable: list[str] = []
    parsed_sources: list[str] = []
    seen: set[str] = set()
    qualified: list[dict[str, str]] = []
    timed_out = False
    transport_detail = ""
    parse_detail = ""

    for name, _label, template in _related_search_sources(derived.text):
        if len(qualified) >= limit:
            break
        if attempts_used >= _RELATED_SEARCH_MAX_ATTEMPTS:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            timed_out = True
            break
        attempts_used += 1
        attempted.append(name)
        parser = _parse_related_rss if name in _RELATED_SEARCH_RSS_SOURCES else parse_search_html
        url = template.format(encoded=quote_plus(derived.text))
        try:
            if load is _related_search_fetcher:
                payload = load(url, timeout=min(_RELATED_SEARCH_REQUEST_TIMEOUT, remaining))
            else:
                payload = load(url)
        except Exception as exc:
            # Detail stays server-side; upstream payloads never reach the API.
            transport_detail = f"{name}: {type(exc).__name__}: {exc}"
            continue
        reachable.append(name)
        if not _related_payload_loaded(payload):
            parse_detail = f"{name}: empty response"
            continue
        try:
            parsed = parser(payload)
        except Exception as exc:
            parse_detail = f"{name}: {type(exc).__name__}: {exc}"
            continue
        if parsed or name in _RELATED_SEARCH_RSS_SOURCES:
            # A well-formed feed with no items is a successful parse; an HTML
            # page without a single result row is indistinguishable from a
            # block/captcha page and must not mask a parse failure.
            parsed_sources.append(name)
        else:
            parse_detail = f"{name}: no parsable result markup"
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
            if canonical in seen or not _related_result_allowed(result, derived.text):
                continue
            seen.add(canonical)
            qualified.append(result)
            if len(qualified) >= limit:
                break

    report = RelatedSearchReport(
        query=derived.text,
        results=qualified[:limit],
        selection=derived.selection,
        strategy=derived.strategy,
        degraded=derived.degraded,
        cause_code="" if qualified else RELATED_SEARCH_CAUSE_NO_RESULT,
    )
    if qualified:
        return report
    if timed_out:
        raise RelatedSearchError(
            RELATED_SEARCH_CAUSE_TIMEOUT,
            detail=f"deadline reached after {attempts_used} attempt(s)",
            sources=tuple(attempted),
        )
    if not reachable:
        raise RelatedSearchError(
            RELATED_SEARCH_CAUSE_TRANSPORT,
            detail=transport_detail or "no source could be reached",
            sources=tuple(attempted),
        )
    if not parsed_sources:
        raise RelatedSearchError(
            RELATED_SEARCH_CAUSE_PARSE,
            detail=parse_detail or "no payload could be parsed",
            sources=tuple(reachable),
        )
    return report


def _related_search_worker(send_conn, selection: str, limit: int) -> None:
    try:
        report = _search_related_news_impl(selection, limit=limit)
        send_conn.send(("ok", _related_report_payload(report)))
    except RelatedSearchError as exc:
        send_conn.send(
            (
                "error",
                {
                    "cause_code": exc.cause_code,
                    "detail": redact_related_detail(exc.detail),
                    "sources": list(exc.sources),
                },
            )
        )
    except Exception as exc:  # keep the closed taxonomy even for a bug
        send_conn.send(
            (
                "error",
                {
                    "cause_code": RELATED_SEARCH_CAUSE_TRANSPORT,
                    "detail": redact_related_detail(f"{type(exc).__name__}: {exc}"),
                    "sources": [],
                },
            )
        )
    finally:
        send_conn.close()


def _related_report_payload(report: RelatedSearchReport) -> dict[str, object]:
    """Picklable form of a report; the parent rebuilds it across the pipe."""
    return {
        "query": report.query,
        "results": report.results,
        "selection": report.selection,
        "strategy": report.strategy,
        "degraded": report.degraded,
        "cause_code": report.cause_code,
    }


def _search_related_news_bounded(
    selection: str, limit: int = 8
) -> RelatedSearchReport:
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
            raise RelatedSearchError(
                RELATED_SEARCH_CAUSE_TIMEOUT,
                detail=(
                    f"child exceeded the {_RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS:.0f}s "
                    f"process timeout (child budget {_RELATED_SEARCH_BUDGET_SECONDS:.0f}s)"
                ),
                sources=tuple(name for name, _label, _template in _RELATED_SEARCH_SOURCES),
            )
        if not receive_conn.poll(0.5):
            raise RelatedSearchError(
                RELATED_SEARCH_CAUSE_TRANSPORT,
                detail="search process exited without returning a result",
            )
        status, payload = receive_conn.recv()
    finally:
        receive_conn.close()
    if status != "ok":
        if isinstance(payload, dict):
            raise RelatedSearchError(
                str(payload.get("cause_code") or RELATED_SEARCH_CAUSE_TRANSPORT),
                detail=str(payload.get("detail") or ""),
                sources=tuple(str(name) for name in payload.get("sources") or ()),
            )
        raise RelatedSearchError(
            RELATED_SEARCH_CAUSE_TRANSPORT, detail="search process reported a failure"
        )
    if not isinstance(payload, dict):
        raise RelatedSearchError(
            RELATED_SEARCH_CAUSE_TRANSPORT, detail="search process returned an unknown payload"
        )
    return RelatedSearchReport(
        query=str(payload.get("query") or ""),
        results=list(payload.get("results") or []),
        selection=str(payload.get("selection") or ""),
        strategy=str(payload.get("strategy") or ""),
        degraded=bool(payload.get("degraded")),
        cause_code=str(payload.get("cause_code") or ""),
    )


def search_related_news(
    selection: str, fetcher: Fetcher | None = None, limit: int = 8
) -> RelatedSearchReport:
    """Search news related to a selection.

    Returns a :class:`RelatedSearchReport` (unpacks as ``(query, results)``).
    Hard failures raise :class:`RelatedSearchError` carrying one of
    ``timeout``/``transport_unavailable``/``parse_error``; a reachable search
    that qualified nothing is a successful report with
    ``cause_code == "no_qualified_result"``.
    """
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
