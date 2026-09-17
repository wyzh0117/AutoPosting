import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import quote_plus

from autoposting.background import (
    RELATED_SEARCH_CAUSE_NO_RESULT,
    RELATED_SEARCH_CAUSE_PARSE,
    RELATED_SEARCH_CAUSE_TIMEOUT,
    RELATED_SEARCH_CAUSE_TRANSPORT,
    RelatedSearchError,
    _parse_ddg_json,
    _parse_wikipedia,
    article_page_text,
    derive_related_query,
    related_article_context,
    related_search_reason,
    gather_background,
    infer_focus,
    parse_search_html,
    search_local,
    search_query,
    search_related_news,
    search_web,
)
import autoposting.background as background
from autoposting.db import Database
from autoposting.models import FeedItem, Source

# A real 200+ character Chinese selection.  Sending this verbatim used to time
# the whole search out; it must degrade to a short leading clause instead.
LONG_CHINESE_SELECTION = (
    "英伟达在大会上发布了新一代AI芯片，这款芯片采用了全新的架构设计，"
    "在推理性能上比上一代产品提升了两倍以上。分析师认为，随着数据中心对算力的需求持续增长，"
    "这款芯片预计将推动公司下一季度的营收增长。与此同时，竞争对手也在加快自研芯片的节奏，"
    "整个行业的竞争格局正在发生变化，供应链厂商同样受到关注。业内普遍预计，"
    "未来几个季度相关产品的出货量还会继续攀升，下游客户也在调整采购计划，"
    "多家机构上调了行业增速预期，但同时也提醒投资者关注供需波动带来的风险。"
)


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

    def test_related_article_context_is_limited_to_adjacent_paragraphs(self) -> None:
        body = "第一段。\n\n第二段包含选中内容。\n\n第三段。\n\n第四段。"
        selection = "选中内容"
        start = body.index(selection)
        context = related_article_context(body, selection, start, start + len(selection))
        self.assertEqual("第一段。\n\n第二段包含选中内容。\n\n第三段。", context)
        self.assertNotIn("第四段", context)

    def test_related_article_context_keeps_first_and_last_selection_paragraphs(self) -> None:
        body = "零。\n\n一。\n\n二。\n\n三。\n\n四。\n\n五。"
        start = body.index("一")
        end = body.index("四") + len("四")
        context = related_article_context(body, body[start:end], start, end)
        self.assertEqual("零。\n\n一。\n\n二。\n\n三。\n\n四。\n\n五。", context)

    def test_related_article_context_crosses_many_paragraphs_without_extra_window(self) -> None:
        body = "零。\n\n一。\n\n二。\n\n三。\n\n四。\n\n五。\n\n六。\n\n七。"
        start = body.index("一")
        end = body.index("六")
        context = related_article_context(body, body[start:end], start, end)
        self.assertIn("一。", context)
        self.assertIn("五。", context)
        self.assertIn("零。", context)
        self.assertIn("六。", context)
        self.assertNotIn("七。", context)

    def test_related_article_context_supports_crlf_paragraphs(self) -> None:
        body = "第一段。\r\n\r\n第二段包含选中内容。\r\n\r\n第三段。\r\n\r\n第四段。"
        selection = "选中内容"
        start = body.index(selection)
        context = related_article_context(body, selection, start, start + len(selection))
        self.assertIn("第一段。", context)
        self.assertIn("第二段包含选中内容。", context)
        self.assertIn("第三段。", context)
        self.assertNotIn("第四段。", context)

    def test_related_article_context_rejects_offset_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            related_article_context("第一段。", "伪造", 0, 2)

    def test_related_article_context_accepts_trimmed_browser_selection(self) -> None:
        body = "第一段。\n\n  选中内容  \n\n第三段。"
        raw_start = body.index("  选中内容")
        raw_end = raw_start + len("  选中内容  ")
        self.assertIn(
            "选中内容",
            related_article_context(body, "选中内容", raw_start, raw_end),
        )

    def test_related_article_context_accepts_browser_utf16_offsets(self) -> None:
        body = "前置😀。\n\n选中内容。\n\n后置。"
        start = len(body[: body.index("选中内容")].encode("utf-16-le")) // 2
        end = start + len("选中内容。".encode("utf-16-le")) // 2
        self.assertIn(
            "选中内容",
            related_article_context(body, "选中内容。", start, end),
        )

    def test_article_page_text_prefers_article_and_removes_page_chrome(self) -> None:
        paragraph = "The report explains the model release and its measured results. " * 5
        html = (
            "<html><header><meta name='theme'>site header</header>"
            "<nav><img src='logo.png'>navigation</nav><main>"
            f"<h1>Release report</h1><p>{paragraph}</p>"
            "<script>ignore()</script></main><footer>footer links</footer></html>"
        ).encode()

        text = article_page_text(html)

        self.assertIn("Release report", text)
        self.assertIn("measured results", text)
        self.assertNotIn("navigation", text)
        self.assertNotIn("ignore", text)

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

    def test_search_related_news_uses_selection_and_filters_social_first_result(self) -> None:
        html = (
            '<a class="result__a" href="https://x.com/post">OpenAI rogue agent swarm</a>'
            '<a class="result__a" href="https://news.example/report?utm_source=rss#top">'
            "OpenAI rogue agent swarm report</a>"
            '<div class="result__snippet">HuggingFace incident</div>'
        ).encode()

        def fetcher(url: str) -> bytes:
            return html if "duckduckgo.com/html" in url else b""

        query, results = search_related_news("OpenAI 的“rogue agent swarm”黑进 HuggingFace。", fetcher)
        self.assertIn("rogue agent swarm", query)
        self.assertEqual("https://news.example/report", results[0]["url"])

    def test_related_search_fetcher_uses_explicit_no_proxy_opener(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit):
                self.limit = limit
                return b"payload"

        response = Response()
        with patch.object(background._RELATED_SEARCH_OPENER, "open", return_value=response) as open_call:
            payload = background._related_search_fetcher("https://example.com", timeout=1.25)

        self.assertEqual(b"payload", payload)
        open_call.assert_called_once()
        self.assertEqual(1.25, open_call.call_args.kwargs["timeout"])
        self.assertEqual(400_000, response.limit)

    def test_related_search_fetcher_uses_native_macos_fallback_without_interpolation(self) -> None:
        native_rss = b"""<rss><channel><item><title>Native result</title>
        <link>https://publisher.example/native</link></item></channel></rss>"""
        malicious_url = "https://news.google.com/rss/search?q=$(touch /tmp/should-not-run);echo pwned"
        completed = subprocess.CompletedProcess([], 0, stdout=native_rss, stderr=b"")
        with (
            patch.object(background._RELATED_SEARCH_OPENER, "open", side_effect=OSError("urllib timeout")),
            patch.object(background.sys, "platform", "darwin"),
            patch.object(background.subprocess, "run", return_value=completed) as run_call,
        ):
            payload = background._related_search_fetcher(malicious_url, timeout=1.25)

        self.assertEqual(native_rss, payload)
        command = run_call.call_args.args[0]
        self.assertEqual(malicious_url, command[-1])
        self.assertNotIn(malicious_url, command[4])
        self.assertFalse(run_call.call_args.kwargs["shell"])
        self.assertEqual(1.25, run_call.call_args.kwargs["timeout"])

    def test_related_search_fetcher_prefers_native_for_google_rss_on_macos(self) -> None:
        with (
            patch.object(background.sys, "platform", "darwin"),
            patch.object(background, "_related_search_native_fetcher", return_value=b"native") as native,
            patch.object(background._RELATED_SEARCH_OPENER, "open") as opener,
        ):
            payload = background._related_search_fetcher("https://news.google.com/rss/search?q=test", timeout=2)

        self.assertEqual(b"native", payload)
        native.assert_called_once_with("https://news.google.com/rss/search?q=test", 2)
        opener.assert_not_called()

    def test_search_related_news_uses_the_derived_query_for_every_source(self) -> None:
        rss = b"""<rss><channel><item>
        <title>OpenAI rogue agent swarm report</title>
        <link>https://publisher.example/rogue-agent-swarm</link>
        <description>HuggingFace incident</description>
        </item></channel></rss>"""
        selection = "OpenAI 的一个“失控智能体集群”（rogue agent swarm）黑进了 HuggingFace 和一个德国网站。"
        expected_query = derive_related_query(selection).text
        seen: list[str] = []

        def fetcher(url: str) -> bytes:
            seen.append(url)
            return rss

        report = search_related_news(selection, fetcher)

        self.assertEqual(expected_query, report.query)
        self.assertNotEqual(selection, report.query)
        self.assertLessEqual(len(report.query), 48)
        self.assertTrue(seen)
        for url in seen:
            self.assertIn(quote_plus(expected_query), url)
        self.assertEqual("https://publisher.example/rogue-agent-swarm", report.results[0]["url"])

    def test_search_related_news_moves_on_when_a_source_answers_nothing(self) -> None:
        rss = b"""<rss><channel><item><title>OpenAI agent swarm news</title>
        <link>https://news.example/a?utm_medium=rss</link><description>HuggingFace</description>
        </item></channel></rss>"""
        seen: list[str] = []

        def fetcher(url: str) -> bytes:
            seen.append(url)
            return b"" if "bing.com/search" in url else rss

        report = search_related_news("“OpenAI agent swarm。”", fetcher)

        self.assertEqual("OpenAI agent swarm", report.query)
        self.assertEqual("https://news.example/a", report.results[0]["url"])
        self.assertGreaterEqual(len(seen), 2)
        self.assertIn("bing.com/search", seen[0])

    def test_search_related_news_unwraps_real_google_news_rss_article_token(self) -> None:
        # Google News uses a base64url protobuf token in the RSS link.  This
        # shape decodes to a publisher URL and must not be mistaken for a
        # search-host result.
        rss = b"""<rss><channel><item>
        <title>OpenAI rogue agent swarm report</title>
        <link>https://news.google.com/rss/articles/CBMiaHR0cHM6Ly93d3cudGhldmVyZ2UuY29tL2FpLWFydGlmaWNpYWwtaW50ZWxsaWdlbmNlL3JvZ3VlLWFnZW50LXN3YXJtLXJlcG9ydA?oc=5</link>
        <description>HuggingFace incident</description>
        </item></channel></rss>"""

        def fetcher(url: str) -> bytes:
            return rss if "news.google.com/rss" in url else b""

        # Google News is not in the shipped source list any more, but its
        # article-token unwrapping must keep working if it is re-added.
        google = (
            "bing",
            "Bing",
            "https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en",
        )
        with patch.object(background, "_RELATED_SEARCH_SOURCES", (google,)):
            report = search_related_news("OpenAI rogue agent swarm", fetcher)
        self.assertEqual(
            "https://www.theverge.com/ai-artificial-intelligence/rogue-agent-swarm-report",
            report.results[0]["url"],
        )

    def _bounded_context(self, payload: object, alive: bool = False):
        receive_conn = MagicMock()
        receive_conn.poll.return_value = True
        receive_conn.recv.return_value = payload
        send_conn = MagicMock()
        process = MagicMock()
        process.is_alive.return_value = alive
        context = MagicMock()
        context.Pipe.return_value = (receive_conn, send_conn)
        context.Process.return_value = process
        return context, receive_conn, process

    def _bounded_report(self, **overrides: object) -> dict[str, object]:
        report: dict[str, object] = {
            "query": "query",
            "results": [],
            "selection": "query",
            "strategy": "keywords",
            "degraded": False,
            "cause_code": RELATED_SEARCH_CAUSE_NO_RESULT,
        }
        report.update(overrides)
        return report

    def test_bounded_related_search_prefers_spawn_for_threaded_callers(self) -> None:
        context, _receive_conn, process = self._bounded_context(
            ("ok", self._bounded_report())
        )

        with (
            patch.object(background, "get_all_start_methods", return_value=["fork", "spawn"]),
            patch.object(background, "get_context", return_value=context) as get_context,
        ):
            result = background._search_related_news_bounded("query")

        self.assertEqual("query", result.query)
        self.assertEqual(RELATED_SEARCH_CAUSE_NO_RESULT, result.cause_code)
        get_context.assert_called_once_with("spawn")
        process.start.assert_called_once_with()

    def test_bounded_related_search_propagates_graded_cause(self) -> None:
        context, _receive_conn, _process = self._bounded_context(
            (
                "error",
                {"cause_code": "timeout", "detail": "child exceeded", "sources": ["bing_cn"]},
            )
        )

        with (
            patch.object(background, "get_all_start_methods", return_value=["spawn"]),
            patch.object(background, "get_context", return_value=context),
        ):
            with self.assertRaises(RelatedSearchError) as raised:
                background._search_related_news_bounded("query")

        self.assertEqual(RELATED_SEARCH_CAUSE_TIMEOUT, raised.exception.cause_code)
        self.assertEqual(("bing_cn",), raised.exception.sources)

    def test_bounded_related_search_terminates_child_past_parent_timeout(self) -> None:
        context, _receive_conn, process = self._bounded_context(self._bounded_report(), alive=True)

        with (
            patch.object(background, "get_all_start_methods", return_value=["spawn"]),
            patch.object(background, "get_context", return_value=context),
        ):
            with self.assertRaises(RelatedSearchError) as raised:
                background._search_related_news_bounded("query")

        self.assertEqual(RELATED_SEARCH_CAUSE_TIMEOUT, raised.exception.cause_code)
        process.terminate.assert_called_once_with()

    def test_bounded_related_search_grades_a_child_that_returns_nothing(self) -> None:
        context, receive_conn, _process = self._bounded_context(("ok", {}))
        receive_conn.poll.return_value = False

        with (
            patch.object(background, "get_all_start_methods", return_value=["spawn"]),
            patch.object(background, "get_context", return_value=context),
        ):
            with self.assertRaises(RelatedSearchError) as raised:
                background._search_related_news_bounded("query")

        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, raised.exception.cause_code)

    def test_related_search_parent_timeout_leaves_margin_over_child_budget(self) -> None:
        # Regression: the parent used to join with exactly the child budget,
        # so the child was killed before it could report its own graded result.
        self.assertGreater(
            background._RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS,
            background._RELATED_SEARCH_BUDGET_SECONDS,
        )
        self.assertGreaterEqual(
            background._RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS
            - background._RELATED_SEARCH_BUDGET_SECONDS,
            5.0,
        )
        worst_case_attempts = (
            background._RELATED_SEARCH_MAX_ATTEMPTS
            * background._RELATED_SEARCH_REQUEST_TIMEOUT
        )
        self.assertLess(worst_case_attempts, background._RELATED_SEARCH_BUDGET_SECONDS)
        # Still acceptable for an interactive button.
        self.assertLessEqual(background._RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS, 35.0)

    def test_derive_related_query_keeps_chinese_keywords(self) -> None:
        chip = derive_related_query("英伟达发布新一代AI芯片")

        self.assertIn("英伟达", chip.text)
        self.assertIn("芯片", chip.text)
        self.assertIn("AI", chip.text)
        self.assertNotIn("发布", chip.text)
        self.assertLessEqual(len(chip.text), 48)
        self.assertFalse(chip.degraded)
        self.assertEqual("英伟达发布新一代AI芯片", chip.selection)

        skill = derive_related_query("视频制作 skill 推荐")

        self.assertIn("视频制作", skill.text)
        self.assertIn("skill", skill.text)
        self.assertNotEqual("skill", skill.text)

    def test_derive_related_query_bounds_mid_length_chinese_prose(self) -> None:
        paragraph = (
            "英伟达在大会上发布了新一代AI芯片，这款芯片采用了全新的架构设计，"
            "在推理性能上比上一代产品提升了两倍以上。分析师认为，随着数据中心对算力的需求持续增长，"
            "这款芯片预计将推动公司下一季度的营收增长。与此同时，竞争对手也在加快自研芯片的节奏，"
            "整个行业的竞争格局正在发生变化，供应链厂商同样受到关注。"
        )

        derived = derive_related_query(paragraph)

        self.assertLessEqual(len(derived.text), 48)
        self.assertNotEqual(paragraph, derived.text)
        self.assertIn("芯片", derived.text)
        self.assertFalse(derived.degraded)

    def test_derive_related_query_degrades_long_chinese_paragraph(self) -> None:
        derived = derive_related_query(LONG_CHINESE_SELECTION)

        self.assertGreater(len(LONG_CHINESE_SELECTION), 200)
        self.assertTrue(derived.degraded)
        self.assertEqual("degraded_leading_clause", derived.strategy)
        self.assertLessEqual(len(derived.text), 24)
        self.assertNotEqual(LONG_CHINESE_SELECTION, derived.text)
        self.assertIn("英伟达", derived.text)
        self.assertEqual(LONG_CHINESE_SELECTION, derived.selection)

    def test_derive_related_query_keeps_short_english_selection(self) -> None:
        self.assertEqual(
            "OpenAI rogue agent swarm", derive_related_query("OpenAI rogue agent swarm").text
        )
        self.assertEqual(
            "OpenAI agent swarm", derive_related_query("“OpenAI agent swarm。”").text
        )
        self.assertEqual(
            "DeepSeek Harness", derive_related_query("the DeepSeek Harness").text
        )
        self.assertEqual("选中关键词", derive_related_query("选中关键词").text)

    def test_derive_related_query_falls_back_for_stopword_only_selection(self) -> None:
        self.assertEqual("今日新闻", derive_related_query("今日新闻").text)
        self.assertEqual("", derive_related_query("   ").text)

    def test_related_search_grades_unreachable_sources(self) -> None:
        def fetcher(_url: str) -> bytes:
            raise OSError("dns lookup failed")

        with self.assertRaises(RelatedSearchError) as raised:
            search_related_news("英伟达 AI 芯片", fetcher)

        self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, raised.exception.cause_code)
        self.assertIn("网络不可达", raised.exception.reason)
        self.assertTrue(raised.exception.sources)

    def test_related_search_grades_parse_failure(self) -> None:
        with self.assertRaises(RelatedSearchError) as raised:
            search_related_news("英伟达 AI 芯片", lambda _url: b'{"error": "rate limited"}')

        self.assertEqual(RELATED_SEARCH_CAUSE_PARSE, raised.exception.cause_code)
        self.assertIn("解析失败", raised.exception.reason)

    def test_related_search_grades_budget_timeout(self) -> None:
        def slow(_url: str) -> bytes:
            time.sleep(0.2)
            return b"<rss><channel /></rss>"

        with patch.object(background, "_RELATED_SEARCH_BUDGET_SECONDS", 0.05):
            with self.assertRaises(RelatedSearchError) as raised:
                search_related_news("英伟达 AI 芯片", slow)

        self.assertEqual(RELATED_SEARCH_CAUSE_TIMEOUT, raised.exception.cause_code)
        self.assertIn("检索超时", raised.exception.reason)

    def test_related_search_reports_reachable_but_empty(self) -> None:
        empty_feed = b"<rss><channel><title>no items</title></channel></rss>"

        report = search_related_news("英伟达 AI 芯片", lambda _url: empty_feed)

        self.assertEqual(RELATED_SEARCH_CAUSE_NO_RESULT, report.cause_code)
        self.assertEqual([], report.results)
        query, results = report  # existing callers unpack a 2-tuple
        self.assertEqual(report.query, query)
        self.assertEqual([], results)

    def test_related_search_reason_never_embeds_upstream_detail(self) -> None:
        secret = "SECRET-UPSTREAM-PAYLOAD"

        def fetcher(_url: str) -> bytes:
            raise OSError(secret)

        with self.assertRaises(RelatedSearchError) as raised:
            search_related_news("英伟达 AI 芯片", fetcher)

        self.assertNotIn(secret, raised.exception.reason)
        self.assertIn(secret, raised.exception.detail)
        for cause in (
            RELATED_SEARCH_CAUSE_TIMEOUT,
            RELATED_SEARCH_CAUSE_TRANSPORT,
            RELATED_SEARCH_CAUSE_PARSE,
            RELATED_SEARCH_CAUSE_NO_RESULT,
        ):
            self.assertTrue(related_search_reason(cause).strip())

    def test_related_search_redacts_credentials_from_log_detail(self) -> None:
        detail = "URLError: <urlopen error http://user:pass@proxy.example:8080 refused>"

        redacted = background.redact_related_detail(detail)

        self.assertNotIn("user:pass", redacted)
        self.assertIn("proxy.example", redacted)

    # --- log redaction boundary -------------------------------------------
    #
    # ``redact_related_detail`` is the only thing allowed to turn upstream
    # failure text into log text, so the table below pins every credential
    # shape while also pinning that the line stays diagnosable.

    REDACTION_SECRET = "SUPERSECRETVALUE"

    def test_redact_related_detail_table_removes_credential_shapes(self) -> None:
        secret = self.REDACTION_SECRET
        # (label, detail, secret that must not survive, text that must survive)
        cases = (
            (
                "token query",
                f"ddg: OSError: GET https://api.example/v1?token={secret} failed",
                secret,
                ("ddg: OSError", "api.example", "token="),
            ),
            (
                "access_token upper-case",
                f"upstream 401 at https://api.example/?ACCESS_TOKEN={secret}",
                secret,
                ("upstream 401", "ACCESS_TOKEN="),
            ),
            (
                "api_key",
                f"https://api.example/v1?api_key={secret}&page=1",
                secret,
                ("api_key=", "page=1"),
            ),
            ("apikey", f"https://api.example/v1?apikey={secret}", secret, ("apikey=",)),
            ("key", f"https://api.example/v1?key={secret}", secret, ("key=",)),
            ("secret", f"https://api.example/v1?secret={secret}", secret, ("secret=",)),
            (
                "client_secret",
                f"https://api.example/v1?client_secret={secret}",
                secret,
                ("client_secret=",),
            ),
            (
                "password",
                f"https://api.example/login?password={secret}",
                secret,
                ("password=",),
            ),
            ("passwd", f"https://api.example/?passwd={secret}", secret, ("passwd=",)),
            ("pwd", f"https://api.example/?pwd={secret}", secret, ("pwd=",)),
            ("sig", f"https://api.example/?sig={secret}", secret, ("sig=",)),
            (
                "signature",
                f"https://api.example/?signature={secret}",
                secret,
                ("signature=",),
            ),
            ("auth", f"https://api.example/?auth={secret}", secret, ("auth=",)),
            ("code", f"https://api.example/oauth?code={secret}", secret, ("code=",)),
            (
                "prefixed name",
                f"https://api.example/?auth_token={secret}",
                secret,
                ("auth_token=",),
            ),
            (
                "authorization header",
                f"Authorization: Bearer {secret}",
                secret,
                ("Authorization:", "Bearer"),
            ),
            (
                "bare bearer scheme",
                f"upstream rejected the request: Bearer {secret}",
                secret,
                ("upstream rejected", "Bearer"),
            ),
            (
                "json body",
                '{"token": "%s", "ok": false}' % secret,
                secret,
                ("ok", "false"),
            ),
            (
                "url userinfo",
                f"http://user:{secret}@proxy.example:8080 refused",
                secret,
                ("proxy.example:8080", "refused"),
            ),
            (
                "bare hex token",
                "upstream returned 0123456789abcdef0123456789abcdef for the request",
                "0123456789abcdef0123456789abcdef",
                ("upstream returned", "for the request"),
            ),
            (
                "credential marker run",
                "upstream said SECRET-UPSTREAM-BODY and closed",
                "SECRET-UPSTREAM-BODY",
                ("upstream said", "and closed"),
            ),
            (
                "vendor key",
                "AKIAIOSFODNN7EXAMPLE was rejected",
                "AKIAIOSFODNN7EXAMPLE",
                ("was rejected",),
            ),
        )
        # The URL-userinfo rule deletes the credential outright, so that row is
        # the one shape without a *** placeholder.
        no_placeholder = {"url userinfo"}
        for label, detail, must_vanish, must_survive in cases:
            with self.subTest(label):
                redacted = background.redact_related_detail(detail)
                self.assertNotIn(must_vanish, redacted)
                if label not in no_placeholder:
                    self.assertIn("***", redacted)
                self.assertLessEqual(len(redacted), 400)
                for needle in must_survive:
                    self.assertIn(needle, redacted)

    def test_redact_related_detail_removes_reviewer_repro_string(self) -> None:
        # Reproduced verbatim from the verifier's server-log line.
        detail = (
            "ddg: ConnectionRefusedError: SECRET-UPSTREAM-BODY-"
            "http://internal.example/token=abc"
        )

        redacted = background.redact_related_detail(detail)

        self.assertNotIn("SECRET-UPSTREAM-BODY", redacted)
        self.assertNotIn("abc", redacted)
        self.assertIn("ddg: ConnectionRefusedError", redacted)
        self.assertIn("internal.example", redacted)
        self.assertIn("token=***", redacted)

    def test_redact_related_detail_keeps_ordinary_diagnostics_intact(self) -> None:
        for detail in (
            "ddg: ConnectionRefusedError: connection refused",
            "so.com: empty response",
            "child exceeded the 12s process timeout (child budget 8s)",
            "https://html.duckduckgo.com/html/?q=%E8%8B%B1%E4%BC%9F%E8%BE%BE",
            "author=someone&keyword=chips&zipcode=100000",
        ):
            with self.subTest(detail):
                self.assertEqual(detail, background.redact_related_detail(detail))

    def test_redact_related_detail_is_total_on_weird_input(self) -> None:
        weird = (
            None,
            "",
            "   ",
            "详情没有 URL，也没有凭据",
            "no-scheme.example/path?token=" + self.REDACTION_SECRET,
            f"http://a.example/?token={self.REDACTION_SECRET} "
            f"http://b.example/?key={self.REDACTION_SECRET}",
            b"bytes detail",
            12345,
            ["list"],
            {"dict": 1},
            "x" * 5000,
            "https://@host/",
            "token=",
            "\x00\x01 control chars",
        )
        for value in weird:
            with self.subTest(value=repr(value)[:40]):
                redacted = background.redact_related_detail(value)
                self.assertIsInstance(redacted, str)
                self.assertLessEqual(len(redacted), 400)
                self.assertNotIn(self.REDACTION_SECRET, redacted)

    def test_redact_related_detail_bounds_length_without_cutting_a_secret(self) -> None:
        detail = "prefix " + f"?token={self.REDACTION_SECRET}&" * 40

        redacted = background.redact_related_detail(detail)

        self.assertLessEqual(len(redacted), 400)
        self.assertNotIn(self.REDACTION_SECRET, redacted)

    def test_related_search_worker_redacts_detail_before_it_crosses_the_pipe(self) -> None:
        secret = self.REDACTION_SECRET
        detail = (
            "ddg: ConnectionRefusedError: SECRET-UPSTREAM-BODY-"
            f"http://internal.example/token={secret}"
        )
        send_conn = MagicMock()

        with patch.object(
            background,
            "_search_related_news_impl",
            side_effect=RelatedSearchError(
                RELATED_SEARCH_CAUSE_TRANSPORT, detail=detail, sources=("ddg",)
            ),
        ):
            background._related_search_worker(send_conn, "英伟达 AI 芯片", 1)

        status, payload = send_conn.send.call_args.args[0]
        self.assertEqual("error", status)
        self.assertNotIn(secret, payload["detail"])
        self.assertNotIn("SECRET-UPSTREAM-BODY", payload["detail"])
        self.assertIn("token=***", payload["detail"])

    def test_search_related_news_unwraps_bing_news_redirect(self) -> None:
        rss = b"""<rss><channel><item><title>\xe8\x8b\xb1\xe4\xbc\x9f\xe8\xbe\xbe AI \xe8\x8a\xaf\xe7\x89\x87\xe6\x96\xb0\xe8\xbf\x9b\xe5\xb1\x95</title>
        <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fnews.example%2fai-chip&amp;mkt=zh-cn</link>
        <description>\xe8\x8b\xb1\xe4\xbc\x9f\xe8\xbe\xbe AI \xe8\x8a\xaf\xe7\x89\x87\xe6\x8a\xa5\xe9\x81\x93</description>
        </item></channel></rss>"""

        report = search_related_news("英伟达 AI 芯片", lambda _url: rss)

        self.assertEqual("https://news.example/ai-chip", report.results[0]["url"])


NETWORK_TESTS = bool(os.environ.get("AUTOPOSTING_NETWORK_TESTS"))


@unittest.skipUnless(
    NETWORK_TESTS,
    "real-network smoke test; run with AUTOPOSTING_NETWORK_TESTS=1",
)
class RelatedSearchNetworkTests(unittest.TestCase):
    """One opt-in smoke test over the real wire: hit / unreachable / timeout.

    Skipped by default so the suite stays offline.  Run it manually with
    ``AUTOPOSTING_NETWORK_TESTS=1 PYTHONPATH=src:tests python3 -m unittest \\
    tests.test_background.RelatedSearchNetworkTests -v``.
    """

    SELECTION = "英伟达发布新一代AI芯片"

    def test_real_network_smoke(self) -> None:
        with self.subTest("normal hit"):
            started = time.monotonic()
            report = search_related_news(self.SELECTION)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, background._RELATED_SEARCH_PROCESS_TIMEOUT_SECONDS)
            self.assertEqual("", report.cause_code)
            self.assertTrue(report.results, "expected at least one qualified result")
            self.assertTrue(report.results[0]["title"])
            self.assertTrue(report.results[0]["url"].startswith(("http://", "https://")))

        with self.subTest("all sources unreachable"):
            unreachable = tuple(
                (name, label, "https://related-search-does-not-exist.invalid/rss?q={encoded}")
                for name, label, _template in background._RELATED_SEARCH_SOURCES
            )
            with patch.object(background, "_RELATED_SEARCH_SOURCES", unreachable):
                with self.assertRaises(RelatedSearchError) as raised:
                    background._search_related_news_impl(self.SELECTION, None, limit=1)
            self.assertEqual(RELATED_SEARCH_CAUSE_TRANSPORT, raised.exception.cause_code)

        with self.subTest("budget timeout"):
            # 10.255.255.1:81 drops SYNs, so each real attempt burns its socket
            # timeout and the second attempt sees an expired budget.
            blackhole = tuple(
                (name, label, "http://10.255.255.1:81/search?format=rss&q={encoded}")
                for name, label, _template in background._RELATED_SEARCH_SOURCES
            )
            with (
                patch.object(background, "_RELATED_SEARCH_SOURCES", blackhole),
                patch.object(
                    background,
                    "_RELATED_SEARCH_RSS_SOURCES",
                    frozenset(name for name, _label, _template in blackhole),
                ),
                patch.object(background, "_RELATED_SEARCH_BUDGET_SECONDS", 0.4),
                patch.object(background, "_RELATED_SEARCH_REQUEST_TIMEOUT", 0.3),
            ):
                with self.assertRaises(RelatedSearchError) as raised:
                    background._search_related_news_impl(self.SELECTION, None, limit=1)
            self.assertEqual(RELATED_SEARCH_CAUSE_TIMEOUT, raised.exception.cause_code)
