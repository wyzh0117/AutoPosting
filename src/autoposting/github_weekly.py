"""GitHub AI weekly ranking: fetch and normalize repo facts for the poster pipeline."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Callable

from .diagnostics import log_diagnostic, make_diagnostic

XHS_TITLE = "本周值得关注的github ai项目前10"
TRENDING_URL = "https://github.com/trending?since=weekly"
SEARCH_URL = "https://api.github.com/search/repositories"
AI_QUERY = (
    "(topic:ai OR topic:llm OR topic:openai OR topic:gpt OR topic:machine-learning "
    "OR topic:generative-ai OR topic:deep-learning OR topic:mcp OR topic:rag)"
)
AI_HINT = re.compile(
    r"\b(ai|a\.i\.|llm|gpt|openai|claude|gemini|agent|rag|transformer|diffusion|"
    r"whisper|langchain|ollama|inference|embedding|chatbot|copilot|mcp|"
    r"machine[- ]learning|deep[- ]learning|generative)\b",
    re.I,
)
ARTICLE_RE = re.compile(r"<article class=\"Box-row\"[\s\S]*?</article>", re.I)
HREF_RE = re.compile(
    r'href="/(?P<owner>(?!login|trending|topics|apps|orgs|sponsors|settings|features|about|pricing|explore)[^/]+)/(?P<repo>[A-Za-z0-9_.-]+)"'
)
DESC_RE = re.compile(
    r"<p class=\"col-9[^\"]*\"[^>]*>\s*(?P<desc>[\s\S]*?)\s*</p>",
    re.I,
)
_GITHUB_NOW: datetime | None = None
LANG_RE = re.compile(r'itemprop="programmingLanguage">\s*([^<]+)', re.I)
WEEK_STARS_RE = re.compile(r"([\d,]+)\s+stars this week", re.I)
TOTAL_STARS_RE = re.compile(r'href="[^"]+/stargazers"[^>]*>\s*([\d,]+)', re.I)
TAG_RE = re.compile(r"<[^>]+>")
KIND_RULES = (
    ("mcp", "MCP"),
    ("rag", "RAG"),
    ("webui", "WebUI"),
    ("vscode", "插件"),
    ("dataset", "数据集"),
    ("sdk", "SDK"),
    ("cli", "CLI"),
    ("agent", "Agent"),
    ("framework", "框架"),
    ("model", "模型"),
)

Getter = Callable[[str], object]
LOGGER = logging.getLogger(__name__)


def week_window_start(now: datetime | None = None) -> str:
    moment = now or _GITHUB_NOW or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) - timedelta(days=7)).date().isoformat()


def iso_week_issue(now: datetime | None = None) -> str:
    moment = now or _GITHUB_NOW or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    iso = moment.astimezone(timezone.utc).isocalendar()
    return f"{iso.year}年第{iso.week}周"


def format_stars(count: int) -> str:
    if count >= 1_000_000:
        value = count / 1_000_000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}m"
    if count >= 1000:
        value = count / 1000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(count)


def infer_kind(language: str, topics: list[str] | tuple[str, ...], text: str = "") -> str:
    blob = " ".join(topics) + " " + text
    lowered = blob.lower()
    for token, label in KIND_RULES:
        if token in lowered:
            return label
    return language.strip() or "AI 工具"


def _strip_html(text: str) -> str:
    return unescape(TAG_RE.sub("", text or "")).replace("\n", " ").strip()


def _parse_int(text: str) -> int:
    digits = (text or "").replace(",", "").strip()
    return int(digits) if digits.isdigit() else 0


def parse_trending_html(html: str) -> list[dict[str, object]]:
    repos: list[dict[str, object]] = []
    seen: set[str] = set()
    for block in ARTICLE_RE.findall(html or ""):
        href = None
        for candidate in HREF_RE.finditer(block):
            repo_name = candidate.group("repo")
            if repo_name in {"stargazers", "forks", "issues", "pulls", "actions"}:
                continue
            href = candidate
            break
        if href is None:
            continue
        owner = href.group("owner")
        name = href.group("repo")
        full_name = f"{owner}/{name}"
        if full_name in seen:
            continue
        desc_match = DESC_RE.search(block)
        description = _strip_html(desc_match.group("desc") if desc_match else "")
        haystack = f"{full_name} {description}"
        if AI_HINT.search(haystack) is None:
            continue
        language_match = LANG_RE.search(block)
        language = (language_match.group(1).strip() if language_match else "")
        week_match = WEEK_STARS_RE.search(block)
        total_match = TOTAL_STARS_RE.search(block)
        stars_week = _parse_int(week_match.group(1) if week_match else "0")
        stars = _parse_int(total_match.group(1) if total_match else "0") or stars_week
        seen.add(full_name)
        repos.append(
            _repo_record(
                full_name=full_name,
                description=description,
                stars=stars,
                stars_week=stars_week,
                language=language,
                topics=(),
            )
        )
    repos.sort(key=lambda item: (int(item["stars_week"]), int(item["stars"])), reverse=True)
    return repos


def parse_github_search(payload: dict[str, object]) -> list[dict[str, object]]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    repos: list[dict[str, object]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        full_name = str(entry.get("full_name") or "").strip()
        if "/" not in full_name:
            continue
        topics_raw = entry.get("topics") if isinstance(entry.get("topics"), list) else []
        topics = [str(topic) for topic in topics_raw if isinstance(topic, str)]
        owner = entry.get("owner") if isinstance(entry.get("owner"), dict) else {}
        language = str(entry.get("language") or "")
        description = str(entry.get("description") or "")
        haystack = f"{full_name} {description} {' '.join(topics)}"
        if AI_HINT.search(haystack) is None and not any(
            token in {topic.lower() for topic in topics}
            for token in ("ai", "llm", "openai", "gpt", "rag", "mcp", "machine-learning")
        ):
            continue
        icon = str(owner.get("avatar_url") or "")
        repos.append(
            _repo_record(
                full_name=full_name,
                description=description,
                stars=int(entry.get("stargazers_count") or 0),
                stars_week=0,
                language=language,
                topics=topics,
                icon_url=icon,
                url=str(entry.get("html_url") or f"https://github.com/{full_name}"),
            )
        )
        repos[-1]["created_at"] = str(entry.get("created_at") or "")
        repos[-1]["fresh"] = True
    repos.sort(key=lambda item: int(item["stars"]), reverse=True)
    return repos


def _repo_record(
    *,
    full_name: str,
    description: str,
    stars: int,
    stars_week: int,
    language: str,
    topics: list[str] | tuple[str, ...],
    icon_url: str = "",
    url: str = "",
) -> dict[str, object]:
    owner, name = full_name.split("/", 1)
    kind = infer_kind(language, topics, f"{name} {description}")
    record = {
        "rank": 0,
        "full_name": full_name,
        "name": name,
        "owner": owner,
        "url": url or f"https://github.com/{full_name}",
        "stars": int(stars),
        "stars_label": format_stars(int(stars)),
        "stars_week": int(stars_week),
        "language": language,
        "kind": kind,
        "icon_url": icon_url or f"https://github.com/{owner}.png",
        "description": description,
        "topics": list(topics),
        "created_at": "",
        "fresh": False,
        "summary": "",
        "does": "",
        "how": "",
        "audience": "",
        "scene": "",
        "enriched": False,
    }
    return record


def apply_leaderboard_copy(
    repos: list[dict[str, object]], items: object
) -> list[dict[str, object]]:
    from .copy_validate import COPY_FIELDS, validate_copy_items

    by_name = validate_copy_items(repos, items)
    updated: list[dict[str, object]] = []
    for repo in repos:
        copy = dict(repo)
        incoming = by_name[str(repo.get("full_name") or repo.get("name") or "")]
        for field in COPY_FIELDS:
            copy[field] = str(incoming[field]).strip()
        copy["enriched"] = True
        updated.append(copy)
    return updated


def build_xhs_copy(repos: list[dict[str, object]]) -> tuple[str, str]:
    lines: list[str] = []
    for repo in repos:
        lines.append(f"{repo['rank']}. {repo['name']}")
        lines.append(str(repo.get("summary") or repo.get("description") or ""))
        lines.append(str(repo["url"]))
        lines.append("")
    return XHS_TITLE, "\n".join(lines).strip()


def _search_url(week_start: str, per_page: int) -> str:
    query = f"{AI_QUERY} created:>={week_start}"
    return (
        f"{SEARCH_URL}?{urllib.parse.urlencode({'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': str(per_page)})}"
    )


GITHUB_TOKEN_ENV = "AUTOPOSTING_GITHUB_TOKEN"
GITHUB_TOKEN_FALLBACK_ENV = "GITHUB_TOKEN"
_TOKEN_OVERRIDE: str | None = None
_RATE_STATE: dict[str, object] | None = None


class GitHubFetchError(RuntimeError):
    """A GitHub HTTP call failed; ``kind`` says why."""

    kind: str
    status: int | None
    url: str
    reset_at: float | None

    def __init__(
        self,
        kind: str,
        url: str,
        status: int | None = None,
        reset_at: float | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self.kind = str(kind)
        self.url = str(url)
        self.status = int(status) if status is not None else None
        self.reset_at = float(reset_at) if reset_at is not None else None
        super().__init__(message or _failure_message(self.kind, self.url, self.status))


def _failure_message(kind: str, url: str, status: int | None) -> str:
    """Wording other modules string-match on; the token never reaches it."""
    if kind == "rate_limited":
        quota = f" ({status})" if status is not None else ""
        return f"github_fetch_failed:rate limit exceeded{quota}:{url}"
    return f"github_fetch_failed:{url}"


def set_github_token(token: str | None) -> None:
    """Override the process-wide token; ``None`` falls back to the environment."""
    global _TOKEN_OVERRIDE
    _TOKEN_OVERRIDE = None if token is None else str(token).strip()


def github_token() -> str:
    """Resolve the token to send: override, then AUTOPOSTING_GITHUB_TOKEN, then GITHUB_TOKEN."""
    if _TOKEN_OVERRIDE is not None:
        return _TOKEN_OVERRIDE
    for name in (GITHUB_TOKEN_ENV, GITHUB_TOKEN_FALLBACK_ENV):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def github_rate_state() -> dict[str, object] | None:
    """Latest X-RateLimit-* headers as a fresh dict, or None before any call."""
    return dict(_RATE_STATE) if _RATE_STATE is not None else None


def _header_text(headers: object, name: str) -> str:
    """Read one response header, tolerating mocks and non-string values."""
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    try:
        value = getter(name)
    except (AttributeError, KeyError, TypeError):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _header_number(headers: object, name: str) -> float | None:
    try:
        return float(_header_text(headers, name))
    except ValueError:
        return None


def _record_rate_headers(headers: object) -> None:
    """Remember the newest quota headers; responses without them keep the old state."""
    global _RATE_STATE
    limit = _header_number(headers, "X-RateLimit-Limit")
    remaining = _header_number(headers, "X-RateLimit-Remaining")
    reset = _header_number(headers, "X-RateLimit-Reset")
    resource = _header_text(headers, "X-RateLimit-Resource")
    if limit is None and remaining is None and reset is None and not resource:
        return
    previous = _RATE_STATE or {}
    _RATE_STATE = {
        "limit": int(limit) if limit is not None else int(previous.get("limit") or 0),
        "remaining": int(remaining)
        if remaining is not None
        else int(previous.get("remaining") or 0),
        "reset": float(reset) if reset is not None else float(previous.get("reset") or 0.0),
        "resource": resource or str(previous.get("resource") or ""),
    }


def _rate_limit_marked(status: int | None, body: str, headers: object) -> bool:
    """A 403 is a quota answer only when the response says so; a 429 always is."""
    if status == 429:
        return True
    if status != 403:
        return False
    # "secondary rate limit" also contains "rate limit".
    if "rate limit" in body.lower():
        return True
    return _header_text(headers, "X-RateLimit-Remaining") == "0"


def _http_fetch_error(exc: urllib.error.HTTPError, url: str) -> GitHubFetchError:
    """Classify an HTTP failure; the response body never enters the message."""
    status = getattr(exc, "code", None)
    status = int(status) if status is not None else None
    headers = getattr(exc, "headers", None)
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # an unreadable body must not hide the HTTP status
        body = ""
    if _rate_limit_marked(status, body, headers):
        kind = "rate_limited"
    elif status in (401, 403):
        kind = "forbidden"
    elif status == 404:
        kind = "not_found"
    elif status is not None and 500 <= status < 600:
        kind = "server_error"
    else:
        kind = "http_error"
    return GitHubFetchError(
        kind, url, status=status, reset_at=_header_number(headers, "X-RateLimit-Reset")
    )


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    text = str(reason if reason is not None else exc).lower()
    return "timed out" in text or "timeout" in text


def _network_fetch_error(exc: BaseException, url: str) -> GitHubFetchError:
    return GitHubFetchError("timeout" if _is_timeout(exc) else "unreachable", url)


def default_github_get(url: str, timeout: float = 20, token: str | None = None) -> object:
    from .llm import HTTP_USER_AGENT

    global _GITHUB_NOW
    headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "application/vnd.github+json, text/html"
        if "api.github.com" in url
        else "text/html,application/xhtml+xml",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    credential = github_token() if token is None else str(token).strip()
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _record_rate_headers(getattr(response, "headers", None))
            header_date = response.headers.get("Date")
            if header_date:
                try:
                    parsed_date = parsedate_to_datetime(header_date)
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                    _GITHUB_NOW = parsed_date.astimezone(timezone.utc)
                except (TypeError, ValueError, IndexError):
                    pass
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        _record_rate_headers(getattr(exc, "headers", None))
        raise _http_fetch_error(exc, url) from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # URLError and socket.timeout are OSError subclasses; the classifier
        # separates a timeout from a plain network failure.
        raise _network_fetch_error(exc, url) from exc
    if "api.github.com" in url:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitHubFetchError(
                "bad_json", url, message="github_search_invalid_json"
            ) from exc
        return parsed
    return raw


def fetch_weekly_ai_repos(
    *,
    getter: Getter | None = None,
    now: datetime | None = None,
    limit: int = 10,
    diagnostics: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    fetch = getter or default_github_get
    week_start = week_window_start(now)
    collected: list[dict[str, object]] = []
    seen: set[str] = set()

    def degrade(stage: str, error: object = None, *, detail: object = None) -> None:
        diagnostic = make_diagnostic(
            "github",
            stage,
            error,
            code="github_fetch_failed",
            cause_code="invalid_response" if detail is not None else None,
            detail=detail,
        )
        if diagnostics is not None:
            diagnostics.append(diagnostic)
        log_diagnostic(
            LOGGER,
            diagnostic,
            error if isinstance(error, BaseException) else None,
        )

    def _extend(batch: list[dict[str, object]]) -> None:
        for repo in batch:
            full_name = str(repo["full_name"])
            if full_name in seen or len(collected) >= limit:
                continue
            seen.add(full_name)
            collected.append(repo)

    try:
        payload = fetch(_search_url(week_start, max(limit, 10)))
    except Exception as exc:
        degrade("search", exc)
        payload = {"items": []}
    if isinstance(payload, dict):
        try:
            if "items" not in payload:
                raise ValueError("github_search_items_missing")
            _extend(parse_github_search(payload))
        except Exception as exc:
            degrade("search", exc)
    elif payload is not None:
        degrade("search", detail="github_search_response_not_object")

    if len(collected) < limit:
        try:
            trending = fetch(TRENDING_URL)
        except Exception as exc:
            degrade("trending", exc)
            trending = None
        if isinstance(trending, str):
            try:
                if not trending.strip():
                    raise ValueError("github_trending_response_empty")
                extras = parse_trending_html(trending)
                for repo in extras:
                    repo["fresh"] = False
                _extend(extras)
            except Exception as exc:
                degrade("trending", exc)
        elif trending is not None:
            degrade("trending", detail="github_trending_response_not_text")

    max_stars = max((int(repo["stars"]) for repo in collected), default=1) or 1
    ranked: list[dict[str, object]] = []
    for index, repo in enumerate(collected[:limit], start=1):
        item = dict(repo)
        item["rank"] = index
        item["star_share"] = round(int(item["stars"]) / max_stars, 4)
        ranked.append(item)
    return ranked


def snapshot_payload(
    repos: list[dict[str, object]], *, model_used: str = "", fetched_at: str = ""
) -> dict[str, object]:
    title, body = build_xhs_copy(repos)
    return {
        "ok": True,
        "week_start": week_window_start(),
        "fetched_at": fetched_at,
        "repos": repos,
        "xhs_title": title,
        "xhs_body": body,
        "model_used": model_used,
        "count": len(repos),
    }
