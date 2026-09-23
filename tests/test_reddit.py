import io
import json
import tempfile
import unittest
import urllib.error
from email.message import EmailMessage
from pathlib import Path

from autoposting.db import Database
from autoposting.ingestion import RedditCollector
from autoposting.models import Source
from autoposting.reddit import (
    compose_reddit_content,
    parse_reddit_comments,
    parse_reddit_hot,
    reddit_comments_url,
    reddit_hot_url,
    reddit_rss_url,
)


HOT_JSON = {
    "data": {
        "children": [
            {
                "kind": "t3",
                "data": {
                    "id": "sticky1",
                    "title": "Weekly thread",
                    "selftext": "rules",
                    "permalink": "/r/ChatGPT/comments/sticky1/weekly/",
                    "url": "https://www.reddit.com/r/ChatGPT/comments/sticky1/weekly/",
                    "score": 1,
                    "num_comments": 2,
                    "created_utc": 1700000000,
                    "author": "mod",
                    "stickied": True,
                    "is_self": True,
                    "subreddit": "ChatGPT",
                },
            },
            {
                "kind": "t3",
                "data": {
                    "id": "abc123",
                    "title": "I replaced my morning email with a Claude project",
                    "selftext": "I keep a project with my tone guide and dump the inbox into it.",
                    "permalink": "/r/ClaudeAI/comments/abc123/email/",
                    "url": "https://www.reddit.com/r/ClaudeAI/comments/abc123/email/",
                    "score": 420,
                    "num_comments": 88,
                    "created_utc": 1700000100,
                    "author": "alice",
                    "stickied": False,
                    "is_self": True,
                    "subreddit": "ClaudeAI",
                },
            },
        ]
    }
}

COMMENTS_JSON = [
    {"data": {"children": []}},
    {
        "data": {
            "children": [
                {
                    "kind": "t1",
                    "data": {
                        "author": "alice",
                        "body": "OP repeating the post",
                        "score": 90,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "bob",
                        "body": "[deleted]",
                        "score": 80,
                    },
                },
                {
                    "kind": "more",
                    "data": {"count": 12},
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "carol",
                        "body": "The trick is a daily recap prompt.",
                        "score": 50,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "dave",
                        "body": "I do the same with a project knowledge file.",
                        "score": 40,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "erin",
                        "body": "Watch the context window.",
                        "score": 12,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "author": "frank",
                        "body": "Sixth comment should drop.",
                        "score": 5,
                    },
                },
            ]
        }
    },
]


class RedditParseTests(unittest.TestCase):
    def test_hot_skips_stickied_and_builds_permalink(self) -> None:
        posts = parse_reddit_hot(json.dumps(HOT_JSON).encode(), "ClaudeAI")
        self.assertEqual(1, len(posts))
        post = posts[0]
        self.assertEqual("t3_abc123", post.external_id)
        self.assertEqual(
            "https://www.reddit.com/r/ClaudeAI/comments/abc123/email/",
            post.canonical_url,
        )
        self.assertEqual(420, post.score)
        self.assertEqual(88, post.num_comments)
        self.assertEqual("alice", post.author)
        self.assertTrue(post.published_at.startswith("2023-"))

    def test_comments_skip_op_deleted_and_cap_five(self) -> None:
        comments = parse_reddit_comments(
            json.dumps(COMMENTS_JSON).encode(), op_author="alice", limit=5
        )
        self.assertEqual(
            ["carol", "dave", "erin", "frank"],
            [row.author for row in comments],
        )
        # only four valid after skip; fifth slot unused
        self.assertEqual(4, len(comments))
        self.assertEqual("The trick is a daily recap prompt.", comments[0].body)

    def test_compose_includes_selftext_and_hot_comments(self) -> None:
        posts = parse_reddit_hot(json.dumps(HOT_JSON).encode(), "ClaudeAI")
        comments = parse_reddit_comments(
            json.dumps(COMMENTS_JSON).encode(), op_author="alice", limit=5
        )
        text = compose_reddit_content(posts[0], comments)
        self.assertIn("dump the inbox", text)
        self.assertIn("热评：", text)
        self.assertIn("carol", text)
        self.assertNotIn("[deleted]", text)
        self.assertNotIn("OP repeating", text)

    def test_urls(self) -> None:
        self.assertEqual(
            "https://www.reddit.com/r/ChatGPT/hot.json?limit=15&raw_json=1",
            reddit_hot_url("ChatGPT"),
        )
        self.assertEqual(
            "https://www.reddit.com/r/ChatGPT/comments/abc.json?limit=25&sort=top&raw_json=1",
            reddit_comments_url("ChatGPT", "abc"),
        )
        self.assertEqual("https://www.reddit.com/r/ChatGPT/.rss", reddit_rss_url("ChatGPT"))


class RedditCollectorTests(unittest.TestCase):
    def test_collector_inserts_post_with_engagement(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            if "/hot.json" in url:
                return json.dumps(HOT_JSON).encode()
            return json.dumps(COMMENTS_JSON).encode()

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "db.sqlite")
            database.initialize()
            source = Source(
                source_id="reddit_claude",
                name="r/ClaudeAI",
                type="reddit",
                url_or_handle="ClaudeAI",
                tier="S4",
                topics=("products",),
                enabled=True,
                fetch_interval_minutes=15,
            )
            database.upsert_source(source)
            result = RedditCollector(database, fetcher=fetcher).collect(source)
            self.assertEqual(1, result.inserted)
            self.assertEqual(0, result.failed)
            rows = database.list_hotspot_candidates(
                "2020-01-01T00:00:00+00:00", limit=10, sort="heat", scope="community"
            )
            self.assertEqual(1, len(rows))
            self.assertEqual("I replaced my morning email with a Claude project", rows[0]["title"])
            self.assertEqual(420, rows[0]["engagement"]["score"])
            self.assertEqual(88, rows[0]["engagement"]["comments"])
            news = database.list_hotspot_candidates(
                "2020-01-01T00:00:00+00:00", limit=10, sort="heat", scope="official"
            )
            self.assertEqual([], news)
            self.assertTrue(any("/hot.json" in url for url in calls))
            self.assertTrue(any("/comments/abc123.json" in url for url in calls))

    def test_one_comment_fetch_failure_still_keeps_the_post(self) -> None:
        def fetcher(url: str) -> bytes:
            if "/hot.json" in url:
                return json.dumps(HOT_JSON).encode()
            raise TimeoutError("reddit comments timeout")

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "db.sqlite")
            database.initialize()
            source = Source(
                source_id="reddit_claude",
                name="r/ClaudeAI",
                type="reddit",
                url_or_handle="ClaudeAI",
                tier="S4",
                topics=("products",),
                enabled=True,
                fetch_interval_minutes=15,
            )
            database.upsert_source(source)
            result = RedditCollector(database, fetcher=fetcher).collect(source)
            self.assertEqual(1, result.inserted)
            item = database.list_hotspot_candidates(
                "2020-01-01T00:00:00+00:00", limit=10, scope="community"
            )[0]
            self.assertIn("dump the inbox", str(item["content_text"]))
            self.assertNotIn("热评：", str(item["content_text"]))

    def test_json_block_falls_back_to_subreddit_rss(self) -> None:
        rss = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ChatGPT</title>
  <entry>
    <title>RSS fallback post</title>
    <link href="https://www.reddit.com/r/ChatGPT/comments/rsspost/title/"/>
    <id>https://www.reddit.com/r/ChatGPT/comments/rsspost/title/</id>
    <updated>2026-08-25T00:00:00+00:00</updated>
    <content type="html">I used a project folder as memory.</content>
  </entry>
</feed>
"""

        def fetcher(url: str) -> bytes:
            if "hot.json" in url:
                raise urllib.error.HTTPError(
                    url, 403, "Blocked", EmailMessage(), io.BytesIO()
                )
            if url.endswith(".rss") or "/.rss" in url:
                return rss
            raise urllib.error.HTTPError(
                url, 403, "Blocked", EmailMessage(), io.BytesIO()
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "db.sqlite")
            database.initialize()
            source = Source(
                source_id="reddit_chatgpt",
                name="r/ChatGPT",
                type="reddit",
                url_or_handle="ChatGPT",
                tier="S4",
                topics=("products",),
                enabled=True,
                fetch_interval_minutes=15,
            )
            database.upsert_source(source)
            result = RedditCollector(database, fetcher=fetcher).collect(source)
            self.assertEqual(1, result.inserted)
            row = database.list_hotspot_candidates(
                "2020-01-01T00:00:00+00:00", limit=10, scope="community"
            )[0]
            self.assertEqual("RSS fallback post", row["title"])
            self.assertIn("project folder", str(row["content_text"]))
