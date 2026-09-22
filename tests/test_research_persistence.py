from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoposting.db import Database
from autoposting.decision.contracts import PlannerResult, ResearchIntent, SearchPlan
from autoposting.research.contracts import ResearchStatus, ResearchTopic, SearchResponse, TopicDossier
from autoposting.workbench import Workbench


def _dossier(name: str = "dossier-1") -> TopicDossier:
    return TopicDossier(
        dossier_id=name,
        title="模型发布研究",
        query="模型发布",
        event_ids=("event-1",),
        topics=(ResearchTopic(event_id="event-1", title="模型发布", topic="models"),),
        plan=PlannerResult(
            intent=ResearchIntent(
                goal="研究模型发布",
                concepts=("模型", "发布"),
                search_repo=False,
                search_skill=False,
                search_web=True,
                source_event_ids=("event-1",),
            ),
            plan=SearchPlan(query="模型发布", adapters=("web",), max_candidates=8),
        ),
        evidence=(),
        groups=(),
        status=ResearchStatus.EMPTY,
        degraded=False,
        diagnostics=(),
        created_at="2026-09-22T04:00:00Z",
    )


class ResearchPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "autoposting.db"
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_v3_dossier_round_trip_and_no_source_registration(self) -> None:
        dossier = _dossier()
        self.db.save_dossier(dossier)
        restored = self.db.get_dossier(dossier.dossier_id)
        self.assertEqual(dossier.to_dict(), restored)
        self.assertEqual([dossier.to_dict()], self.db.list_dossiers())
        with self.db.connect() as connection:
            self.assertEqual(
                "topic-dossier-envelope-v1",
                json.loads(
                    connection.execute(
                        "SELECT dossier_json FROM topic_dossiers WHERE dossier_id = ?",
                        (dossier.dossier_id,),
                    ).fetchone()["dossier_json"]
                )["schema_version"],
            )
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0])

    def test_dossier_update_list_limit_and_delete(self) -> None:
        self.db.save_dossier(_dossier("one"))
        self.db.save_dossier(_dossier("two"))
        self.assertEqual(1, len(self.db.list_dossiers(limit=1)))
        self.assertTrue(self.db.delete_dossier("one"))
        self.assertFalse(self.db.delete_dossier("one"))
        self.assertIsNone(self.db.get_dossier("one"))

    def test_dossier_payload_is_strict_at_database_boundary(self) -> None:
        with self.assertRaises(ValueError):
            self.db.save_dossier({"schema_version": "future-v9"})
        with self.assertRaises(ValueError):
            self.db.list_dossiers(limit=0)

    def test_workbench_research_facade_uses_database_repository(self) -> None:
        event_id = self.db.create_event("模型发布", "models", "TEST", "research:facade")

        class Search:
            def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> SearchResponse:
                return SearchResponse(
                    query=query,
                    adapters=adapters,
                    results=(
                        {
                            "title": "官方说明",
                            "url": "https://example.com/official",
                            "snippet": "摘要",
                        },
                    ),
                )

        workbench = Workbench(self.db)
        workbench.research_service.search_adapter = Search()
        workbench.research_service.reader = lambda _candidate: {
            "status": "full",
            "text": "官方正文",
            "quality_score": 0.9,
        }

        dossier = workbench.research([event_id])

        self.assertEqual([event_id], dossier["event_ids"])
        self.assertEqual(dossier, workbench.get_research(str(dossier["dossier_id"])))
        self.assertEqual(1, len(workbench.list_research()))
        self.assertTrue(workbench.delete_research(str(dossier["dossier_id"])))


if __name__ == "__main__":
    unittest.main()
