import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoposting.content import Draft
from autoposting.db import Database
from autoposting.scheduler import PublishScheduler, parse_run_at


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "autoposting.db")
        self.db.initialize()
        self.db.create_event("Example", "models", "TEST", "schedule:example")
        self.db.upsert_draft(
            Draft(
                "draft-schedule",
                self.db.create_event("Example", "models", "TEST", "schedule:example"),
                "wechat",
                "标题",
                "正文 lab_blog AIGC",
                (),
                ("lab_blog",),
            )
        )
        self.db.approve_draft("draft-schedule", "tester")
        event_id = self.db.get_draft("draft-schedule")["event_id"]
        self.db.update_event_status(str(event_id), "APPROVED")
        self.published: list[tuple[str, str, bool]] = []

        def publisher(draft_id: str, platform: str, live: bool) -> dict[str, object]:
            self.published.append((draft_id, platform, live))
            return {"ok": True, "status": "DRY_RUN"}

        self.scheduler = PublishScheduler(self.db, publisher, interval_seconds=60)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_parse_run_at_accepts_local_naive_datetime(self) -> None:
        parsed = parse_run_at("2026-08-13T15:30")
        self.assertTrue(parsed.endswith("+00:00") or "+" in parsed[10:] or parsed.endswith("Z"))

    def test_due_job_publishes_once(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        job = self.db.create_scheduled_job("draft-schedule", "wechat", False, past)
        results = self.scheduler.tick()
        self.assertEqual(1, len(results))
        self.assertEqual("DONE", results[0]["status"])
        self.assertEqual([("draft-schedule", "wechat", False)], self.published)
        self.assertEqual("DONE", self.db.get_scheduled_job(str(job["job_id"]))["status"])
        self.assertEqual([], self.scheduler.tick())

    def test_future_job_waits(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.db.create_scheduled_job("draft-schedule", "wechat", True, future)
        self.assertEqual([], self.scheduler.tick())
        self.assertEqual([], self.published)

    def test_stale_threshold_defaults_and_can_be_overridden(self) -> None:
        default = PublishScheduler(self.db, lambda *_: {}, interval_seconds=15)
        self.assertEqual(60.0, default.stale_after_seconds)
        self.assertEqual(240.0, self.scheduler.stale_after_seconds)
        overridden = PublishScheduler(
            self.db,
            lambda *_: {},
            interval_seconds=15,
            stale_after_seconds=1.5,
        )
        self.assertEqual(1.5, overridden.stale_after_seconds)

    def test_publisher_failure_finishes_failed_without_leaking_exception(self) -> None:
        secret = "access_token=secret-value"

        def publisher(_draft_id: str, _platform: str, _live: bool) -> dict[str, object]:
            raise RuntimeError(secret)

        scheduler = PublishScheduler(self.db, publisher, interval_seconds=15)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        job = self.db.create_scheduled_job("draft-schedule", "wechat", False, past)
        with self.assertLogs("autoposting.scheduler", level="ERROR") as captured:
            results = scheduler.tick()
        text = "\n".join(captured.output)
        job_id = str(job["job_id"])
        self.assertEqual("FAILED", results[0]["status"])
        self.assertEqual("publisher_failed", results[0]["error"])
        self.assertEqual("FAILED", self.db.get_scheduled_job(job_id)["status"])
        self.assertEqual("publisher_failed", self.db.get_scheduled_job(job_id)["error_message"])
        self.assertIn(job_id, text)
        self.assertIn("stage=publisher", text)
        self.assertIn("exception_type=RuntimeError", text)
        self.assertIn("Traceback", text)
        self.assertNotIn(secret, text)

    def test_non_json_publisher_result_finishes_failed(self) -> None:
        def publisher(_draft_id: str, _platform: str, _live: bool) -> dict[str, object]:
            return {"not_json": object()}

        scheduler = PublishScheduler(self.db, publisher, interval_seconds=15)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        job = self.db.create_scheduled_job("draft-schedule", "wechat", False, past)
        with self.assertLogs("autoposting.scheduler", level="ERROR") as captured:
            results = scheduler.tick()
        text = "\n".join(captured.output)
        row = self.db.get_scheduled_job(str(job["job_id"]))
        self.assertEqual("FAILED", results[0]["status"])
        self.assertEqual("result_not_json_serializable", results[0]["error"])
        self.assertEqual("FAILED", row["status"])
        self.assertEqual("result_not_json_serializable", row["error_message"])
        self.assertIsNone(row["result_json"])
        self.assertIn("stage=serialize_result", text)

    def test_non_object_publisher_result_finishes_failed(self) -> None:
        def publisher(_draft_id: str, _platform: str, _live: bool) -> dict[str, object]:
            return []  # type: ignore[return-value]

        scheduler = PublishScheduler(self.db, publisher, interval_seconds=15)
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        job = self.db.create_scheduled_job("draft-schedule", "wechat", False, past)
        with self.assertLogs("autoposting.scheduler", level="ERROR") as captured:
            results = scheduler.tick()
        row = self.db.get_scheduled_job(str(job["job_id"]))
        self.assertEqual("result_not_json_object", results[0]["error"])
        self.assertEqual("FAILED", row["status"])
        self.assertEqual("result_not_json_object", row["error_message"])
        self.assertIn("stage=validate_result", "\n".join(captured.output))

    def test_stale_reconcile_skips_active_job(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        job = self.db.create_scheduled_job("draft-schedule", "wechat", False, past)
        job_id = str(job["job_id"])
        self.assertTrue(self.db.claim_scheduled_job(job_id))
        with self.scheduler._active_job_lock:
            self.scheduler._active_job_ids.add(job_id)
        try:
            self.assertEqual([], self.scheduler.tick(now=datetime(2099, 1, 1, tzinfo=timezone.utc)))
        finally:
            with self.scheduler._active_job_lock:
                self.scheduler._active_job_ids.discard(job_id)
        self.assertEqual("RUNNING", self.db.get_scheduled_job(job_id)["status"])
