import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoposting.cli import build_parser, main
from autoposting.db import Database


class CliTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(argv)
        return exit_code, json.loads(output.getvalue())

    def test_self_check_command_outputs_machine_readable_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code, payload = self._run(["self-check", "--workspace", temp_dir])

            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])

    def test_collect_cli_fixture_reports_mixed_collection_run(self) -> None:
        feed = (
            b"<rss version='2.0'><channel><title>Fixture</title>"
            b"<item><guid>fixture-1</guid><title>Model One news</title>"
            b"<link>https://www.reddit.com/r/LocalLLaMA/comments/fixture1/discussion</link>"
            b"<description>Model One news content</description></item>"
            b"</channel></rss>"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "autoposting.db"
            sources_path = root / "sources.json"
            sources_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            },
                            {
                                "source_id": "reddit_ai",
                                "name": "Reddit AI",
                                "type": "reddit",
                                "url_or_handle": "LocalLLaMA",
                                "tier": "S4",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "autoposting.ingestion._default_fetcher", return_value=feed
            ), patch("autoposting.reddit._default_reddit_fetcher", return_value=feed):
                init_code, _ = self._run(
                    ["init", "--db", str(db_path), "--sources", str(sources_path)]
                )
                collect_code, result = self._run(
                    ["collect", "--db", str(db_path), "--sources", str(sources_path)]
                )

            self.assertEqual(0, init_code)
            self.assertEqual(0, collect_code)
            self.assertTrue(result["ok"])
            self.assertEqual(
                {"lab_blog", "reddit_ai"}, set(result["requested_sources"])
            )
            self.assertEqual(
                {"lab_blog", "reddit_ai"}, set(result["successful_sources"])
            )
            self.assertEqual(2, len(result["source_attempts"]))
            self.assertTrue(result["run_id"])

    def test_visual_render_example_writes_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exit_code, payload = self._run(
                ["visual-render", "--example", "--out", temp_dir, "--no-screenshot"]
            )
            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertNotIn("html_text", payload["pages"][0])
            self.assertTrue(Path(payload["pages"][0]["html"]).is_file())

    def test_package_command_has_no_live_publishing_option(self) -> None:
        package_parser = next(
            action.choices["package"]
            for action in build_parser()._actions
            if getattr(action, "choices", None) and "package" in action.choices
        )
        option_strings = {
            option
            for action in package_parser._actions
            for option in action.option_strings
        }

        self.assertNotIn("--live", option_strings)
        self.assertNotIn("--no-dry-run", option_strings)

    def test_web_subcommand_defaults_to_localhost_and_rejects_other_hosts(self) -> None:
        args = build_parser().parse_args(["web"])
        self.assertEqual("127.0.0.1", args.host)
        self.assertEqual(8080, args.port)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources_path = root / "sources.json"
            sources_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            exit_code, payload = self._run(
                [
                    "web",
                    "--db",
                    str(root / "autoposting.db"),
                    "--sources",
                    str(sources_path),
                    "--host",
                    "0.0.0.0",
                ]
            )
            self.assertEqual(1, exit_code)
            self.assertIn("127.0.0.1", payload["error"])

    def test_web_subcommand_wires_lazy_default_renderer(self) -> None:
        captured: dict[str, object] = {}

        def fake_renderer(url: str) -> str:
            raise AssertionError(f"renderer should remain lazy: {url}")

        def capture_run_web(app: object, host: str, port: int) -> None:
            captured["app"] = app
            captured["host"] = host
            captured["port"] = port

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sources_path = root / "sources.json"
            sources_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch("autoposting.web.playwright_render_fetcher", fake_renderer), patch(
                "autoposting.web.run_web", side_effect=capture_run_web
            ):
                exit_code, payload = self._run(
                    [
                        "web",
                        "--db",
                        str(root / "autoposting.db"),
                        "--sources",
                        str(sources_path),
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        app = captured["app"]
        self.assertIs(fake_renderer, app.render_fetcher)
        self.assertIs(fake_renderer, app.workbench.render_fetcher)

    def test_init_event_draft_review_and_package_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "autoposting.db"
            sources_path = root / "sources.json"
            facts_path = root / "facts.json"
            sources_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            facts_path.write_text(
                json.dumps(
                    {
                        "event": "Model One launch",
                        "claims": [
                            {
                                "text": "Model One is available today.",
                                "source_ids": ["lab_blog"],
                                "confidence": "high",
                            }
                        ],
                        "unknowns": ["Independent benchmarks are unavailable."],
                        "conflicts": [],
                    }
                ),
                encoding="utf-8",
            )

            init_code, init_result = self._run(
                ["init", "--db", str(db_path), "--sources", str(sources_path)]
            )
            event_code, event_result = self._run(
                [
                    "event-create",
                    "--db",
                    str(db_path),
                    "--name",
                    "Model One launch",
                    "--topic",
                    "models",
                    "--stable-key",
                    "daily:2026-08-08:model-one",
                ]
            )
            draft_code, draft_result = self._run(
                [
                    "draft",
                    "--db",
                    str(db_path),
                    "--event-id",
                    str(event_result["event_id"]),
                    "--facts",
                    str(facts_path),
                ]
            )
            draft_ids = [draft["draft_id"] for draft in draft_result["drafts"]]
            for draft_id in draft_ids:
                review_code, _ = self._run(
                    [
                        "approve",
                        "--db",
                        str(db_path),
                        "--draft-id",
                        str(draft_id),
                        "--reviewer",
                        "human-reviewer",
                    ]
                )
                self.assertEqual(0, review_code)
            package_code, package_result = self._run(
                [
                    "package",
                    "--db",
                    str(db_path),
                    "--draft-id",
                    str(draft_ids[0]),
                    "--output",
                    str(root / "packages"),
                ]
            )

            self.assertEqual(0, init_code)
            self.assertEqual(1, init_result["sources_registered"])
            self.assertEqual(0, event_code)
            self.assertEqual(0, draft_code)
            self.assertEqual({"wechat", "xiaohongshu"}, {item["platform"] for item in draft_result["drafts"]})
            self.assertEqual(0, package_code)
            self.assertEqual("DRY_RUN", package_result["status"])
            self.assertTrue(Path(str(package_result["package_path"])).exists())
            self.assertTrue(Path(str(package_result["archive_path"])).exists())
            self.assertEqual("APPROVED", Database(db_path).get_event(str(event_result["event_id"]))["status"])


if __name__ == "__main__":
    unittest.main()
