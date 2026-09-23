"""Related-search coverage, retargeted at its retained owners.

The product-level ``/api/related-search`` endpoint, ``App.related_search``,
``Workbench.related_search`` and the ``#news-supplement-*`` drawer are gone, but
the related-search engine was deliberately kept:

* ``autoposting.background.search_related_news`` owns the search itself;
* ``autoposting.research.adapters.RelatedNewsSearchAdapter`` owns the Research
  Service's web-search surface and reuses that engine.

These tests carry the coverage the retired ``Workbench.related_search`` tests
had — derived query and hits, multi-hit feeds, empty-is-not-a-failure, the
graded failure taxonomy, degraded long selections, and the two security
regressions (no upstream response text in a reason/log, no credentials in a
log) — against those two owners.  Assertions stay behavioural: returned
values, cause codes, transported payloads and captured log records.
"""

import io
import logging
import multiprocessing
import time
import unittest
from unittest.mock import MagicMock, patch

import autoposting.background as background
from autoposting.background import (
    RELATED_SEARCH_CAUSE_NO_RESULT,
    RELATED_SEARCH_CAUSE_PARSE,
    RELATED_SEARCH_CAUSE_TIMEOUT,
    RELATED_SEARCH_CAUSE_TRANSPORT,
    RELATED_SEARCH_CAUSES,
    RelatedSearchError,
    derive_related_query,
    redact_related_detail,
    related_search_reason,
    search_related_news,
)
from autoposting.research.adapters import RelatedNewsSearchAdapter
from autoposting.radar import cluster_candidates
from autoposting.research.contracts import ResearchStatus, ResearchTopic

SELECTION = "英伟达发布新一代AI芯片"

# A real 200+ character Chinese selection.  Sent verbatim it used to time the
# whole search out, so the engine degrades it to its leading clause instead —
# and must report that it did, rather than truncate silently.
LONG_CHINESE_SELECTION = (
    "英伟达在大会上发布了新一代AI芯片，这款芯片采用了全新的架构设计，"
    "在推理性能上比上一代产品提升了两倍以上。分析师认为，随着数据中心对算力的需求持续增长，"
    "这款芯片预计将推动公司下一季度的营收增长。与此同时，竞争对手也在加快自研芯片的节奏，"
    "整个行业的竞争格局正在发生变化，供应链厂商同样受到关注。业内普遍预计，"
    "未来几个季度相关产品的出货量还会继续攀升，下游客户也在调整采购计划，"
    "多家机构上调了行业增速预期，但同时也提醒投资者关注供需波动带来的风险。"
)

DDG_HTML = (
    '<a class="result__a" href="https://news.example/chip">英伟达 AI 芯片新进展</a>'
    '<div class="result__snippet">英伟达新一代芯片报道</div>'
).encode("utf-8")

GOOGLE_RSS = (
    "<rss><channel>"
    "<item><title>英伟达芯片一</title><link>https://news.a.example/one</link>"
    "<description>一</description></item>"
    "<item><title>英伟达芯片二</title><link>https://news.b.example/two</link>"
    "<description>二</description></item>"
    "</channel></rss>"
).encode("utf-8")

ONE_HIT_RSS = (
    "<rss><channel><item>"
    "<title>英伟达发布新一代AI芯片</title>"
    "<link>https://news.example/ai-chip</link>"
    "<description>英伟达 AI 芯片报道</description>"
    "</item></channel></rss>"
).encode("utf-8")

EMPTY_FEED = b"<rss><channel><title>no items</title></channel></rss>"

# The reproduced upstream failure text: a credential in the query string, a
# single-use credential value and a whole HTML body behind it.
SECRET = "SUPERSECRETVALUE"
BEARER_SECRET = "sk-live-SECRET"
UPSTREAM_MARKER = "SECRET-UPSTREAM-BODY"
UPSTREAM_BODY = (
    "<html><body><h1>upstream rejected the request</h1>"
    f"<p>{UPSTREAM_MARKER} token={SECRET}</p>"
    f"<p>Authorization: Bearer {BEARER_SECRET}</p>"
    "<p>" + ("filler words " * 200) + "</p>"
    "</body></html>"
)
UPSTREAM_URL_RESPONSE = (
    "ddg: RuntimeError: upstream rejected the request; "
    f"see https://api.example/v1?api_key={SECRET} for details"
)


def _duckduckgo_fetcher(url: str) -> bytes:
    return DDG_HTML if "duckduckgo.com/html" in url else b""


def _google_rss_fetcher(url: str) -> bytes:
    return GOOGLE_RSS if "news.google.com/rss" in url else b""


def _empty_fetcher(_url: str) -> bytes:
    return EMPTY_FEED


def _unreachable_fetcher(_url: str) -> bytes:
    raise OSError("dns lookup failed")


def _unparsable_fetcher(_url: str) -> bytes:
    return b'{"error": "rate limited"}'


class RelatedSearchEngineTests(unittest.TestCase):
    """``autoposting.background.search_related_news`` — the retained engine."""

    def test_related_search_returns_derived_query_and_items(self) -> None:
        report = search_related_news(SELECTION, _duckduckgo_fetcher)

        # An empty cause_code is this layer's "ok": the search completed.
        self.assertEqual("", report.cause_code)
        self.assertEqual(derive_related_query(SELECTION).text, report.query)
        self.assertEqual(SELECTION, report.selection)
        self.assertFalse(report.degraded)
        self.assertEqual(1, len(report.results))
        self.assertEqual("https://news.example/chip", report.results[0]["url"])

    def test_related_search_returns_more_than_one_hit_from_rss(self) -> None:
        report = search_related_news(SELECTION, _google_rss_fetcher)

        self.assertEqual(2, len(report.results))
        self.assertEqual("https://news.a.example/one", report.results[0]["url"])
        self.assertEqual("https://news.b.example/two", report.results[1]["url"])
        self.assertFalse(report.degraded)
        self.assertEqual(SELECTION, report.selection)

    def test_related_search_empty_result_is_not_a_failure(self) -> None:
        report = search_related_news(SELECTION, _empty_fetcher)

        # Nothing qualified, but every source answered: this is a completed
        # search with a graded, renderable reason — not a raised failure.
        self.assertEqual([], report.results)
        self.assertEqual(RELATED_SEARCH_CAUSE_NO_RESULT, report.cause_code)
        self.assertIn(RELATED_SEARCH_CAUSE_NO_RESULT, RELATED_SEARCH_CAUSES)
        self.assertIn("未找到相关新闻", related_search_reason(report.cause_code))

    def test_related_search_maps_graded_failures_to_cause_codes(self) -> None:
        for cause, fetcher, expected_reason in (
            (RELATED_SEARCH_CAUSE_TRANSPORT, _unreachable_fetcher, "网络不可达"),
            (RELATED_SEARCH_CAUSE_PARSE, _unparsable_fetcher, "解析失败"),
        ):
            with self.subTest(cause):
                with self.assertRaises(RelatedSearchError) as raised:
                    search_related_news(SELECTION, fetcher)
                exc = raised.exception
                self.assertEqual(cause, exc.cause_code)
                self.assertEqual(cause, str(exc))
                self.assertIn(cause, RELATED_SEARCH_CAUSES)
                self.assertIn(expected_reason, exc.reason)
                self.assertTrue(exc.sources)

    def test_related_search_timeout_is_graded(self) -> None:
        def slow(_url: str) -> bytes:
            time.sleep(0.2)
            return b"<rss><channel /></rss>"

        with patch.object(background, "_RELATED_SEARCH_BUDGET_SECONDS", 0.05):
            with self.assertRaises(RelatedSearchError) as raised:
                search_related_news(SELECTION, slow)

        self.assertEqual(RELATED_SEARCH_CAUSE_TIMEOUT, raised.exception.cause_code)
        self.assertIn("检索超时", raised.exception.reason)

    def test_related_search_reason_does_not_leak_upstream_text(self) -> None:
        secret = "secret upstream failure"

        def fetcher(_url: str) -> bytes:
            raise RuntimeError(secret)

        with self.assertRaises(RelatedSearchError) as raised:
            search_related_news(SELECTION, fetcher)

        exc = raised.exception
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, exc.cause_code)
        self.assertNotIn(secret, exc.reason)
        self.assertIn("网络不可达", exc.reason)
        # The detail stays server-side: only its redacted, bounded form may
        # reach a log.  ``redact_related_detail`` keeps diagnostic prose on
        # purpose (background.py documents that), so the guarantee there is the
        # credential/body redaction asserted in the log-boundary tests below —
        # here the guarantee is that the caller-facing reason is pure taxonomy.
        self.assertLessEqual(len(redact_related_detail(exc.detail)), 400)

    def test_related_search_reports_degraded_long_selection_query(self) -> None:
        report = search_related_news(LONG_CHINESE_SELECTION, lambda _url: ONE_HIT_RSS)

        self.assertTrue(report.degraded)
        self.assertEqual("degraded_leading_clause", report.strategy)
        # The raw selection is reported for context, not silently dropped.
        self.assertEqual(LONG_CHINESE_SELECTION, report.selection)
        self.assertLess(len(report.query), len(LONG_CHINESE_SELECTION))
        self.assertIn("英伟达", report.query)
        self.assertEqual("https://news.example/ai-chip", report.results[0]["url"])

    def test_related_search_returned_upstream_payload_never_reaches_the_report(self) -> None:
        poisoned = (
            f"<html><body>Bearer {BEARER_SECRET} token={SECRET} {UPSTREAM_MARKER}"
            + ("z" * 2_000)
            + "</body></html>"
        ).encode("utf-8")
        seen: list[str] = []

        def fetcher(url: str) -> bytes:
            seen.append(url)
            return poisoned

        report = search_related_news(SELECTION, fetcher)

        # Non-vacuous: the poisoned payload really went through the engine.
        self.assertTrue(seen)
        self.assertTrue(all(derive_related_query(SELECTION).text in url or "%" in url for url in seen))
        self.assertEqual([], report.results)
        self.assertEqual(RELATED_SEARCH_CAUSE_NO_RESULT, report.cause_code)
        serialized = " ".join(
            [report.query, report.selection, report.strategy, report.cause_code]
            + [str(item) for item in report.results]
        )
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(BEARER_SECRET, serialized)
        self.assertNotIn(UPSTREAM_MARKER, serialized)


class RelatedSearchLogBoundaryTests(unittest.TestCase):
    """The retained engine's log/process boundary.

    ``redact_related_detail`` is documented as *the* boundary: the only form of
    a failure detail allowed to reach a server log or to cross the child
    process pipe on its way to one.  These tests drive that boundary through
    the real worker and the real bounded parent, and capture what a logger
    built from the transported text would actually emit.
    """

    def _log_sink(self) -> io.StringIO:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        logger = logging.getLogger("autoposting.background")
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, handler)
        return stream

    def test_log_boundary_never_carries_credentials_or_upstream_body(self) -> None:
        def fetcher(_url: str) -> bytes:
            raise ConnectionRefusedError("ddg: " + UPSTREAM_BODY)

        with self.assertRaises(RelatedSearchError) as raised:
            search_related_news(SELECTION, fetcher)
        exc = raised.exception

        stream = self._log_sink()
        logging.getLogger("autoposting.background").warning(
            "related search failed cause=%s detail=%s",
            exc.cause_code,
            redact_related_detail(exc.detail),
        )
        logged = stream.getvalue()

        self.assertIn("related search failed", logged)
        self.assertIn(exc.cause_code, logged)
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, exc.cause_code)
        for leaked in (SECRET, BEARER_SECRET, UPSTREAM_MARKER):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, logged)
                self.assertNotIn(leaked, exc.reason)
                self.assertNotIn(leaked, redact_related_detail(exc.detail))
        # Redaction stays diagnosable rather than blanking the line, and the
        # log line itself is bounded.
        self.assertIn("ConnectionRefusedError", logged)
        self.assertIn("token=***", logged)
        self.assertLessEqual(len(redact_related_detail(exc.detail)), 400)

    def _bounded_context(self, payload: object):
        receive_conn = MagicMock()
        receive_conn.poll.return_value = True
        receive_conn.recv.return_value = payload
        send_conn = MagicMock()
        process = MagicMock()
        process.is_alive.return_value = False
        context = MagicMock()
        context.Pipe.return_value = (receive_conn, send_conn)
        context.Process.return_value = process
        return context

    def test_process_boundary_never_carries_the_upstream_body(self) -> None:
        receive_conn, send_conn = multiprocessing.Pipe(duplex=False)
        self.addCleanup(receive_conn.close)
        failure = RelatedSearchError(
            RELATED_SEARCH_CAUSE_PARSE,
            detail="google: ValueError: " + UPSTREAM_BODY,
            sources=("google",),
        )

        # Real child worker: the only thing it can send a logging parent.
        with patch.object(background, "_search_related_news_impl", side_effect=failure):
            background._related_search_worker(send_conn, SELECTION, 8)
        status, payload = receive_conn.recv()

        self.assertEqual("error", status)
        self.assertEqual(RELATED_SEARCH_CAUSE_PARSE, payload["cause_code"])
        self.assertEqual(("google",), tuple(payload["sources"]))
        self.assertIn("ValueError", payload["detail"])
        self.assertLessEqual(len(payload["detail"]), 400)
        for leaked in (SECRET, BEARER_SECRET, UPSTREAM_MARKER):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, str(payload["detail"]))

        # Real bounded parent: whatever a caller logs or renders comes from here.
        with patch.object(
            background, "get_context", return_value=self._bounded_context(("error", payload))
        ):
            with self.assertRaises(RelatedSearchError) as raised:
                background._search_related_news_bounded(SELECTION, 8)

        exc = raised.exception
        self.assertEqual(RELATED_SEARCH_CAUSE_PARSE, exc.cause_code)
        self.assertEqual(("google",), exc.sources)
        for leaked in (SECRET, BEARER_SECRET, UPSTREAM_MARKER):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, exc.detail)
                self.assertNotIn(leaked, exc.reason)
        self.assertIn("解析失败", exc.reason)


class RelatedNewsSearchAdapterTests(unittest.TestCase):
    """``RelatedNewsSearchAdapter`` — the Research Service's search surface."""

    def test_successful_search_returns_non_degraded_response_with_candidates(self) -> None:
        response = RelatedNewsSearchAdapter(fetcher=_google_rss_fetcher).search(
            derive_related_query(SELECTION).text,
            adapters=("news", "web"),
            limit=1,
        )

        self.assertFalse(response.degraded)
        self.assertEqual("", response.cause_code)
        self.assertEqual((), response.diagnostics)
        self.assertEqual(ResearchStatus.OK, response.status)
        self.assertEqual(derive_related_query(SELECTION).text, response.query)
        self.assertEqual(("news", "web"), response.adapters)
        # limit is forwarded: the feed held two hits.
        self.assertEqual(1, len(response.results))
        self.assertEqual("https://news.a.example/one", response.results[0]["url"])

    def test_related_search_error_degrades_and_carries_its_cause_code(self) -> None:
        response = RelatedNewsSearchAdapter(fetcher=_unreachable_fetcher).search(
            SELECTION, adapters=("web",), limit=5
        )

        self.assertTrue(response.degraded)
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, response.cause_code)
        self.assertEqual((), response.results)
        self.assertEqual(ResearchStatus.DEGRADED, response.status)
        diagnostic = response.diagnostics[0]
        self.assertEqual("research_search_failed", diagnostic["code"])
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, diagnostic["cause_code"])
        self.assertTrue(diagnostic["degraded"])
        self.assertEqual("related_search", diagnostic["source"])
        self.assertEqual("research_search", diagnostic["stage"])

    def test_unexpected_exception_degrades_as_unknown_error(self) -> None:
        def searcher(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("boom")

        response = RelatedNewsSearchAdapter(searcher=searcher).search(
            SELECTION, adapters=("web",), limit=5
        )

        self.assertTrue(response.degraded)
        self.assertEqual("unknown_error", response.cause_code)
        self.assertEqual("unknown_error", response.diagnostics[0]["cause_code"])
        self.assertEqual((), response.results)

    def test_diagnostic_never_carries_upstream_url_credentials(self) -> None:
        def fetcher(_url: str) -> bytes:
            raise RuntimeError(UPSTREAM_URL_RESPONSE)

        response = RelatedNewsSearchAdapter(fetcher=fetcher).search(
            SELECTION, adapters=("web",), limit=5
        )

        diagnostic = response.diagnostics[0]
        detail = str(diagnostic["detail"])
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, diagnostic["cause_code"])
        self.assertNotIn(SECRET, detail)
        self.assertNotIn(UPSTREAM_URL_RESPONSE, detail)
        self.assertLessEqual(len(detail), 240)
        self.assertIn("RuntimeError", detail)

    def test_diagnostic_redacts_the_engine_boundary_not_just_urls(self) -> None:
        # This adapter is now the only consumer of the related-search engine, so
        # it is also the only place the engine's redaction boundary can run.
        # A bare scheme credential and an upstream body marker must be redacted
        # here, exactly as they were on the retired workbench path.
        def bare_credential(query: str, *, fetcher: object = None, limit: int = 5) -> object:
            raise RelatedSearchError(
                RELATED_SEARCH_CAUSE_TRANSPORT,
                detail="ddg: RuntimeError: upstream said Bearer sk-live-SECRET and SECRET-UPSTREAM-BODY",
            )

        response = RelatedNewsSearchAdapter(searcher=bare_credential).search(
            SELECTION, adapters=("web",), limit=5
        )
        detail = str(response.diagnostics[0]["detail"])
        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, response.diagnostics[0]["cause_code"])
        self.assertNotIn("sk-live-SECRET", detail)
        self.assertNotIn("SECRET-UPSTREAM-BODY", detail)
        # Redaction, not suppression: it is still triageable.
        self.assertIn("RuntimeError", detail)
        self.assertIn("***", detail)

    def test_unexpected_exception_detail_is_redacted_too(self) -> None:
        def boom(query: str, *, fetcher: object = None, limit: int = 5) -> object:
            raise RuntimeError("upstream said Bearer sk-live-SECRET and SECRET-UPSTREAM-BODY")

        response = RelatedNewsSearchAdapter(searcher=boom).search(
            SELECTION, adapters=("web",), limit=5
        )
        detail = str(response.diagnostics[0]["detail"])
        self.assertEqual("unknown_error", response.diagnostics[0]["cause_code"])
        self.assertNotIn("sk-live-SECRET", detail)
        self.assertNotIn("SECRET-UPSTREAM-BODY", detail)


if __name__ == "__main__":
    unittest.main()

class SourcePairingTests(unittest.TestCase):
    """A topic must pair each source with its own url, never a neighbour's.

    The radar payload already has this shape: ``source_ids`` is a sorted set
    while the urls live in the ``sources`` records.  Zipping those two lists
    attributed one source's url to another, and the article then linked its
    sentences to the wrong page.
    """

    EVENT = {
        "event_id": "cluster-1",
        "title": "Introducing GPT-6 Sol and Luna",
        "source_ids": ["hn_frontpage", "openai_news", "reddit_openai"],
        "sources": [
            {"source_id": "openai_news", "url": "https://openai.com/index/gpt-6"},
            {"source_id": "reddit_openai", "url": "https://www.reddit.com/r/OpenAI/comments/1wngyux/"},
            {"source_id": "hn_frontpage", "url": "https://news.ycombinator.com/item?id=43123456"},
        ],
        "item_ids": ["item-1"],
    }

    def test_records_decide_the_pairing_even_when_ids_are_sorted(self) -> None:
        topic = ResearchTopic.from_mapping(self.EVENT)
        self.assertEqual(len(topic.source_ids), len(topic.source_urls))
        self.assertEqual(
            {
                "openai_news": "https://openai.com/index/gpt-6",
                "reddit_openai": "https://www.reddit.com/r/OpenAI/comments/1wngyux/",
                "hn_frontpage": "https://news.ycombinator.com/item?id=43123456",
            },
            dict(zip(topic.source_ids, topic.source_urls)),
        )

    def test_an_id_without_a_record_keeps_a_blank_slot(self) -> None:
        topic = ResearchTopic.from_mapping(
            {
                "event_id": "e",
                "title": "t",
                "source_ids": ["openai_news", "ghost_feed"],
                "sources": [{"source_id": "openai_news", "url": "https://openai.com/index/gpt-6"}],
            }
        )
        self.assertEqual(("openai_news", "ghost_feed"), topic.source_ids)
        self.assertEqual(("https://openai.com/index/gpt-6", ""), topic.source_urls)

    def test_a_short_url_list_is_never_zipped_onto_a_longer_id_list(self) -> None:
        topic = ResearchTopic.from_mapping(
            {
                "event_id": "e",
                "title": "t",
                "source_ids": ["a", "b", "c"],
                "source_urls": ["https://a.example/1", "https://b.example/2"],
            }
        )
        self.assertEqual(("", "", ""), topic.source_urls)

    def test_the_radar_event_publishes_an_aligned_url_list(self) -> None:
        candidate = {
            "item_id": "item-1",
            "title": "Introducing GPT-6 Sol and Luna",
            "content_text": "GPT-6 Sol and Luna 发布。",
            "observed_at": "2026-09-23T02:00:00+00:00",
            "sources": [
                {"source_id": "openai_news", "name": "OpenAI News", "url": "https://openai.com/index/gpt-6", "tier": "S1"},
                {"source_id": "hn_frontpage", "name": "Hacker News", "url": "https://news.ycombinator.com/item?id=1", "tier": "S4"},
            ],
            "source_ids": ["openai_news", "hn_frontpage"],
            "source_urls": ["https://openai.com/index/gpt-6", "https://news.ycombinator.com/item?id=1"],
        }
        events = cluster_candidates([candidate])
        self.assertTrue(events)
        event = events[0]
        # The two lists are index-for-index aligned, so a positional reader can
        # never attribute one source's url to another one.
        self.assertEqual(len(event["source_ids"]), len(event["source_urls"]))
        for source_id, url in zip(event["source_ids"], event["source_urls"]):
            self.assertTrue(url, f"{source_id} lost its url")
        self.assertEqual(
            {"openai_news": "https://openai.com/index/gpt-6",
             "hn_frontpage": "https://news.ycombinator.com/item?id=1"},
            dict(zip(event["source_ids"], event["source_urls"])),
        )
        topic = ResearchTopic.from_mapping(event)
        self.assertEqual(len(topic.source_ids), len(topic.source_urls))
        for source_id, url in zip(topic.source_ids, topic.source_urls):
            self.assertTrue(url, f"{source_id} lost its url")
        # A sorted id list must not be paired with a differently ordered url
        # list: sources whose sort order differs from insertion order would flip.
        reordered = {
            **candidate,
            "sources": [
                {"source_id": "zeta_feed", "name": "Zeta", "url": "https://zeta.example/post", "tier": "S3"},
                {"source_id": "alpha_feed", "name": "Alpha", "url": "https://alpha.example/post", "tier": "S3"},
            ],
            "source_ids": ["zeta_feed", "alpha_feed"],
            "source_urls": ["https://zeta.example/post", "https://alpha.example/post"],
        }
        [reordered_event] = cluster_candidates([reordered])
        pairs = dict(zip(reordered_event["source_ids"], reordered_event["source_urls"]))
        self.assertEqual("https://zeta.example/post", pairs["zeta_feed"])
        self.assertEqual("https://alpha.example/post", pairs["alpha_feed"])
