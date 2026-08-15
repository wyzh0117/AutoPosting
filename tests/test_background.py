import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autoposting.background import (
    _parse_ddg_json,
    _parse_wikipedia,
    gather_background,
    infer_focus,
    parse_search_html,
    search_local,
    search_query,
    search_web,
)
from autoposting.db import Database
from autoposting.models import FeedItem, Source


DDG_HTML = (
    "<html><body>"
    '<a class="result__a" href="https://github.com/deepseek-ai/deepseek-harness">DeepSeek Harness preview</a>'
    '<a class="result__snippet">An open agent framework released with V4-Pro.</a>'
    '<a class="result__a" href="https://www.ithome.com/0/123.htm">ITHome: Harness preview</a>'
    '<div class="result__snippet">An agent framework compared with Claude Cowork.</div>'
    "</body></html>"
).encode("utf-8")


class BackgroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "bg.db")
        self.db.initialize()
        self.source = Source(
            "lab_blog", "Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15
        )
        self.db.upsert_source(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_html_and_local_related_items(self) -> None:
        results = parse_search_html(DDG_HTML)
        self.assertEqual("DeepSeek Harness preview", results[0]["title"])
        self.assertIn("github.com/deepseek-ai", results[0]["url"])
        self.assertIn("agent", results[0]["snippet"])
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_item(
            self.source,
            FeedItem("h1", "https://lab.example/h1", "DeepSeek Harness", "Comments", now, {}),
        )
        self.db.insert_item(
            self.source,
            FeedItem(
                "h2",
                "https://lab.example/h2",
                "对标 Claude Cowork：DeepSeek Harness 公测",
                "开源代码智能体框架。",
                now,
                {},
            ),
        )
        related = search_local(self.db, "DeepSeek Harness")
        self.assertTrue(any("公测" in item["title"] for item in related))
        self.assertEqual("DeepSeek Harness", infer_focus("DeepSeek Harness"))
        self.assertIn("2026", search_query("DeepSeek Harness"))

    def test_gather_background_uses_fetcher_and_survives_search_failure(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_item(
            self.source,
            FeedItem("h1", "https://lab.example/h1", "DeepSeek Harness", "Comments", now, {}),
        )
        pages = {
            "https://lab.example/h1": b"<html><p>Harness is an open agent loop.</p></html>",
            "https://html.duckduckgo.com/html/?q=DeepSeek+Harness+2026": DDG_HTML,
        }

        def fetcher(url: str) -> bytes:
            for key, payload in pages.items():
                if url.startswith(key.split("?")[0]) or url == key:
                    return payload
            raise OSError("offline")

        note = gather_background(
            self.db,
            "DeepSeek Harness",
            "Comments",
            page_url="https://lab.example/h1",
            fetcher=fetcher,
        )
        self.assertIn("agent loop", note.source_excerpt)
        self.assertTrue(note.web)
        self.assertEqual("DeepSeek Harness", note.focus)

        empty = gather_background(self.db, "DeepSeek Harness", "Comments", fetcher=lambda _: b"<html></html>")
        self.assertEqual([], empty.web)

    def test_search_web_falls_back_to_wikipedia_json(self) -> None:
        wiki = json.dumps(
            [
                "DeepSeek",
                ["DeepSeek"],
                ["A Chinese AI company."],
                ["https://en.wikipedia.org/wiki/DeepSeek"],
            ]
        ).encode()
        self.assertEqual("DeepSeek", _parse_wikipedia(wiki)[0]["title"])
        instant = json.dumps(
            {
                "Heading": "DeepSeek",
                "AbstractText": "An AI lab.",
                "AbstractURL": "https://en.wikipedia.org/wiki/DeepSeek",
                "RelatedTopics": [],
            }
        ).encode()
        self.assertEqual("DeepSeek", _parse_ddg_json(instant)[0]["title"])

        def fetcher(url: str) -> bytes:
            if "wikipedia.org" in url:
                return wiki
            return b"<html></html>"

        results = search_web("DeepSeek Harness 2026", fetcher=fetcher)
        self.assertEqual("https://en.wikipedia.org/wiki/DeepSeek", results[0]["url"])
