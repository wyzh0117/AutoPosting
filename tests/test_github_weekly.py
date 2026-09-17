import http.client
import io
import json
import os
import socket
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from autoposting import github_weekly
from autoposting.db import Database
from autoposting.github_weekly import (
    XHS_TITLE,
    GitHubFetchError,
    apply_leaderboard_copy,
    build_xhs_copy,
    default_github_get,
    fetch_weekly_ai_repos,
    format_stars,
    github_rate_state,
    github_token,
    infer_kind,
    parse_github_search,
    parse_trending_html,
    set_github_token,
    week_window_start,
)
from autoposting.llm import LLMService

from test_llm import FakeRunner, _db


TRENDING_HTML = """
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/acme/llm-cli">acme / llm-cli</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A tiny LLM CLI agent.</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/acme/llm-cli/stargazers">8,200</a>
  <span class="d-inline-block float-sm-right">1,200 stars this week</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/other/note-app">other / note-app</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A notes app with folders.</p>
  <span itemprop="programmingLanguage">TypeScript</span>
  <a href="/other/note-app/stargazers">50,000</a>
  <span class="d-inline-block float-sm-right">9,000 stars this week</span>
</article>
<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/beta/rag-kit">beta / rag-kit</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">RAG toolkit for private docs.</p>
  <span itemprop="programmingLanguage">Go</span>
  <a href="/beta/rag-kit/stargazers">3,100</a>
  <span class="d-inline-block float-sm-right">800 stars this week</span>
</article>
"""


def _search_item(full_name: str, stars: int, description: str, language: str, topics: list[str]) -> dict:
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "description": description,
        "stargazers_count": stars,
        "html_url": f"https://github.com/{full_name}",
        "language": language,
        "topics": topics,
        "owner": {"login": owner, "avatar_url": f"https://github.com/{owner}.png"},
        "created_at": "2026-08-25T00:00:00Z",
        "pushed_at": "2026-08-28T00:00:00Z",
    }


class GitHubWeeklyTests(unittest.TestCase):
    def test_parse_trending_without_h2_wrapper(self) -> None:
        html = """
        <article class="Box-row">
          <a href="/anthropics/claude-plugins-community">anthropics / claude-plugins-community</a>
          <p class="col-9 color-fg-muted my-1 tmp-pr-4">Plugin marketplace for Claude Code.</p>
          <span itemprop="programmingLanguage">TypeScript</span>
          <a href="/anthropics/claude-plugins-community/stargazers">9,100</a>
          <span>2,207 stars this week</span>
        </article>
        """
        repos = parse_trending_html(html)
        self.assertEqual(["anthropics/claude-plugins-community"], [repo["full_name"] for repo in repos])
        self.assertEqual(2207, repos[0]["stars_week"])

    def test_parse_trending_keeps_ai_and_ranks_by_weekly_stars(self) -> None:
        repos = parse_trending_html(TRENDING_HTML)
        names = [repo["full_name"] for repo in repos]
        self.assertEqual(["acme/llm-cli", "beta/rag-kit"], names)
        self.assertEqual(1200, repos[0]["stars_week"])
        self.assertEqual(800, repos[1]["stars_week"])
        self.assertNotIn("other/note-app", names)

    def test_format_stars_and_kind(self) -> None:
        self.assertEqual("1.2k", format_stars(1200))
        self.assertEqual("820", format_stars(820))
        self.assertEqual("Agent", infer_kind("Python", ["llm", "agent"]))
        self.assertEqual("RAG", infer_kind("Go", ["rag"]))
        self.assertEqual("Python", infer_kind("Python", ["misc"]))

    def test_iso_week_issue_uses_calendar_year_and_iso_week(self) -> None:
        from autoposting.github_weekly import iso_week_issue

        self.assertEqual(
            "2026年第37周",
            iso_week_issue(datetime(2026, 9, 10, tzinfo=timezone.utc)),
        )
        self.assertEqual(
            "2026年第1周",
            iso_week_issue(datetime(2026, 1, 1, tzinfo=timezone.utc)),
        )

    def test_fetch_fills_from_search_when_trending_is_thin(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if "github.com/trending" in url:
                return TRENDING_HTML
            parsed = urlparse(url)
            query = parse_qs(parsed.query).get("q", [""])[0]
            self.assertIn("topic:ai", query)
            items = [
                _search_item("acme/llm-cli", 8200, "A tiny LLM CLI agent.", "Python", ["llm", "cli"]),
                _search_item("gamma/new-agent", 410, "Brand new coding agent.", "Rust", ["agent"]),
                _search_item("delta/mcp-box", 380, "MCP servers in a box.", "TypeScript", ["mcp"]),
            ]
            return {"items": items}

        repos = fetch_weekly_ai_repos(
            getter=getter, now=datetime(2026, 8, 29, tzinfo=timezone.utc), limit=3
        )
        self.assertEqual(3, len(repos))
        self.assertEqual(
            ["acme/llm-cli", "gamma/new-agent", "delta/mcp-box"],
            [r["full_name"] for r in repos],
        )
        self.assertTrue(all(repo["fresh"] for repo in repos))
        self.assertEqual(1, repos[0]["rank"])
        self.assertTrue(repos[0]["icon_url"])
        self.assertEqual("CLI", repos[0]["kind"])
        self.assertTrue(any("search/repositories" in url for url in calls))

    def test_fetch_fills_trending_after_new_repos(self) -> None:
        def getter(url: str) -> object:
            if "github.com/trending" in url:
                return TRENDING_HTML
            return {
                "items": [
                    _search_item(
                        "gamma/new-agent", 410, "Brand new coding agent.", "Rust", ["agent"]
                    )
                ]
            }

        repos = fetch_weekly_ai_repos(
            getter=getter, now=datetime(2026, 8, 29, tzinfo=timezone.utc), limit=3
        )
        self.assertEqual(
            ["gamma/new-agent", "acme/llm-cli", "beta/rag-kit"],
            [repo["full_name"] for repo in repos],
        )
        self.assertTrue(repos[0]["fresh"])
        self.assertFalse(repos[1]["fresh"])
        self.assertFalse(repos[2]["fresh"])

    def test_fetch_keeps_facts_and_xhs_layout_without_kind_copy(self) -> None:
        repos = fetch_weekly_ai_repos(
            getter=lambda url: TRENDING_HTML if "trending" in url else {"items": []},
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
            limit=2,
        )
        title, body = build_xhs_copy(repos)
        self.assertEqual(XHS_TITLE, title)
        self.assertIn("llm-cli", body)
        self.assertIn("https://github.com/acme/llm-cli", body)
        self.assertFalse(repos[0]["enriched"])
        self.assertFalse(str(repos[0].get("summary") or "").strip())
        self.assertFalse(str(repos[0].get("does") or "").strip())
        self.assertIn("tiny LLM CLI", str(repos[0].get("description") or ""))

    def test_leaderboard_model_only_rewrites_copy_fields(self) -> None:
        repos = [
            {
                "rank": 1,
                "full_name": "acme/llm-cli",
                "name": "llm-cli",
                "owner": "acme",
                "url": "https://github.com/acme/llm-cli",
                "stars": 8200,
                "stars_label": "8.2k",
                "stars_week": 1200,
                "language": "Python",
                "kind": "CLI",
                "icon_url": "https://github.com/acme.png",
                "description": "A tiny LLM CLI agent.",
                "summary": "A tiny LLM CLI agent.",
                "does": "A tiny LLM CLI agent.",
                "scene": "适合做 CLI 的人",
                "how": "开源仓库",
                "enriched": False,
            }
        ]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fake = FakeRunner(
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "full_name": "acme/llm-cli",
                            "summary": "把大模型塞进命令行的小工具。",
                            "does": "在终端里直接问模型、跑脚本。",
                            "scene": "本地开发、快速试验提示词。",
                            "how": "clone 后按 README 装好，对着仓库说话。",
                            "audience": "习惯开终端试验提示词的人。",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        service = LLMService(_db(temp), runner=fake)
        service.configure(model="qwen3.8-max", base_url="", api_key="")
        updated = service.enrich_github_repos(repos)
        self.assertEqual("acme/llm-cli", updated[0]["full_name"])
        self.assertEqual(8200, updated[0]["stars"])
        self.assertEqual("https://github.com/acme/llm-cli", updated[0]["url"])
        self.assertEqual("CLI", updated[0]["kind"])
        self.assertEqual("把大模型塞进命令行的小工具。", updated[0]["summary"])
        self.assertEqual("在终端里直接问模型、跑脚本。", updated[0]["does"])
        self.assertTrue(updated[0]["enriched"])
        self.assertIn('"summary":"..."', fake.calls[0][2])
        self.assertIn("不要改项目名", fake.calls[0][2])
        self.assertIn("完整陈述句", fake.calls[0][2])
        self.assertIn("22", fake.calls[0][2])
        from autoposting.leaderboard_prompt import load_leaderboard_copy_prompt

        doc = load_leaderboard_copy_prompt()
        self.assertIn("【does＝做什么？", doc)
        self.assertIn(doc[:80], fake.calls[0][2])
        self.assertNotIn("主语用「它/你/这个工具」", doc)
        self.assertIn("禁止以「它」", doc)
        self.assertRegex(doc, r"how / scene / audience[^\n]*一条一行")
        self.assertIn("信息密度优先于文采", doc)

    def test_enrich_times_out_instead_of_hanging(self) -> None:
        import time

        from autoposting import llm as llm_mod
        from autoposting.llm import LLMError

        class Hang:
            def __call__(self, argv: list[str]) -> object:
                time.sleep(3)
                return types.SimpleNamespace(stdout="{}", stderr="", returncode=0)

        repos = [
            {
                "rank": 1,
                "full_name": "acme/llm-cli",
                "name": "llm-cli",
                "description": "A tiny LLM CLI agent.",
                "kind": "CLI",
                "language": "Python",
                "topics": ["cli"],
            }
        ]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LLMService(_db(temp), runner=Hang())
        service.configure(model="deepseek-v4-flash", base_url="", api_key="")
        original = llm_mod.LEADERBOARD_ENRICH_SECONDS
        llm_mod.LEADERBOARD_ENRICH_SECONDS = 0.2
        try:
            started = time.perf_counter()
            with self.assertRaises(LLMError) as ctx:
                service.enrich_github_repos(repos)
            elapsed = time.perf_counter() - started
        finally:
            llm_mod.LEADERBOARD_ENRICH_SECONDS = original
        self.assertEqual("llm_timeout", ctx.exception.code)
        self.assertLess(elapsed, 1.5)

    def test_apply_leaderboard_copy_ignores_name_tampering(self) -> None:
        repos = [
            {
                "full_name": "acme/llm-cli",
                "name": "llm-cli",
                "summary": "old",
                "does": "old",
                "scene": "old",
                "how": "old",
                "enriched": False,
            }
        ]
        updated = apply_leaderboard_copy(
            repos,
            [
                {
                    "full_name": "acme/llm-cli",
                    "name": "HACKED",
                    "summary": "新简介",
                    "does": "新能力",
                    "scene": "新场景",
                    "how": "新用法",
                    "audience": "新人群",
                    "url": "https://evil.example",
                    "stars": 1,
                }
            ],
        )
        self.assertEqual("llm-cli", updated[0]["name"])
        self.assertEqual("新简介", updated[0]["summary"])
        self.assertTrue(updated[0]["enriched"])

    def test_apply_leaderboard_copy_keeps_newlines_and_skips_ellipsis(self) -> None:
        repos = [
            {
                "full_name": "acme/llm-cli",
                "name": "llm-cli",
                "audience": "old",
                "scene": "old",
            }
        ]
        updated = apply_leaderboard_copy(
            repos,
            [
                {
                    "full_name": "acme/llm-cli",
                    "summary": "把多个助手按职责排成一条流水线。",
                    "does": "组织角色一起协作。",
                    "how": "先定职责再串任务。",
                    "audience": "做架构设计的人\n写技术文档的人\n要快速上手的人",
                    "scene": "本地把开发、内容和运营串成一条流水线\n先用一个真实任务看完整协作",
                }
            ],
        )
        self.assertEqual(
            "做架构设计的人\n写技术文档的人\n要快速上手的人",
            updated[0]["audience"],
        )
        self.assertNotIn(" ", str(updated[0]["audience"]).replace("\n", ""))
        self.assertNotIn("…", str(updated[0]["scene"]))
        self.assertIn("\n", str(updated[0]["scene"]))

    def test_week_window_is_rolling_seven_days(self) -> None:
        start = week_window_start(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))
        self.assertEqual("2026-08-22", start)


API_URL = "https://api.github.com/search/repositories?q=topic%3Aai"
QUOTA_HEADERS = {
    "X-RateLimit-Limit": "60",
    "X-RateLimit-Remaining": "0",
    "X-RateLimit-Reset": "1800000000",
    "X-RateLimit-Resource": "core",
}


class _FakeResponse:
    """Minimal urlopen() context manager for the HTTP-layer tests."""

    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = dict(headers or {})

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _http_error(
    status: int, *, body: bytes = b"", headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        API_URL, status, f"HTTP {status}", dict(headers or {}), io.BytesIO(body)
    )


def _request_headers(request: urllib.request.Request) -> dict[str, str]:
    """Header names are capitalized by Request, so compare case-insensitively."""
    return {name.lower(): value for name, value in request.header_items()}


class GitHubHttpLayerTests(unittest.TestCase):
    """Failure classification, optional token, and quota visibility."""

    def setUp(self) -> None:
        set_github_token(None)
        patcher = mock.patch.object(github_weekly, "_RATE_STATE", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(set_github_token, None)

    def _patched_urlopen(self, outcome: object) -> list[urllib.request.Request]:
        """Patch the network seam; an exception outcome is raised instead of answered."""
        sent: list[urllib.request.Request] = []

        def fake_urlopen(
            request: urllib.request.Request, timeout: float | None = None
        ) -> object:
            sent.append(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        patcher = mock.patch.object(github_weekly.urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

    def _failure(self, outcome: object) -> GitHubFetchError:
        self._patched_urlopen(outcome)
        with self.assertRaises(GitHubFetchError) as ctx:
            default_github_get(API_URL)
        return ctx.exception

    def test_success_with_token_sends_bearer_and_returns_json(self) -> None:
        payload = {"items": [{"full_name": "acme/llm-cli"}]}
        sent = self._patched_urlopen(
            _FakeResponse(
                json.dumps(payload).encode("utf-8"),
                {"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"},
            )
        )
        result = default_github_get(API_URL, token="  ghp-token-123  ")
        self.assertEqual(payload, result)
        headers = _request_headers(sent[0])
        self.assertEqual("Bearer ghp-token-123", headers["authorization"])
        self.assertTrue(headers["user-agent"])
        self.assertIn("application/vnd.github+json", headers["accept"])
        self.assertEqual("2022-11-28", headers["x-github-api-version"])

    def test_success_without_token_omits_authorization(self) -> None:
        sent = self._patched_urlopen(_FakeResponse(b'{"items": []}'))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("", github_token())
            self.assertEqual({"items": []}, default_github_get(API_URL))
        self.assertNotIn("authorization", _request_headers(sent[0]))

    def test_env_tokens_are_used_when_no_override_is_set(self) -> None:
        sent = self._patched_urlopen(_FakeResponse(b'{"items": []}'))
        with mock.patch.dict(
            os.environ,
            {"AUTOPOSTING_GITHUB_TOKEN": " primary-token ", "GITHUB_TOKEN": "fallback"},
            clear=True,
        ):
            self.assertEqual("primary-token", github_token())
            default_github_get(API_URL)
        self.assertEqual("Bearer primary-token", _request_headers(sent[0])["authorization"])

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": " fallback-token "}, clear=True):
            self.assertEqual("fallback-token", github_token())
            default_github_get(API_URL)
        self.assertEqual("Bearer fallback-token", _request_headers(sent[1])["authorization"])

        with mock.patch.dict(
            os.environ,
            {"AUTOPOSTING_GITHUB_TOKEN": "   ", "GITHUB_TOKEN": "fallback-token"},
            clear=True,
        ):
            self.assertEqual("fallback-token", github_token())

    def test_override_and_explicit_token_win_over_env(self) -> None:
        sent = self._patched_urlopen(_FakeResponse(b'{"items": []}'))
        with mock.patch.dict(os.environ, {"AUTOPOSTING_GITHUB_TOKEN": "env-token"}, clear=True):
            default_github_get(API_URL)
            self.assertEqual("Bearer env-token", _request_headers(sent[0])["authorization"])
            set_github_token("override-token")
            self.assertEqual("override-token", github_token())
            default_github_get(API_URL)
            self.assertEqual("Bearer override-token", _request_headers(sent[1])["authorization"])
            default_github_get(API_URL, token=" explicit-token ")
            self.assertEqual("Bearer explicit-token", _request_headers(sent[2])["authorization"])
            default_github_get(API_URL, token="")
            self.assertNotIn("authorization", _request_headers(sent[3]))

    def test_403_with_rate_limit_body_is_rate_limited(self) -> None:
        self._patched_urlopen(
            _http_error(
                403,
                body=b'{"message":"API rate limit exceeded for 1.2.3.4."}',
                headers=QUOTA_HEADERS,
            )
        )
        with self.assertRaises(GitHubFetchError) as ctx:
            default_github_get(API_URL)
        error = ctx.exception
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual("rate_limited", error.kind)
        self.assertEqual(403, error.status)
        self.assertEqual(API_URL, error.url)
        self.assertEqual(
            f"github_fetch_failed:rate limit exceeded (403):{API_URL}", str(error)
        )
        self.assertIn("rate limit", str(error))
        self.assertIn("403", str(error))
        self.assertEqual(1800000000.0, error.reset_at)
        self.assertIsInstance(error.__cause__, urllib.error.HTTPError)
        self.assertEqual(
            {"limit": 60, "remaining": 0, "reset": 1800000000.0, "resource": "core"},
            github_rate_state(),
        )

    def test_403_rate_limit_markers_body_header_and_429(self) -> None:
        secondary = self._failure(
            _http_error(403, body=b"You have exceeded a secondary rate limit.")
        )
        self.assertEqual("rate_limited", secondary.kind)
        header_only = self._failure(
            _http_error(403, body=b"nope", headers={"X-RateLimit-Remaining": "0"})
        )
        self.assertEqual("rate_limited", header_only.kind)
        self.assertIsNone(header_only.reset_at)
        too_many = self._failure(_http_error(429, body=b'{"message":"slow down"}'))
        self.assertEqual("rate_limited", too_many.kind)
        self.assertEqual(429, too_many.status)
        self.assertIn("rate limit", str(too_many))

    def test_http_status_kinds(self) -> None:
        cases = (
            (401, "forbidden"),
            (403, "forbidden"),
            (404, "not_found"),
            (500, "server_error"),
            (503, "server_error"),
            (418, "http_error"),
            (429, "rate_limited"),
        )
        for status, kind in cases:
            with self.subTest(status=status):
                error = self._failure(_http_error(status, body=b'{"message":"nope"}'))
                self.assertEqual(kind, error.kind)
                self.assertEqual(status, error.status)
                self.assertEqual(API_URL, error.url)
                if kind == "rate_limited":
                    self.assertIn("rate limit", str(error))
                    self.assertIn(str(status), str(error))
                else:
                    self.assertEqual(f"github_fetch_failed:{API_URL}", str(error))
                self.assertIsInstance(error.__cause__, urllib.error.HTTPError)

    def test_timeout_and_unreachable_kinds(self) -> None:
        cases = (
            (socket.timeout("timed out"), "timeout"),
            (TimeoutError("timed out"), "timeout"),
            (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
            (urllib.error.URLError("name resolution"), "unreachable"),
            (ConnectionResetError(54, "Connection reset by peer"), "unreachable"),
            (http.client.IncompleteRead(b"half a body"), "unreachable"),
        )
        for outcome, kind in cases:
            with self.subTest(outcome=repr(outcome)):
                error = self._failure(outcome)
                self.assertEqual(kind, error.kind)
                self.assertIsNone(error.status)
                self.assertIsNone(error.reset_at)
                self.assertEqual(f"github_fetch_failed:{API_URL}", str(error))
                self.assertIs(error.__cause__, outcome)

    def test_invalid_json_on_api_host_is_bad_json(self) -> None:
        self._patched_urlopen(_FakeResponse(b"<html>not json</html>"))
        with self.assertRaises(GitHubFetchError) as ctx:
            default_github_get(API_URL)
        error = ctx.exception
        self.assertEqual("bad_json", error.kind)
        self.assertEqual("github_search_invalid_json", str(error))
        self.assertEqual(API_URL, error.url)
        self.assertIsNone(error.status)
        self.assertIsInstance(error.__cause__, json.JSONDecodeError)

    def test_non_api_url_still_returns_raw_text(self) -> None:
        self._patched_urlopen(_FakeResponse(b"<html>trending</html>"))
        self.assertEqual(
            "<html>trending</html>", default_github_get("https://github.com/trending?since=weekly")
        )

    def test_rate_state_is_none_until_a_response_carries_quota_headers(self) -> None:
        self.assertIsNone(github_rate_state())
        self._patched_urlopen(_FakeResponse(b'{"items": []}'))
        default_github_get(API_URL)
        self.assertIsNone(github_rate_state())

    def test_rate_state_is_a_read_only_copy(self) -> None:
        sent = self._patched_urlopen(
            _FakeResponse(
                b'{"items": []}',
                {
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "14",
                    "X-RateLimit-Reset": "1800000000",
                    "X-RateLimit-Resource": "core",
                },
            )
        )
        default_github_get(API_URL)
        self.assertTrue(sent)
        state = github_rate_state()
        self.assertEqual(
            {"limit": 60, "remaining": 14, "reset": 1800000000.0, "resource": "core"},
            state,
        )
        state["remaining"] = 999
        state["resource"] = "hacked"
        self.assertEqual(14, github_rate_state()["remaining"])
        self.assertEqual("core", github_rate_state()["resource"])

    def test_token_never_reaches_messages_or_rate_state(self) -> None:
        token = "ghp_do_not_leak_me_0123456789"
        set_github_token(token)
        outcomes = (
            _http_error(
                403, body=b'{"message":"API rate limit exceeded"}', headers=QUOTA_HEADERS
            ),
            _http_error(404, body=b'{"message":"Not Found"}'),
            TimeoutError("timed out"),
            _FakeResponse(b"<html>not json</html>"),
        )
        for outcome in outcomes:
            with self.subTest(outcome=repr(outcome)):
                sent = self._patched_urlopen(outcome)
                with self.assertRaises(GitHubFetchError) as ctx:
                    default_github_get(API_URL)
                self.assertEqual(
                    f"Bearer {token}", _request_headers(sent[0])["authorization"]
                )
                rendered = repr(ctx.exception) + str(ctx.exception) + repr(ctx.exception.args)
                self.assertNotIn(token, rendered)
                self.assertNotIn(token, repr(github_rate_state()))
                self.assertNotIn(token, repr(ctx.exception.__cause__))

    def test_fetch_weekly_ai_repos_still_swallows_github_fetch_error(self) -> None:
        def getter(url: str) -> object:
            raise GitHubFetchError("rate_limited", url, status=403, reset_at=1.0)

        repos = fetch_weekly_ai_repos(
            getter=getter, now=datetime(2026, 8, 29, tzinfo=timezone.utc), limit=3
        )
        self.assertEqual([], repos)
