import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from autoposting.background import (
    _parse_ddg_json,
    _parse_wikipedia,
    article_page_text,
    related_article_context,
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
        malicious_url = "https://example.com/?q=$(touch /tmp/should-not-run);echo pwned"
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

    def test_search_related_news_falls_back_to_rss_and_cleans_wrappers(self) -> None:
        rss = b"""<rss><channel><item><title>OpenAI agent swarm news</title>
        <link>https://news.example/a?utm_medium=rss</link><description>HuggingFace</description>
        </item></channel></rss>"""

        def fetcher(url: str) -> bytes:
            return rss if "news.google.com/rss" in url else b""

        query, results = search_related_news("“OpenAI agent swarm。”", fetcher)
        self.assertEqual("“OpenAI agent swarm。”", query)
        self.assertEqual("https://news.example/a", results[0]["url"])

    def test_search_related_news_compresses_empty_exact_google_query(self) -> None:
        rss = b"""<rss><channel><item>
        <title>OpenAI rogue agent swarm report</title>
        <link>https://publisher.example/rogue-agent-swarm</link>
        <description>HuggingFace incident</description>
        </item></channel></rss>"""

        def fetcher(url: str) -> bytes:
            if "news.google.com/rss" not in url:
                return b""
            return rss if "OpenAI+rogue+agent+swarm+HuggingFace" in url else b"<rss><channel /></rss>"

        query, results = search_related_news(
            "OpenAI 的一个“失控智能体集群”（rogue agent swarm）黑进了 HuggingFace 和一个德国网站。",
            fetcher,
        )
        self.assertEqual("OpenAI rogue agent swarm HuggingFace", query)
        self.assertEqual("https://publisher.example/rogue-agent-swarm", results[0]["url"])

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

        _, results = search_related_news("OpenAI rogue agent swarm", fetcher)
        self.assertEqual(
            "https://www.theverge.com/ai-artificial-intelligence/rogue-agent-swarm-report",
            results[0]["url"],
        )

    def test_bounded_related_search_prefers_spawn_for_threaded_callers(self) -> None:
        receive_conn = MagicMock()
        receive_conn.poll.return_value = True
        receive_conn.recv.return_value = ("ok", ("query", []))
        send_conn = MagicMock()
        process = MagicMock()
        process.is_alive.return_value = False
        context = MagicMock()
        context.Pipe.return_value = (receive_conn, send_conn)
        context.Process.return_value = process

        with (
            patch.object(background, "get_all_start_methods", return_value=["fork", "spawn"]),
            patch.object(background, "get_context", return_value=context) as get_context,
        ):
            result = background._search_related_news_bounded("query")

        self.assertEqual(("query", []), result)
        get_context.assert_called_once_with("spawn")
        process.start.assert_called_once_with()
