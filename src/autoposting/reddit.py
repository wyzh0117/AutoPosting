"""Reddit public JSON collector for viewpoint candidates."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

from .db import Database
from .images import BROWSER_UA
from .models import CollectionResult, FeedItem, Source

Fetcher = Callable[[str], bytes]

REDDIT_ORIGIN = "https://www.reddit.com"
HOT_LIMIT = 15
COMMENT_FETCH_LIMIT = 25
COMMENT_KEEP = 5
POST_CAP = 12


@dataclass(frozen=True)
class RedditPost:
    id: str
    external_id: str
    title: str
    selftext: str
    canonical_url: str
    linked_url: str
    score: int
    num_comments: int
    published_at: str
    author: str
    is_self: bool
    subreddit: str


@dataclass(frozen=True)
class RedditComment:
    author: str
    score: int
    body: str


def reddit_hot_url(subreddit: str) -> str:
    return f"{REDDIT_ORIGIN}/r/{quote(subreddit)}/hot.json?limit={HOT_LIMIT}&raw_json=1"


def reddit_comments_url(subreddit: str, post_id: str) -> str:
    return (
        f"{REDDIT_ORIGIN}/r/{quote(subreddit)}/comments/{quote(post_id)}.json"
        f"?limit={COMMENT_FETCH_LIMIT}&sort=top&raw_json=1"
    )


def reddit_rss_url(subreddit: str) -> str:
    return f"{REDDIT_ORIGIN}/r/{quote(subreddit)}/.rss"


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _permalink_url(permalink: str) -> str:
    path = permalink if permalink.startswith("/") else f"/{permalink}"
    return f"{REDDIT_ORIGIN}{path}"


def parse_reddit_hot(payload: bytes, subreddit: str) -> list[RedditPost]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reddit_hot_json_invalid") from exc
    children = []
    if isinstance(data, dict):
        listing = data.get("data")
        if isinstance(listing, dict) and isinstance(listing.get("children"), list):
            children = listing["children"]
    posts: list[RedditPost] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            continue
        raw = child.get("data")
        if not isinstance(raw, dict):
            continue
        if raw.get("stickied") is True:
            continue
        post_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        permalink = str(raw.get("permalink") or "").strip()
        if not post_id or not title or not permalink:
            continue
        created = raw.get("created_utc")
        if isinstance(created, (int, float)):
            published = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
        else:
            published = datetime.now(timezone.utc).isoformat()
        posts.append(
            RedditPost(
                id=post_id,
                external_id=f"t3_{post_id}",
                title=title,
                selftext=str(raw.get("selftext") or "").strip(),
                canonical_url=_permalink_url(permalink),
                linked_url=str(raw.get("url") or "").strip(),
                score=_as_int(raw.get("score")),
                num_comments=_as_int(raw.get("num_comments")),
                published_at=published,
                author=str(raw.get("author") or "").strip(),
                is_self=bool(raw.get("is_self")),
                subreddit=str(raw.get("subreddit") or subreddit),
            )
        )
        if len(posts) >= POST_CAP:
            break
    return posts


def parse_reddit_comments(
    payload: bytes, op_author: str, limit: int = COMMENT_KEEP
) -> list[RedditComment]:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    listing = None
    if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], dict):
        listing = data[1].get("data")
    elif isinstance(data, dict):
        listing = data.get("data")
    children = []
    if isinstance(listing, dict) and isinstance(listing.get("children"), list):
        children = listing["children"]
    scored: list[RedditComment] = []
    op = (op_author or "").lower()
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        raw = child.get("data")
        if not isinstance(raw, dict):
            continue
        author = str(raw.get("author") or "").strip()
        body = str(raw.get("body") or "").strip()
        if not body or body in {"[deleted]", "[removed]"}:
            continue
        if author.lower() == op:
            continue
        scored.append(
            RedditComment(author=author or "deleted", score=_as_int(raw.get("score")), body=body)
        )
    scored.sort(key=lambda row: row.score, reverse=True)
    return scored[: max(limit, 0)]


_POST_ID_RE = re.compile(r"/comments/([A-Za-z0-9]+)/")


def posts_from_rss_items(items: list, subreddit: str) -> list[RedditPost]:
    posts: list[RedditPost] = []
    for index, item in enumerate(items):
        url = str(getattr(item, "canonical_url", "") or "")
        match = _POST_ID_RE.search(url)
        if not match:
            continue
        post_id = match.group(1)
        title = str(getattr(item, "title", "") or "").strip()
        if not title:
            continue
        posts.append(
            RedditPost(
                id=post_id,
                external_id=f"t3_{post_id}",
                title=title,
                selftext=str(getattr(item, "content_text", "") or "").strip(),
                canonical_url=_permalink_url(f"/r/{subreddit}/comments/{post_id}/"),
                linked_url=url,
                score=max(1000 - index * 10, 1),
                num_comments=0,
                published_at=str(getattr(item, "published_at", "") or datetime.now(timezone.utc).isoformat()),
                author="",
                is_self=True,
                subreddit=subreddit,
            )
        )
        if len(posts) >= POST_CAP:
            break
    return posts


def compose_reddit_content(post: RedditPost, comments: list[RedditComment]) -> str:
    parts: list[str] = []
    if post.selftext:
        parts.append(post.selftext)
    elif post.linked_url and post.linked_url != post.canonical_url:
        parts.append(f"[链接帖] {post.linked_url}")
    if comments:
        lines = ["热评："]
        for index, comment in enumerate(comments, start=1):
            lines.append(f"{index}. @{comment.author}（{comment.score}）：{comment.body}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts).strip()


def _default_reddit_fetcher(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


class RedditCollector:
    def __init__(self, database: Database, fetcher: Fetcher | None = None) -> None:
        self.database = database
        self.fetcher = fetcher or _default_reddit_fetcher

    def _load_posts(self, subreddit: str) -> tuple[list[RedditPost], bool]:
        try:
            posts = parse_reddit_hot(self.fetcher(reddit_hot_url(subreddit)), subreddit)
            if posts:
                return posts, True
        except urllib.error.HTTPError as exc:
            if exc.code not in {403, 429, 404}:
                raise
        except (OSError, TimeoutError, urllib.error.URLError, ValueError):
            pass
        from .ingestion import parse_feed

        items = parse_feed(self.fetcher(reddit_rss_url(subreddit)))
        posts = posts_from_rss_items(items, subreddit)
        if not posts:
            raise ValueError("reddit_listing_empty")
        return posts, False

    def collect(self, source: Source) -> CollectionResult:
        if source.type != "reddit":
            raise ValueError(f"RedditCollector cannot collect source type: {source.type}")
        subreddit = source.url_or_handle.lstrip("r/").lstrip("/")
        try:
            posts, from_json = self._load_posts(subreddit)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            self.database.record_fetch_result(source.source_id, str(exc))
            return CollectionResult(inserted=0, duplicates=0, failed=1)
        inserted = 0
        duplicates = 0
        for post in posts:
            comments: list[RedditComment] = []
            if from_json:
                try:
                    comments = parse_reddit_comments(
                        self.fetcher(reddit_comments_url(subreddit, post.id)),
                        op_author=post.author,
                    )
                except (OSError, TimeoutError, urllib.error.URLError, ValueError):
                    comments = []
            item = FeedItem(
                external_id=post.external_id,
                canonical_url=post.canonical_url,
                title=post.title,
                content_text=compose_reddit_content(post, comments),
                published_at=post.published_at,
                raw_data={
                    "score": str(post.score),
                    "comments": str(post.num_comments),
                    "permalink": post.canonical_url,
                    "subreddit": post.subreddit,
                    "author": post.author,
                },
            )
            if self.database.insert_item(source, item):
                inserted += 1
            else:
                duplicates += 1
        self.database.record_fetch_result(source.source_id)
        return CollectionResult(inserted=inserted, duplicates=duplicates, failed=0)
