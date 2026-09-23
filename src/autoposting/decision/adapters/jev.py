"""Strict, injectable Jev adapter.

The adapter deliberately accepts a provider callable rather than credentials.
The existing LLM service/keychain remains the owner of endpoint credentials;
callers can pass a bound ``LLMService.run`` or another approved provider.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Protocol

from ...diagnostics import cause_code_for, make_diagnostic
from ..contracts import JEV_ASSESSMENT_VERSION, JEVAssessment, ResearchIntent
from ..rubrics import Rubric


class JevProvider(Protocol):
    def __call__(self, payload: Mapping[str, object]) -> object:
        ...


class JevError(Exception):
    """Stable, redacted Jev failure returned to DecisionService."""

    def __init__(self, code: str, reason: str | None = None, *, cause_code: str | None = None, detail: object = None) -> None:
        self.code = code
        self.reason = reason or code
        self.cause_code = cause_code or code
        self.diagnostic = make_diagnostic(
            "jev",
            "assess",
            code="decision_failed",
            cause_code=self.cause_code,
            detail=detail if detail is not None else self.reason,
        )
        super().__init__(self.code)


def _parse_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise JevError("jev_invalid_response", cause_code="invalid_response", detail="provider output is not JSON")
    text = value.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise JevError("jev_invalid_response", cause_code="invalid_json", detail=exc) from None
    if not isinstance(parsed, Mapping):
        raise JevError("jev_invalid_response", cause_code="invalid_response", detail="provider output must be an object")
    return parsed


def build_jev_prompt(payload: Mapping[str, object]) -> str:
    """Describe the *response* contract, then hand over the request payload.

    Before 3.2.2 this prompt asked for "the supplied Jev assessment contract" and
    appended only the request body (jev-request-v1).  The response schema was
    never stated, so every provider reply failed JEVAssessment validation and
    Jev reported invalid_response on every call.
    """
    skeleton = json.dumps(
        {
            "schema_version": JEV_ASSESSMENT_VERSION,
            "score": 0,
            "confidence": 0,
            "decision": "review",
            "reasons": ["short reason"],
            "resource_type": None,
            "relation": None,
            "worth_reading": None,
            "duplicate": None,
            "duplicate_of": None,
        },
        ensure_ascii=False,
    )
    return (
        "You are AutoPosting Jev, the semantic evidence judge. Assess the given "
        "candidate against the given rubric and return one JSON object only. "
        "Do not search, and do not write prose outside the JSON object.\n"
        "Copy this exact shape, including schema_version and every key:\n"
        f"{skeleton}\n"
        "Types are strict: score is a number from 0 to 100; confidence is a number "
        "from 0 to 1; reasons is an array of short strings; decision is exactly one "
        'of "include", "exclude", "review". resource_type is null or one of '
        '"repo", "skill", "tool", "website", "other". relation is null or one of '
        '"same_event", "background", "follow_up", "industry_reaction", '
        '"user_experience", "competitor", "criticism", "related_project", '
        '"topic_only", "irrelevant". worth_reading and duplicate are null or '
        "true/false. duplicate_of is null or a candidate id.\n"
        "Request:\n"
        + json.dumps(dict(payload), ensure_ascii=False)
    )


def _as_number(value: object) -> object:
    if isinstance(value, str):
        text = value.strip().rstrip("%")
        try:
            return float(text)
        except ValueError:
            return value
    return value


def _as_list(value: object) -> object:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


def _normalize_provider_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Coerce one LLM reply into the strict assessment contract.

    ``from_dict`` stays strict because it also guards persisted payloads; this is
    the LLM boundary.  A 0-100 score or a 0-1 confidence often arrives as a
    string, and a single reason often arrives as a bare string.
    """
    data = dict(payload)
    data.setdefault("schema_version", JEV_ASSESSMENT_VERSION)
    for field in ("score", "confidence"):
        if field in data:
            data[field] = _as_number(data[field])
    if "reasons" in data:
        data["reasons"] = _as_list(data["reasons"])
    for field in ("resource_type", "relation", "duplicate_of"):
        if data.get(field) in ("", "null", "none", "None"):
            data[field] = None
    return data


def _call_provider(provider: object, payload: Mapping[str, object]) -> object:
    if callable(provider):
        return provider(payload)
    assessor = getattr(provider, "assess", None)
    if callable(assessor):
        return assessor(payload)
    runner = getattr(provider, "run", None)
    if callable(runner):
        prompt = build_jev_prompt(payload)
        return runner(prompt)
    raise JevError("jev_invalid_provider", cause_code="provider_error", detail="provider has no callable interface")


class JevAdapter:
    """Call and validate one Jev assessment without knowing its transport."""

    def __init__(self, provider: JevProvider | object | None = None) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return (
            callable(self.provider)
            or callable(getattr(self.provider, "assess", None))
            or callable(getattr(self.provider, "run", None))
        )

    def assess(
        self,
        candidate: Mapping[str, object],
        *,
        candidate_id: str,
        rubric: Rubric,
        intent: ResearchIntent | None = None,
    ) -> JEVAssessment:
        if not isinstance(candidate, Mapping):
            raise JevError("jev_invalid_request", cause_code="invalid_response", detail="candidate must be an object")
        if self.provider is None:
            raise JevError("jev_not_configured", cause_code="not_configured")
        payload: dict[str, object] = {
            "schema_version": "jev-request-v1",
            "candidate_id": candidate_id,
            "rubric": rubric.to_dict(),
            "candidate": dict(candidate),
        }
        if intent is not None:
            payload["intent"] = intent.to_dict()
        try:
            parsed = _parse_object(_call_provider(self.provider, payload))
            return JEVAssessment.from_dict(_normalize_provider_payload(parsed))
        except JevError:
            raise
        except (TypeError, ValueError) as exc:
            raise JevError("jev_invalid_response", cause_code="invalid_response", detail=exc) from None
        except TimeoutError as exc:
            raise JevError("jev_timeout", cause_code="timeout", detail=exc) from None
        except Exception as exc:
            cause = getattr(exc, "cause_code", None) or getattr(exc, "code", None)
            if not cause:
                cause = cause_code_for(exc)
            if str(cause) in {"llm_timeout", "timeout"}:
                cause = "timeout"
            elif str(cause) in {"llm_invalid_output", "invalid_response"}:
                cause = "invalid_response"
            raise JevError("jev_failed", cause_code=str(cause), detail=exc) from None
