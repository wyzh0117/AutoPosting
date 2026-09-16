import json
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autoposting.db import Database
from autoposting.github_weekly import (
    XHS_TITLE,
    apply_leaderboard_copy,
    build_xhs_copy,
    fetch_weekly_ai_repos,
    format_stars,
    infer_kind,
    parse_github_search,
    parse_trending_html,
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

    def test_fallback_copy_and_xhs_layout(self) -> None:
        repos = fetch_weekly_ai_repos(
            getter=lambda url: TRENDING_HTML if "trending" in url else {"items": []},
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
            limit=2,
        )
        title, body = build_xhs_copy(repos)
        self.assertEqual(XHS_TITLE, title)
        self.assertIn("llm-cli", body)
        self.assertIn("https://github.com/acme/llm-cli", body)
        self.assertIn(repos[0]["summary"], body)
        self.assertFalse(repos[0]["enriched"])
        self.assertTrue(repos[0]["summary"])
        self.assertTrue(repos[0]["does"])
        self.assertTrue(repos[0]["scene"])
        self.assertTrue(repos[0]["how"])
        self.assertNotIn("项目主页", repos[0]["how"])
        self.assertNotIn("项目主页", repos[0]["summary"])
        self.assertFalse(str(repos[0]["summary"]).startswith("它"))
        self.assertIn("给", repos[0]["summary"])
        self.assertNotIn("tiny LLM CLI", repos[0]["does"])
        self.assertRegex(repos[0]["does"], r"[\u4e00-\u9fff]")

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
        self.assertIn("项目简介", fake.calls[0][2])
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
        self.assertIn("禁止用省略号截断", doc)

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
