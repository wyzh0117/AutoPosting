"""Decision orchestration and objective/Jev mixed scoring."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from .adapters.jev import JevAdapter, JevError
from .contracts import (
    DecisionAssessment,
    DecisionChoice,
    DecisionStatus,
    EvidenceRelation,
    ResearchIntent,
    ResourceType,
)
from .rubrics import (
    JEV_MIN_CONFIDENCE,
    JEV_SEMANTIC_WEIGHT,
    NEWS_RUBRIC_ID,
    OBJECTIVE_WEIGHT,
    Rubric,
    get_rubric,
)


ObjectiveScorer = Callable[[Mapping[str, object]], float]


def _default_objective_score(candidate: Mapping[str, object]) -> float:
    value = candidate.get("objective_score", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(100.0, max(0.0, float(value)))


def _candidate_id(candidate: Mapping[str, object], explicit: str | None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    for key in ("candidate_id", "id", "event_id", "item_id", "url"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("candidate_id required")


class DecisionService:
    """Assess generic mappings without importing radar or web-layer objects."""

    def __init__(
        self,
        jev: JevAdapter | None = None,
        *,
        objective_scorer: ObjectiveScorer | None = None,
        min_confidence: float = JEV_MIN_CONFIDENCE,
    ) -> None:
        if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)):
            raise ValueError("min_confidence must be a number")
        if min_confidence < 0 or min_confidence > 1:
            raise ValueError("min_confidence must be between 0 and 1")
        self.jev = jev
        self.objective_scorer = objective_scorer or _default_objective_score
        self.min_confidence = float(min_confidence)

    def assess(
        self,
        candidate: Mapping[str, object],
        *,
        candidate_id: str | None = None,
        objective_score: float | None = None,
        intent: ResearchIntent | None = None,
        rubric_id: str = NEWS_RUBRIC_ID,
    ) -> DecisionAssessment:
        if not isinstance(candidate, Mapping):
            raise ValueError("candidate must be an object")
        stable_id = _candidate_id(candidate, candidate_id)
        rubric = get_rubric(rubric_id)
        raw_objective = self.objective_scorer(candidate) if objective_score is None else objective_score
        if isinstance(raw_objective, bool) or not isinstance(raw_objective, (int, float)):
            raise ValueError("objective_score must be a number")
        objective = min(100.0, max(0.0, float(raw_objective)))
        if self.jev is None:
            return self._objective_only(
                stable_id,
                objective,
                rubric,
                cause_code="not_configured",
                detail="jev adapter is not configured",
            )
        try:
            semantic = self.jev.assess(
                candidate,
                candidate_id=stable_id,
                rubric=rubric,
                intent=intent,
            )
        except JevError as exc:
            return self._objective_only(
                stable_id,
                objective,
                rubric,
                cause_code=exc.cause_code,
                detail=exc.diagnostic.get("detail"),
                diagnostic=exc.diagnostic,
            )
        if semantic.confidence < self.min_confidence:
            return self._objective_only(
                stable_id,
                objective,
                rubric,
                cause_code="low_confidence",
                detail=f"confidence below {self.min_confidence:g}",
                diagnostic={
                    "code": "decision_degraded",
                    "cause_code": "low_confidence",
                    "source": "jev",
                    "stage": "assess",
                    "degraded": True,
                    "detail": f"confidence below {self.min_confidence:g}",
                },
                semantic_score=semantic.score,
                confidence=semantic.confidence,
                # A low-confidence semantic choice must not route a candidate
                # as included/excluded; callers can still inspect the raw
                # semantic fields and reasons for a human review UI.
                decision=DecisionChoice.REVIEW,
                resource_type=semantic.resource_type,
                relation=semantic.relation,
                worth_reading=semantic.worth_reading,
                duplicate=semantic.duplicate,
                duplicate_of=semantic.duplicate_of,
                reasons=semantic.reasons,
            )
        final_score = round(OBJECTIVE_WEIGHT * objective + JEV_SEMANTIC_WEIGHT * semantic.score, 4)
        return DecisionAssessment(
            candidate_id=stable_id,
            objective_score=objective,
            semantic_score=semantic.score,
            final_score=final_score,
            confidence=semantic.confidence,
            decision=semantic.decision,
            resource_type=semantic.resource_type,
            relation=semantic.relation,
            worth_reading=semantic.worth_reading,
            duplicate=semantic.duplicate,
            duplicate_of=semantic.duplicate_of,
            reasons=semantic.reasons,
            rubric_id=rubric.rubric_id,
            rubric_version=rubric.version,
            status=DecisionStatus.OK,
            degraded=False,
            diagnostics=(),
        )

    def assess_many(
        self,
        candidates: Iterable[Mapping[str, object]],
        *,
        intent: ResearchIntent | None = None,
        rubric_id: str = NEWS_RUBRIC_ID,
    ) -> list[DecisionAssessment]:
        return [self.assess(candidate, intent=intent, rubric_id=rubric_id) for candidate in candidates]

    @staticmethod
    def _objective_only(
        candidate_id: str,
        objective: float,
        rubric: Rubric,
        *,
        cause_code: str,
        detail: object,
        diagnostic: Mapping[str, object] | None = None,
        semantic_score: float | None = None,
        confidence: float | None = None,
        decision: DecisionChoice = DecisionChoice.REVIEW,
        resource_type: ResourceType | None = None,
        relation: EvidenceRelation | None = None,
        worth_reading: bool | None = None,
        duplicate: bool | None = None,
        duplicate_of: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> DecisionAssessment:
        item = dict(diagnostic or {})
        item.setdefault("code", "decision_degraded")
        item.setdefault("cause_code", cause_code)
        item.setdefault("source", "jev")
        item.setdefault("stage", "assess")
        item.setdefault("degraded", True)
        item.setdefault("detail", str(detail))
        return DecisionAssessment(
            candidate_id=candidate_id,
            objective_score=objective,
            semantic_score=semantic_score,
            final_score=objective,
            confidence=confidence,
            decision=decision,
            resource_type=resource_type,
            relation=relation,
            worth_reading=worth_reading,
            duplicate=duplicate,
            duplicate_of=duplicate_of,
            reasons=reasons,
            rubric_id=rubric.rubric_id,
            rubric_version=rubric.version,
            status=DecisionStatus.DEGRADED,
            degraded=True,
            diagnostics=(item,),
        )
