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
