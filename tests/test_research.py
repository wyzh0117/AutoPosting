from __future__ import annotations

import unittest
from datetime import datetime, timezone

from autoposting.decision import DecisionService, LLMPlanner, PlannerResult, ResearchIntent, SearchPlan
from autoposting.decision.adapters import JevAdapter
from autoposting.research.contracts import ResearchStatus, SearchResponse, TopicDossier
from autoposting.research.service import ResearchError, ResearchService


def _plan_payload() -> dict[str, object]:
    return {
        "schema_version": "planner-result-v1",
        "intent": {
            "schema_version": "research-intent-v1",
            "goal": "研究 AI 模型发布",
            "concepts": ["AI 模型", "发布"],
            "search_repo": False,
            "search_skill": False,
            "search_web": True,
            "preferred_signals": ["official source"],
            "source_event_ids": ["event-1", "event-2"],
        },
        "plan": {
            "schema_version": "search-plan-v1",
            "query": "AI 模型发布",
            "adapters": ["web"],
            "max_candidates": 10,
        },
    }


class MemoryDossierStore:
    def __init__(self) -> None:
        self.saved: dict[str, TopicDossier] = {}

    def save_dossier(self, dossier: TopicDossier) -> None:
        self.saved[dossier.dossier_id] = dossier

    def get_dossier(self, dossier_id: str) -> TopicDossier | None:
        return self.saved.get(dossier_id)

    def list_dossiers(self, limit: int = 20) -> list[TopicDossier]:
        return list(self.saved.values())[:limit]


class FakeSearch:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> SearchResponse:
        self.calls.append((query, adapters, limit))
        return SearchResponse(query=query, results=tuple(self.results), adapters=adapters)


class FakeReader:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def read(self, candidate: object) -> object:
        self.urls.append(candidate.url)
        return {
            "status": "full",
            "title": candidate.title + "（正文）",
            "text": "官方页面正文，包含足够的研究上下文。",
            "quality_score": 0.92,
        }


class ResearchTests(unittest.TestCase):
    def _service(
        self,
        *,
        search: FakeSearch,
        planner: LLMPlanner | None = None,
        reader: FakeReader | None = None,
        repository: MemoryDossierStore | None = None,
    ) -> ResearchService:
        planner = planner or LLMPlanner(lambda _prompt: _plan_payload())
        return ResearchService(
            planner,
            DecisionService(),
            lambda event_id: {
                "event_id": event_id,
                "title": "模型发布" if event_id == "event-1" else "行业反应",
                "topic": "models",
                "source_ids": ["official-source"],
                "item_ids": [event_id + "-item"],
            },
            search_adapter=search,
            reader=reader,
            repository=repository,
            clock=lambda: datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            max_candidates=10,
            max_deep_read=2,
        )

    def test_multi_select_plans_searches_reads_and_persists_dossier(self) -> None:
        search = FakeSearch(
            [
                {"title": "官方发布说明", "url": "https://example.com/official", "snippet": "官方摘要"},
                {"title": "行业报道", "url": "https://example.com/media", "snippet": "媒体摘要"},
            ]
        )
        reader = FakeReader()
        store = MemoryDossierStore()
        dossier = self._service(search=search, reader=reader, repository=store).research(
            ["event-1", "event-2"]
        )
        self.assertEqual(("event-1", "event-2"), dossier.event_ids)
        self.assertEqual(("web",), search.calls[0][1])
        self.assertEqual(2, len(dossier.evidence))
        self.assertEqual(2, len(reader.urls))
        self.assertIs(store.saved[dossier.dossier_id], dossier)
        self.assertTrue(all(item.source_scope == "evidence" for item in dossier.evidence))
        self.assertTrue(all(item.content for item in dossier.evidence))
        self.assertTrue(dossier.groups)

    def test_jev_relation_is_used_for_dossier_grouping(self) -> None:
        search = FakeSearch(
            [{"title": "后续进展", "url": "https://example.com/follow", "snippet": "后续"}]
        )
        jev = JevAdapter(
            lambda _payload: {
                "schema_version": "jev-assessment-v1",
                "score": 90,
                "confidence": 0.95,
                "decision": "include",
                "relation": "follow_up",
                "worth_reading": True,
                "duplicate": False,
                "reasons": ["same topic and later event"],
            }
        )
        service = self._service(search=search)
        service.decision_service = DecisionService(jev)
        dossier = service.research(["event-1"])
        self.assertEqual("follow_up", dossier.evidence[0].relation.value)
        self.assertEqual("follow_up", dossier.groups[0].relation.value)

    def test_high_confidence_exclusion_is_not_deep_read(self) -> None:
        search = FakeSearch(
            [{"title": "应排除", "url": "https://example.com/exclude", "snippet": "噪声"}]
        )
        reader = FakeReader()
        jev = JevAdapter(
            lambda _payload: {
                "schema_version": "jev-assessment-v1",
                "score": 95,
                "confidence": 0.98,
                "decision": "exclude",
                "relation": "irrelevant",
                "worth_reading": False,
                "duplicate": False,
                "reasons": ["unrelated"],
            }
        )
        service = self._service(search=search, reader=reader)
        service.decision_service = DecisionService(jev)
        dossier = service.research(["event-1"])
        self.assertFalse(reader.urls)
        self.assertEqual("exclude", dossier.evidence[0].assessment.decision.value)

    def test_planner_failure_uses_explicit_fallback_and_degrades(self) -> None:
        search = FakeSearch(
            [{"title": "搜索结果", "url": "https://example.com/result", "snippet": "摘要"}]
        )
        dossier = self._service(
            search=search,
            planner=LLMPlanner(lambda _prompt: "not-json"),
        ).research(["event-1"])
        self.assertTrue(dossier.degraded)
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertTrue(any(item.get("source") == "planner" for item in dossier.diagnostics))
        self.assertEqual(("github", "skills", "web"), dossier.plan.plan.adapters)

    def test_search_failure_is_degraded_and_does_not_register_evidence_source(self) -> None:
        class BrokenSearch:
            def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> SearchResponse:
                return SearchResponse(
                    query=query,
                    adapters=adapters,
                    degraded=True,
                    cause_code="transport_unavailable",
                    diagnostics=(
                        {
                            "code": "research_search_failed",
                            "cause_code": "transport_unavailable",
                            "source": "research_search",
                            "stage": "research_search",
                            "degraded": True,
                            "detail": "network unavailable",
                        },
                    ),
                )

        store = MemoryDossierStore()
        dossier = self._service(search=BrokenSearch(), repository=store).research(["event-1"])
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertFalse(dossier.evidence)
        self.assertTrue(dossier.diagnostics)
        self.assertFalse(hasattr(store, "sources"))

    def test_persistence_failure_returns_degraded_dossier(self) -> None:
        class BrokenStore:
            def save_dossier(self, _dossier: TopicDossier) -> None:
                raise RuntimeError("storage down")

        dossier = self._service(
            search=FakeSearch(
                [{"title": "结果", "url": "https://example.com/result", "snippet": "摘要"}]
            ),
            repository=BrokenStore(),
        ).research(["event-1"])
        self.assertTrue(dossier.degraded)
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertEqual("dossier_persist_failed", dossier.diagnostics[-1]["code"])

    def test_invalid_selected_event_and_duplicate_urls_are_safe(self) -> None:
        search = FakeSearch(
            [
                {"title": "可用", "url": "https://example.com/a#fragment", "snippet": "摘要"},
                {"title": "重复", "url": "https://EXAMPLE.com/a", "snippet": "重复"},
                {"title": "缺 URL", "snippet": "无 URL"},
            ]
        )
        service = self._service(search=search)
        service.event_resolver = lambda event_id: (
            None
            if event_id == "missing-event"
            else {
                "event_id": event_id,
                "title": "模型发布",
                "topic": "models",
                "source_ids": ["official-source"],
                "item_ids": [event_id + "-item"],
            }
        )
        with self.assertRaises(ResearchError):
            service.research(["missing-event"])
        dossier = service.research(["event-1"])
        self.assertEqual(1, len(dossier.evidence))
        self.assertTrue(dossier.diagnostics)

    def test_restore_reads_repository_object(self) -> None:
        store = MemoryDossierStore()
        dossier = self._service(
            search=FakeSearch(
                [{"title": "结果", "url": "https://example.com/a", "snippet": "摘要"}]
            ),
            repository=store,
        ).research(["event-1"])
        restored = self._service(search=FakeSearch([]), repository=store).restore(dossier.dossier_id)
        self.assertEqual(dossier.to_dict(), restored.to_dict())


if __name__ == "__main__":
    unittest.main()
