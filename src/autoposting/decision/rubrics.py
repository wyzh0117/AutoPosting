"""Central Product Rules and Jev rubric registry."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .contracts import ContractError

OBJECTIVE_WEIGHT = 0.60
JEV_SEMANTIC_WEIGHT = 0.40
JEV_MIN_CONFIDENCE = 0.50


@dataclass(frozen=True)
class RubricQuestion:
    question_id: str
    version: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.question_id, "version": self.version, "label": self.label}


@dataclass(frozen=True)
class Rubric:
    rubric_id: str
    version: str
    domain: str
    questions: tuple[RubricQuestion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.rubric_id,
            "version": self.version,
            "domain": self.domain,
            "questions": [question.to_dict() for question in self.questions],
        }


NEWS_RUBRIC_ID = "news_event_v1"
RESOURCE_RUBRIC_ID = "resource_candidate_v1"
RESEARCH_RUBRIC_ID = "evidence_relation_v1"

NEWS_RUBRIC = Rubric(
    NEWS_RUBRIC_ID,
    "1",
    "news_event",
    (
        RubricQuestion("news.importance", "1", "industry importance"),
        RubricQuestion("news.novelty", "1", "novelty"),
        RubricQuestion("news.value", "1", "value to the target reader"),
        RubricQuestion("news.research", "1", "worth further research"),
        RubricQuestion("news.marketing", "1", "marketing or recycled-news risk"),
        RubricQuestion("news.discussion", "1", "discussion value"),
    ),
)

RESOURCE_RUBRIC = Rubric(
    RESOURCE_RUBRIC_ID,
    "1",
    "resource_candidate",
    (
        RubricQuestion("resource.relevance", "1", "fit for the goal"),
        RubricQuestion("resource.type", "1", "resource type"),
        RubricQuestion("resource.read", "1", "worth reading in depth"),
        RubricQuestion("resource.duplicate", "1", "duplicate or near duplicate"),
    ),
)

RESEARCH_RUBRIC = Rubric(
    RESEARCH_RUBRIC_ID,
    "1",
    "evidence_relation",
    (
        RubricQuestion("evidence.relation", "1", "relation to the selected topic"),
        RubricQuestion("evidence.relevance", "1", "relevance to the research intent"),
    ),
)

QUESTION_REGISTRY = MappingProxyType(
    {
        question.question_id: question
        for rubric in (NEWS_RUBRIC, RESOURCE_RUBRIC, RESEARCH_RUBRIC)
        for question in rubric.questions
    }
)

RUBRIC_REGISTRY = MappingProxyType(
    {
        NEWS_RUBRIC.rubric_id: NEWS_RUBRIC,
        RESOURCE_RUBRIC.rubric_id: RESOURCE_RUBRIC,
        RESEARCH_RUBRIC.rubric_id: RESEARCH_RUBRIC,
    }
)


def get_rubric(rubric_id: str) -> Rubric:
    try:
        return RUBRIC_REGISTRY[rubric_id]
    except KeyError as exc:
        raise ContractError("unknown_rubric") from exc
