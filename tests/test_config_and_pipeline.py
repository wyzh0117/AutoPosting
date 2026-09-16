import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
            self.assertIn("simulated feed outage", first.errors["broken"])
            self.assertEqual(2, second.skipped_not_due)
            self.assertIn("simulated feed outage", db.get_source("broken")["last_error"])

    def test_example_registry_uses_first_party_feeds(self) -> None:
        sources = load_sources(
            Path(__file__).resolve().parents[1] / "config" / "sources.example.json"
        )
        enabled = [source for source in sources if source.enabled]
        disabled_ids = {source.source_id for source in sources if not source.enabled}

        self.assertGreaterEqual(len(enabled), 8)
        self.assertTrue(all(source.type in {"rss", "sitemap", "reddit"} for source in enabled))
        reddit = [source for source in enabled if source.type == "reddit"]
        self.assertEqual(6, len(reddit))
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

    def test_x_collect_without_token_is_recorded_as_source_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source("x_watch", "X watch", "x", "@sama", "S4", ("models",), True, 15)
            result = collect_sources(db, (source,), force=True)
            self.assertEqual(1, result.failed)
            self.assertEqual("x_bearer_token_required", result.errors["x_watch"])
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
