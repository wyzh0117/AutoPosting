import io
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from autoposting.images import BROWSER_UA
from autoposting.reader import (
    BLOCKED,
    FAILED,
    FULL,
    PARTIAL,
    PAYWALLED,
    ReaderService,
    classify_fetch_error,
    classify_http_status,
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
        # A bare OSError is no longer collapsed into "fetch_failed"; the
        # classifier names the transport failure instead.
        self.assertEqual("connection_reset", failed.failure_reason)
        self.assertEqual("connection_reset", failed.cause_code)

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


class ReaderFetchClassificationTests(unittest.TestCase):
    @staticmethod
    def _raising(exc: BaseException) -> ReaderService:
        return ReaderService(lambda _url, error=exc: (_ for _ in ()).throw(error))

    def test_fetch_errors_map_to_specific_cause_codes(self) -> None:
        cases: list[tuple[str, BaseException]] = [
            ("dns_failure", socket.gaierror(-2, "Name or service not known")),
            ("timeout", TimeoutError("timed out")),
            ("timeout", socket.timeout("timed out")),
            ("tls_failure", ssl.SSLError("handshake failure")),
            ("tls_failure", ssl.SSLCertVerificationError("certificate verify failed")),
            ("connection_reset", ConnectionResetError("reset by peer")),
            ("blocked_http", HTTPError("https://example.com/a", 403, "Forbidden", {}, None)),
            ("blocked_http", HTTPError("https://example.com/a", 401, "Unauthorized", {}, None)),
            ("blocked_http", HTTPError("https://example.com/a", 429, "Slow down", {}, None)),
            ("http_error", HTTPError("https://example.com/a", 500, "Server Error", {}, None)),
            ("http_error", HTTPError("https://example.com/a", 404, "Not Found", {}, None)),
            ("invalid_content", ValueError("bad payload")),
            ("invalid_content", TypeError("bad payload")),
        ]
        for expected, exc in cases:
            with self.subTest(expected=expected, error=type(exc).__name__):
                document = self._raising(exc).read("https://example.com/a")
                self.assertEqual(expected, document.cause_code)
                self.assertEqual(expected, document.failure_reason)
                self.assertEqual(
                    BLOCKED if expected == "blocked_http" else FAILED, document.status
                )

    def test_unexpected_fetcher_error_is_reader_internal_error(self) -> None:
        # ``read`` only handles transport/parse exceptions; anything else is
        # classified where it is caught (the research layer) so it is never
        # reported as a generic unknown error.
        self.assertEqual("reader_internal_error", classify_fetch_error(RuntimeError("boom")))
        self.assertEqual("connection_reset", classify_fetch_error(OSError("offline")))

    def test_urllib_error_reason_is_unwrapped(self) -> None:
        from urllib.error import URLError

        self.assertEqual("dns_failure", classify_fetch_error(URLError(socket.gaierror(-2, "dns"))))
        self.assertEqual("timeout", classify_fetch_error(URLError(TimeoutError("slow"))))
        self.assertEqual("connection_reset", classify_fetch_error(URLError("no reason")))

    def test_http_status_keeps_cause_code_and_names_the_block_kind(self) -> None:
        self.assertEqual(("", ""), classify_http_status(200))
        self.assertEqual(("blocked_http", "forbidden"), classify_http_status(403))
        self.assertEqual(("blocked_http", "auth_required"), classify_http_status(401))
        self.assertEqual(("blocked_http", "rate_limited"), classify_http_status(429))
        self.assertEqual(("http_error", ""), classify_http_status(503))
        self.assertEqual(
            ("blocked_http", "cloudflare"),
            classify_http_status(403, "<title>Just a moment...</title>"),
        )
        # The cause_code stays status-derived, so an existing ``http_error``
        # log line is unchanged; only the new blocked_kind names the block.
        self.assertEqual(
            ("http_error", "cloudflare"),
            classify_http_status(503, "Checking your browser before accessing"),
        )
        self.assertEqual(
            ("blocked_http", "user_agent"),
            classify_http_status(403, "Your user agent is blocked"),
        )
        self.assertEqual(
            ("blocked_http", "anti_bot"),
            classify_http_status(403, "Please complete the captcha to continue"),
        )
        self.assertEqual(
            ("blocked_http", "robots"),
            classify_http_status(403, "Blocked by robots.txt policy"),
        )
        self.assertEqual(
            ("blocked_http", "login_wall"),
            classify_http_status(403, "Sign in to continue reading"),
        )
        self.assertEqual(
            ("blocked_http", "js_required"),
            classify_http_status(403, "Please enable JavaScript to continue"),
        )
        self.assertEqual(
            ("blocked_http", "redirect_to_challenge"),
            classify_http_status(
                403,
                "",
                final_url="https://example.com/cdn-cgi/challenge-platform/h/b",
            ),
        )
        self.assertEqual(
            ("blocked_http", "redirect_to_challenge"),
            classify_http_status(403, "", final_url="https://verify.example.com/captcha"),
        )

    def test_http_error_status_tuple_is_classified(self) -> None:
        service = ReaderService(lambda _url: (b"<html>nope</html>", 403))

        document = service.read("https://example.com/a")

        self.assertEqual(BLOCKED, document.status)
        self.assertEqual("blocked_http", document.cause_code)
        self.assertEqual("forbidden", document.blocked_kind)

    def test_http_error_body_refines_the_block_kind(self) -> None:
        exc = HTTPError(
            "https://example.com/a",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"<html>Just a moment... __cf_chl</html>"),
        )

        document = self._raising(exc).read("https://example.com/a")

        self.assertEqual("blocked_http", document.cause_code)
        self.assertEqual("cloudflare", document.blocked_kind)

    def test_http_200_challenge_page_names_its_block(self) -> None:
        # The most common block is not an HTTP error at all: a 200 whose body is
        # a challenge page.  It must carry the same vocabulary as a 4xx block.
        # The exact gate verdict depends on the gate's own marker list, so this
        # asserts the substance (a named block, never FULL) rather than pinning
        # which of blocked/partial/paywalled the gate picked.
        cases: list[tuple[bytes, str]] = [
            (b"<html><body>Just a moment...</body></html>", "cloudflare"),
            (b"<html><body>Checking your browser before accessing</body></html>", "cloudflare"),
            (b"<html><body>Please verify you are human to continue</body></html>", "anti_bot"),
            (b"<html><body>Enable JavaScript to continue reading</body></html>", "js_required"),
            (b"<html><body>Sign in to continue reading this article</body></html>", "login_wall"),
        ]
        for payload, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                document = ReaderService(lambda _url, body=payload: body).read(
                    "https://example.com/a"
                )
                self.assertIn(document.status, {BLOCKED, PARTIAL, PAYWALLED})
                self.assertEqual("blocked_http", document.cause_code)
                self.assertEqual(expected_kind, document.blocked_kind)

    def test_successful_fetch_of_unusable_bodies_is_classified(self) -> None:
        cases: list[tuple[str, bytes]] = [
            ("unsupported_content", b'{"ok": true, "data": []}'),
            ("unsupported_content", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"),
            ("unsupported_content", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"),
            ("invalid_content", b"{this is not json"),
        ]
        for expected, payload in cases:
            with self.subTest(expected=expected, payload=payload[:12]):
                document = ReaderService(lambda _url, body=payload: body).read(
                    "https://example.com/a"
                )
                self.assertEqual(FAILED, document.status)
                self.assertEqual(expected, document.cause_code)
                self.assertEqual(expected, document.failure_reason)
                self.assertEqual("", document.text)

    def test_oversized_body_is_too_large_and_keeps_no_text(self) -> None:
        oversized = b"<article><p>" + b"x" * (12 * 1024 * 1024) + b"</p></article>"

        document = ReaderService(lambda _url: oversized).read("https://example.com/a")

        self.assertEqual(FAILED, document.status)
        self.assertEqual("too_large", document.cause_code)
        self.assertEqual("too_large", document.failure_reason)
        self.assertEqual("", document.text)

    def test_partial_feed_content_survives_a_blocked_page_fetch(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            raise HTTPError(url, 403, "Forbidden", {}, None)

        rss = "<p>RSS 摘要只包含开头一段，但这是已经收录的正文。</p>"
        document = ReaderService(fetcher).read(
            "https://example.com/blocked",
            rss_content=rss,
            title_hint="被拦截的文章",
        )

        self.assertEqual(["https://example.com/blocked"], calls)
        self.assertEqual(PARTIAL, document.status)
        self.assertTrue(document.text)
        self.assertIn("已经收录的正文", document.text)
        self.assertTrue(document.degraded)
        self.assertEqual("feed", document.fallback_source)
        self.assertEqual("blocked_http", document.cause_code)
        self.assertEqual("blocked_http", document.failure_reason)
        self.assertEqual("forbidden", document.blocked_kind)
        self.assertEqual("rss", document.fetch_method)
        self.assertEqual("https://example.com/blocked", document.original_url)

    def test_partial_feed_content_survives_a_transport_failure(self) -> None:
        rss = "<p>Feed body that the collector already stored.</p>" * 3

        document = ReaderService(self._raising(TimeoutError("slow")).fetcher).read(
            "https://example.com/slow",
            rss_content=rss,
        )

        self.assertTrue(document.degraded)
        self.assertEqual("feed", document.fallback_source)
        self.assertEqual("timeout", document.cause_code)
        self.assertEqual("partial", document.status)

    def test_transport_failure_without_feed_content_stays_failed(self) -> None:
        document = ReaderService(self._raising(ConnectionResetError("reset")).fetcher).read(
            "https://example.com/a",
            rss_content="   ",
        )

        self.assertEqual(FAILED, document.status)
        self.assertEqual("connection_reset", document.cause_code)
        self.assertFalse(document.degraded)
        self.assertIsNone(document.fallback_source)

    def test_full_feed_content_still_avoids_the_fetch(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            raise AssertionError("full feed content must not fetch")

        rss = "<p>" + ("Complete feed article body and facts. " * 12) + "</p>"
        document = ReaderService(fetcher).read("https://example.com/full", rss_content=rss)

        self.assertEqual(FULL, document.status)
        self.assertEqual([], calls)
        self.assertFalse(document.degraded)

    def test_blocked_page_is_never_retried_and_headers_never_change(self) -> None:
        seen: list[object] = []

        def fake_urlopen(request: object, timeout: int | None = None) -> bytes:
            seen.append(request)
            raise HTTPError(getattr(request, "full_url", ""), 403, "Forbidden", {}, None)

        with patch("autoposting.reader.urlopen", fake_urlopen):
            document = ReaderService().read("https://example.com/blocked")

        # Exactly one attempt: a block is reported, never worked around.
        self.assertEqual(1, len(seen))
        headers = {key.lower(): value for key, value in seen[0].header_items()}
        self.assertEqual(BROWSER_UA, headers.get("user-agent"))
        self.assertNotIn("cookie", headers)
        self.assertNotIn("referer", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual("blocked_http", document.cause_code)
        self.assertEqual("", document.text)


if __name__ == "__main__":
    unittest.main()
