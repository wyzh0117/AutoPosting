"""LLM-backed ResearchIntent/SearchPlan planning with injectable providers."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Protocol

from ..diagnostics import cause_code_for, make_diagnostic
from .contracts import (
    PLANNER_RESULT_VERSION,
    RESEARCH_INTENT_VERSION,
    SEARCH_PLAN_VERSION,
    ContractError,
    PlannerResult,
    ResearchIntent,
    SearchPlan,
)

PLANNER_PROMPT_VERSION = "research-planner-v1"


class PlannerProvider(Protocol):
    def __call__(self, prompt: str) -> object:
        ...


class PlannerError(Exception):
    """Stable, redacted Planner failure."""

    def __init__(self, code: str, reason: str | None = None, *, cause_code: str | None = None, detail: object = None) -> None:
        self.code = code
        self.reason = reason or code
        self.cause_code = cause_code or code
        self.diagnostic = make_diagnostic(
            "planner",
            "plan",
            code="planner_failed",
            cause_code=self.cause_code,
            detail=detail if detail is not None else self.reason,
        )
        super().__init__(self.code)


def build_planner_prompt(query: str, *, source_event_ids: tuple[str, ...] = ()) -> str:
    """Build a bounded prompt; the caller still owns the source-event state."""
    if not isinstance(query, str) or not query.strip() or len(query.strip()) > 200:
        raise ValueError("query must be 1-200 characters")
    event_ids = json.dumps(list(source_event_ids), ensure_ascii=False)
    skeleton = json.dumps(
        {
            "schema_version": PLANNER_RESULT_VERSION,
            "intent": {
                "schema_version": RESEARCH_INTENT_VERSION,
                "goal": "one sentence",
                "concepts": ["keyword", "keyword"],
                "search_repo": True,
                "search_skill": False,
                "search_web": True,
                "preferred_signals": ["signal"],
                "source_event_ids": list(source_event_ids),
            },
            "plan": {
                "schema_version": SEARCH_PLAN_VERSION,
                "query": "one search query",
                "adapters": ["repo_search", "web_search"],
                "max_candidates": 50,
            },
        },
        ensure_ascii=False,
    )
    return (
        f"You are AutoPosting Query Planner, contract {PLANNER_PROMPT_VERSION}.\n"
        "Return one JSON object only. Do not search, judge, or write prose.\n"
        "Copy this exact shape, including every schema_version and every key:\n"
        f"{skeleton}\n"
        "Types are strict: search_repo, search_skill and search_web are true/false "
        "flags for whether that lane is searched, NOT lists of terms. concepts, "
        "preferred_signals, adapters and source_event_ids are arrays of strings. "
        "max_candidates is an integer between 1 and 500. Put search terms in "
        "concepts and in plan.query, never inside the three flags. "
        "Set at least one of the three flags to true.\n"
        f"Selected source_event_ids are authoritative: {event_ids}\n"
        f"User request: <request>{query.strip()}</request>"
    )


def _parse_json_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise PlannerError("planner_invalid_response", cause_code="invalid_response", detail="provider output is not JSON")
    text = value.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PlannerError("planner_invalid_response", cause_code="invalid_json", detail=exc) from None
    if not isinstance(parsed, Mapping):
        raise PlannerError("planner_invalid_response", cause_code="invalid_response", detail="provider output must be an object")
    return parsed


def _call_provider(provider: object, prompt: str) -> object:
    if callable(provider):
        return provider(prompt)
    runner = getattr(provider, "run", None)
    if callable(runner):
        return runner(prompt)
    raise PlannerError("planner_invalid_provider", cause_code="provider_error", detail="provider has no callable interface")


_SEARCH_LANES = ("search_repo", "search_skill", "search_web")
_LIST_FIELDS = ("concepts", "preferred_signals", "source_event_ids", "adapters")


def _lane(value: object) -> bool:
    """A search lane arrives as a bool, or as a term list meaning "use it"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() for item in value)
    if value is None:
        return False
    return bool(value)


def _as_list(value: object) -> object:
    """A single string where an array of strings is required."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


def _as_int(value: object) -> object:
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return value


def _normalize_provider_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Make one LLM reply satisfy the strict, storage-pinned contract.

    ``from_dict`` stays strict because it also guards persisted payloads.  The
    LLM boundary is where a reply is coerced into that contract.  Without this
    step the Planner could never accept a real reply: the prompt omitted the
    required ``schema_version``, and the three lane keys read like term lists
    while the contract types them as booleans, so every call degraded while
    ``configured`` still reported True.
    """
    data = dict(payload)
    data.setdefault("schema_version", PLANNER_RESULT_VERSION)
    intent = data.get("intent")
    if isinstance(intent, Mapping):
        intent = dict(intent)
        intent.setdefault("schema_version", RESEARCH_INTENT_VERSION)
        for lane in _SEARCH_LANES:
            if lane in intent:
                intent[lane] = _lane(intent[lane])
        for field in ("concepts", "preferred_signals", "source_event_ids"):
            if field in intent:
                intent[field] = _as_list(intent[field])
        data["intent"] = intent
    plan = data.get("plan")
    if isinstance(plan, Mapping):
        plan = dict(plan)
        plan.setdefault("schema_version", SEARCH_PLAN_VERSION)
        if "adapters" in plan:
            plan["adapters"] = _as_list(plan["adapters"])
        if "max_candidates" in plan:
            plan["max_candidates"] = _as_int(plan["max_candidates"])
        data["plan"] = plan
    return data


def _validate_result(payload: Mapping[str, object]) -> PlannerResult:
    try:
        return PlannerResult.from_dict(payload)
    except ContractError as exc:
        raise PlannerError("planner_invalid_response", cause_code="invalid_response", detail=exc.code) from None


class LLMPlanner:
    """Translate natural language to a strict, versioned PlannerResult."""

    def __init__(self, provider: PlannerProvider | object | None = None) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return callable(self.provider) or callable(getattr(self.provider, "run", None))

    def plan(
        self,
        query: str,
        *,
        source_event_ids: tuple[str, ...] | list[str] = (),
    ) -> PlannerResult:
        prompt = build_planner_prompt(query, source_event_ids=tuple(source_event_ids))
        if self.provider is None:
            raise PlannerError("planner_not_configured", cause_code="not_configured")
        try:
            payload = _parse_json_object(_call_provider(self.provider, prompt))
        except PlannerError:
            raise
        except TimeoutError as exc:
            raise PlannerError("planner_timeout", cause_code="timeout", detail=exc) from None
        except Exception as exc:
            cause = getattr(exc, "cause_code", None) or getattr(exc, "code", None) or cause_code_for(exc)
            if str(cause) in {"llm_timeout", "timeout"}:
                cause = "timeout"
            elif str(cause) in {"llm_invalid_output", "invalid_response"}:
                cause = "invalid_response"
            raise PlannerError("planner_failed", cause_code=str(cause), detail=exc) from None
        result = _validate_result(_normalize_provider_payload(payload))
        selected = tuple(source_event_ids)
        if selected and result.intent.source_event_ids != selected:
            result = PlannerResult(result.intent.with_source_event_ids(selected), result.plan)
        return result


def build_fallback_plan(query: str, *, source_event_ids: tuple[str, ...] | list[str] = ()) -> PlannerResult:
    """Deterministic lexical plan for an unavailable Planner.

    This is an explicit, caller-visible degraded path; it never pretends to be
    an LLM interpretation.
    """
    normalized = " ".join(query.split()) if isinstance(query, str) else ""
    if not normalized or len(normalized) > 200:
        raise ValueError("query must be 1-200 characters")
    concepts = tuple(dict.fromkeys(re.findall(r"[\w\u4e00-\u9fff-]+", normalized)))[:12]
    if not concepts:
        concepts = (normalized,)
    intent = ResearchIntent(
        goal=normalized,
        concepts=concepts,
        search_repo=True,
        search_skill=True,
        search_web=True,
        preferred_signals=(),
        source_event_ids=tuple(source_event_ids),
    )
    return PlannerResult(
        intent=intent,
        plan=SearchPlan(
            query=normalized,
            adapters=("github", "skills", "web"),
            max_candidates=50,
        ),
    )
