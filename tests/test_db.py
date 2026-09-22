import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoposting.content import Draft
from autoposting.db import Database


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
            self.assertEqual(1, connection.execute("PRAGMA user_version").fetchone()[0])
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
