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
from ..contracts import JEVAssessment, ResearchIntent
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


def _call_provider(provider: object, payload: Mapping[str, object]) -> object:
    if callable(provider):
        return provider(payload)
    assessor = getattr(provider, "assess", None)
    if callable(assessor):
        return assessor(payload)
    runner = getattr(provider, "run", None)
    if callable(runner):
        prompt = (
            "Return one JSON object only matching the supplied Jev assessment contract. "
            "Do not include prose outside the JSON object.\n"
            + json.dumps(dict(payload), ensure_ascii=False)
        )
        return runner(prompt)
    raise JevError("jev_not_configured", cause_code="not_configured", detail="provider has no callable interface")


class JevAdapter:
    """Call and validate one Jev assessment without knowing its transport."""

    def __init__(self, provider: JevProvider | object | None = None) -> None:
        self.provider = provider

    @property
    def configured(self) -> bool:
        return self.provider is not None

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
            return JEVAssessment.from_dict(parsed)
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
