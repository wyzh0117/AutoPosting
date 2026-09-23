import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from autoposting.config import ConfigError, load_sources
from autoposting.db import Database
from autoposting.pipeline import collect_sources
from autoposting.models import Source
from autoposting.selfcheck import run_self_check

from test_ingestion import RSS_FEED


class ConfigAndPipelineTests(unittest.TestCase):
    def test_existing_database_gains_translation_model_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE model_endpoints (
                        endpoint_id TEXT PRIMARY KEY,
                        base_url TEXT NOT NULL,
                        models_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE model_endpoint_purposes (
                        purpose TEXT PRIMARY KEY CHECK (purpose IN ('article', 'chat')),
                        endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
                        model TEXT NOT NULL,
                        effort TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO model_endpoints VALUES (
                        'legacy', 'https://models.example/v1', '[]', 'now', 'now'
                    );
                    INSERT INTO model_endpoint_purposes VALUES (
                        'article', 'legacy', 'old-model', 'none', 'now'
                    );
                    """
                )

            database = Database(path)
            database.initialize()
            database.set_model_purpose("translation", "legacy", "translate-model", "none")

            self.assertEqual("old-model", database.get_model_purpose("article")["model"])
            self.assertEqual(
                "translate-model", database.get_model_purpose("translation")["model"]
            )
            database.set_model_purpose("leaderboard", "legacy", "board-model", "none")
            self.assertEqual("board-model", database.get_model_purpose("leaderboard")["model"])

    def test_source_registry_loads_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "Example AI Lab",
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

            sources = load_sources(path)

            self.assertEqual(1, len(sources))
            self.assertEqual(15, sources[0].fetch_interval_minutes)
            self.assertEqual(("models",), sources[0].topics)

    def test_source_registry_accepts_canonical_type_fields_without_legacy_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "official_blog",
                                "name": "Official blog",
                                "source_type": "official",
                                "adapter_type": "rss",
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

            source = load_sources(path)[0]

            self.assertEqual("rss", source.type)
            self.assertEqual("official", source.source_type)
            self.assertEqual("rss", source.adapter_type)

    def test_enabled_x_source_requires_manual_identity_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "staff_account",
                                "name": "Unconfirmed staff",
                                "type": "x",
                                "url_or_handle": "@unconfirmed",
                                "tier": "S2",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                                "identity_verified": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "identity_verified"):
                load_sources(path)

    def test_s4_x_source_can_enable_without_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "x_watch",
                                "name": "X watch",
                                "type": "x",
                                "url_or_handle": "@example",
                                "tier": "S4",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sources = load_sources(path)
            self.assertEqual("x", sources[0].type)
            self.assertTrue(sources[0].enabled)
            self.assertEqual("S4", sources[0].tier)

    def test_content_filter_must_be_ai_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "Example AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                                "content_filter": "sports",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "content_filter"):
                load_sources(path)

    def test_topic_scope_and_language_are_parsed_and_normalised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "name": "Example AI Lab",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                                "topic_scope": "  Models  ",
                                "language": "EN-us",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            source = load_sources(path)[0]

            self.assertEqual("models", source.topic_scope)
            self.assertEqual("en-us", source.language)

    def test_invalid_topic_scope_and_language_are_rejected(self) -> None:
        for field, value, pattern in (
            ("topic_scope", "9 bad scope", "topic_scope"),
            ("language", "english language", "language"),
        ):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as temp_dir:
                    path = Path(temp_dir) / "sources.json"
                    path.write_text(
                        json.dumps(
                            {
                                "sources": [
                                    {
                                        "source_id": "lab_blog",
                                        "name": "Example AI Lab",
                                        "type": "rss",
                                        "url_or_handle": "https://example.com/feed.xml",
                                        "tier": "S1",
                                        "topics": ["models"],
                                        "enabled": True,
                                        "fetch_interval": "15m",
                                        field: value,
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ConfigError, pattern):
                        load_sources(path)

    def test_missing_required_source_field_is_still_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "lab_blog",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                                "topic_scope": "models",
                                "language": "en",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "missing fields: name"):
                load_sources(path)

    def test_unimplemented_github_adapter_fails_as_unsupported_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "repo_watch",
                "Repo watch",
                "",
                "https://github.com/openai/openai-python",
                "S1",
                ("models",),
                True,
                60,
                source_type="repository",
                adapter_type="github",
            )
            db.upsert_source(source)

            def fetcher(url: str) -> bytes:  # pragma: no cover - must never run
                raise AssertionError(f"unsupported adapter must not fetch: {url}")

            result = collect_sources(db, (source,), fetcher=fetcher, force=True)

            self.assertEqual(1, result.failed)
            self.assertEqual("unsupported_source", result.errors["repo_watch"]["cause_code"])
            self.assertEqual(
                "collector_not_implemented_for_github", result.errors["repo_watch"]["detail"]
            )
            # GitHub Releases must be registered as rss/releases.atom until the
            # github adapter exists.
            self.assertIn(
                "collector_not_implemented_for_github",
                str(db.get_source("repo_watch")["last_error"]),
            )

    def test_collection_respects_interval_and_records_source_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "sources.json"
            path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "source_id": "working",
                                "name": "Working feed",
                                "type": "rss",
                                "url_or_handle": "https://example.com/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            },
                            {
                                "source_id": "broken",
                                "name": "Broken feed",
                                "type": "rss",
                                "url_or_handle": "https://broken.example/feed.xml",
                                "tier": "S1",
                                "topics": ["models"],
                                "enabled": True,
                                "fetch_interval": "15m",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            db = Database(root / "autoposting.db")
            db.initialize()
            sources = load_sources(path)

            def fetcher(url: str) -> bytes:
                if "broken" in url:
                    raise OSError("simulated feed outage")
                return RSS_FEED

            first = collect_sources(db, sources, fetcher=fetcher)
            second = collect_sources(db, sources, fetcher=fetcher)

            self.assertEqual(1, first.inserted)
            self.assertEqual(1, first.failed)
            self.assertEqual("source_fetch_failed", first.errors["broken"]["code"])
            self.assertEqual("unreachable", first.errors["broken"]["cause_code"])
            self.assertIn("simulated feed outage", first.errors["broken"]["detail"])
            self.assertTrue(first.errors["broken"]["degraded"])
            self.assertTrue(first.run_id)
            self.assertEqual(("working", "broken"), first.requested_sources)
            first_run = db.get_collection_run(first.run_id)
            self.assertEqual("FAILED", first_run["status"])
            self.assertEqual(first.items_fetched, first_run["items_fetched"])
            self.assertEqual(first.items_after_dedupe, first_run["items_after_dedupe"])
            self.assertEqual(
                {"working": "SUCCESS", "broken": "FAILED"},
                {row["source_id"]: row["status"] for row in first_run["source_attempts"]},
            )
            self.assertEqual(2, second.skipped_not_due)
            self.assertIn("simulated feed outage", db.get_source("broken")["last_error"])

    def test_database_runtime_source_state_is_not_overwritten_by_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            runtime = Source(
                "runtime",
                "Runtime source",
                "rss",
                "https://example.com/feed.xml",
                "S2",
                ("models",),
                True,
                15,
                source_type="official",
                adapter_type="rss",
                topic_scope="models",
                language="en",
            )
            db.upsert_source(runtime)
            db.set_source_enabled("runtime", False)
            db.set_source_fetch_interval("runtime", 60)

            # A config reload carries edited metadata; runtime toggles must win.
            db.upsert_source(
                Source(
                    "runtime",
                    "Runtime source v2",
                    "rss",
                    "https://example.com/feed-v2.xml",
                    "S2",
                    ("models", "products"),
                    True,
                    15,
                    source_type="official",
                    adapter_type="rss",
                    topic_scope="devtools",
                    language="en",
                )
            )

            row = db.get_source("runtime")
            self.assertEqual(0, row["enabled"])
            self.assertEqual(60, row["fetch_interval_minutes"])
            self.assertEqual("official", row["source_type"])
            self.assertEqual("rss", row["adapter_type"])
            self.assertEqual("Runtime source v2", row["name"])
            self.assertEqual("https://example.com/feed-v2.xml", row["url_or_handle"])
            self.assertEqual("devtools", row["topic_scope"])
            self.assertEqual("en", row["language"])

    def test_example_registry_uses_first_party_feeds(self) -> None:
        sources = load_sources(
            Path(__file__).resolve().parents[1] / "config" / "sources.example.json"
        )
        enabled = [source for source in sources if source.enabled]
        disabled_ids = {source.source_id for source in sources if not source.enabled}

        self.assertGreaterEqual(len(enabled), 8)
        self.assertTrue(all(source.type in {"rss", "sitemap", "reddit"} for source in enabled))
        reddit = [source for source in enabled if source.type == "reddit"]
        self.assertEqual(11, len(reddit))
        self.assertTrue(all(source.tier == "S4" for source in reddit))
        self.assertTrue(
            all("aihot.virxact.com" not in source.url_or_handle for source in enabled)
        )
        self.assertIn("aihot_aggregate", disabled_ids)
        self.assertTrue(any(source.source_id == "openai_news" for source in enabled))
        self.assertTrue(any(source.source_id == "deepseek_official" for source in enabled))
        deepseek = next(source for source in enabled if source.source_id == "deepseek_official")
        self.assertEqual("sitemap", deepseek.type)
        self.assertEqual("S1", deepseek.tier)
        self.assertIn("api-docs.deepseek.com", deepseek.url_or_handle)
        self.assertTrue(any(source.source_id == "ithome" for source in enabled))
        mixed = [source for source in enabled if source.source_id in {"ithome", "hn_frontpage"}]
        self.assertTrue(mixed)
        self.assertTrue(all(source.content_filter == "ai" for source in mixed))
        x_sources = [source for source in sources if source.type == "x"]
        self.assertGreaterEqual(len(x_sources), 8)
        self.assertTrue(all(not source.enabled for source in x_sources))
        official = next(source for source in x_sources if source.source_id == "x_openai")
        self.assertEqual("@OpenAI", official.url_or_handle)
        self.assertTrue(official.identity_verified)

    def test_example_registry_carries_every_registry_field(self) -> None:
        registry_path = Path(__file__).resolve().parents[1] / "config" / "sources.example.json"
        sources = load_sources(registry_path)
        by_id = {source.source_id: source for source in sources}

        # topic_scope/language are required for new registry entries; the shipped
        # file carries both on every entry even though old user configs may omit
        # them (see REQUIRED_SOURCE_FIELDS).  Check the raw file, not the parsed
        # Source, because parsing fills canonical defaults for absent keys.
        required_fields = {
            "source_id",
            "name",
            "source_type",
            "type",
            "adapter_type",
            "url_or_handle",
            "tier",
            "topics",
            "enabled",
            "fetch_interval",
            "identity_verified",
            "topic_scope",
            "language",
        }
        for entry in json.loads(registry_path.read_text(encoding="utf-8"))["sources"]:
            self.assertFalse(required_fields - entry.keys(), entry.get("source_id"))
        for source in sources:
            self.assertTrue(source.topic_scope, source.source_id)
            self.assertTrue(source.language, source.source_id)

        google = by_id["google_ai_blog"]
        self.assertEqual(
            "https://blog.google/innovation-and-ai/technology/ai/rss/",
            google.url_or_handle,
        )
        self.assertEqual("official", google.source_type)
        self.assertEqual("rss", google.adapter_type)
        self.assertEqual("S1", google.tier)
        self.assertTrue(google.enabled)
        self.assertTrue(google.identity_verified)

        official_additions = [
            "nvidia_developer_blog",
            "github_releases_openai_python",
            "github_releases_transformers",
            "github_releases_anthropic_sdk",
            "github_releases_langchain",
            "github_releases_ollama",
        ]
        for source_id in official_additions:
            source = by_id[source_id]
            self.assertTrue(source.enabled, source_id)
            self.assertEqual("S1", source.tier, source_id)
            self.assertTrue(source.identity_verified, source_id)
            self.assertEqual("rss", source.adapter_type, source_id)
        nvidia = by_id["nvidia_developer_blog"]
        self.assertEqual("official", nvidia.source_type)
        self.assertEqual("developer.nvidia.com", urlsplit(nvidia.url_or_handle).netloc)
        # GitHub Releases are registered through the existing rss adapter: the
        # github/api/account adapters are schema-legal but not implemented.
        github = by_id["github_releases_openai_python"]
        self.assertEqual("repository", github.source_type)
        self.assertEqual("rss", github.adapter_type)
        self.assertIn("/releases.atom", github.url_or_handle)

        community_additions = {
            "reddit_machinelearning",
            "reddit_artificial",
            "reddit_llmdevs",
            "reddit_ai_agents",
            "reddit_singularity",
            "github_blog",
            "latent_space",
            "lobsters",
        }
        self.assertTrue(community_additions <= set(by_id))
        expected_scopes = {
            "github_blog": "devtools",
            "latent_space": "devtools",
            "lobsters": "devtools",
            "reddit_machinelearning": "community",
            "reddit_artificial": "community",
            "reddit_singularity": "community",
        }
        for source_id, scope in expected_scopes.items():
            self.assertEqual(scope, by_id[source_id].topic_scope, source_id)
        self.assertTrue(by_id["github_blog"].content_filter == "ai")
        self.assertTrue(by_id["lobsters"].content_filter == "ai")

        media_additions = ["mit_tech_review", "wired_ai"]
        for source_id in media_additions:
            source = by_id[source_id]
            self.assertTrue(source.enabled, source_id)
            self.assertEqual("S3", source.tier, source_id)
            self.assertEqual("media", source.source_type, source_id)
            self.assertEqual("business", source.topic_scope, source_id)
            self.assertTrue(source.identity_verified, source_id)

        # Meta AI stays disabled: no verified first-party feed from this host.
        meta = by_id["meta_ai_news"]
        self.assertFalse(meta.enabled)
        self.assertFalse(meta.identity_verified)
        self.assertIn("unverified", meta.name)

    def test_one_failed_attempt_increments_the_streak_exactly_once(self) -> None:
        """A single failed fetch must count as one failure, not two.

        The collectors used to record the failure and re-raise, and the pipeline
        recorded the re-raised exception again, so the registry showed
        consecutive_failures = 2 after one attempt.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "broken_feed",
                "Broken feed",
                "rss",
                "https://example.invalid/feed.xml",
                "S2",
                ("models",),
                True,
                60,
                adapter_type="rss",
            )
            db.upsert_source(source)

            def fetcher(url: str) -> bytes:
                raise OSError("connection reset by peer")

            # Three separate attempts, each of which must add exactly one.
            # assertLogs also keeps the intentional failure traceback out of the
            # suite output, and proves the attempt is still logged once.
            for expected in (1, 2, 3):
                with self.assertLogs("autoposting.pipeline", level="ERROR") as captured:
                    result = collect_sources(db, (source,), fetcher=fetcher, force=True)
                self.assertEqual(1, len(captured.records))
                self.assertIn("connection reset by peer", captured.output[0])
                self.assertEqual(1, result.failed)
                row = db.get_source("broken_feed")
                self.assertEqual(expected, row["consecutive_failures"])
                # A failure must never overwrite the last success timestamp.
                self.assertIsNone(row["last_success_at"])

            # One success resets the streak and stamps the success time.
            def ok_fetcher(url: str) -> bytes:
                return (
                    b'<?xml version="1.0"?><rss version="2.0"><channel>'
                    b"<item><title>ok</title><link>https://example.com/ok</link>"
                    b"<description>ok</description></item></channel></rss>"
                )

            collect_sources(db, (source,), fetcher=ok_fetcher, force=True)
            row = db.get_source("broken_feed")
            self.assertEqual(0, row["consecutive_failures"])
            self.assertIsNotNone(row["last_success_at"])

    def test_x_collect_without_token_is_recorded_as_source_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source("x_watch", "X watch", "x", "@sama", "S4", ("models",), True, 15)
            result = collect_sources(db, (source,), force=True)
            self.assertEqual(1, result.failed)
            self.assertEqual("credential_required", result.errors["x_watch"]["cause_code"])
            self.assertEqual("x_bearer_token_required", result.errors["x_watch"]["detail"])
            self.assertIn("x_bearer_token_required", str(db.get_source("x_watch")["last_error"]))

    def test_existing_database_gains_sitemap_source_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            db = Database(path)
            db.initialize()
            with db.connect() as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript(
                    """
                    DROP TABLE sources;
                    CREATE TABLE sources (
                        source_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x')),
                        url_or_handle TEXT NOT NULL,
                        tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
                        topics_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                        fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
                        last_fetched_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT INTO sources VALUES (
                        'lab_blog', 'Lab', 'rss', 'https://lab.example/feed.xml', 'S1',
                        '["models"]', 1, 15, NULL, NULL, 't', 't'
                    );
                    """
                )
                connection.execute("PRAGMA foreign_keys = ON")
            db.initialize()
            db.upsert_source(
                Source(
                    "deepseek_official",
                    "DeepSeek Official",
                    "sitemap",
                    "https://api-docs.deepseek.com/sitemap.xml",
                    "S1",
                    ("models",),
                    True,
                    15,
                )
            )
            self.assertEqual("sitemap", db.get_source("deepseek_official")["type"])
            self.assertEqual("lab_blog", db.get_source("lab_blog")["source_id"])

    def test_offline_self_check_proves_acceptance_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_self_check(Path(temp_dir))
            repeated_report = run_self_check(Path(temp_dir))

            self.assertTrue(report["ok"])
            self.assertTrue(all(report["checks"].values()))
            self.assertTrue(repeated_report["ok"])
            self.assertEqual(1, report["metrics"]["items_after_duplicate_collection"])
            self.assertEqual(2, report["metrics"]["drafts"])
            self.assertEqual(1, report["metrics"]["dry_run_attempts"])


if __name__ == "__main__":
    unittest.main()
