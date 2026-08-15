import json
import tempfile
import unittest
from pathlib import Path

from autoposting.content import Claim, ContentWorkflow, FactList
from autoposting.db import Database
from autoposting.models import Source
from autoposting.publishing import LivePublishingDisabled, PublicationError, PublicationService


class PublishingTests(unittest.TestCase):
    def _drafts(self, db: Database):
        db.upsert_source(
            Source("lab_blog", "AI Lab", "rss", "https://example.com/feed.xml", "S1", ("models",), True, 15)
        )
        event_id = db.create_event("Model One launch", "models", "DAILY", "publish:model-one")
        facts = FactList(
            event="Model One launch",
            claims=(Claim("Model One is available today.", ("lab_blog",), "high"),),
            unknowns=(),
            conflicts=(),
        )
        workflow = ContentWorkflow(db)
        workflow.save_fact_list(event_id, facts)
        return event_id, workflow, workflow.generate_drafts(event_id, facts)

    def test_dry_run_writes_one_idempotent_local_package_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "autoposting.db")
            db.initialize()
            event_id, workflow, drafts = self._drafts(db)
            for draft in drafts:
                workflow.approve_draft(draft.draft_id, "human-reviewer")
            service = PublicationService(db, root / "packages", dry_run=True, max_retries=3)

            first = service.publish(drafts[0].draft_id)
            second = service.publish(drafts[0].draft_id)

            self.assertEqual(first.attempt_id, second.attempt_id)
            self.assertEqual("DRY_RUN", first.status)
            self.assertEqual(1, db.count_publication_attempts())
            payload = json.loads(first.package_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["dry_run"])
            self.assertEqual("wechat", payload["platform"])
            self.assertTrue(first.archive_path.exists())
            archive = json.loads(first.archive_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["title"], archive["title"])
            self.assertEqual(payload["body"], archive["body"])
            self.assertEqual("APPROVED", db.get_event(event_id)["status"])
            self.assertEqual("APPROVED", db.get_draft(drafts[0].draft_id)["status"])

    def test_unapproved_and_live_requests_fail_with_recorded_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "autoposting.db")
            db.initialize()
            _, workflow, drafts = self._drafts(db)
            service = PublicationService(db, root / "packages", dry_run=True, max_retries=3)

            with self.assertRaises(PublicationError):
                service.publish(drafts[0].draft_id)
            failed = db.get_latest_publication_attempt(drafts[0].draft_id)
            self.assertEqual("FAILED", failed["status"])
            self.assertEqual("draft_not_approved", failed["error_message"])

            for draft in drafts:
                workflow.approve_draft(draft.draft_id, "human-reviewer")
            live_service = PublicationService(db, root / "packages", dry_run=False, max_retries=3)
            with self.assertRaises(LivePublishingDisabled):
                live_service.publish(drafts[0].draft_id)
            live_failed = db.get_latest_publication_attempt(drafts[0].draft_id)
            self.assertEqual("FAILED", live_failed["status"])
            self.assertEqual("live_publishing_disabled", live_failed["error_message"])
            self.assertLessEqual(int(live_failed["retry_count"]), 3)

    def test_historical_dry_run_not_returned_after_draft_loses_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "autoposting.db")
            db.initialize()
            _, workflow, drafts = self._drafts(db)
            for draft in drafts:
                workflow.approve_draft(draft.draft_id, "human-reviewer")
            service = PublicationService(db, root / "packages", dry_run=True, max_retries=3)
            first = service.publish(drafts[0].draft_id)
            self.assertEqual("DRY_RUN", first.status)

            with db.connect() as connection:
                connection.execute(
                    "UPDATE drafts SET status = 'REJECTED' WHERE draft_id = ?",
                    (drafts[0].draft_id,),
                )

            with self.assertRaisesRegex(PublicationError, "draft_not_approved"):
                service.publish(drafts[0].draft_id)
            failed = db.get_latest_publication_attempt(drafts[0].draft_id)
            self.assertEqual("FAILED", failed["status"])
            self.assertEqual("draft_not_approved", failed["error_message"])

    def test_identical_regeneration_keeps_approval_and_historical_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "autoposting.db")
            db.initialize()
            event_id, workflow, drafts = self._drafts(db)
            for draft in drafts:
                workflow.approve_draft(draft.draft_id, "human-reviewer")
            service = PublicationService(db, root / "packages", dry_run=True, max_retries=3)
            first = service.publish(drafts[0].draft_id)

            facts = FactList(
                event="Model One launch",
                claims=(Claim("Model One is available today.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow.generate_drafts(event_id, facts)

            stored = db.get_draft(drafts[0].draft_id)
            self.assertEqual("APPROVED", stored["status"])
            self.assertEqual("human-reviewer", stored["approved_by"])
            second = service.publish(drafts[0].draft_id)
            self.assertEqual(first.attempt_id, second.attempt_id)
            self.assertEqual("DRY_RUN", second.status)
            self.assertEqual(1, db.count_publication_attempts())

    def test_one_approved_platform_cannot_be_packaged_before_event_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = Database(root / "autoposting.db")
            db.initialize()
            event_id, workflow, drafts = self._drafts(db)
            workflow.approve_draft(drafts[0].draft_id, "human-reviewer")

            with self.assertRaisesRegex(PublicationError, "event_not_approved"):
                PublicationService(db, root / "packages", dry_run=True).publish(
                    drafts[0].draft_id
                )

            self.assertEqual("DRAFTED", db.get_event(event_id)["status"])
            attempt = db.get_latest_publication_attempt(drafts[0].draft_id)
            self.assertEqual("FAILED", attempt["status"])
            self.assertEqual("event_not_approved", attempt["error_message"])


if __name__ == "__main__":
    unittest.main()
