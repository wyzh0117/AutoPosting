"""Versioned, persistence-ready contracts for research workflows.

These contracts intentionally do not import Database, Workbench, or HTTP
types.  The persistence owner can store ``TopicDossier.to_dict()`` using the
project's existing JSON envelope and implement :class:`DossierRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from ..decision.contracts import (
    DecisionAssessment,
    EvidenceRelation,
    PlannerResult,
)

RESEARCH_CONTRACT_VERSION = "research-contracts-v1"
RESEARCH_TOPIC_VERSION = "research-topic-v1"
EVIDENCE_CANDIDATE_VERSION = "evidence-candidate-v1"
EVIDENCE_GROUP_VERSION = "evidence-group-v1"
SEARCH_RESPONSE_VERSION = "search-response-v1"
TOPIC_DOSSIER_VERSION = "topic-dossier-v1"

_MAX_ID_CHARS = 400
_MAX_TEXT_CHARS = 20_000
_MAX_TITLE_CHARS = 1_000
_MAX_URL_CHARS = 4_000
_MAX_LIST_ITEMS = 100


class ResearchStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    EMPTY = "empty"


def _text(value: object, name: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise ValueError(f"{name}_must_be_string")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise ValueError(f"{name}_required")
    if len(normalized) > limit:
        raise ValueError(f"{name}_too_long")
    return normalized


def _strings(value: object, name: str, *, limit: int = _MAX_ID_CHARS) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name}_must_be_string_list")
    if len(value) > _MAX_LIST_ITEMS:
        raise ValueError(f"{name}_too_many")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _text(item, name, limit=limit)
        if cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return tuple(result)


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}_must_be_object")
    return value


def _schema(value: Mapping[str, object], expected: str, name: str) -> None:
    version = value.get("schema_version")
    if version is not None and version != expected:
        raise ValueError(f"{name}_unknown_schema_version")


def _url(value: object, name: str = "url") -> str:
    text = _text(value, name, limit=_MAX_URL_CHARS)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name}_must_be_http_url")
    return text


def _source_urls(value: object, source_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Keep source urls positionally aligned with ``source_ids``.

    An event must never hand over two independently ordered lists: ``source_ids``
    is a sorted set in the radar payload while the urls follow source-record
    order, so a blind zip attributes one source's url to another.  The lists are
    therefore only used as a pairing when they have exactly one url per id;
    otherwise the urls are dropped instead of shifted.  A blank entry means
    "this source has no known url".
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError("source_urls_must_be_string_list")
    if len(value) > _MAX_LIST_ITEMS:
        raise ValueError("source_urls_too_many")
    urls = [
        _url(item, "source_url") if isinstance(item, str) and item.strip() else ""
        for item in value
    ]
    if len(urls) == len(source_ids):
        return tuple(urls)
    return tuple("" for _ in source_ids)


def _paired_sources(data: Mapping[str, object]) -> tuple[list[object], list[object]]:
    """Read the source ids and their own urls out of one event payload.

    ``sources`` records carry ``source_id`` and ``url`` together, so they are the
    only trustworthy pairing.  They are preferred whenever they name at least one
    source; ids that only appear in ``source_ids`` keep their slot with a blank
    url instead of borrowing a neighbour's.
    """
    records = data.get("sources")
    ids: list[object] = []
    urls: list[object] = []
    if isinstance(records, (list, tuple)):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            source_id = str(record.get("source_id") or record.get("id") or "").strip()
            url = str(record.get("url") or record.get("source_url") or "").strip()
            if not source_id or not url or source_id in ids:
                continue
            ids.append(source_id)
            urls.append(url)
    if not ids:
        return list(data.get("source_ids", ())), list(data.get("source_urls", ()))
    for source_id in data.get("source_ids", ()) if isinstance(data.get("source_ids"), (list, tuple)) else ():
        cleaned = str(source_id).strip()
        if cleaned and cleaned not in ids:
            ids.append(cleaned)
            urls.append("")
    return ids, urls


@dataclass(frozen=True)
class ResearchTopic:
    """A selected Event Cluster reduced to the research boundary."""

    event_id: str
    title: str
    topic: str = ""
    source_ids: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id", limit=_MAX_ID_CHARS))
        object.__setattr__(self, "title", _text(self.title, "title", limit=_MAX_TITLE_CHARS))
        object.__setattr__(self, "topic", _text(self.topic, "topic", limit=200, required=False))
        source_ids = _strings(self.source_ids, "source_ids")
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "source_urls", _source_urls(self.source_urls, source_ids))
        object.__setattr__(self, "item_ids", _strings(self.item_ids, "item_ids"))

    @classmethod
    def from_mapping(cls, value: object, *, event_id: str | None = None) -> "ResearchTopic":
        data = _object(value, "event")
        selected_id = event_id or data.get("event_id") or data.get("id")
        title = data.get("title") or data.get("name")
        source_ids, source_urls = _paired_sources(data)
        item_ids = data.get("item_ids", ())
        return cls(
            event_id=str(selected_id or ""),
            title=str(title or ""),
            topic=str(data.get("topic") or ""),
            source_ids=_strings(source_ids, "source_ids"),
            source_urls=tuple(str(item) for item in source_urls),
            item_ids=_strings(item_ids, "item_ids"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_TOPIC_VERSION,
            "event_id": self.event_id,
            "title": self.title,
            "topic": self.topic,
            "source_ids": list(self.source_ids),
            "source_urls": list(self.source_urls),
            "item_ids": list(self.item_ids),
        }


@dataclass(frozen=True)
class SearchResponse:
    """Normalized output of one or more search adapters."""

    query: str
    results: tuple[Mapping[str, object], ...] = ()
    adapters: tuple[str, ...] = ()
    degraded: bool = False
    cause_code: str = ""
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "query", limit=200))
        object.__setattr__(self, "adapters", _strings(self.adapters, "adapters", limit=80))
        normalized_results: list[Mapping[str, object]] = []
        for result in self.results:
            if not isinstance(result, Mapping):
                raise ValueError("results_must_be_object_list")
            normalized_results.append(dict(result))
        object.__setattr__(self, "results", tuple(normalized_results))
        normalized_diagnostics: list[Mapping[str, object]] = []
        for diagnostic in self.diagnostics:
            if not isinstance(diagnostic, Mapping):
                raise ValueError("diagnostics_must_be_object_list")
            normalized_diagnostics.append(dict(diagnostic))
        object.__setattr__(self, "diagnostics", tuple(normalized_diagnostics))
        if not isinstance(self.degraded, bool):
            raise ValueError("degraded_must_be_boolean")
        object.__setattr__(self, "cause_code", _text(self.cause_code, "cause_code", limit=120, required=False))

    @property
    def status(self) -> ResearchStatus:
        if self.degraded or self.diagnostics:
            return ResearchStatus.DEGRADED
        return ResearchStatus.OK if self.results else ResearchStatus.EMPTY

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SEARCH_RESPONSE_VERSION,
            "query": self.query,
            "results": [dict(item) for item in self.results],
            "adapters": list(self.adapters),
            "degraded": self.degraded,
            "cause_code": self.cause_code,
            "diagnostics": [dict(item) for item in self.diagnostics],
        }


@dataclass(frozen=True)
class EvidenceCandidate:
    """A temporary research source; it is never a Subscription Source."""

    evidence_id: str
    title: str
    url: str
    snippet: str = ""
    source: str = "search"
    event_ids: tuple[str, ...] = ()
    source_scope: str = "evidence"
    relation: EvidenceRelation = EvidenceRelation.TOPIC_ONLY
    assessment: DecisionAssessment | None = None
    content: str = ""
    reader_status: str = "unread"
    reader_quality: float | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, "evidence_id", limit=_MAX_ID_CHARS))
        object.__setattr__(self, "title", _text(self.title, "title", limit=_MAX_TITLE_CHARS))
        object.__setattr__(self, "url", _url(self.url))
        object.__setattr__(self, "snippet", _text(self.snippet, "snippet", limit=2_000, required=False))
        object.__setattr__(self, "source", _text(self.source, "source", limit=120))
        object.__setattr__(self, "event_ids", _strings(self.event_ids, "event_ids"))
        if self.source_scope != "evidence":
            raise ValueError("evidence_source_scope_required")
        if not isinstance(self.relation, EvidenceRelation):
            object.__setattr__(self, "relation", EvidenceRelation(self.relation))
        object.__setattr__(self, "content", _text(self.content, "content", limit=_MAX_TEXT_CHARS, required=False))
        object.__setattr__(self, "reader_status", _text(self.reader_status, "reader_status", limit=80))
        if self.reader_quality is not None:
            if isinstance(self.reader_quality, bool) or not isinstance(self.reader_quality, (int, float)):
                raise ValueError("reader_quality_must_be_number")
            if not 0.0 <= float(self.reader_quality) <= 1.0:
                raise ValueError("reader_quality_out_of_range")
            object.__setattr__(self, "reader_quality", float(self.reader_quality))
        if self.failure_reason is not None:
            object.__setattr__(self, "failure_reason", _text(self.failure_reason, "failure_reason", limit=200))

    def with_assessment(self, assessment: DecisionAssessment, relation: EvidenceRelation | None = None) -> "EvidenceCandidate":
        return replace(
            self,
            assessment=assessment,
            relation=relation or assessment.relation or EvidenceRelation.TOPIC_ONLY,
        )

    def with_document(
        self,
        *,
        content: str = "",
        reader_status: str = "failed",
        reader_quality: float | None = None,
        failure_reason: str | None = None,
        title: str | None = None,
    ) -> "EvidenceCandidate":
        return replace(
            self,
            title=title or self.title,
            content=content,
            reader_status=reader_status,
            reader_quality=reader_quality,
            failure_reason=failure_reason,
        )

    def decision_input(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "source_scope": self.source_scope,
            "event_ids": list(self.event_ids),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_CANDIDATE_VERSION,
            "evidence_id": self.evidence_id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "event_ids": list(self.event_ids),
            "source_scope": self.source_scope,
            "relation": self.relation.value,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "content": self.content,
            "reader_status": self.reader_status,
            "reader_quality": self.reader_quality,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class EvidenceGroup:
    relation: EvidenceRelation
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.relation, EvidenceRelation):
            object.__setattr__(self, "relation", EvidenceRelation(self.relation))
        object.__setattr__(self, "evidence_ids", _strings(self.evidence_ids, "evidence_ids"))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": EVIDENCE_GROUP_VERSION,
            "relation": self.relation.value,
            "evidence_ids": list(self.evidence_ids),
        }


class DossierRepository(Protocol):
    """Persistence seam for the DB owner in the next slice.

    Implementations should store ``TopicDossier.to_dict()`` in the project's
    existing versioned JSON envelope.  Research never registers evidence URLs
    as subscription sources.
    """

    def save_dossier(self, dossier: "TopicDossier") -> None:
        ...

    def get_dossier(self, dossier_id: str) -> "TopicDossier | Mapping[str, object] | None":
        ...

    def list_dossiers(self, limit: int = 20) -> list["TopicDossier | Mapping[str, object]"]:
        ...

    def delete_dossier(self, dossier_id: str) -> bool:
        ...


@dataclass(frozen=True)
class TopicDossier:
    dossier_id: str
    title: str
    query: str
    event_ids: tuple[str, ...]
    topics: tuple[ResearchTopic, ...]
    plan: PlannerResult
    evidence: tuple[EvidenceCandidate, ...]
    groups: tuple[EvidenceGroup, ...]
    status: ResearchStatus
    degraded: bool
    diagnostics: tuple[Mapping[str, object], ...]
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dossier_id", _text(self.dossier_id, "dossier_id", limit=_MAX_ID_CHARS))
        object.__setattr__(self, "title", _text(self.title, "title", limit=_MAX_TITLE_CHARS))
        object.__setattr__(self, "query", _text(self.query, "query", limit=200))
        object.__setattr__(self, "event_ids", _strings(self.event_ids, "event_ids"))
        if not self.event_ids:
            raise ValueError("event_ids_required")
        if not isinstance(self.status, ResearchStatus):
            object.__setattr__(self, "status", ResearchStatus(self.status))
        if not isinstance(self.degraded, bool):
            raise ValueError("degraded_must_be_boolean")
        normalized_diagnostics: list[Mapping[str, object]] = []
        for diagnostic in self.diagnostics:
            if not isinstance(diagnostic, Mapping):
                raise ValueError("diagnostics_must_be_object_list")
            normalized_diagnostics.append(dict(diagnostic))
        object.__setattr__(self, "diagnostics", tuple(normalized_diagnostics))
        object.__setattr__(self, "created_at", _text(self.created_at, "created_at", limit=80))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": TOPIC_DOSSIER_VERSION,
            "dossier_id": self.dossier_id,
            "title": self.title,
            "query": self.query,
            "event_ids": list(self.event_ids),
            "topics": [topic.to_dict() for topic in self.topics],
            "plan": self.plan.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
            "groups": [group.to_dict() for group in self.groups],
            "status": self.status.value,
            "degraded": self.degraded,
            "diagnostics": [dict(item) for item in self.diagnostics],
            "created_at": self.created_at,
        }
