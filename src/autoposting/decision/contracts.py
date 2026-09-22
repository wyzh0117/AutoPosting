"""Versioned, JSON-safe contracts shared by planner and decision callers.

The contracts deliberately contain no radar, database, or web-layer types.  A
caller supplies a mapping plus a stable candidate id; this keeps the decision
layer usable by news, research, and resource discovery alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

CONTRACT_VERSION = "decision-contracts-v1"
RESEARCH_INTENT_VERSION = "research-intent-v1"
SEARCH_PLAN_VERSION = "search-plan-v1"
PLANNER_RESULT_VERSION = "planner-result-v1"
JEV_ASSESSMENT_VERSION = "jev-assessment-v1"
DECISION_ASSESSMENT_VERSION = "decision-assessment-v1"

_MAX_GOAL_CHARS = 1_000
_MAX_QUERY_CHARS = 200
_MAX_TERM_CHARS = 200
_MAX_LIST_ITEMS = 32
_MAX_REASON_CHARS = 240


class ContractError(ValueError):
    """A stable validation error for untrusted structured payloads."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason or code
        super().__init__(self.code)


class DecisionChoice(str, Enum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    REVIEW = "review"


class DecisionStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"


class EvidenceRelation(str, Enum):
    SAME_EVENT = "same_event"
    BACKGROUND = "background"
    FOLLOW_UP = "follow_up"
    INDUSTRY_REACTION = "industry_reaction"
    USER_EXPERIENCE = "user_experience"
    COMPETITOR = "competitor"
    CRITICISM = "criticism"
    RELATED_PROJECT = "related_project"
    TOPIC_ONLY = "topic_only"
    IRRELEVANT = "irrelevant"


class ResourceType(str, Enum):
    REPO = "repo"
    SKILL = "skill"
    TOOL = "tool"
    WEBSITE = "website"
    OTHER = "other"


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_must_be_object")
    return value


def _schema(value: Mapping[str, object], expected: str, name: str) -> None:
    version = value.get("schema_version")
    if version != expected:
        raise ContractError(f"{name}_unknown_schema_version")


def _unknown(value: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise ContractError(f"{name}_unknown_field_{unknown[0]}")


def _text(value: object, name: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise ContractError(f"{name}_must_be_string")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise ContractError(f"{name}_required")
    if len(normalized) > limit:
        raise ContractError(f"{name}_too_long")
    return normalized


def _strings(value: object, name: str, *, limit: int = _MAX_TERM_CHARS) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name}_must_be_string_list")
    if len(value) > _MAX_LIST_ITEMS:
        raise ContractError(f"{name}_too_many")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, name, limit=limit)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name}_must_be_boolean")
    return value


def _number(value: object, name: str, *, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name}_must_be_number")
    number = float(value)
    if number != number or number < lower or number > upper:
        raise ContractError(f"{name}_out_of_range")
    return number


@dataclass(frozen=True)
class ResearchIntent:
    """The user's research goal after Planner interpretation."""

    goal: str
    concepts: tuple[str, ...]
    search_repo: bool
    search_skill: bool
    search_web: bool
    preferred_signals: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal", limit=_MAX_GOAL_CHARS))
        object.__setattr__(self, "concepts", _strings(self.concepts, "concepts"))
        if not self.concepts:
            raise ContractError("concepts_required")
        object.__setattr__(self, "preferred_signals", _strings(self.preferred_signals, "preferred_signals"))
        object.__setattr__(self, "source_event_ids", _strings(self.source_event_ids, "source_event_ids"))
        for name in ("search_repo", "search_skill", "search_web"):
            _bool(getattr(self, name), name)
        if not (self.search_repo or self.search_skill or self.search_web):
            raise ContractError("at_least_one_search_lane_required")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_INTENT_VERSION,
            "goal": self.goal,
            "concepts": list(self.concepts),
            "search_repo": self.search_repo,
            "search_skill": self.search_skill,
            "search_web": self.search_web,
            "preferred_signals": list(self.preferred_signals),
            "source_event_ids": list(self.source_event_ids),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ResearchIntent":
        data = _object(value, "research_intent")
        _schema(data, RESEARCH_INTENT_VERSION, "research_intent")
        _unknown(
            data,
            {
                "schema_version",
                "goal",
                "concepts",
                "search_repo",
                "search_skill",
                "search_web",
                "preferred_signals",
                "source_event_ids",
            },
            "research_intent",
        )
        return cls(
            goal=_text(data.get("goal"), "goal", limit=_MAX_GOAL_CHARS),
            concepts=_strings(data.get("concepts"), "concepts"),
            search_repo=_bool(data.get("search_repo"), "search_repo"),
            search_skill=_bool(data.get("search_skill"), "search_skill"),
            search_web=_bool(data.get("search_web"), "search_web"),
            preferred_signals=_strings(data.get("preferred_signals", ()), "preferred_signals"),
            source_event_ids=_strings(data.get("source_event_ids", ()), "source_event_ids"),
        )

    def with_source_event_ids(self, event_ids: tuple[str, ...] | list[str]) -> "ResearchIntent":
        return ResearchIntent(
            goal=self.goal,
            concepts=self.concepts,
            search_repo=self.search_repo,
            search_skill=self.search_skill,
            search_web=self.search_web,
            preferred_signals=self.preferred_signals,
            source_event_ids=tuple(event_ids),
        )


@dataclass(frozen=True)
class SearchPlan:
    """A concrete adapter plan produced from a :class:`ResearchIntent`."""

    query: str
    adapters: tuple[str, ...]
    max_candidates: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "query", limit=_MAX_QUERY_CHARS))
        object.__setattr__(self, "adapters", _strings(self.adapters, "adapters", limit=80))
        if not self.adapters:
            raise ContractError("adapters_required")
        if isinstance(self.max_candidates, bool) or not isinstance(self.max_candidates, int):
            raise ContractError("max_candidates_must_be_integer")
        if self.max_candidates < 1 or self.max_candidates > 500:
            raise ContractError("max_candidates_out_of_range")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SEARCH_PLAN_VERSION,
            "query": self.query,
            "adapters": list(self.adapters),
            "max_candidates": self.max_candidates,
        }

    @classmethod
    def from_dict(cls, value: object) -> "SearchPlan":
        data = _object(value, "search_plan")
        _schema(data, SEARCH_PLAN_VERSION, "search_plan")
        _unknown(data, {"schema_version", "query", "adapters", "max_candidates"}, "search_plan")
        return cls(
            query=_text(data.get("query"), "query", limit=_MAX_QUERY_CHARS),
            adapters=_strings(data.get("adapters"), "adapters", limit=80),
            max_candidates=data.get("max_candidates", 50),
        )


@dataclass(frozen=True)
class PlannerResult:
    """The stable Planner output passed to search orchestration."""

    intent: ResearchIntent
    plan: SearchPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLANNER_RESULT_VERSION,
            "intent": self.intent.to_dict(),
            "plan": self.plan.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlannerResult":
        data = _object(value, "planner_result")
        _schema(data, PLANNER_RESULT_VERSION, "planner_result")
        _unknown(data, {"schema_version", "intent", "plan"}, "planner_result")
        return cls(
            intent=ResearchIntent.from_dict(data.get("intent")),
            plan=SearchPlan.from_dict(data.get("plan")),
        )


@dataclass(frozen=True)
class JEVAssessment:
    """The semantic portion returned by a Jev provider."""

    score: float
    confidence: float
    decision: DecisionChoice
    reasons: tuple[str, ...] = ()
    resource_type: ResourceType | None = None
    relation: EvidenceRelation | None = None
    worth_reading: bool | None = None
    duplicate: bool | None = None
    duplicate_of: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _number(self.score, "score", lower=0.0, upper=100.0))
        object.__setattr__(self, "confidence", _number(self.confidence, "confidence", lower=0.0, upper=1.0))
        if not isinstance(self.decision, DecisionChoice):
            try:
                object.__setattr__(self, "decision", DecisionChoice(self.decision))
            except (TypeError, ValueError):
                raise ContractError("decision_invalid") from None
        object.__setattr__(self, "reasons", _strings(self.reasons, "reasons", limit=_MAX_REASON_CHARS))
        if self.resource_type is not None and not isinstance(self.resource_type, ResourceType):
            try:
                object.__setattr__(self, "resource_type", ResourceType(self.resource_type))
            except (TypeError, ValueError):
                raise ContractError("resource_type_invalid") from None
        if self.relation is not None and not isinstance(self.relation, EvidenceRelation):
            try:
                object.__setattr__(self, "relation", EvidenceRelation(self.relation))
            except (TypeError, ValueError):
                raise ContractError("relation_invalid") from None
        for name in ("worth_reading", "duplicate"):
            value = getattr(self, name)
            if value is not None:
                _bool(value, name)
        if self.duplicate_of is not None:
            object.__setattr__(self, "duplicate_of", _text(self.duplicate_of, "duplicate_of", limit=200))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": JEV_ASSESSMENT_VERSION,
            "score": self.score,
            "confidence": self.confidence,
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "resource_type": self.resource_type.value if self.resource_type else None,
            "relation": self.relation.value if self.relation else None,
            "worth_reading": self.worth_reading,
            "duplicate": self.duplicate,
            "duplicate_of": self.duplicate_of,
        }

    @classmethod
    def from_dict(cls, value: object) -> "JEVAssessment":
        data = _object(value, "jev_assessment")
        _schema(data, JEV_ASSESSMENT_VERSION, "jev_assessment")
        _unknown(
            data,
            {
                "schema_version",
                "score",
                "confidence",
                "decision",
                "reasons",
                "resource_type",
                "relation",
                "worth_reading",
                "duplicate",
                "duplicate_of",
            },
            "jev_assessment",
        )
        resource_type = data.get("resource_type")
        relation = data.get("relation")
        return cls(
            score=_number(data.get("score"), "score", lower=0.0, upper=100.0),
            confidence=_number(data.get("confidence"), "confidence", lower=0.0, upper=1.0),
            decision=data.get("decision"),
            reasons=_strings(data.get("reasons", ()), "reasons", limit=_MAX_REASON_CHARS),
            resource_type=None if resource_type is None else resource_type,
            relation=None if relation is None else relation,
            worth_reading=data.get("worth_reading"),
            duplicate=data.get("duplicate"),
            duplicate_of=data.get("duplicate_of"),
        )


# Keep the public spelling aligned with the product name while retaining the
# uppercase acronym used by the versioned payload type.
JevAssessment = JEVAssessment


@dataclass(frozen=True)
class DecisionAssessment:
    """A generic candidate decision; no radar-specific type is required."""

    candidate_id: str
    objective_score: float
    semantic_score: float | None
    final_score: float
    confidence: float | None
    decision: DecisionChoice
    resource_type: ResourceType | None
    relation: EvidenceRelation | None
    worth_reading: bool | None
    duplicate: bool | None
    duplicate_of: str | None
    reasons: tuple[str, ...]
    rubric_id: str
    rubric_version: str
    status: DecisionStatus
    degraded: bool
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate_id", limit=400))
        object.__setattr__(self, "objective_score", _number(self.objective_score, "objective_score", lower=0.0, upper=100.0))
        if self.semantic_score is not None:
            object.__setattr__(self, "semantic_score", _number(self.semantic_score, "semantic_score", lower=0.0, upper=100.0))
        object.__setattr__(self, "final_score", _number(self.final_score, "final_score", lower=0.0, upper=100.0))
        if self.confidence is not None:
            object.__setattr__(self, "confidence", _number(self.confidence, "confidence", lower=0.0, upper=1.0))
        if not isinstance(self.decision, DecisionChoice):
            try:
                object.__setattr__(self, "decision", DecisionChoice(self.decision))
            except (TypeError, ValueError):
                raise ContractError("decision_invalid") from None
        if self.resource_type is not None and not isinstance(self.resource_type, ResourceType):
            try:
                object.__setattr__(self, "resource_type", ResourceType(self.resource_type))
            except (TypeError, ValueError):
                raise ContractError("resource_type_invalid") from None
        if self.relation is not None and not isinstance(self.relation, EvidenceRelation):
            try:
                object.__setattr__(self, "relation", EvidenceRelation(self.relation))
            except (TypeError, ValueError):
                raise ContractError("relation_invalid") from None
        for name in ("worth_reading", "duplicate"):
            value = getattr(self, name)
            if value is not None:
                _bool(value, name)
        if self.duplicate_of is not None:
            object.__setattr__(self, "duplicate_of", _text(self.duplicate_of, "duplicate_of", limit=400))
        object.__setattr__(self, "reasons", _strings(self.reasons, "reasons", limit=_MAX_REASON_CHARS))
        object.__setattr__(self, "rubric_id", _text(self.rubric_id, "rubric_id", limit=120))
        object.__setattr__(self, "rubric_version", _text(self.rubric_version, "rubric_version", limit=40))
        if not isinstance(self.status, DecisionStatus):
            try:
                object.__setattr__(self, "status", DecisionStatus(self.status))
            except (TypeError, ValueError):
                raise ContractError("status_invalid") from None
        _bool(self.degraded, "degraded")
        normalized_diagnostics: list[Mapping[str, object]] = []
        for diagnostic in self.diagnostics:
            if not isinstance(diagnostic, Mapping):
                raise ContractError("diagnostics_must_be_object_list")
            normalized_diagnostics.append(dict(diagnostic))
        object.__setattr__(self, "diagnostics", tuple(normalized_diagnostics))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DECISION_ASSESSMENT_VERSION,
            "candidate_id": self.candidate_id,
            "objective_score": self.objective_score,
            "semantic_score": self.semantic_score,
            "final_score": self.final_score,
            "confidence": self.confidence,
            "decision": self.decision.value,
            "resource_type": self.resource_type.value if self.resource_type else None,
            "relation": self.relation.value if self.relation else None,
            "worth_reading": self.worth_reading,
            "duplicate": self.duplicate,
            "duplicate_of": self.duplicate_of,
            "reasons": list(self.reasons),
            "rubric_id": self.rubric_id,
            "rubric_version": self.rubric_version,
            "status": self.status.value,
            "degraded": self.degraded,
            "diagnostics": [dict(item) for item in self.diagnostics],
        }
