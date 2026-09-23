"""Research orchestration from Event Cluster ids to Topic Dossier."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from ..decision.contracts import (
    DecisionAssessment,
    DecisionChoice,
    DecisionStatus,
    EvidenceRelation,
    PlannerResult,
    ResourceType,
)
from ..decision.planner import LLMPlanner, PlannerError, build_fallback_plan
from ..decision.rubrics import RESEARCH_RUBRIC_ID
from ..decision.service import DecisionService
from ..diagnostics import make_diagnostic, normalize_diagnostic
from ..reader import ArticleDocument, classify_fetch_error
from .adapters import EvidenceReader, RelatedNewsSearchAdapter, SearchAdapter
from .contracts import (
    DossierRepository,
    EvidenceCandidate,
    EvidenceGroup,
    ResearchStatus,
    ResearchTopic,
    SearchResponse,
    TopicDossier,
)

MAX_SELECTED_EVENTS = 16
MAX_SEARCH_CANDIDATES = 24
MAX_DEEP_READ = 6
OBJECTIVE_EVIDENCE_START = 100.0
OBJECTIVE_EVIDENCE_STEP = 5.0
FALLBACK_READ_SCORE = 50.0
RELATION_ORDER = (
    EvidenceRelation.SAME_EVENT,
    EvidenceRelation.BACKGROUND,
    EvidenceRelation.FOLLOW_UP,
    EvidenceRelation.INDUSTRY_REACTION,
    EvidenceRelation.USER_EXPERIENCE,
    EvidenceRelation.COMPETITOR,
    EvidenceRelation.CRITICISM,
    EvidenceRelation.RELATED_PROJECT,
    EvidenceRelation.TOPIC_ONLY,
    EvidenceRelation.IRRELEVANT,
)


class EventResolver(Protocol):
    def get_event(self, event_id: str) -> Mapping[str, object] | None:
        ...


class ResearchError(Exception):
    """Stable input/persistence boundary error."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason or code
        super().__init__(self.code)


def _clean_event_ids(event_ids: Iterable[str]) -> tuple[str, ...]:
    if not isinstance(event_ids, (list, tuple)):
        raise ResearchError("event_ids_must_be_list")
    if not event_ids or len(event_ids) > MAX_SELECTED_EVENTS:
        raise ResearchError("event_ids_count_invalid")
    result: list[str] = []
    seen: set[str] = set()
    for value in event_ids:
        if not isinstance(value, str) or not value.strip():
            raise ResearchError("event_id_invalid")
        cleaned = value.strip()
        if len(cleaned) > 400:
            raise ResearchError("event_id_too_long")
        if cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    if not result:
        raise ResearchError("event_ids_count_invalid")
    return tuple(result)


def _query_from_topics(topics: tuple[ResearchTopic, ...], query: str | None) -> str:
    if query is not None:
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 200:
            raise ResearchError("query_invalid")
        return " ".join(query.split())
    text = "；".join(topic.title for topic in topics)
    normalized = " ".join(text.split())[:200]
    if not normalized:
        raise ResearchError("query_invalid")
    return normalized


def _canonical_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _evidence_id(url: str) -> str:
    return hashlib.sha256(f"evidence:v1:{url}".encode("utf-8")).hexdigest()[:24]


def _candidate_text(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split())


def _reader_result(value: object, candidate: EvidenceCandidate) -> tuple[EvidenceCandidate, dict[str, object] | None]:
    if isinstance(value, ArticleDocument):
        status = str(value.status or "failed")
        content = _candidate_text(value.text)
        title = _candidate_text(value.title, candidate.title)
        failure = _candidate_text(value.failure_reason or "") or None
        cause = _candidate_text(value.cause_code or "") or failure
        # A read is degraded when the document says a legal fallback replaced
        # the page, or when it produced nothing usable at all.
        degraded = bool(value.degraded) or not (status in {"full", "partial"} and bool(content))
        updated = candidate.with_document(
            content=content,
            reader_status=status,
            reader_quality=float(value.quality_score),
            failure_reason=failure,
            title=title,
            read_method=str(value.fetch_method or "none"),
            degraded=degraded,
            cause_code=cause,
            fallback_source=value.fallback_source,
            blocked_kind=value.blocked_kind,
        )
        if status in {"full", "partial"} and content and not degraded:
            return updated, None
        return updated, make_diagnostic(
            "reader",
            "evidence_read",
            code="evidence_read_degraded",
            cause_code=cause or "no_content",
            detail=failure or "reader returned no usable content",
        )
    if isinstance(value, Mapping):
        status = _candidate_text(value.get("status"), "failed")
        content = _candidate_text(value.get("text") or value.get("content"))
        title = _candidate_text(value.get("title"), candidate.title)
        reason = _candidate_text(value.get("failure_reason") or value.get("error")) or None
        cause = _candidate_text(value.get("cause_code") or "") or reason
        quality = value.get("quality_score")
        numeric_quality = float(quality) if isinstance(quality, (int, float)) and not isinstance(quality, bool) else None
        degraded = bool(value.get("degraded", False)) or not (
            status in {"full", "partial"} and bool(content)
        )
        updated = candidate.with_document(
            content=content,
            reader_status=status,
            reader_quality=numeric_quality,
            failure_reason=reason,
            title=title,
            read_method=_candidate_text(value.get("read_method") or value.get("fetch_method")) or "none",
            degraded=degraded,
            cause_code=cause,
            fallback_source=_candidate_text(value.get("fallback_source")) or None,
            blocked_kind=_candidate_text(value.get("blocked_kind")) or None,
        )
        if status in {"full", "partial"} and content and not degraded:
            return updated, None
        return updated, make_diagnostic(
            "reader",
            "evidence_read",
            code="evidence_read_degraded",
            cause_code=cause or "no_content",
            detail=reason or "reader returned no usable content",
        )
    if isinstance(value, str) and value.strip():
        return candidate.with_document(content=value, reader_status="full", reader_quality=1.0), None
    return candidate.with_document(reader_status="failed", failure_reason="invalid_reader_response"), make_diagnostic(
        "reader",
        "evidence_read",
        code="evidence_read_failed",
        cause_code="invalid_response",
        detail="reader returned an unsupported shape",
    )


class ResearchService:
    """Run one selected-event research workflow with safe degradation."""

    def __init__(
        self,
        planner: LLMPlanner,
        decision_service: DecisionService,
        event_resolver: EventResolver | Callable[[str], Mapping[str, object] | None],
        *,
        search_adapter: SearchAdapter | None = None,
        reader: EvidenceReader | Callable[[EvidenceCandidate], object] | None = None,
        repository: DossierRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        max_candidates: int = MAX_SEARCH_CANDIDATES,
        max_deep_read: int = MAX_DEEP_READ,
    ) -> None:
        if max_candidates < 1 or max_candidates > 100:
            raise ValueError("max_candidates out of range")
        if max_deep_read < 0 or max_deep_read > max_candidates:
            raise ValueError("max_deep_read out of range")
        self.planner = planner
        self.decision_service = decision_service
        self.event_resolver = event_resolver
        self.search_adapter = search_adapter or RelatedNewsSearchAdapter()
        self.reader = reader
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_candidates = max_candidates
        self.max_deep_read = max_deep_read

    def research(
        self,
        event_ids: list[str] | tuple[str, ...],
        *,
        query: str | None = None,
    ) -> TopicDossier:
        selected_ids = _clean_event_ids(event_ids)
        topics = tuple(self._load_topic(event_id) for event_id in selected_ids)
        search_query = _query_from_topics(topics, query)
        diagnostics: list[dict[str, object]] = []
        degraded = False
        try:
            planned = self.planner.plan(search_query, source_event_ids=selected_ids)
        except PlannerError as exc:
            planned = build_fallback_plan(search_query, source_event_ids=selected_ids)
            diagnostics.append(dict(exc.diagnostic))
            degraded = True

        response = self._search(
            planned.plan.query,
            planned.plan.adapters,
            limit=min(self.max_candidates, planned.plan.max_candidates),
        )
        diagnostics.extend(dict(item) for item in response.diagnostics)
        degraded = degraded or response.degraded or bool(response.diagnostics)
        candidates, candidate_diagnostics = self._normalize_candidates(response.results, selected_ids)
        diagnostics.extend(candidate_diagnostics)
        degraded = degraded or bool(candidate_diagnostics)

        assessed: list[tuple[EvidenceCandidate, float]] = []
        for index, candidate in enumerate(candidates):
            objective = max(0.0, OBJECTIVE_EVIDENCE_START - index * OBJECTIVE_EVIDENCE_STEP)
            try:
                assessment = self.decision_service.assess(
                    candidate.decision_input(),
                    candidate_id=candidate.evidence_id,
                    objective_score=objective,
                    intent=planned.intent,
                    rubric_id=RESEARCH_RUBRIC_ID,
                )
            except Exception as exc:
                diagnostic = make_diagnostic(
                    "decision",
                    "evidence_assess",
                    code="evidence_assessment_failed",
                    cause_code="unknown_error",
                    detail=exc,
                )
                diagnostics.append(diagnostic)
                degraded = True
                assessed.append((candidate, objective))
                continue
            diagnostics.extend(dict(item) for item in assessment.diagnostics)
            degraded = degraded or assessment.degraded
            assessed.append((candidate.with_assessment(assessment), assessment.final_score))

        assessed.sort(key=lambda item: item[1], reverse=True)
        candidates_with_documents = self._read_high_value(assessed, diagnostics)
        degraded = degraded or any(item.failure_reason for item in candidates_with_documents)
        groups = self._group(candidates_with_documents)
        status = (
            ResearchStatus.DEGRADED
            if degraded
            else ResearchStatus.OK
            if candidates_with_documents
            else ResearchStatus.EMPTY
        )
        if not candidates_with_documents and response.cause_code == "no_qualified_result":
            status = ResearchStatus.EMPTY
        title = topics[0].title if len(topics) == 1 else "；".join(topic.title for topic in topics)[:1_000]
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        dossier = TopicDossier(
            dossier_id=self._dossier_id(selected_ids, planned.plan.query),
            title=title,
            query=search_query,
            event_ids=selected_ids,
            topics=topics,
            plan=planned,
            evidence=tuple(candidates_with_documents),
            groups=tuple(groups),
            status=status,
            degraded=degraded,
            diagnostics=tuple(diagnostics),
            created_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        return self._persist(dossier, diagnostics)

    def restore(self, dossier_id: str) -> TopicDossier:
        if self.repository is None:
            raise ResearchError("dossier_repository_not_configured")
        raw = self.repository.get_dossier(dossier_id)
        if raw is None:
            raise ResearchError("dossier_not_found")
        if isinstance(raw, TopicDossier):
            return raw
        if not isinstance(raw, Mapping):
            raise ResearchError("dossier_invalid_storage")
        data: object = raw
        if raw.get("data") is not None and raw.get("schema_version") in {"topic-dossier-envelope-v1", "topic-dossier-v1"}:
            data = raw.get("data")
        return self._decode_dossier(data)

    def _load_topic(self, event_id: str) -> ResearchTopic:
        try:
            if callable(self.event_resolver):
                value = self.event_resolver(event_id)
            else:
                value = self.event_resolver.get_event(event_id)
        except KeyError:
            value = None
        if value is None:
            raise ResearchError("event_not_found")
        try:
            return ResearchTopic.from_mapping(value, event_id=event_id)
        except (TypeError, ValueError) as exc:
            raise ResearchError("event_invalid") from exc

    def _search(
        self,
        query: str,
        adapters: tuple[str, ...],
        *,
        limit: int | None = None,
    ) -> SearchResponse:
        try:
            response = self.search_adapter.search(
                query,
                adapters=adapters,
                limit=limit if limit is not None else self.max_candidates,
            )
        except Exception as exc:
            diagnostic = make_diagnostic(
                "research_search",
                "research_search",
                code="research_search_failed",
                cause_code="unknown_error",
                detail=exc,
            )
            return SearchResponse(
                query=query,
                adapters=adapters,
                degraded=True,
                cause_code="unknown_error",
                diagnostics=(diagnostic,),
            )
        if isinstance(response, SearchResponse):
            return replace(
                response,
                diagnostics=tuple(
                    normalize_diagnostic(item, "research_search", "research_search")
                    for item in response.diagnostics
                ),
            )
        if isinstance(response, Mapping):
            try:
                raw_results = response.get("results", ())
                raw_diagnostics = response.get("diagnostics", ())
                if not isinstance(raw_results, (list, tuple)) or not isinstance(
                    raw_diagnostics, (list, tuple)
                ):
                    raise ValueError("search_response_lists_required")
                return SearchResponse(
                    query=str(response.get("query") or query),
                    results=tuple(raw_results),
                    adapters=adapters,
                    degraded=response.get("degraded", False),
                    cause_code=str(response.get("cause_code") or ""),
                    diagnostics=tuple(
                        normalize_diagnostic(item, "research_search", "research_search")
                        for item in raw_diagnostics
                    ),
                )
            except (TypeError, ValueError) as exc:
                return SearchResponse(
                    query=query,
                    adapters=adapters,
                    degraded=True,
                    cause_code="invalid_response",
                    diagnostics=(make_diagnostic("research_search", "research_search", code="research_search_failed", cause_code="invalid_response", detail=exc),),
                )
        return SearchResponse(
            query=query,
            adapters=adapters,
            degraded=True,
            cause_code="invalid_response",
            diagnostics=(make_diagnostic("research_search", "research_search", code="research_search_failed", cause_code="invalid_response", detail="adapter returned unsupported shape"),),
        )

    @staticmethod
    def _normalize_candidates(
        results: Iterable[Mapping[str, object]], event_ids: tuple[str, ...]
    ) -> tuple[list[EvidenceCandidate], list[dict[str, object]]]:
        candidates: list[EvidenceCandidate] = []
        diagnostics: list[dict[str, object]] = []
        seen_urls: set[str] = set()
        for result in results:
            url = _canonical_url(result.get("url") or result.get("canonical_url"))
            title = _candidate_text(result.get("title") or result.get("name"))
            if not url or not title:
                diagnostics.append(
                    make_diagnostic(
                        "research_search",
                        "candidate_normalize",
                        code="evidence_candidate_skipped",
                        cause_code="invalid_response",
                        detail="search result missing title or HTTP URL",
                    )
                )
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                candidates.append(
                    EvidenceCandidate(
                        evidence_id=_evidence_id(url),
                        title=title,
                        url=url,
                        snippet=_candidate_text(result.get("snippet") or result.get("summary"))[:2_000],
                        source=_candidate_text(result.get("source") or result.get("source_name"), "related_search")[:120],
                        event_ids=event_ids,
                    )
                )
            except (TypeError, ValueError) as exc:
                diagnostics.append(
                    make_diagnostic(
                        "research_search",
                        "candidate_normalize",
                        code="evidence_candidate_skipped",
                        cause_code="invalid_response",
                        detail=exc,
                    )
                )
        return candidates, diagnostics

    def _read_high_value(
        self,
        assessed: list[tuple[EvidenceCandidate, float]],
        diagnostics: list[dict[str, object]],
    ) -> list[EvidenceCandidate]:
        if not assessed:
            return []
        selected: list[EvidenceCandidate] = []
        for candidate, score in assessed:
            assessment = candidate.assessment
            high_value = score >= FALLBACK_READ_SCORE
            if assessment is not None:
                if assessment.status == DecisionStatus.DEGRADED:
                    # Objective-only fallback may still deepen a candidate
                    # when Jev is unavailable, but never a known duplicate.
                    high_value = high_value and assessment.worth_reading is not False and assessment.duplicate is not True
                else:
                    # A high-confidence Jev exclusion/review is not a deep
                    # read candidate merely because its objective score is high.
                    high_value = high_value and assessment.decision.value == "include" and assessment.worth_reading is not False and assessment.duplicate is not True
            if high_value and len(selected) < self.max_deep_read:
                selected.append(candidate)
        selected_ids = {item.evidence_id for item in selected}
        result: list[EvidenceCandidate] = []
        for candidate, _score in assessed:
            if candidate.evidence_id not in selected_ids or self.reader is None:
                result.append(candidate)
                continue
            try:
                reader = self.reader
                value = reader(candidate) if callable(reader) else reader.read(candidate)
                updated, diagnostic = _reader_result(value, candidate)
                result.append(updated)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
            except Exception as exc:
                cause_code = classify_fetch_error(exc)
                diagnostics.append(
                    make_diagnostic(
                        "reader",
                        "evidence_read",
                        code="evidence_read_failed",
                        cause_code=cause_code,
                        detail=exc,
                    )
                )
                result.append(
                    candidate.with_document(
                        reader_status="failed",
                        failure_reason="reader_failed",
                        degraded=True,
                        cause_code=cause_code,
                    )
                )
        return result

    @staticmethod
    def _group(candidates: Iterable[EvidenceCandidate]) -> list[EvidenceGroup]:
        by_relation: dict[EvidenceRelation, list[str]] = {relation: [] for relation in RELATION_ORDER}
        for candidate in candidates:
            by_relation.setdefault(candidate.relation, []).append(candidate.evidence_id)
        return [
            EvidenceGroup(relation, tuple(by_relation[relation]))
            for relation in RELATION_ORDER
            if by_relation.get(relation)
        ]

    def _persist(self, dossier: TopicDossier, diagnostics: list[dict[str, object]]) -> TopicDossier:
        if self.repository is None:
            return dossier
        try:
            self.repository.save_dossier(dossier)
        except Exception as exc:
            diagnostics.append(
                make_diagnostic(
                    "dossier_store",
                    "dossier_persist",
                    code="dossier_persist_failed",
                    cause_code="storage_error",
                    detail=exc,
                )
            )
            dossier = replace(
                dossier,
                status=ResearchStatus.DEGRADED,
                degraded=True,
                diagnostics=tuple(diagnostics),
            )
            # Best effort retry is deliberately omitted: a failing repository
            # must not turn a user-facing research result into a write loop.
            return dossier
        return dossier

    @staticmethod
    def _dossier_id(event_ids: tuple[str, ...], query: str) -> str:
        identity = "|".join(event_ids) + "|" + query
        return hashlib.sha256(f"topic-dossier:v1:{identity}".encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _decode_dossier(value: object) -> TopicDossier:
        if not isinstance(value, Mapping):
            raise ResearchError("dossier_invalid_storage")
        try:
            topics = tuple(
                ResearchTopic.from_mapping(item, event_id=str(item.get("event_id") or ""))
                for item in value.get("topics", ())
                if isinstance(item, Mapping)
            )
            evidence: list[EvidenceCandidate] = []
            for item in value.get("evidence", ()):
                if not isinstance(item, Mapping):
                    continue
                assessment_value = item.get("assessment")
                assessment = (
                    ResearchService._decode_assessment(assessment_value)
                    if isinstance(assessment_value, Mapping)
                    else None
                )
                evidence.append(
                    EvidenceCandidate(
                        evidence_id=str(item.get("evidence_id") or ""),
                        title=str(item.get("title") or ""),
                        url=str(item.get("url") or ""),
                        snippet=str(item.get("snippet") or ""),
                        source=str(item.get("source") or "search"),
                        event_ids=tuple(str(item_id) for item_id in item.get("event_ids", ()) if str(item_id)),
                        source_scope=str(item.get("source_scope") or "evidence"),
                        relation=EvidenceRelation(str(item.get("relation") or EvidenceRelation.TOPIC_ONLY.value)),
                        assessment=assessment,
                        content=str(item.get("content") or ""),
                        reader_status=str(item.get("reader_status") or "unread"),
                        reader_quality=item.get("reader_quality"),
                        failure_reason=item.get("failure_reason"),
                        read_method=str(item.get("read_method") or "none"),
                        degraded=bool(item.get("degraded", False)),
                        cause_code=item.get("cause_code"),
                        fallback_source=item.get("fallback_source"),
                        blocked_kind=item.get("blocked_kind"),
                    )
                )
            groups = tuple(
                EvidenceGroup(
                    relation=EvidenceRelation(str(item.get("relation") or EvidenceRelation.TOPIC_ONLY.value)),
                    evidence_ids=tuple(str(evidence_id) for evidence_id in item.get("evidence_ids", ()) if str(evidence_id)),
                )
                for item in value.get("groups", ())
                if isinstance(item, Mapping)
            )
            return TopicDossier(
                dossier_id=str(value.get("dossier_id") or ""),
                title=str(value.get("title") or ""),
                query=str(value.get("query") or ""),
                event_ids=tuple(str(event_id) for event_id in value.get("event_ids", ()) if str(event_id)),
                topics=topics,
                plan=PlannerResult.from_dict(value.get("plan")),
                evidence=tuple(evidence),
                groups=groups,
                status=ResearchStatus(str(value.get("status") or ResearchStatus.EMPTY.value)),
                degraded=bool(value.get("degraded", False)),
                diagnostics=tuple(item for item in value.get("diagnostics", ()) if isinstance(item, Mapping)),
                created_at=str(value.get("created_at") or ""),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ResearchError("dossier_invalid_storage") from exc

    @staticmethod
    def _decode_assessment(value: Mapping[str, object]) -> DecisionAssessment:
        """Rehydrate the nested decision without importing radar or web types."""
        resource_type = value.get("resource_type")
        relation = value.get("relation")
        return DecisionAssessment(
            candidate_id=str(value.get("candidate_id") or ""),
            objective_score=value.get("objective_score", 0.0),
            semantic_score=value.get("semantic_score"),
            final_score=value.get("final_score", 0.0),
            confidence=value.get("confidence"),
            decision=DecisionChoice(str(value.get("decision") or DecisionChoice.REVIEW.value)),
            resource_type=None if resource_type is None else ResourceType(str(resource_type)),
            relation=None if relation is None else EvidenceRelation(str(relation)),
            worth_reading=value.get("worth_reading"),
            duplicate=value.get("duplicate"),
            duplicate_of=value.get("duplicate_of"),
            reasons=tuple(str(item) for item in value.get("reasons", ()) if isinstance(item, str)),
            rubric_id=str(value.get("rubric_id") or RESEARCH_RUBRIC_ID),
            rubric_version=str(value.get("rubric_version") or "unknown"),
            status=DecisionStatus(str(value.get("status") or DecisionStatus.DEGRADED.value)),
            degraded=bool(value.get("degraded", False)),
            diagnostics=tuple(item for item in value.get("diagnostics", ()) if isinstance(item, Mapping)),
        )
