import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from autoposting.content import Draft
from autoposting.decision.contracts import PlannerResult, ResearchIntent, SearchPlan
from autoposting.db import Database
from autoposting.models import FeedItem, Source
from autoposting.resource_discovery import ResourceCandidate, ResourceSet, ResourceStatus
from autoposting.workbench import HotspotService


V4_SOURCES_SCHEMA = """
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit', 'html', 'github', 'api', 'account')),
    url_or_handle TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
    topics_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
    source_type TEXT NOT NULL DEFAULT 'other' CHECK (source_type IN ('official', 'media', 'community', 'developer', 'research', 'repository', 'other')),
    adapter_type TEXT NOT NULL DEFAULT 'rss' CHECK (adapter_type IN ('rss', 'sitemap', 'html', 'reddit', 'github', 'api', 'account', 'x')),
    identity_verified INTEGER NOT NULL DEFAULT 0 CHECK (identity_verified IN (0, 1)),
    content_filter TEXT NOT NULL DEFAULT '' CHECK (content_filter IN ('', 'ai')),
    last_fetched_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

V5_REGISTRY_COLUMNS = {"topic_scope", "language", "last_success_at", "consecutive_failures"}


def _registry_columns(db: Database) -> set[str]:
    with db.connect() as connection:
        return {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(sources)").fetchall()
        }


def _legacy_v4_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(V4_SOURCES_SCHEMA)
        connection.execute(
            "INSERT INTO sources (source_id, name, type, url_or_handle, tier, topics_json, "
            "enabled, fetch_interval_minutes, source_type, adapter_type, identity_verified, "
            "content_filter, last_fetched_at, last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-v4",
                "Legacy V4",
                "rss",
                "https://example.test/legacy.xml",
                "S2",
                '["models"]',
                1,
                30,
                "official",
                "rss",
                1,
                "ai",
                "2026-09-19T00:00:00+00:00",
                None,
                "2026-09-01T00:00:00+00:00",
                "2026-09-19T00:00:00+00:00",
            ),
        )
        connection.execute("PRAGMA user_version = 4")


def _rfc822(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _rss_feed(entries: list[tuple[str, str, str, int]]) -> bytes:
    items = "".join(
        f"<item><guid>{guid}</guid><title>{title}</title><link>{url}</link>"
        f"<description>{title} summary</description>"
        f"<pubDate>{_rfc822(minutes_ago)}</pubDate></item>"
        for guid, title, url, minutes_ago in entries
    )
    return (
        "<rss version='2.0'><channel><title>Fixture</title>" + items + "</channel></rss>"
    ).encode("utf-8")


EMPTY_RSS_FEED = _rss_feed([])


class HotspotFavoriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_favorite_add_list_remove_round_trip(self) -> None:
        first = self.db.add_hotspot_favorite(
            "item-a",
            "Alpha title",
            "Alpha summary",
            {"item_id": "item-a", "title": "Alpha title", "summary": "Alpha summary"},
        )
        second = self.db.add_hotspot_favorite(
            "item-b",
            "Beta title",
            "Beta summary",
            {"item_id": "item-b", "title": "Beta title", "summary": "Beta summary"},
        )

        listed = self.db.list_hotspot_favorites()
        self.assertEqual(["item-b", "item-a"], [row["item_id"] for row in listed])
        self.assertEqual("Beta title", listed[0]["title"])
        self.assertEqual("Beta summary", listed[0]["summary"])
        self.assertEqual("item-b", listed[0]["payload"]["item_id"])
        self.assertTrue(first["favorited_at"])
        self.assertTrue(second["favorited_at"])
        self.assertGreaterEqual(second["favorited_at"], first["favorited_at"])

        removed = self.db.remove_hotspot_favorite("item-b")
        self.assertTrue(removed)
        remaining = self.db.list_hotspot_favorites()
        self.assertEqual(["item-a"], [row["item_id"] for row in remaining])
        self.assertFalse(self.db.remove_hotspot_favorite("item-b"))

    def test_favorite_envelope_and_corruption_diagnostic_preserve_raw_value(self) -> None:
        self.db.add_hotspot_favorite("item-a", "Alpha", "Summary", {"extra": "value"})
        with self.db.connect() as connection:
            raw = str(
                connection.execute(
                    "SELECT payload_json FROM hotspot_favorites WHERE item_id = 'item-a'"
                ).fetchone()["payload_json"]
            )
            self.assertEqual("favorite-payload-v1", json.loads(raw)["schema_version"])
            connection.execute(
                "UPDATE hotspot_favorites SET payload_json = ? WHERE item_id = 'item-a'",
                ('{"schema_version":"future-v9","data":{}}',),
            )

        listed = self.db.list_hotspot_favorites()
        self.assertEqual("item-a", listed[0]["item_id"])
        self.assertTrue(listed[0]["degraded"])
        self.assertEqual("favorite_payload_unknown_schema_version", listed[0]["cause_code"])
        with self.db.connect() as connection:
            self.assertEqual(
                '{"schema_version":"future-v9","data":{}}',
                connection.execute(
                    "SELECT payload_json FROM hotspot_favorites WHERE item_id = 'item-a'"
                ).fetchone()["payload_json"],
            )


class ScheduledJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        event_id = self.db.create_event("Schedule", "tests", "TEST", "schedule:db")
        self.db.upsert_draft(
            Draft(
                "draft-schedule-db",
                event_id,
                "wechat",
                "标题",
                "正文",
                (),
                ("source",),
            )
        )
        self.job_counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _job(self) -> str:
        self.job_counter += 1
        row = self.db.create_scheduled_job(
            "draft-schedule-db",
            "wechat",
            False,
            f"2026-09-19T00:00:{self.job_counter:02d}+00:00",
        )
        return str(row["job_id"])

    def test_finish_only_updates_running_and_returns_whether_it_wrote(self) -> None:
        job_id = self._job()
        self.assertFalse(self.db.finish_scheduled_job(job_id, "DONE", result_json="{}"))
        self.assertEqual("PENDING", self.db.get_scheduled_job(job_id)["status"])
        self.assertTrue(self.db.claim_scheduled_job(job_id))
        self.assertTrue(self.db.finish_scheduled_job(job_id, "DONE", result_json="{}"))
        self.assertFalse(self.db.finish_scheduled_job(job_id, "FAILED", error_message="late"))
        self.assertEqual("DONE", self.db.get_scheduled_job(job_id)["status"])

        with self.db.connect() as connection:
            result_json = str(
                connection.execute(
                    "SELECT result_json FROM scheduled_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()["result_json"]
            )
        self.assertEqual("scheduled-result-v1", json.loads(result_json)["schema_version"])
        self.assertEqual({}, self.db.get_scheduled_job(job_id)["result"])

    def test_scheduled_result_corruption_is_diagnostic_without_status_change(self) -> None:
        job_id = self._job()
        self.assertTrue(self.db.claim_scheduled_job(job_id))
        self.assertTrue(self.db.finish_scheduled_job(job_id, "DONE", result_json='{"ok":true}'))
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE scheduled_jobs SET result_json = ? WHERE job_id = ?",
                ('{"schema_version":"future-v9","data":{}}', job_id),
            )
        row = self.db.get_scheduled_job(job_id)
        self.assertEqual("DONE", row["status"])
        self.assertTrue(row["degraded"])
        self.assertEqual("scheduled_result_unknown_schema_version", row["cause_code"])

    def test_stale_reconcile_only_repairs_non_active_running_rows(self) -> None:
        stale_id = self._job()
        active_id = self._job()
        done_id = self._job()
        cancelled_id = self._job()
        self.assertTrue(self.db.claim_scheduled_job(stale_id))
        self.assertTrue(self.db.claim_scheduled_job(active_id))
        self.assertTrue(self.db.claim_scheduled_job(done_id))
        self.assertTrue(self.db.finish_scheduled_job(done_id, "DONE", result_json="{}"))
        self.assertEqual("PENDING", self.db.get_scheduled_job(cancelled_id)["status"])
        self.assertEqual("CANCELLED", self.db.cancel_scheduled_job(cancelled_id)["status"])

        repaired = self.db.reconcile_stale_running_jobs(
            "9999-01-01T00:00:00+00:00",
            active_job_ids=[active_id],
        )
        self.assertEqual([stale_id], repaired)
        self.assertEqual("FAILED", self.db.get_scheduled_job(stale_id)["status"])
        self.assertEqual(
            "scheduler_finish_failed",
            self.db.get_scheduled_job(stale_id)["error_message"],
        )
        self.assertEqual("RUNNING", self.db.get_scheduled_job(active_id)["status"])
        self.assertEqual("DONE", self.db.get_scheduled_job(done_id)["status"])
        self.assertEqual("CANCELLED", self.db.get_scheduled_job(cancelled_id)["status"])


class JsonEnvelopeAndMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_github_weekly_writes_envelopes_and_reads_legacy_v0(self) -> None:
        repos = [{"name": "repo", "description": "desc"}]
        images = [{"id": "cover", "kind": "cover"}]
        visual = {"theme": "dark"}
        diagnostics = [{"code": "github_fetch_failed", "cause_code": "timeout"}]
        self.db.save_github_weekly(
            "2026-09-19", repos, "title", "body", "model", images, visual, diagnostics
        )
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM github_weekly WHERE id = 1").fetchone()
            self.assertEqual("github-weekly-repos-v1", json.loads(row["repos_json"])["schema_version"])
            self.assertEqual("github-weekly-images-v1", json.loads(row["images_json"])["schema_version"])
            self.assertEqual("github-weekly-visual-v1", json.loads(row["visual_json"])["schema_version"])
            self.assertEqual(
                "github-weekly-diagnostics-v1",
                json.loads(row["diagnostics_json"])["schema_version"],
            )
            connection.execute(
                "UPDATE github_weekly SET repos_json = ?, images_json = ?, visual_json = ? WHERE id = 1",
                (json.dumps(repos), json.dumps(images), json.dumps(visual)),
            )

        snapshot = self.db.get_github_weekly()
        self.assertEqual(repos, snapshot["repos"])
        self.assertEqual(images, snapshot["images"])
        self.assertEqual(visual, snapshot["visual"])
        self.assertTrue(snapshot["degraded"])
        self.assertEqual(diagnostics, snapshot["degraded_reasons"])

    def test_corrupt_github_weekly_field_is_not_written_back(self) -> None:
        self.db.save_github_weekly("2026-09-19", [{"name": "repo"}], "title", "body")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE github_weekly SET repos_json = ? WHERE id = 1",
                ('{"schema_version":"future-v9"}',),
            )
        snapshot = self.db.get_github_weekly()
        self.assertEqual([], snapshot["repos"])
        self.assertTrue(snapshot["degraded"])
        self.assertIn("github_weekly_repos_unknown_schema_version", snapshot["degraded_reasons"])
        with self.db.connect() as connection:
            self.assertEqual(
                '{"schema_version":"future-v9"}',
                connection.execute(
                    "SELECT repos_json FROM github_weekly WHERE id = 1"
                ).fetchone()["repos_json"],
            )

    def test_skill_snapshot_envelope_and_degraded_read(self) -> None:
        payload = {"query": "json", "user_title": "我的标题", "skills": []}
        self.db.save_skill_finder(payload)
        with self.db.connect() as connection:
            row = connection.execute("SELECT payload_json FROM skill_finder WHERE id = 1").fetchone()
            self.assertEqual("skill-finder-snapshot-v1", json.loads(row["payload_json"])["schema_version"])
            connection.execute(
                "UPDATE skill_finder SET payload_json = ? WHERE id = 1",
                ("not-json",),
            )
        snapshot = self.db.get_skill_finder()
        self.assertTrue(snapshot["degraded"])
        self.assertEqual("skill_finder_snapshot_invalid_json", snapshot["cause_code"])
        with self.db.connect() as connection:
            self.assertEqual(
                "not-json",
                connection.execute(
                    "SELECT payload_json FROM skill_finder WHERE id = 1"
                ).fetchone()["payload_json"],
            )

    def test_user_version_migration_is_idempotent_and_sanitizes_legacy_error(self) -> None:
        with self.db.connect() as connection:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
            connection.execute(
                "INSERT INTO sources (source_id, name, type, url_or_handle, tier, topics_json, "
                "enabled, fetch_interval_minutes, created_at, updated_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    "Legacy",
                    "rss",
                    "https://example.test/feed",
                    "S1",
                    "[]",
                    1,
                    15,
                    "2026-09-19T00:00:00+00:00",
                    "2026-09-19T00:00:00+00:00",
                    "https://example.test/?access_token=SECRET",
                ),
            )
            connection.execute("PRAGMA user_version = 0")

        self.db.initialize()
        source = self.db.get_source("legacy")
        self.assertTrue(str(source["last_error"]).startswith("legacy_source_error:"))
        self.assertNotIn("SECRET", str(source["last_error"]))
        first = source["last_error"]
        self.db.initialize()
        self.assertEqual(first, self.db.get_source("legacy")["last_error"])

    def test_user_version_migration_resanitizes_legacy_diagnostic(self) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO sources (source_id, name, type, url_or_handle, tier, topics_json, "
                "enabled, fetch_interval_minutes, created_at, updated_at, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-diagnostic",
                    "Legacy diagnostic",
                    "rss",
                    "https://example.test/feed",
                    "S1",
                    "[]",
                    1,
                    15,
                    "2026-09-19T00:00:00+00:00",
                    "2026-09-19T00:00:00+00:00",
                    "code=source_fetch_failed;cause_code=unknown_error;source=legacy-diagnostic;"
                    "stage=collect;degraded=1;detail=token=SECRET https://private.example/x",
                ),
            )
            connection.execute("PRAGMA user_version = 0")

        self.db.initialize()
        stored = str(self.db.get_source("legacy-diagnostic")["last_error"])
        self.assertNotIn("SECRET", stored)
        self.assertNotIn("private.example", stored)
        self.assertTrue(stored.startswith("code=source_fetch_failed;"))

    def test_failed_migration_does_not_advance_user_version(self) -> None:
        with self.db.connect() as connection:
            connection.execute("PRAGMA user_version = 0")
        with patch.object(self.db, "_migrate_v1", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_failed_initialize_rolls_back_schema_version_and_business_data(self) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO sources (source_id, name, type, url_or_handle, tier, topics_json, "
                "enabled, fetch_interval_minutes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "keep",
                    "Keep",
                    "rss",
                    "https://example.test/feed",
                    "S1",
                    "[]",
                    1,
                    15,
                    "2026-09-19T00:00:00+00:00",
                    "2026-09-19T00:00:00+00:00",
                ),
            )
            before_tables = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall()
            ]
            before_source = dict(
                connection.execute(
                    "SELECT source_id, name, url_or_handle FROM sources WHERE source_id = 'keep'"
                ).fetchone()
            )
            connection.execute("PRAGMA user_version = 0")

        with patch.object(self.db, "_migrate_v1", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.db.initialize()

        with self.db.connect() as connection:
            after_tables = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall()
            ]
            after_source = dict(
                connection.execute(
                    "SELECT source_id, name, url_or_handle FROM sources WHERE source_id = 'keep'"
                ).fetchone()
            )
            self.assertEqual(before_tables, after_tables)
            self.assertEqual(before_source, after_source)
            self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])

        fresh = Database(Path(self.temp_dir.name) / "fresh.db")
        with patch.object(fresh, "_migrate_v1", side_effect=RuntimeError("fresh boom")):
            with self.assertRaisesRegex(RuntimeError, "fresh boom"):
                fresh.initialize()
        with fresh.connect() as connection:
            self.assertEqual(
                [],
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall(),
            )
            self.assertEqual(0, connection.execute("PRAGMA user_version").fetchone()[0])


class SourceRegistryMigrationV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_v4_database_upgrades_to_v5_and_keeps_legacy_rows(self) -> None:
        path = Path(self.temp_dir.name) / "legacy-v4.db"
        _legacy_v4_database(path)
        self.assertFalse(V5_REGISTRY_COLUMNS & _registry_columns(Database(path)))

        upgraded = Database(path)
        upgraded.initialize()

        self.assertTrue(V5_REGISTRY_COLUMNS <= _registry_columns(upgraded))
        with upgraded.connect() as connection:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])

        row = upgraded.get_source("legacy-v4")
        self.assertEqual("Legacy V4", row["name"])
        self.assertEqual("https://example.test/legacy.xml", row["url_or_handle"])
        self.assertEqual("S2", row["tier"])
        self.assertEqual(1, row["enabled"])
        self.assertEqual(30, row["fetch_interval_minutes"])
        self.assertEqual("official", row["source_type"])
        self.assertEqual("rss", row["adapter_type"])
        self.assertEqual(1, row["identity_verified"])
        self.assertEqual("ai", row["content_filter"])
        self.assertEqual("2026-09-19T00:00:00+00:00", row["last_fetched_at"])
        self.assertIsNone(row["last_error"])
        # Documented defaults: scope/language are empty and the previous success
        # time stays unknown rather than being fabricated from an error-free row.
        self.assertEqual("", row["topic_scope"])
        self.assertEqual("", row["language"])
        self.assertIsNone(row["last_success_at"])
        self.assertEqual(0, row["consecutive_failures"])

        upgraded.initialize()
        with upgraded.connect() as connection:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(row, upgraded.get_source("legacy-v4"))

        fresh = Database(Path(self.temp_dir.name) / "fresh-v5.db")
        fresh.initialize()
        self.assertTrue(V5_REGISTRY_COLUMNS <= _registry_columns(fresh))
        with fresh.connect() as connection:
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])

    def test_failed_v5_migration_rolls_back_columns_and_version(self) -> None:
        path = Path(self.temp_dir.name) / "rollback-v4.db"
        _legacy_v4_database(path)
        failing = Database(path)

        with patch.object(failing, "_migrate_v5", side_effect=RuntimeError("v5 boom")):
            with self.assertRaisesRegex(RuntimeError, "v5 boom"):
                failing.initialize()

        self.assertFalse(V5_REGISTRY_COLUMNS & _registry_columns(failing))
        with failing.connect() as connection:
            self.assertEqual(4, connection.execute("PRAGMA user_version").fetchone()[0])
            row = connection.execute(
                "SELECT name, url_or_handle, enabled FROM sources WHERE source_id = 'legacy-v4'"
            ).fetchone()
        self.assertEqual("Legacy V4", row["name"])
        self.assertEqual("https://example.test/legacy.xml", row["url_or_handle"])
        self.assertEqual(1, row["enabled"])


class ResourceSetPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "autoposting.db"
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _resource_set(set_id: str = "set-1", created_at: str = "2026-09-22T00:00:00+00:00") -> ResourceSet:
        intent = ResearchIntent(
            goal="Find browser automation resources",
            concepts=("browser automation",),
            search_repo=True,
            search_skill=True,
            search_web=False,
            preferred_signals=("recent",),
            source_event_ids=("event-1",),
        )
        plan = SearchPlan("browser automation", ("github", "skills"), max_candidates=10)
        candidate = ResourceCandidate(
            resource_id="repo:acme/tool",
            kind="repo",
            name="Tool",
            url="https://github.com/acme/tool?access_token=ignored",
            source="github",
            description="A useful tool",
            metadata={"full_name": "acme/tool"},
            objective_score=88,
            selected=True,
            source_ids=("event-1",),
        )
        return ResourceSet(
            set_id=set_id,
            query="browser automation",
            plan=PlannerResult(intent=intent, plan=plan),
            preset="github_weekly",
            candidates=(candidate,),
            adapter_status={
                "github": {
                    "status": "ok",
                    "count": 1,
                    "cause_code": "",
                    "note": "token=SUPERSECRET",
                    "headers": {"Authorization": "Bearer SUPERSECRET"},
                }
            },
            status=ResourceStatus.OK,
            degraded=False,
            diagnostics=({"code": "resource_warning", "detail": "token=SUPERSECRET"},),
            created_at=created_at,
        )

    def test_v4_round_trip_keeps_candidates_and_redacts_secrets(self) -> None:
        resource_set = self._resource_set()
        self.db.save_resource_set(resource_set)

        payload = self.db.get_resource_set("set-1")
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual("resource-set-v1", payload["schema_version"])
        self.assertEqual("credential=<redacted>", payload["diagnostics"][0]["detail"])
        self.assertEqual("credential=<redacted>", payload["adapter_status"]["github"]["note"])
        # An absent adapter cause must survive persistence as "", not as the
        # sanitized-detail fallback "unknown_error" (round-trip fidelity).
        self.assertEqual("", payload["adapter_status"]["github"]["cause_code"])
        restored = ResourceSet.from_dict(payload)
        self.assertEqual("repo:acme/tool", restored.candidates[0].resource_id)
        self.assertEqual(1, restored.counts["repo"])

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM resource_sets WHERE set_id = 'set-1'"
            ).fetchone()
            self.assertEqual(5, connection.execute("PRAGMA user_version").fetchone()[0])
            envelope = json.loads(row["resource_set_json"])
            self.assertEqual("resource-set-envelope-v1", envelope["schema_version"])
            self.assertIn("candidates", envelope["data"])
            self.assertNotIn("SUPERSECRET", row["resource_set_json"])
            self.assertNotIn("headers", row["resource_set_json"])

    def test_restart_list_limit_and_delete(self) -> None:
        self.db.save_resource_set(self._resource_set())
        self.db.save_resource_set(
            self._resource_set("set-2", "2026-09-22T01:00:00+00:00")
        )

        reopened = Database(self.path)
        reopened.initialize()
        self.assertEqual("set-1", reopened.get_resource_set("set-1")["set_id"])
        self.assertEqual(["set-2"], [item["set_id"] for item in reopened.list_resource_sets(1)])
        self.assertTrue(reopened.delete_resource_set("set-1"))
        self.assertFalse(reopened.delete_resource_set("set-1"))
        self.assertIsNone(reopened.get_resource_set("set-1"))
        with self.assertRaisesRegex(ValueError, "resource_set_limit_invalid"):
            reopened.list_resource_sets(0)

    def test_unknown_or_corrupt_payload_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "resource_set_unknown_schema_version"):
            self.db.save_resource_set({"schema_version": "future-v9"})
        with self.assertRaisesRegex(ValueError, "resource_set_id_required"):
            self.db.get_resource_set("")

        self.db.save_resource_set(self._resource_set())
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE resource_sets SET resource_set_json = ? WHERE set_id = 'set-1'",
                ('{"schema_version":"future-v9","data":{}}',),
            )
        with self.assertRaisesRegex(ValueError, "resource_set_unknown_schema_version"):
            self.db.get_resource_set("set-1")
        with self.assertRaisesRegex(ValueError, "resource_set_unknown_schema_version"):
            self.db.list_resource_sets()

    def test_resource_set_storage_never_registers_subscription_sources(self) -> None:
        """Evidence storage keeps its own tables; only upsert_source writes sources."""
        self.db.save_resource_set(self._resource_set())
        with self.db.connect() as connection:
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )


class DraftListingTests(unittest.TestCase):
    """The publishing-center listing stays a basic, ordered persistence query."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        self.event_id = self.db.create_event("Publish", "tests", "TEST", "publish:db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _draft(self, draft_id: str, platform: str = "wechat", title: str = "标题") -> None:
        self.db.upsert_draft(
            Draft(draft_id, self.event_id, platform, title, "正文 AIGC", (), ("source",))
        )

    def test_list_drafts_orders_by_activity_and_joins_latest_attempt(self) -> None:
        self._draft("draft-a", title="第一篇")
        self._draft("draft-b", platform="xiaohongshu", title="第二篇")
        self.db.record_publication_attempt(
            "attempt-1", "draft-a", "wechat", False, "DRY_RUN", None, 0
        )

        rows = self.db.list_drafts(10)
        by_id = {row["draft_id"]: row for row in rows}
        self.assertEqual({"draft-a", "draft-b"}, set(by_id))
        self.assertEqual("DRY_RUN", by_id["draft-a"]["attempt_status"])
        self.assertEqual("", str(by_id["draft-b"]["attempt_status"] or ""))
        self.assertEqual("DRAFTED", by_id["draft-a"]["status"])
        self.assertEqual("wechat", by_id["draft-a"]["platform"])
        self.assertEqual("第二篇", by_id["draft-b"]["title"])
        # Newest activity first: draft-b was written last.
        self.assertEqual("draft-b", rows[0]["draft_id"])

        self.db.approve_draft("draft-a", "human")
        self.assertEqual("APPROVED", {row["draft_id"]: row for row in self.db.list_drafts(10)}["draft-a"]["status"])

        with self.assertRaisesRegex(ValueError, "draft_limit_invalid"):
            self.db.list_drafts(0)
        with self.assertRaisesRegex(ValueError, "draft_limit_invalid"):
            self.db.list_drafts(True)


class CollectionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "autoposting.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.db.upsert_source(
            Source(
                "official",
                "Official feed",
                "rss",
                "https://example.com/feed.xml",
                "S1",
                ("models",),
                True,
                15,
                source_type="official",
                adapter_type="rss",
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_collection_run_attempts_and_diagnostics_survive_reopen(self) -> None:
        run_id = self.db.start_collection_run(
            ("official",), started_at="2026-09-22T08:00:00+00:00"
        )
        self.assertEqual("RUNNING", self.db.get_collection_run(run_id)["status"])
        self.assertEqual(
            "PENDING", self.db.get_collection_run(run_id)["source_attempts"][0]["status"]
        )

        self.db.record_collection_source_attempt(run_id, "official", "RUNNING")
        self.db.record_collection_source_attempt(
            run_id,
            "official",
            "FAILED",
            items_fetched=4,
            items_inserted=2,
            items_after_dedupe=2,
            diagnostic={
                "code": "source_fetch_failed",
                "cause_code": "unreachable",
                "source": "official",
                "stage": "collect",
                "detail": "feed outage",
            },
        )
        finished = self.db.finish_collection_run(
            run_id,
            status="FAILED",
            finished_at="2026-09-22T08:00:03+00:00",
            clusters_created_or_updated=3,
        )

        self.assertEqual("FAILED", finished["status"])
        self.assertEqual(["official"], finished["attempted_sources"])
        self.assertEqual(["official"], finished["failed_sources"])
        self.assertEqual(4, finished["items_fetched"])
        self.assertEqual(2, finished["items_after_dedupe"])
        self.assertEqual(3, finished["clusters_created_or_updated"])
        self.assertTrue(finished["degraded"])
        self.assertEqual("unreachable", finished["diagnostics"][0]["cause_code"])

        reopened = Database(self.path)
        reopened.initialize()
        restored = reopened.get_collection_run(run_id)
        self.assertEqual(finished["status"], restored["status"])
        self.assertEqual(finished["diagnostics"], restored["diagnostics"])
        self.assertEqual(
            "FAILED", restored["source_attempts"][0]["status"]
        )
        self.assertEqual([run_id], [row["run_id"] for row in reopened.list_collection_runs()])


class SourceRegistryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        self.db.upsert_source(
            Source(
                "runtime",
                "Runtime",
                "rss",
                "https://example.test/feed.xml",
                "S2",
                ("models",),
                True,
                15,
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _health(self) -> dict[str, str]:
        return {str(row["source_id"]): str(row["health"]) for row in self.db.list_sources()}

    def test_record_fetch_result_tracks_last_success_and_failure_streak(self) -> None:
        self.db.record_fetch_result("runtime")
        first = self.db.get_source("runtime")
        self.assertTrue(first["last_success_at"])
        self.assertIsNone(first["last_error"])
        self.assertEqual(0, first["consecutive_failures"])
        success_at = str(first["last_success_at"])

        diagnostic = {
            "code": "source_fetch_failed",
            "cause_code": "unreachable",
            "source": "runtime",
            "stage": "collect",
            "detail": "feed outage",
        }
        self.db.record_fetch_result("runtime", diagnostic)
        failed = self.db.get_source("runtime")
        # The previous success time is history: a failure must not erase it.
        self.assertEqual(success_at, failed["last_success_at"])
        self.assertEqual(1, failed["consecutive_failures"])
        self.assertTrue(
            str(failed["last_error"]).startswith(
                "code=source_fetch_failed;cause_code=unreachable;source=runtime;stage=collect;"
            )
        )

        self.db.record_fetch_result("runtime", diagnostic)
        self.assertEqual(2, self.db.get_source("runtime")["consecutive_failures"])

        self.db.record_fetch_result("runtime")
        recovered = self.db.get_source("runtime")
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(0, recovered["consecutive_failures"])
        self.assertGreaterEqual(str(recovered["last_success_at"]), success_at)

    def test_list_sources_health_is_derived_from_stored_evidence(self) -> None:
        for source_id in ("never", "broken", "off"):
            self.db.upsert_source(
                Source(
                    source_id,
                    source_id.title(),
                    "rss",
                    f"https://example.test/{source_id}.xml",
                    "S2",
                    ("models",),
                    True,
                    15,
                )
            )
        self.db.set_source_enabled("off", False)
        self.db.record_fetch_result("runtime")
        self.db.record_fetch_result(
            "broken",
            {"code": "source_fetch_failed", "cause_code": "unreachable", "detail": "outage"},
        )

        self.assertEqual(
            {"runtime": "ok", "never": "never", "broken": "error", "off": "disabled"},
            self._health(),
        )

        # A 15m source whose last attempt is older than 30m reads as stale.
        stale_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE sources SET last_fetched_at = ? WHERE source_id = 'runtime'",
                (stale_at,),
            )
        self.assertEqual("stale", self._health()["runtime"])

        broken = next(row for row in self.db.list_sources() if row["source_id"] == "broken")
        for key in ("topic_scope", "language", "last_success_at", "consecutive_failures"):
            self.assertIn(key, broken)
        self.assertEqual(1, broken["consecutive_failures"])
        self.assertIsNone(broken["last_success_at"])

    def test_list_sources_health_agrees_with_the_web_registry_decorator(self) -> None:
        from autoposting import web

        decorator = getattr(web, "_runtime_source_health", None)
        if decorator is None:  # pragma: no cover - registry UI moved off this helper
            self.skipTest("web source-registry decorator is gone")
        self.db.record_fetch_result("runtime")
        self.db.upsert_source(
            Source(
                "never",
                "Never",
                "rss",
                "https://example.test/never.xml",
                "S2",
                ("models",),
                True,
                15,
            )
        )
        for row in self.db.list_sources():
            self.assertEqual(
                str(row["health"]),
                str(decorator(dict(row))["status"]),
                str(row["source_id"]),
            )


class ItemUrlLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        self.source = Source(
            "lab_blog",
            "AI Lab",
            "rss",
            "https://lab.example/feed.xml",
            "S1",
            ("models",),
            True,
            15,
        )
        self.db.upsert_source(self.source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_item_by_url_reads_known_url_and_never_raises_on_miss(self) -> None:
        inserted = self.db.insert_item(
            self.source,
            FeedItem(
                external_id="entry-1",
                canonical_url="https://lab.example/news/alpha",
                title="Alpha",
                content_text="Alpha body",
                published_at=None,
                raw_data={},
            ),
        )
        self.assertTrue(inserted)

        row = self.db.get_item_by_url("https://lab.example/news/alpha")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertTrue(str(row["item_id"]))
        self.assertEqual("Alpha", row["title"])
        self.assertEqual("Alpha body", row["content_text"])
        self.assertEqual("https://lab.example/news/alpha", row["canonical_url"])

        self.assertIsNone(self.db.get_item_by_url("https://lab.example/news/missing"))
        self.assertIsNone(self.db.get_item_by_url(""))
        self.assertIsNone(self.db.get_item_by_url("   "))


class CollectionRunClusterCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "autoposting.db"
        self.db = Database(self.path)
        self.db.initialize()
        for source_id in ("official", "second"):
            self.db.upsert_source(
                Source(
                    source_id,
                    source_id.title(),
                    "rss",
                    f"https://example.test/{source_id}.xml",
                    "S1",
                    ("models",),
                    True,
                    15,
                )
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_all_acceptance_counters_expose_post_collection_clusters(self) -> None:
        run_id = self.db.start_collection_run(
            ("official", "second"), started_at="2026-09-22T08:00:00+00:00"
        )
        self.db.record_collection_source_attempt(
            run_id, "official", "SUCCESS", items_fetched=5, items_inserted=4, items_after_dedupe=4
        )
        self.db.record_collection_source_attempt(
            run_id,
            "second",
            "FAILED",
            items_fetched=2,
            items_inserted=0,
            items_after_dedupe=0,
            diagnostic={
                "code": "source_fetch_failed",
                "cause_code": "unreachable",
                "source": "second",
                "stage": "collect",
                "detail": "feed outage",
            },
        )
        self.db.finish_collection_run(run_id, finished_at="2026-09-22T08:00:05+00:00")
        # finish_collection_run still defaults to 0: the real count only exists
        # after the window is clustered.
        self.assertEqual(0, self.db.get_collection_run(run_id)["clusters_created_or_updated"])

        self.db.set_collection_run_clusters(run_id, 3)
        run = self.db.get_collection_run(run_id)
        self.assertEqual(["official", "second"], sorted(run["requested_sources"]))
        self.assertEqual(["official", "second"], sorted(run["attempted_sources"]))
        self.assertEqual(["official"], list(run["successful_sources"]))
        self.assertEqual(["second"], list(run["failed_sources"]))
        self.assertEqual(7, run["items_fetched"])
        self.assertEqual(4, run["items_inserted"])
        self.assertEqual(4, run["items_after_dedupe"])
        self.assertEqual(3, run["clusters_created_or_updated"])

        self.db.set_collection_run_clusters(run_id, 3)
        self.assertEqual(3, self.db.get_collection_run(run_id)["clusters_created_or_updated"])
        for invalid in (-1, True):
            with self.assertRaisesRegex(ValueError, "non-negative"):
                self.db.set_collection_run_clusters(run_id, invalid)
        with self.assertRaisesRegex(ValueError, "run_id required"):
            self.db.set_collection_run_clusters("   ", 1)
        # An unknown run is a no-op rather than a KeyError.
        self.db.set_collection_run_clusters("run-missing", 2)

        reopened = Database(self.path)
        reopened.initialize()
        self.assertEqual(
            3, reopened.get_collection_run(run_id)["clusters_created_or_updated"]
        )


class WorkbenchClusterCounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        self.sources = (
            Source("lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15),
            Source("media_feed", "AI Media", "rss", "https://media.example/feed.xml", "S3", ("models",), True, 15),
        )
        for source in self.sources:
            self.db.upsert_source(source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_collect_persists_the_real_cluster_count_on_the_run(self) -> None:
        fetcher = {
            "https://lab.example/feed.xml": _rss_feed(
                [("a1", "Model One ships", "https://lab.example/a1", 10)]
            ),
            "https://media.example/feed.xml": _rss_feed(
                [
                    ("b1", "Chip prices rise", "https://media.example/b1", 20),
                    ("b2", "Datacenter buildout expands", "https://media.example/b2", 40),
                ]
            ),
        }
        payload = HotspotService(self.db, self.sources, fetcher=fetcher.get).collect(24)

        events = payload["radar"]["events"]
        self.assertTrue(events)
        clusters = len(events)
        self.assertEqual(clusters, payload["collection"]["clusters_created_or_updated"])
        self.assertEqual(clusters, payload["collection_run"]["clusters_created_or_updated"])
        run = self.db.get_collection_run(str(payload["collection"]["run_id"]))
        self.assertGreater(run["clusters_created_or_updated"], 0)
        self.assertEqual(clusters, run["clusters_created_or_updated"])
        self.assertEqual("DONE", run["status"])
        self.assertTrue(run["finished_at"])

    def test_source_health_exposes_last_success_and_failure_streak(self) -> None:
        broken = Source(
            "broken", "Broken", "rss", "https://broken.example/feed.xml", "S3", ("models",), True, 15
        )
        self.db.upsert_source(broken)

        def fetcher(url: str) -> bytes:
            if "lab.example" in url:
                return _rss_feed([("a1", "Model One ships", "https://lab.example/a1", 10)])
            if "media.example" in url:
                return EMPTY_RSS_FEED
            raise OSError("simulated feed outage")

        payload = HotspotService(self.db, (*self.sources, broken), fetcher=fetcher).collect(24)
        health = {str(row["source_id"]): row for row in payload["sources"]}

        self.assertEqual("ok", health["lab_blog"]["status"])
        self.assertTrue(health["lab_blog"]["last_success_at"])
        self.assertEqual(0, health["lab_blog"]["consecutive_failures"])
        self.assertEqual("error", health["broken"]["status"])
        self.assertTrue(health["broken"]["last_fetched_at"])
        self.assertEqual("", health["broken"]["last_success_at"])
        # One failed attempt is recorded exactly once (the pipeline is the sole
        # recorder; the collector only raises).  Asserting equality, not ">= 1",
        # is what makes a double-count regression fail here.
        self.assertEqual(1, health["broken"]["consecutive_failures"])

    def test_collect_without_clusters_still_finishes_the_run(self) -> None:
        fetcher = {
            "https://lab.example/feed.xml": EMPTY_RSS_FEED,
            "https://media.example/feed.xml": EMPTY_RSS_FEED,
        }
        payload = HotspotService(self.db, self.sources, fetcher=fetcher.get).collect(24)

        self.assertEqual([], payload["radar"]["events"])
        self.assertEqual(0, payload["collection"]["clusters_created_or_updated"])
        run = self.db.get_collection_run(str(payload["collection"]["run_id"]))
        self.assertEqual(0, run["clusters_created_or_updated"])
        self.assertEqual("DONE", run["status"])
        self.assertTrue(run["finished_at"])
