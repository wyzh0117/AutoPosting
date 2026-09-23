"""Topic Dossier -> Article: the official chain, proven end to end.

The refactor's whole point is that Article Generation *consumes* a finished
research result. So the tests here do not check markup: they run the real
Research Service, then the real article path, and assert on the produced draft
plus the fact that no second search happened.
"""

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from autoposting.article_context import build_dossier_context
from autoposting.db import Database
from autoposting.llm import LLMService
from autoposting.models import FeedItem, Source
from autoposting.web import WebApp, create_server

from test_workbench import FakeModel, _feed


def _decode(response: object) -> object:
    """Read either a JSON payload or the index HTML from a response."""
    text = response.read().decode("utf-8")  # type: ignore[attr-defined]
    ctype = response.headers.get("Content-Type", "")  # type: ignore[attr-defined]
    return json.loads(text) if "application/json" in ctype else text


class CountingSearch:
    """A search adapter that records every call it receives."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> dict[str, object]:
        self.queries.append(query)
        return {
            "query": query,
            "adapters": list(adapters),
            "results": [
                {
                    "title": "官方研究说明",
                    "url": "https://example.com/research",
                    "snippet": "研究摘要",
                }
            ],
        }


class CountingReader:
    def __init__(self) -> None:
        self.reads: list[object] = []

    def read(self, candidate: object) -> dict[str, object]:
        self.reads.append(candidate)
        return {
            "status": "full",
            "title": getattr(candidate, "title", "证据"),
            "text": "研究正文",
            "quality_score": 0.9,
        }


class DossierArticleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        self.source = Source(
            "lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15
        )
        self.db.upsert_source(self.source)
        self.feed = _feed([("w1", "Model One news", "https://lab.example/w1", 5)])
        self.model = FakeModel()
        self.app = WebApp(
            self.db,
            (self.source,),
            self.root / "packages",
            fetcher=lambda _: self.feed,
            llm_service=LLMService(self.db, runner=self.model),
            page_fetcher=lambda _: b"",
        )
        self.service = self.app.workbench.research_service
        self.search = CountingSearch()
        self.reader = CountingReader()
        self.service.search_adapter = self.search
        self.service.reader = self.reader
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, dict[str, object]]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc)

    def _dossier(self) -> dict[str, object]:
        """Research a real radar cluster: the same shape the UI hands over.

        The cluster must carry real source ids, because that is what lets the
        finished article cite its sources. The app records these clusters in
        ``_radar_events`` during a hotspot refresh; this builds the identical
        mapping rather than inventing a shape research would never see.
        """
        self.db.insert_item(
            self.source,
            FeedItem("w1", "https://lab.example/w1", "Model One news", "Model One news 内容", None, {}),
        )
        item_id = str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])
        cluster = {
            "event_id": "cluster-1",
            "name": "模型发布",
            "title": "模型发布",
            "topic": "models",
            "source_ids": ["lab_blog"],
            "source_urls": ["https://lab.example/w1"],
            "item_ids": [item_id],
        }
        self.app._radar_events["cluster-1"] = cluster
        self.app._radar_events[item_id] = cluster
        status, created = self._request(
            "POST", "/api/research", {"event_ids": ["cluster-1"], "query": "模型发布研究"}
        )
        self.assertEqual(200, status)
        return created["dossier"]

    def test_dossier_reaches_the_article_without_a_second_search(self) -> None:
        dossier = self._dossier()
        research_calls = len(self.search.queries)
        self.assertEqual(1, research_calls)

        status, article = self._request(
            "POST", "/api/articles", {"dossier_id": dossier["dossier_id"], "platform": "wechat"}
        )
        self.assertEqual(200, status)
        self.assertTrue(article["ok"])

        # The whole point: generating the article ran research zero more times.
        self.assertEqual(research_calls, len(self.search.queries))

        draft = self.db.get_draft(article["draft_id"])
        self.assertIn("AIGC", draft["body"])
        # The citation id became a real link to the dossier's source, and the
        # draft records the source: the article is traceable, not decorative.
        self.assertIn("https://lab.example/w1", draft["body"])
        self.assertEqual(["lab_blog"], json.loads(draft["source_ids_json"]))

        # The dossier material, not a fresh search, is what reached the model.
        prompt = self.model.calls[-1][2]
        self.assertIn("研究证据（Topic Dossier）", prompt)
        self.assertIn(dossier["dossier_id"], prompt)
        self.assertIn("research_context", prompt)
        # The retired chains must not reappear inside the prompt.
        self.assertNotIn("chosen_hooks", prompt)
        self.assertNotIn("related_news", prompt)

    def test_dossier_article_records_the_topic_dossier_event(self) -> None:
        dossier = self._dossier()
        self._request(
            "POST", "/api/articles", {"dossier_id": dossier["dossier_id"], "platform": "wechat"}
        )
        row = (
            self.db.connect()
            .execute("SELECT event_id, trigger_type FROM events ORDER BY created_at DESC LIMIT 1")
            .fetchone()
        )
        self.assertEqual("TOPIC_DOSSIER", row["trigger_type"])
        expected = hashlib.sha256(
            f"topic-dossier:{dossier['dossier_id']}:wechat".encode()
        ).hexdigest()[:24]
        self.assertEqual(expected, row["event_id"])
        # The persisted draft points at that event, so the article is traceable
        # back to the dossier that produced it.
        draft = self.db.get_draft(
            str(self.db.connect().execute("SELECT draft_id FROM drafts LIMIT 1").fetchone()["draft_id"])
        )
        self.assertEqual(expected, draft["event_id"])

    def test_generation_needs_no_picked_candidate_item(self) -> None:
        # A dossier article has no candidate item; requiring one would keep the
        # old "pick from the news list" chain alive.
        dossier = self._dossier()
        status, article = self._request(
            "POST", "/api/articles", {"dossier_id": dossier["dossier_id"], "platform": "wechat"}
        )
        self.assertEqual(200, status)
        self.assertTrue(article["draft_id"])

    def test_missing_dossier_is_a_400_with_a_stable_reason(self) -> None:
        status, payload = self._request(
            "POST", "/api/articles", {"dossier_id": "dossier-missing", "platform": "wechat"}
        )
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])
        self.assertEqual("dossier_not_found", payload["error"])
        self.assertEqual("ArticleContextError", payload["error_type"])
        self.assertTrue(str(payload["error_reason"]).strip())

    def test_unknown_dossier_never_reaches_the_research_layer(self) -> None:
        status, payload = self._request(
            "POST", "/api/articles", {"dossier_id": "dossier-missing", "platform": "wechat"}
        )
        self.assertEqual(400, status)
        self.assertEqual("ArticleContextError", payload["error_type"])
        # No search ran: a missing dossier is an error, never a reason to research.
        self.assertEqual([], self.search.queries)

    def test_dossier_without_evidence_fails_instead_of_searching(self) -> None:
        dossier = self._dossier()
        research_calls = len(self.search.queries)
        empty = dict(dossier)
        empty["evidence"] = []
        self.app.workbench.get_research = lambda dossier_id: empty  # type: ignore[method-assign]
        status, payload = self._request(
            "POST", "/api/articles", {"dossier_id": dossier["dossier_id"], "platform": "wechat"}
        )
        self.assertEqual(400, status)
        self.assertEqual("dossier_evidence_required", payload["error"])
        self.assertEqual(research_calls, len(self.search.queries))

    def test_blank_dossier_id_is_rejected_before_any_work(self) -> None:
        for value in ("", "   ", 7):
            with self.subTest(value=value):
                status, payload = self._request(
                    "POST", "/api/articles", {"dossier_id": value, "platform": "wechat"}
                )
                self.assertEqual(400, status)
                self.assertEqual("dossier_id_required", payload["error"])

    def test_manual_article_still_works_without_a_dossier(self) -> None:
        self.db.insert_item(
            self.source,
            FeedItem("w1", "https://lab.example/w1", "Model One news", "Model One news 内容", None, {}),
        )
        item_id = str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])
        status, article = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "wechat"}
        )
        self.assertEqual(200, status)
        self.assertTrue(article["draft_id"])
        prompt = self.model.calls[-1][2]
        self.assertIn("article_context", prompt)
        self.assertIn('"source_type": "manual"', prompt)


class RetiredSurfaceTests(unittest.TestCase):
    """The retired chains must be unreachable, not merely unused."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        self.source = Source(
            "lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15
        )
        self.db.upsert_source(self.source)
        self.app = WebApp(
            self.db,
            (self.source,),
            self.root / "packages",
            fetcher=lambda _: _feed([("w1", "Model One news", "https://lab.example/w1", 5)]),
            llm_service=LLMService(self.db, runner=FakeModel()),
            page_fetcher=lambda _: b"",
        )
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, object]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc)

    def test_retired_endpoints_are_gone(self) -> None:
        for path in ("/api/hooks/find", "/api/related-search"):
            with self.subTest(path=path):
                status, payload = self._request("POST", path, {"item_id": "x"})
                self.assertEqual(404, status)
                self.assertNotEqual("hooks", payload.get("error") if isinstance(payload, dict) else None)

    def test_retired_workbench_methods_are_gone(self) -> None:
        for name in ("find_hooks", "related_search"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.app.workbench, name))

    def test_hooks_module_and_llm_extractor_are_gone(self) -> None:
        import importlib

        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("autoposting.hooks")
        self.assertFalse(hasattr(LLMService, "extract_hook_candidates"))

    def test_lane_is_only_a_thin_request_alias(self) -> None:
        # `lane` survives at the HTTP boundary and nowhere else: the data layer
        # speaks `scope`.
        import inspect

        from autoposting.db import Database
        from autoposting.workbench import HotspotService

        for owner in (Database.list_hotspot_candidates, HotspotService._sources_for_scope):
            with self.subTest(owner=owner.__qualname__):
                self.assertNotIn("lane", inspect.signature(owner).parameters)

        # The alias still resolves for old links and old bookmarks.
        status, payload = self._request("GET", "/api/hotspots?hours=24&lane=viewpoints")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

        status, payload = self._request("GET", "/api/hotspots?hours=24&scope=community")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_unknown_scope_or_lane_is_rejected_rather_than_widened(self) -> None:
        for path in (
            "/api/hotspots?hours=24&scope=bogus",
            "/api/hotspots?hours=24&lane=bogus",
        ):
            with self.subTest(path=path):
                status, payload = self._request("GET", path)
                self.assertEqual(400, status)
                message = str(payload.get("error")) + str(payload.get("error_reason"))
                # The message names whichever parameter the caller used.
                self.assertIn("lane" if "lane=" in path else "scope", message)

    def test_scope_is_accepted_on_collect(self) -> None:
        status, payload = self._request("POST", "/api/collect", {"hours": 24, "scope": "official"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        status, payload = self._request("POST", "/api/collect", {"hours": 24, "lane": "hotspots"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_index_page_has_no_retired_dom_or_javascript(self) -> None:
        status, html = self._request("GET", "/")
        self.assertEqual(200, status)
        html = str(html)
        for retired in (
            'id="find-hooks-btn"',
            'id="hook-list"',
            'id="related-search-btn"',
            'id="news-supplement-btn"',
            'id="news-supplement-drawer"',
            'id="skill-search-row"',
            'id="skill-source-bar"',
            'id="skill-degraded-note"',
            'id="skill-list"',
            'id="github-list"',
            'data-lane',
            "body.lane-",
            "legacy-lane-tabs",
            "function findHooks(",
            "function relatedSearch(",
            "function setLane(",
            "function renderNewsSupplements(",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, html)

    def test_index_page_carries_the_new_handoff_contract(self) -> None:
        status, html = self._request("GET", "/")
        html = str(html)
        for present in (
            'id="research-to-creation-btn"',
            'id="article-context"',
            'id="article-context-clear"',
            'id="news-scope-select"',
            'id="skills-sources"',
            'id="skills-source-chips"',
            'id="skills-degraded-note"',
            "payload.dossier_id",
            "function setArticleContextFromDossier(",
            "function clearArticleContext(",
            "function setNewsScope(",
        ):
            with self.subTest(present=present):
                self.assertIn(present, html)

    def test_version_badge_matches_the_package_version(self) -> None:
        import autoposting

        status, payload = self._request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertEqual(autoposting.__version__, payload["version"])
        self.assertEqual("3.2.1", payload["version"])
        status, html = self._request("GET", "/")
        self.assertIn("v3.2.1", str(html))


if __name__ == "__main__":
    unittest.main()

class DossierCitationUrlTests(unittest.TestCase):
    """The dossier hands over a source and its own url, and the article uses it.

    Regression: an event payload whose ``source_ids`` were a sorted set while the
    urls followed source-record order produced an article that linked a sentence
    about OpenAI's release to the Reddit thread — a wrong citation the reader
    cannot detect.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "citation.db")
        self.db.initialize()
        self.source = Source(
            source_id="openai_news",
            name="OpenAI News",
            type="rss",
            url_or_handle="https://openai.com/news/rss.xml",
            tier="S1",
            topics=("models",),
            enabled=True,
            fetch_interval_minutes=30,
        )
        self.db.upsert_source(self.source)
        for source in (
            Source(
                source_id="reddit_openai",
                name="Reddit r/OpenAI",
                type="reddit",
                url_or_handle="https://www.reddit.com/r/OpenAI/",
                tier="S4",
                topics=("models",),
                enabled=True,
                fetch_interval_minutes=30,
            ),
            Source(
                source_id="lab_blog",
                name="Lab Blog",
                type="rss",
                url_or_handle="https://lab.example/feed.xml",
                tier="S1",
                topics=("models",),
                enabled=True,
                fetch_interval_minutes=30,
            ),
        ):
            self.db.upsert_source(source)

    def test_the_context_pairs_each_source_with_its_own_url(self) -> None:
        dossier = {
            "schema_version": "topic-dossier-v1",
            "dossier_id": "d1",
            "title": "模型发布",
            "query": "模型发布",
            "event_ids": ["cluster-1"],
            "topics": [
                {
                    "event_id": "cluster-1",
                    "title": "模型发布",
                    "source_ids": ["lab_blog", "openai_news", "reddit_openai"],
                    "source_urls": [
                        "https://lab.example/w1",
                        "https://openai.com/index/gpt-6",
                        "https://www.reddit.com/r/OpenAI/comments/1wngyux/",
                    ],
                    "item_ids": [],
                }
            ],
            "evidence": [
                {
                    "evidence_id": "e1",
                    "title": "官方说明",
                    "url": "https://lab.example/w1",
                    "snippet": "研究摘要",
                    "source": "lab_blog",
                    "relation": "official",
                    "reader_status": "full",
                    "reader_quality": 0.9,
                    "assessment": {"decision": "include"},
                }
            ],
            "status": "ok",
        }
        context = build_dossier_context(dossier)
        paired = {ref["source_id"]: ref["source_url"] for ref in context.source_refs}
        self.assertEqual("https://lab.example/w1", paired["lab_blog"])
        self.assertEqual("https://openai.com/index/gpt-6", paired["openai_news"])
        self.assertEqual(
            "https://www.reddit.com/r/OpenAI/comments/1wngyux/", paired["reddit_openai"]
        )

    def test_a_short_url_list_leaves_the_extra_sources_unlinked(self) -> None:
        # Not shifted onto the wrong source: an unlinked source is honest, a
        # wrongly linked one is not.
        dossier = {
            "schema_version": "topic-dossier-v1",
            "dossier_id": "d2",
            "title": "模型发布",
            "query": "模型发布",
            "event_ids": ["cluster-1"],
            "topics": [
                {
                    "event_id": "cluster-1",
                    "title": "模型发布",
                    "source_ids": ["lab_blog", "openai_news", "reddit_openai"],
                    "source_urls": ["https://lab.example/w1"],
                    "item_ids": [],
                }
            ],
            "evidence": [
                {
                    "evidence_id": "e1",
                    "title": "官方说明",
                    "url": "https://lab.example/w1",
                    "snippet": "研究摘要",
                    "source": "lab_blog",
                    "relation": "official",
                    "reader_status": "full",
                    "reader_quality": 0.9,
                    "assessment": {"decision": "include"},
                }
            ],
            "status": "ok",
        }
        context = build_dossier_context(dossier)
        self.assertEqual(
            {"lab_blog": "", "openai_news": "", "reddit_openai": ""},
            {ref["source_id"]: ref["source_url"] for ref in context.source_refs},
        )

    def test_two_sources_sharing_one_url_keep_their_own_slots(self) -> None:
        # The same story on two feeds is normal.  De-duplicating the positional
        # url list shifted every later url onto the wrong source, and the
        # article then cited Reddit for an OpenAI claim.
        dossier = {
            "schema_version": "topic-dossier-v1",
            "dossier_id": "d3",
            "title": "模型发布",
            "query": "模型发布",
            "event_ids": ["cluster-1"],
            "topics": [
                {
                    "event_id": "cluster-1",
                    "title": "模型发布",
                    "source_ids": ["openai_news", "lab_blog", "reddit_openai"],
                    "source_urls": [
                        "https://openai.com/index/gpt-6",
                        "https://openai.com/index/gpt-6",
                        "https://www.reddit.com/r/OpenAI/comments/1wngyux/",
                    ],
                    "item_ids": [],
                }
            ],
            "evidence": [
                {
                    "evidence_id": "e1",
                    "title": "官方说明",
                    "url": "https://lab.example/w1",
                    "snippet": "研究摘要",
                    "source": "lab_blog",
                    "relation": "official",
                    "reader_status": "full",
                    "reader_quality": 0.9,
                    "assessment": {"decision": "include"},
                }
            ],
            "status": "ok",
        }
        context = build_dossier_context(dossier)
        self.assertEqual(
            {
                "openai_news": "https://openai.com/index/gpt-6",
                "lab_blog": "https://openai.com/index/gpt-6",
                "reddit_openai": "https://www.reddit.com/r/OpenAI/comments/1wngyux/",
            },
            {ref["source_id"]: ref["source_url"] for ref in context.source_refs},
        )
