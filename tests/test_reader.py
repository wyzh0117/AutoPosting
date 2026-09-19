import unittest

from autoposting.reader import (
    BLOCKED,
    FAILED,
    FULL,
    PAYWALLED,
    ReaderService,
)


class ReaderServiceTests(unittest.TestCase):
    def test_html_extracts_article_and_removes_chrome(self) -> None:
        paragraph = "The report explains the model release and measured results. " * 6
        payload = (
            "<html><header>site chrome</header><nav>menu</nav><main>"
            "<h1>Model release</h1>"
            f"<p>{paragraph}</p><p>It also records the consequences for users.</p>"
            "</main><footer>footer links</footer></html>"
        ).encode()

        document = ReaderService().read("https://example.com/article", payload=payload)

        self.assertEqual(FULL, document.status)
        self.assertIn("measured results", document.text)
        self.assertNotIn("site chrome", document.text)
        self.assertNotIn("menu", document.text)
        self.assertTrue(document.content_hash)
        self.assertGreaterEqual(document.quality_score, 0.55)

    def test_json_ld_article_body_wins_over_short_html(self) -> None:
        body = (
            "The structured article body describes the launch in detail, including "
            "the measured result and the implications for developers and readers."
        )
        payload = (
            '<script type="application/ld+json">'
            '{"@type":"NewsArticle","headline":"Launch report",'
            f'"articleBody":{body!r}'
            "}</script><p>short teaser</p>"
        ).replace("'", '"').encode()

        document = ReaderService().read("https://example.com/article", payload=payload)

        self.assertEqual("json-ld", document.extractor)
        self.assertIn("structured article body", document.text)
        self.assertEqual("Launch report", document.title)

    def test_rss_full_content_does_not_fetch_again(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            return b"<p>HTTP fallback</p>"

        rss = "<p>" + ("The RSS item contains the complete article body and facts. " * 8) + "</p>"
        document = ReaderService(fetcher).read(
            "https://example.com/article?utm_source=test",
            rss_content=rss,
            title_hint="RSS article",
        )

        self.assertEqual(FULL, document.status)
        self.assertEqual("rss", document.extractor)
        self.assertEqual([], calls)
        self.assertEqual("https://example.com/article", document.canonical_url)

    def test_gate_classifies_blocked_paywall_and_fetch_failure(self) -> None:
        service = ReaderService(lambda _url: (_ for _ in ()).throw(OSError("offline")))

        blocked = service.read("https://example.com/a", payload=b"<body>Access denied</body>")
        paywalled = service.read(
            "https://example.com/a", payload=b"<body>Subscribe to continue reading</body>"
        )
        failure_page = service.read(
            "https://example.com/a", payload=b"<body>502 Bad Gateway</body>"
        )
        failed = service.read("https://example.com/a")

        self.assertEqual(BLOCKED, blocked.status)
        self.assertEqual("blocked_page", blocked.failure_reason)
        self.assertEqual(PAYWALLED, paywalled.status)
        self.assertEqual("paywall_or_truncated", paywalled.failure_reason)
        self.assertEqual(FAILED, failure_page.status)
        self.assertEqual("failure_page", failure_page.failure_reason)
        self.assertEqual(FAILED, failed.status)
        self.assertEqual("fetch_failed", failed.failure_reason)

    def test_quality_score_prefers_substantive_article(self) -> None:
        service = ReaderService()
        good = service.read(
            "https://example.com/good",
            payload=(
                "<article><h1>Model release</h1>"
                + "".join(
                    f"<p>Paragraph {index} explains the release, measured result, and context for users. "
                    "More detail makes this useful evidence.</p>"
                    for index in range(5)
                )
                + "</article>"
            ).encode(),
        )
        thin = service.read("https://example.com/thin", payload=b"<p>Short teaser.</p>")

        self.assertGreater(good.quality_score, thin.quality_score)
        self.assertEqual(FULL, good.status)
        self.assertEqual("partial", thin.status)

    def test_js_shell_uses_one_dynamic_render_and_becomes_full(self) -> None:
        http_calls: list[str] = []
        render_calls: list[str] = []
        shell = b'<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
        paragraph = "The rendered article contains verified facts and context for readers. " * 8
        rendered = f"<article><h1>Rendered report</h1><p>{paragraph}</p></article>"

        def fetcher(url: str) -> bytes:
            http_calls.append(url)
            return shell

        def render_fetcher(url: str) -> str:
            render_calls.append(url)
            return rendered

        document = ReaderService(fetcher, render_fetcher=render_fetcher).read(
            "https://example.com/app"
        )

        self.assertEqual(FULL, document.status)
        self.assertEqual("dynamic", document.fetch_method)
        self.assertEqual("html", document.extractor)
        self.assertIn("verified facts", document.text)
        self.assertEqual(["https://example.com/app"], http_calls)
        self.assertEqual(["https://example.com/app"], render_calls)

    def test_static_full_article_does_not_call_dynamic_renderer(self) -> None:
        calls: list[str] = []
        paragraph = "The static page contains the complete report and measured results. " * 10

        def render_fetcher(url: str) -> str:
            calls.append(url)
            raise AssertionError("static full content should not render")

        document = ReaderService(
            lambda _url: f"<article><p>{paragraph}</p></article>".encode(),
            render_fetcher=render_fetcher,
        ).read("https://example.com/static")

        self.assertEqual(FULL, document.status)
        self.assertEqual("http", document.fetch_method)
        self.assertEqual([], calls)

    def test_dynamic_failure_never_promotes_shell_to_full(self) -> None:
        calls: list[str] = []

        def render_fetcher(url: str) -> str:
            calls.append(url)
            raise OSError("browser unavailable")

        document = ReaderService(
            lambda _url: b'<html><body><div id="app"></div><script>boot()</script></body></html>',
            render_fetcher=render_fetcher,
        ).read("https://example.com/dynamic")

        self.assertNotEqual(FULL, document.status)
        self.assertEqual("dynamic", document.fetch_method)
        self.assertEqual("dynamic_fetch_failed", document.failure_reason)
        self.assertEqual(["https://example.com/dynamic"], calls)

    def test_missing_default_renderer_is_observable(self) -> None:
        def render_fetcher(_: str) -> str:
            raise RuntimeError("dynamic_renderer_unavailable")

        document = ReaderService(
            lambda _url: b'<html><body><div id="app"></div><script>boot()</script></body></html>',
            render_fetcher=render_fetcher,
        ).read("https://example.com/dynamic")

        self.assertNotEqual(FULL, document.status)
        self.assertEqual("dynamic", document.fetch_method)
        self.assertEqual("dynamic_renderer_unavailable", document.failure_reason)


if __name__ == "__main__":
    unittest.main()
