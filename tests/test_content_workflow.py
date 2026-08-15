import tempfile
import unittest
from pathlib import Path

from autoposting.content import Claim, ContentWorkflow, Draft, FactList, FactValidationError
from autoposting.db import Database
from autoposting.models import Source


class ContentWorkflowTests(unittest.TestCase):
    @staticmethod
    def _register_sources(db: Database) -> None:
        sources = (
            Source("lab_blog", "AI Lab", "rss", "https://example.com/feed.xml", "S1", ("models",), True, 15),
            Source("media_feed", "AI Media", "rss", "https://media.example/feed.xml", "S3", ("models",), True, 15),
        )
        for source in sources:
            db.upsert_source(source)

    def test_claim_without_sources_is_failed_and_cannot_be_drafted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            event_id = db.create_event("Model One launch", "models", "DAILY", "daily:model-one")
            facts = FactList(
                event="Model One launch",
                claims=(
                    Claim(
                        text="Model One launched on August 8.",
                        source_ids=(),
                        confidence="high",
                    ),
                ),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)

            result = workflow.save_fact_list(event_id, facts)

            self.assertFalse(result.valid)
            self.assertIn("claim_missing_sources", result.errors)
            self.assertEqual("FAILED", db.get_event(event_id)["status"])
            self.assertIn("claim_missing_sources", db.get_event(event_id)["error_message"])
            with self.assertRaises(FactValidationError):
                workflow.generate_drafts(event_id, facts)
            self.assertEqual(0, db.count_drafts())

    def test_valid_facts_generate_distinct_idempotent_drafts_and_require_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Model One launch", "models", "DAILY", "daily:model-one")
            facts = FactList(
                event="Model One launch",
                claims=(
                    Claim("Model One is available today.", ("lab_blog",), "high"),
                    Claim("The release includes an API.", ("lab_blog", "media_feed"), "medium"),
                ),
                unknowns=("Independent benchmark results are not available.",),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)

            validation = workflow.save_fact_list(event_id, facts)
            first = workflow.generate_drafts(event_id, facts)
            second = workflow.generate_drafts(event_id, facts)

            self.assertTrue(validation.valid)
            self.assertEqual({draft.draft_id for draft in first}, {draft.draft_id for draft in second})
            self.assertEqual({"wechat", "xiaohongshu"}, {draft.platform for draft in first})
            self.assertEqual(2, db.count_drafts())
            wechat = next(draft for draft in first if draft.platform == "wechat")
            xiaohongshu = next(draft for draft in first if draft.platform == "xiaohongshu")
            self.assertIn("已确认事实", wechat.body)
            self.assertIn("关键信息", xiaohongshu.body)
            self.assertNotEqual(wechat.body, xiaohongshu.body)
            self.assertIn("AIGC", wechat.body)
            self.assertIn("AIGC", xiaohongshu.body)
            self.assertEqual({"lab_blog", "media_feed"}, set(wechat.source_ids))
            self.assertIn(
                "Independent benchmark results are not available.",
                db.get_event(event_id)["fact_list_json"],
            )
            self.assertEqual("DRAFTED", db.get_event(event_id)["status"])

            workflow.approve_draft(wechat.draft_id, approved_by="human-reviewer")
            self.assertEqual("DRAFTED", db.get_event(event_id)["status"])
            workflow.approve_draft(xiaohongshu.draft_id, approved_by="human-reviewer")
            self.assertEqual("APPROVED", db.get_event(event_id)["status"])

    def test_human_can_reject_draft_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Rejectable event", "models", "DAILY", "rejectable")
            facts = FactList(
                event="Rejectable event",
                claims=(Claim("A traceable claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, facts)
            draft = workflow.generate_drafts(event_id, facts)[0]

            workflow.reject_draft(draft.draft_id, "human-reviewer", "Tone needs revision")

            self.assertEqual("REJECTED", db.get_draft(draft.draft_id)["status"])
            self.assertEqual("Tone needs revision", db.get_draft(draft.draft_id)["review_note"])
            self.assertEqual("REJECTED", db.get_event(event_id)["status"])

            workflow.save_fact_list(event_id, facts)

            self.assertEqual("REJECTED", db.get_event(event_id)["status"])

    def test_s4_only_claim_fails_validation_but_mixed_tiers_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            db.upsert_source(
                Source("rumor_board", "Rumor Board", "rss", "https://rumor.example/feed.xml", "S4", ("models",), True, 15)
            )
            event_id = db.create_event("S4 event", "models", "DAILY", "s4-only")
            s4_only = FactList(
                event="S4 event",
                claims=(Claim("An unverified rumor.", ("rumor_board",), "low"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)

            result = workflow.save_fact_list(event_id, s4_only)

            self.assertTrue(result.valid)
            self.assertEqual("VERIFIED", db.get_event(event_id)["status"])
            drafts = workflow.generate_drafts(event_id, s4_only)
            self.assertTrue(drafts)
            self.assertIn("【信息来源 1 条", drafts[0].body)

            self._register_sources(db)
            mixed_event = db.create_event("Mixed tiers", "models", "DAILY", "mixed-tiers")
            mixed = FactList(
                event="Mixed tiers",
                claims=(
                    Claim("A rumor confirmed by the lab.", ("rumor_board", "lab_blog"), "medium"),
                ),
                unknowns=(),
                conflicts=(),
            )
            self.assertTrue(workflow.save_fact_list(mixed_event, mixed).valid)

    def test_single_platform_draft_generation_and_approval_completes_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Single platform", "models", "DAILY", "single-platform")
            facts = FactList(
                event="Single platform",
                claims=(Claim("A traceable claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, facts)

            draft = workflow.generate_draft(event_id, facts, "wechat")

            self.assertEqual("wechat", draft.platform)
            self.assertEqual(1, db.count_drafts())
            self.assertEqual("DRAFTED", db.get_event(event_id)["status"])
            with self.assertRaises(ValueError):
                workflow.generate_draft(event_id, facts, "douyin")

            workflow.approve_draft(draft.draft_id, "human-reviewer")

            self.assertEqual("APPROVED", db.get_draft(draft.draft_id)["status"])
            self.assertEqual("APPROVED", db.get_event(event_id)["status"])
            self.assertTrue(db.event_has_all_platforms_approved(event_id, "draft-v1"))

    def test_resaving_identical_facts_preserves_approved_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Retry", "models", "DAILY", "retry-approved")
            facts = FactList(
                event="Retry",
                claims=(Claim("A traceable claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, facts)
            draft = workflow.generate_draft(event_id, facts, "wechat")
            workflow.approve_draft(draft.draft_id, "human-reviewer")

            workflow.save_fact_list(event_id, facts)

            self.assertEqual("APPROVED", db.get_event(event_id)["status"])
            self.assertEqual("APPROVED", db.get_draft(draft.draft_id)["status"])

    def test_current_processing_version_approval_ignores_historical_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Upgrade", "models", "DAILY", "upgrade-version")
            facts = FactList(
                event="Upgrade",
                claims=(Claim("A traceable claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, facts)
            historical = workflow.generate_draft(event_id, facts, "wechat")
            current = Draft(
                draft_id="current-article-draft",
                event_id=event_id,
                platform="wechat",
                title="当前中文稿",
                body="当前正文；来源 lab_blog；AIGC 声明。",
                tags=(),
                source_ids=("lab_blog",),
                generator="deepseek-v4-flash:article-v1",
                processing_version="article-v1",
            )
            db.upsert_draft(current)
            db.update_event_status(event_id, "DRAFTED")

            workflow.approve_draft(current.draft_id, "human-reviewer")

            self.assertEqual("DRAFTED", db.get_draft(historical.draft_id)["status"])
            self.assertEqual("APPROVED", db.get_event(event_id)["status"])

            with self.assertRaisesRegex(ValueError, "draft_superseded"):
                workflow.approve_draft(historical.draft_id, "human-reviewer")
            with self.assertRaisesRegex(ValueError, "draft_superseded"):
                workflow.reject_draft(historical.draft_id, "human-reviewer", "stale")

    def test_changed_regeneration_revokes_approval_and_clears_review_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Revision", "models", "DAILY", "revision-review")
            workflow = ContentWorkflow(db)
            first_facts = FactList(
                event="Revision",
                claims=(Claim("Version one claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            second_facts = FactList(
                event="Revision",
                claims=(Claim("Version two claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow.save_fact_list(event_id, first_facts)
            draft = workflow.generate_draft(event_id, first_facts, "xiaohongshu")
            workflow.reject_draft(draft.draft_id, "human-reviewer", "needs revision")
            self.assertEqual("REJECTED", db.get_draft(draft.draft_id)["status"])

            workflow.save_fact_list(event_id, second_facts)
            workflow.generate_draft(event_id, second_facts, "xiaohongshu")

            stored = db.get_draft(draft.draft_id)
            self.assertEqual("DRAFTED", stored["status"])
            self.assertIsNone(stored["approved_by"])
            self.assertIsNone(stored["approved_at"])
            self.assertIsNone(stored["reviewed_by"])
            self.assertIsNone(stored["reviewed_at"])
            self.assertIsNone(stored["review_note"])
            self.assertIn("Version two claim.", stored["body"])

    def test_edit_draft_validates_and_revokes_approval_when_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Editable", "models", "DAILY", "editable")
            facts = FactList(
                event="Editable",
                claims=(Claim("A traceable claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, facts)
            draft = workflow.generate_draft(event_id, facts, "wechat")
            workflow.approve_draft(draft.draft_id, "reviewer")

            changed = workflow.edit_draft(
                draft.draft_id,
                "编辑后的标题",
                draft.body + "\n人工补充内容。",
            )

            self.assertEqual("DRAFTED", changed["status"])
            self.assertEqual("DRAFTED", db.get_event(event_id)["status"])
            self.assertIsNone(changed["approved_by"])
            with self.assertRaisesRegex(ValueError, "aigc_disclosure_required"):
                workflow.edit_draft(draft.draft_id, "标题", "正文 lab_blog")
            with self.assertRaisesRegex(ValueError, "source_ids_required"):
                workflow.edit_draft(draft.draft_id, "标题", "正文 lab_blog_extra AIGC")

    def test_unresolved_conflict_and_excess_evidence_are_rejected(self) -> None:
        from autoposting.content import Conflict, validate_fact_list

        facts = FactList(
            event="Conflicting report",
            claims=(
                Claim(
                    "The unverified number is 42.",
                    ("source-a",),
                    "low",
                    exceeds_evidence=True,
                ),
            ),
            unknowns=(),
            conflicts=(Conflict("Sources disagree", ("source-a", "source-b"), ""),),
        )

        result = validate_fact_list(facts)

        self.assertFalse(result.valid)
        self.assertIn("claim_exceeds_evidence", result.errors)
        self.assertIn("conflict_unresolved", result.errors)

    def test_unknown_source_id_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            event_id = db.create_event("Unknown source", "models", "DAILY", "unknown-source")
            facts = FactList(
                event="Unknown source",
                claims=(Claim("A claim with a made-up reference.", ("not_registered",), "high"),),
                unknowns=(),
                conflicts=(),
            )

            result = ContentWorkflow(db).save_fact_list(event_id, facts)

            self.assertFalse(result.valid)
            self.assertIn("claim_unknown_source:not_registered", result.errors)
            self.assertEqual("FAILED", db.get_event(event_id)["status"])

    def test_failed_fact_revision_blocks_approval_of_stale_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Revision", "models", "DAILY", "revision")
            valid_facts = FactList(
                event="Revision",
                claims=(Claim("A supported claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            invalid_facts = FactList(
                event="Revision",
                claims=(Claim("An unsupported revision.", (), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, valid_facts)
            draft = workflow.generate_drafts(event_id, valid_facts)[0]
            workflow.save_fact_list(event_id, invalid_facts)

            with self.assertRaisesRegex(ValueError, "event_not_ready_for_approval"):
                workflow.approve_draft(draft.draft_id, "human-reviewer")

            self.assertEqual("FAILED", db.get_event(event_id)["status"])

    def test_drafting_requires_the_exact_verified_fact_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            self._register_sources(db)
            event_id = db.create_event("Immutable facts", "models", "DAILY", "immutable-facts")
            verified = FactList(
                event="Immutable facts",
                claims=(Claim("A supported claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            changed = FactList(
                event="Immutable facts",
                claims=(Claim("A different claim.", ("lab_blog",), "high"),),
                unknowns=(),
                conflicts=(),
            )
            workflow = ContentWorkflow(db)
            workflow.save_fact_list(event_id, verified)

            with self.assertRaisesRegex(FactValidationError, "fact_list_not_verified"):
                workflow.generate_drafts(event_id, changed)


if __name__ == "__main__":
    unittest.main()