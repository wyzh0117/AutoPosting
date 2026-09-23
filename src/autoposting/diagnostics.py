"""Small, stable diagnostics shared by source collection boundaries."""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping

_DETAIL_LIMIT = 240
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:authorization|access[_-]?token|api[_-]?key|appsecret|"
    r"secret|password|passwd|pwd|bearer|token|signature|sig|auth)"
    r"(?![A-Za-z0-9])\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:/(?:Users|private|tmp|var|home)/|[A-Za-z]:[\\/])[^\s,;]+")
_SQL_RE = re.compile(r"(?is)\b(?:select|insert|update|delete|alter|create|drop|pragma)\b.*")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_KNOWN_CAUSES = {
    "auth_failed",
    "bad_json",
    "blocked_http",
    "connection_reset",
    "dns_failure",
    "forbidden",
    "http_error",
    "invalid_content",
    "invalid_json",
    "invalid_response",
    "low_confidence",
    "not_configured",
    "not_found",
    "provider_error",
    "rate_limited",
    "reader_internal_error",
    "server_error",
    "timeout",
    "tls_failure",
    "too_large",
    "unknown_error",
    "unreachable",
    "unsupported_content",
}


def _safe_name(value: object, fallback: str) -> str:
    text = str(value or "")
    return text if _SAFE_NAME_RE.fullmatch(text) else fallback


def sanitize_detail(detail: object) -> str:
    """Keep ordinary diagnostics while removing URLs, secrets, paths and SQL."""
    try:
        text = "" if detail is None else str(detail)
    except Exception:
        return "detail_suppressed"
    try:
        text = text[:1000]
        text = _URL_RE.sub("<url redacted>", text)
        text = _CREDENTIAL_RE.sub("credential=<redacted>", text)
        text = _PATH_RE.sub("<path redacted>", text)
        text = _SQL_RE.sub("<sql redacted>", text)
        text = " ".join(text.replace(";", " ").split())
    except Exception:
        return "detail_suppressed"
    return text[:_DETAIL_LIMIT] or "unknown_error"


def cause_code_for(error: object) -> str:
    kind = getattr(error, "kind", None)
    if isinstance(kind, str) and kind in _KNOWN_CAUSES:
        return kind
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "unreachable"
    if isinstance(error, (ValueError, TypeError)):
        return "invalid_response"
    return "unknown_error"


def make_diagnostic(
    source: str,
    stage: str,
    error: object = None,
    *,
    code: str = "source_fetch_failed",
    cause_code: str | None = None,
    detail: object = None,
    degraded: bool = True,
) -> dict[str, object]:
    if detail is None:
        detail = error
    if isinstance(error, BaseException) and detail is error:
        detail = f"{type(error).__name__}: {error}"
    return {
        "code": _safe_name(code, "source_fetch_failed"),
        "cause_code": _safe_name(cause_code or cause_code_for(error), "unknown_error"),
        "degraded": bool(degraded),
        "source": _safe_name(source, "unknown"),
        "stage": _safe_name(stage, "unknown"),
        "detail": sanitize_detail(detail),
    }


def normalize_diagnostic(
    value: object,
    source: str,
    stage: str = "collect",
) -> dict[str, object]:
    if isinstance(value, Mapping) and value.get("code"):
        return make_diagnostic(
            str(value.get("source") or source),
            str(value.get("stage") or stage),
            code=str(value.get("code") or "source_fetch_failed"),
            cause_code=str(value.get("cause_code") or "unknown_error"),
            detail=value.get("detail"),
            degraded=bool(value.get("degraded", True)),
        )
    return make_diagnostic(source, stage, value)


def diagnostic_summary(value: Mapping[str, object]) -> str:
    code = str(value.get("code") or "source_fetch_failed")
    cause = str(value.get("cause_code") or "unknown_error")
    detail = sanitize_detail(value.get("detail"))
    return f"{code}:{cause}: {detail}"


def serialize_diagnostic(value: Mapping[str, object]) -> str:
    normalized = normalize_diagnostic(value, str(value.get("source") or "unknown"))
    return ";".join(
        (
            f"code={normalized['code']}",
            f"cause_code={normalized['cause_code']}",
            f"source={normalized['source']}",
            f"stage={normalized['stage']}",
            f"degraded={1 if normalized['degraded'] else 0}",
            f"detail={sanitize_detail(normalized['detail'])}",
        )
    )


def parse_diagnostic(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or not value.startswith("code="):
        return None
    fields: dict[str, str] = {}
    for chunk in value.split(";"):
        key, separator, item = chunk.partition("=")
        if separator:
            fields[key] = item
    if not fields.get("code") or not fields.get("cause_code"):
        return None
    return {
        "code": _safe_name(fields["code"], "source_fetch_failed"),
        "cause_code": _safe_name(fields["cause_code"], "unknown_error"),
        "degraded": fields.get("degraded") == "1",
        "source": _safe_name(fields.get("source"), "unknown"),
        "stage": _safe_name(fields.get("stage"), "unknown"),
        "detail": sanitize_detail(fields.get("detail")),
    }


def log_diagnostic(
    logger: logging.Logger,
    diagnostic: Mapping[str, object],
    error: BaseException | None = None,
) -> None:
    traceback_text = "Traceback unavailable"
    if error is not None:
        stack = "\n".join(
            sanitize_detail(line)
            for line in "".join(traceback.format_tb(error.__traceback__)).splitlines()
            if line.strip()
        ).strip()
        if stack:
            traceback_text = "Traceback (most recent call last):\n" + stack
    logger.error(
        "diagnostic code=%s cause_code=%s source=%s stage=%s degraded=%s traceback=%s",
        diagnostic.get("code"),
        diagnostic.get("cause_code"),
        diagnostic.get("source"),
        diagnostic.get("stage"),
        diagnostic.get("degraded"),
        traceback_text,
        extra={
            "code": diagnostic.get("code"),
            "cause_code": diagnostic.get("cause_code"),
            "source": diagnostic.get("source"),
            "stage": diagnostic.get("stage"),
            "degraded": diagnostic.get("degraded"),
            "traceback": traceback_text,
        },
    )
