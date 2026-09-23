"""Degraded-read behaviour of the research reader boundary.

A single unreadable page must never fail the Topic Dossier.  These tests drive
the real ``ReaderServiceEvidenceAdapter`` -> ``ReaderService`` chain with a
blocked page and assert the legal fallback order: already-collected feed
content, then search-snippet evidence, then a degraded document.  Nothing here
retries a blocked URL or changes a request identity.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError

from autoposting.article_context import build_dossier_context
from autoposting.decision import DecisionService, LLMPlanner
from autoposting.diagnostics import diagnostic_summary, serialize_diagnostic
from autoposting.reader import BLOCKED, FULL, PARTIAL, ReaderService
from autoposting.research.adapters import ReaderServiceEvidenceAdapter
from autoposting.research.contracts import (
    EvidenceCandidate,
    ResearchStatus,
    SearchResponse,
    TopicDossier,
)
from autoposting.research.service import ResearchService

BLOCKED_URL = "https://example.com/blocked"
SNIPPET = "搜索结果里保留的摘要，用来证明证据行本身没有丢失。"

# Short enough to extract as a partial document, so the page fetch is still
# attempted and the blocked response has to fall back to this text.
PARTIAL_FEED = "<p>采集器已收录的 RSS 摘要正文，可以作为降级证据使用。</p>"
# Long enough to extract as a full document, so no page fetch may happen.
FULL_FEED = "<p>" + ("Collected feed article body and facts. " * 12) + "</p>"


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
            "source_event_ids": ["event-1"],
        },
        "plan": {
            "schema_version": "search-plan-v1",
            "query": "AI 模型发布",
            "adapters": ["web"],
            "max_candidates": 10,
        },
    }


class StaticSearch:
    def __init__(self, url: str = BLOCKED_URL) -> None:
        self.url = url

    def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> SearchResponse:
        return SearchResponse(
            query=query,
            adapters=adapters,
            results=({"title": "被拦截的页面", "url": self.url, "snippet": SNIPPET},),
        )


class MemoryDossierStore:
    """Mimic the database envelope so ``restore`` uses ``_decode_dossier``."""

    def __init__(self) -> None:
        self.saved: dict[str, dict[str, object]] = {}

    def save_dossier(self, dossier: TopicDossier) -> None:
        self.saved[dossier.dossier_id] = {
            "schema_version": "topic-dossier-envelope-v1",
            "data": dossier.to_dict(),
        }

    def get_dossier(self, dossier_id: str) -> dict[str, object] | None:
        return self.saved.get(dossier_id)

    def list_dossiers(self, limit: int = 20) -> list[dict[str, object]]:
        return list(self.saved.values())[:limit]


def _blocked_fetcher(calls: list[str]):
    def fetcher(url: str) -> bytes:
        calls.append(url)
        raise HTTPError(url, 403, "Forbidden", {}, None)

    return fetcher


class ResearchReaderFallbackTests(unittest.TestCase):
    def _service(
        self,
        *,
        reader: object,
        search: StaticSearch | None = None,
        repository: MemoryDossierStore | None = None,
    ) -> ResearchService:
        return ResearchService(
            LLMPlanner(lambda _prompt: _plan_payload()),
            DecisionService(),
            lambda event_id: {
                "event_id": event_id,
                "title": "模型发布",
                "topic": "models",
                "source_ids": ["official-source"],
                "item_ids": [event_id + "-item"],
            },
            search_adapter=search or StaticSearch(),
            reader=reader,
            repository=repository,
            clock=lambda: datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            max_candidates=10,
            max_deep_read=2,
        )

    def test_blocked_page_degrades_the_dossier_without_raising(self) -> None:
        calls: list[str] = []
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher(calls)),
            fallback_resolver=lambda _url: {"title": "被拦截的页面", "content_text": PARTIAL_FEED},
        )

        dossier = self._service(reader=reader).research(["event-1"])

        # The dossier survives one unreadable page and says so.
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertTrue(dossier.degraded)
        self.assertEqual(1, len(dossier.evidence))
        self.assertEqual(["https://example.com/blocked"], calls)

        row = dossier.evidence[0]
        self.assertTrue(row.degraded)
        self.assertEqual("blocked_http", row.cause_code)
        self.assertEqual("feed", row.fallback_source)
        self.assertEqual("forbidden", row.blocked_kind)
        self.assertEqual(PARTIAL, row.reader_status)
        self.assertEqual("rss", row.read_method)
        self.assertTrue(row.content)
        self.assertEqual(BLOCKED_URL, row.url)
        self.assertEqual(BLOCKED_URL, row.original_url)
        self.assertTrue(row.snippet)

    def test_degraded_read_diagnostic_keeps_the_production_log_contract(self) -> None:
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=lambda _url: {"content_text": PARTIAL_FEED},
        )

        dossier = self._service(reader=reader).research(["event-1"])

        diagnostics = [
            dict(item)
            for item in dossier.diagnostics
            if dict(item).get("code") == "evidence_read_degraded"
        ]
        self.assertTrue(diagnostics)
        self.assertEqual("blocked_http", diagnostics[0]["cause_code"])
        self.assertEqual("reader", diagnostics[0]["source"])
        self.assertEqual("evidence_read", diagnostics[0]["stage"])
        self.assertTrue(diagnostics[0]["degraded"])
        # The log line keeps the production shape (code:cause) and now names
        # the specific cause instead of a generic fetch_failed.
        self.assertTrue(
            diagnostic_summary(diagnostics[0]).startswith("evidence_read_degraded:blocked_http:")
        )
        self.assertIn("cause_code=blocked_http", serialize_diagnostic(diagnostics[0]))

    def test_degraded_evidence_is_visible_in_the_article_context(self) -> None:
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=lambda _url: {"content_text": PARTIAL_FEED},
        )
        dossier = self._service(reader=reader).research(["event-1"])

        context = build_dossier_context(dossier.to_dict())
        row = context.evidence[0]

        self.assertTrue(row["degraded"])
        self.assertEqual("blocked_http", row["cause_code"])
        self.assertEqual("feed", row["fallback_source"])
        self.assertEqual("rss", row["read_method"])
        self.assertIn("降级：blocked_http / 来源：feed", context.material)

    def test_collected_feed_content_reads_without_any_page_fetch(self) -> None:
        calls: list[str] = []
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher(calls)),
            fallback_resolver=lambda url: (
                {"title": "已收录页面", "content_text": FULL_FEED, "canonical_url": url}
            ),
        )

        dossier = self._service(reader=reader).research(["event-1"])

        self.assertEqual([], calls)
        row = dossier.evidence[0]
        self.assertEqual(FULL, row.reader_status)
        self.assertEqual("rss", row.read_method)
        self.assertFalse(row.degraded)
        self.assertFalse(row.cause_code)
        self.assertIn("Collected feed article body", row.content)
        self.assertFalse(
            [
                item
                for item in dossier.diagnostics
                if dict(item).get("code") == "evidence_read_degraded"
            ]
        )

    def test_resolver_failure_or_absence_is_not_an_exception(self) -> None:
        def broken_resolver(_url: str) -> object:
            raise RuntimeError("store offline")

        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=broken_resolver,
        )

        dossier = self._service(reader=reader).research(["event-1"])

        row = dossier.evidence[0]
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertEqual(BLOCKED, row.reader_status)
        self.assertTrue(row.degraded)
        self.assertEqual("blocked_http", row.cause_code)
        self.assertIsNone(row.fallback_source)
        self.assertFalse(row.content)
        # Without collected content the search snippet is what remains legal.
        self.assertTrue(row.snippet)

    def test_adapter_without_a_resolver_still_works(self) -> None:
        reader = ReaderServiceEvidenceAdapter(ReaderService(_blocked_fetcher([])))

        dossier = self._service(reader=reader).research(["event-1"])

        self.assertTrue(dossier.evidence)
        self.assertEqual("blocked_http", dossier.evidence[0].cause_code)
        self.assertIsNone(dossier.evidence[0].fallback_source)

    def test_failing_reader_object_is_classified_and_degrades(self) -> None:
        class BrokenReader:
            def read(self, _candidate: object) -> object:
                raise RuntimeError("reader exploded")

        dossier = self._service(reader=BrokenReader()).research(["event-1"])

        row = dossier.evidence[0]
        self.assertEqual(ResearchStatus.DEGRADED, dossier.status)
        self.assertTrue(row.degraded)
        self.assertEqual("reader_internal_error", row.cause_code)
        self.assertEqual("failed", row.reader_status)
        self.assertTrue(
            [
                item
                for item in dossier.diagnostics
                if dict(item).get("code") == "evidence_read_failed"
                and dict(item).get("cause_code") == "reader_internal_error"
            ]
        )

    def test_new_evidence_fields_survive_persist_and_restore(self) -> None:
        store = MemoryDossierStore()
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=lambda _url: {"content_text": PARTIAL_FEED},
        )
        service = self._service(reader=reader, repository=store)

        dossier = service.research(["event-1"])
        stored = store.saved[dossier.dossier_id]["data"]
        decoded = ResearchService._decode_dossier(stored)
        restored = service.restore(dossier.dossier_id)

        for candidate in (decoded.evidence[0], restored.evidence[0]):
            self.assertTrue(candidate.degraded)
            self.assertEqual("blocked_http", candidate.cause_code)
            self.assertEqual("feed", candidate.fallback_source)
            self.assertEqual("forbidden", candidate.blocked_kind)
            self.assertEqual("rss", candidate.read_method)
            self.assertEqual(PARTIAL, candidate.reader_status)
            self.assertTrue(candidate.content)
            self.assertEqual(BLOCKED_URL, candidate.original_url)
        self.assertEqual(dossier.to_dict(), restored.to_dict())
        self.assertEqual(
            {
                "read_method": "rss",
                "degraded": True,
                "cause_code": "blocked_http",
                "fallback_source": "feed",
                "blocked_kind": "forbidden",
            },
            {
                key: restored.evidence[0].to_dict()[key]
                for key in (
                    "read_method",
                    "degraded",
                    "cause_code",
                    "fallback_source",
                    "blocked_kind",
                )
            },
        )

    def test_evidence_source_keeps_the_required_field_names(self) -> None:
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=lambda _url: {"content_text": PARTIAL_FEED},
        )

        dossier = self._service(reader=reader).research(["event-1"])
        payload = dossier.to_dict()["evidence"][0]

        # The doc's field names map onto the existing contract names: ``url``
        # is the canonical payload field (``original_url`` aliases it),
        # ``reader_status`` is the read status, and the adapter keeps
        # ``source_type`` as the literal "evidence" (``source_scope``).
        self.assertEqual(BLOCKED_URL, payload["url"])
        self.assertEqual(BLOCKED_URL, dossier.evidence[0].original_url)
        self.assertEqual("evidence", payload["source_scope"])
        self.assertEqual(PARTIAL, payload["reader_status"])
        self.assertEqual(PARTIAL, dossier.evidence[0].read_status)
        self.assertEqual("rss", payload["read_method"])
        self.assertTrue(payload["degraded"])
        self.assertEqual("blocked_http", payload["cause_code"])
        self.assertEqual("feed", payload["fallback_source"])
        self.assertEqual("forbidden", payload["blocked_kind"])

    def test_adapter_document_carries_source_type_and_read_method(self) -> None:
        reader = ReaderServiceEvidenceAdapter(
            ReaderService(_blocked_fetcher([])),
            fallback_resolver=lambda _url: {"content_text": PARTIAL_FEED},
        )

        document = reader.read(
            EvidenceCandidate(evidence_id="e1", title="被拦截的页面", url=BLOCKED_URL)
        )

        self.assertEqual("evidence", document.source_type)
        self.assertEqual("rss", document.fetch_method)
        self.assertEqual(BLOCKED_URL, document.original_url)
        self.assertTrue(document.degraded)
        self.assertEqual("blocked_http", document.cause_code)
        self.assertEqual("feed", document.fallback_source)


if __name__ == "__main__":
    unittest.main()