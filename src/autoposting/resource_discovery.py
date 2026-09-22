"""Unified resource discovery contracts and orchestration.

The module is deliberately independent of the web and database layers.  Existing
GitHub Weekly, Skill Finder, and web search implementations are supplied through
small adapters; this keeps the large legacy services as compatibility surfaces
while giving new callers one versioned ResourceSet payload.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import inspect
import math
from pathlib import PurePosixPath
import uuid
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from .decision import (
    DecisionAssessment,
    DecisionChoice,
    DecisionService,
    PlannerResult,
    ResourceType,
    build_fallback_plan,
)
from .decision.planner import PlannerError
from .decision.rubrics import RESOURCE_RUBRIC_ID
from .diagnostics import make_diagnostic, normalize_diagnostic
from .skill_links import parse_skill_links


RESOURCE_CANDIDATE_VERSION = "resource-candidate-v1"
RESOURCE_ASSESSMENT_VERSION = "resource-assessment-v1"
RESOURCE_SET_VERSION = "resource-set-v1"
GITHUB_WEEKLY_PRESET = "github_weekly"
GITHUB_WEEKLY_QUERY = "本周 AI 新项目"

_MAX_QUERY_CHARS = 200
_MAX_TEXT_CHARS = 20_000
_MAX_TITLE_CHARS = 1_000
_MAX_URL_CHARS = 4_000
_MAX_ID_CHARS = 400
_MAX_METADATA_ITEMS = 80
_SECRET_WORDS = {"api_key", "apikey", "token", "secret", "password", "authorization", "cookie"}


class ResourceStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    EMPTY = "empty"


class ResourceDiscoveryError(ValueError):
    """Stable error for invalid contracts or unavailable persistence."""

    def __init__(self, code: str, detail: object = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


class ResourceAdapter(Protocol):
    """One bounded search source.

    Implementations may return a list of mappings, a mapping with ``results`` /
    ``items``, or a response object exposing those fields and diagnostics.
    """

    def search(self, query: str, *, limit: int) -> object:
        ...


class ResourceSetRepository(Protocol):
    """Persistence seam for the database owner.

    Repositories store ``ResourceSet.to_dict()`` in the project's existing
    versioned envelope.  This module never imports Database or writes SQL.
    """

    def save_resource_set(self, resource_set: "ResourceSet | Mapping[str, object]") -> None:
        ...

    def get_resource_set(self, set_id: str) -> "Mapping[str, object] | None":
        ...

    def list_resource_sets(self, limit: int = 20) -> list["Mapping[str, object]"]:  # pragma: no cover - protocol
        ...

    def delete_resource_set(self, set_id: str) -> bool:  # pragma: no cover - protocol
        ...


def _text(value: object, name: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        if not required and value is None:
            return ""
        raise ResourceDiscoveryError(f"{name}_must_be_string")
    normalized = " ".join(value.split())
    if required and not normalized:
        raise ResourceDiscoveryError(f"{name}_required")
    if len(normalized) > limit:
        raise ResourceDiscoveryError(f"{name}_too_long")
    return normalized


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ResourceDiscoveryError(f"{name}_must_be_string_list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _text(item, name, limit=_MAX_ID_CHARS)
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return tuple(result)


def _schema(value: Mapping[str, object], expected: str, name: str) -> None:
    if value.get("schema_version") != expected:
        raise ResourceDiscoveryError(f"{name}_unknown_schema_version")


def _unknown(value: Mapping[str, object], allowed: set[str], name: str) -> None:
    unknown = sorted(str(key) for key in value if str(key) not in allowed)
    if unknown:
        raise ResourceDiscoveryError(f"{name}_unknown_field_{unknown[0]}")


def _url(value: object, name: str = "url") -> str:
    text = _text(value, name, limit=_MAX_URL_CHARS)
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ResourceDiscoveryError(f"{name}_must_be_http_url")
    if parsed.username or parsed.password:
        raise ResourceDiscoveryError(f"{name}_credentials_not_allowed")
    # Search result query strings are not part of resource identity and can
    # contain access tokens.  Keep only the safe canonical page URL.
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        raise ResourceDiscoveryError(f"{name}_invalid_port") from None
    netloc = host.lower() + (f":{port}" if port is not None else "")
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _safe_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return not any(word in normalized for word in _SECRET_WORDS)


def _json_safe(value: object, *, depth: int = 0) -> object:
    """Bound metadata and remove secret-shaped keys before persistence/output."""
    if depth > 4:
        return str(value)[:500]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for key, item in list(value.items())[:_MAX_METADATA_ITEMS]:
            if not _safe_key(key):
                continue
            output[str(key)] = _json_safe(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:_MAX_METADATA_ITEMS]]
    return str(value)[:500]


def _canonical_adapter(name: object) -> str:
    value = str(name or "").strip().lower().replace("-", "_")
    aliases = {
        "repo": "github",
        "repos": "github",
        "github": "github",
        "github_weekly": "github",
        "skill": "skills",
        "skills": "skills",
        "skills.sh": "skills",
        "skill_sh": "skills",
        "web": "web",
        "website": "web",
    }
    return aliases.get(value, value)


def _kind(value: object, source: str, raw: Mapping[str, object]) -> ResourceType:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "repo": ResourceType.REPO,
        "repository": ResourceType.REPO,
        "github": ResourceType.REPO,
        "skill": ResourceType.SKILL,
        "skills": ResourceType.SKILL,
        "tool": ResourceType.TOOL,
        "website": ResourceType.WEBSITE,
        "web": ResourceType.WEBSITE,
    }
    if text in aliases:
        return aliases[text]
    if raw.get("skill_path") or raw.get("skill_excerpt") or "skill" in source.lower():
        return ResourceType.SKILL
    if "github" in source.lower() or raw.get("full_name"):
        return ResourceType.REPO
    return ResourceType.WEBSITE


_SAFE_METADATA_FIELDS = {
    "full_name",
    "owner",
    "source_id",
    "source_repo",
    "skill_path",
    "raw_url",
    "skill_excerpt",
    "readme_excerpt",
    "page_excerpt",
    "docs_exists",
    "stage_id",
    "stage_name",
    "topics",
    "icon_url",
    "language",
    "stars",
    "stargazers_count",
    "stars_week",
    "forks",
    "forks_count",
    "installs",
    "fresh",
    "created_at",
    "updated_at",
    "pushed_at",
    "released_at",
    "published_at",
    "activity_score",
    "release_freshness",
    "docs_exists",
    "has_readme",
    "manual",
    "rank",
    "topics",
    "category",
    "version",
    "license",
}


def _metadata(raw: Mapping[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    nested = raw.get("metadata")
    if isinstance(nested, Mapping):
        metadata.update(nested)
    for key in _SAFE_METADATA_FIELDS:
        if key in raw:
            metadata[key] = raw[key]
    return dict(_json_safe(metadata)) if isinstance(_json_safe(metadata), dict) else {}


def _skill_path(metadata: Mapping[str, object]) -> str:
    value = str(metadata.get("skill_path") or "").strip().strip("/")
    if not value:
        return ""
    # Avoid path traversal in a value later used by the package renderer.
    parts = [part for part in PurePosixPath(value).parts if part not in {".", ".."}]
    return "/".join(parts)


def _resource_identity(kind: ResourceType, url: str, metadata: Mapping[str, object]) -> str:
    full_name = str(metadata.get("full_name") or metadata.get("source_id") or "").strip().lower()
    path = _skill_path(metadata).lower()
    if full_name and kind == ResourceType.SKILL:
        return f"{kind.value}:{full_name}:{path}"
    if full_name and kind == ResourceType.REPO:
        return f"{kind.value}:{full_name}"
    return f"{kind.value}:{url.lower()}"


def _resource_id(kind: ResourceType, url: str, metadata: Mapping[str, object], explicit: object) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return _text(explicit, "resource_id", limit=_MAX_ID_CHARS)
    identity = _resource_identity(kind, url, metadata)
    if len(identity) <= _MAX_ID_CHARS:
        return identity
    return f"{kind.value}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def _number(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def objective_resource_score(candidate: Mapping[str, object], *, now: datetime | None = None) -> float:
    """Compute bounded objective signals shared by every resource adapter."""
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), Mapping) else candidate
    stars = max(0.0, _number(metadata.get("stars") or metadata.get("stargazers_count")))
    forks = max(0.0, _number(metadata.get("forks") or metadata.get("forks_count")))
    installs = max(0.0, _number(metadata.get("installs")))
    popularity = min(32.0, math.log10(max(stars, installs, 0.0) + 1.0) * 8.0)
    fork_signal = min(10.0, math.log10(forks + 1.0) * 5.0)
    activity = _number(metadata.get("activity_score"), default=-1.0)
    if activity < 0:
        activity = 20.0 if metadata.get("fresh") else 0.0
    activity_signal = min(20.0, max(0.0, activity / 100.0 * 20.0))
    latest = _date(metadata.get("updated_at") or metadata.get("pushed_at") or metadata.get("released_at"))
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    freshness = 0.0
    if latest is not None:
        age = max(0.0, (moment - latest.astimezone(timezone.utc)).total_seconds() / 86400.0)
        freshness = max(0.0, 20.0 - min(age, 365.0) / 365.0 * 20.0)
    elif metadata.get("fresh"):
        freshness = 12.0
    docs = 10.0 if metadata.get("docs_exists") or metadata.get("has_readme") else 0.0
    if metadata.get("skill_path") or metadata.get("skill_excerpt"):
        docs = max(docs, 10.0)
    source = str(candidate.get("source") or "").lower()
    source_signal = 8.0 if source in {"github", "skills", "skills.sh", "pasted"} else 4.0
    explicit = _number(candidate.get("score"), default=-1.0)
    total = popularity + fork_signal + activity_signal + freshness + docs + source_signal
    if explicit >= 0:
        total = max(total, min(100.0, explicit))
    return round(min(100.0, max(0.0, total)), 4)


def _decode_decision(value: object) -> DecisionAssessment:
    if not isinstance(value, Mapping):
        raise ResourceDiscoveryError("decision_assessment_must_be_object")
    _schema(value, "decision-assessment-v1", "decision_assessment")
    allowed = {
        "schema_version",
        "candidate_id",
        "objective_score",
        "semantic_score",
        "final_score",
        "confidence",
        "decision",
        "resource_type",
        "relation",
        "worth_reading",
        "duplicate",
        "duplicate_of",
        "reasons",
        "rubric_id",
        "rubric_version",
        "status",
        "degraded",
        "diagnostics",
    }
    _unknown(value, allowed, "decision_assessment")
    diagnostics = value.get("diagnostics", ())
    if not isinstance(diagnostics, (list, tuple)):
        raise ResourceDiscoveryError("decision_diagnostics_must_be_list")
    return DecisionAssessment(
        candidate_id=str(value.get("candidate_id") or ""),
        objective_score=value.get("objective_score", 0),
        semantic_score=value.get("semantic_score"),
        final_score=value.get("final_score", value.get("objective_score", 0)),
        confidence=value.get("confidence"),
        decision=value.get("decision", DecisionChoice.REVIEW.value),
        resource_type=value.get("resource_type"),
        relation=value.get("relation"),
        worth_reading=value.get("worth_reading"),
        duplicate=value.get("duplicate"),
        duplicate_of=value.get("duplicate_of"),
        reasons=tuple(value.get("reasons") or ()),
        rubric_id=str(value.get("rubric_id") or RESOURCE_RUBRIC_ID),
        rubric_version=str(value.get("rubric_version") or "1"),
        status=value.get("status", "degraded"),
        degraded=bool(value.get("degraded", False)),
        diagnostics=tuple(item for item in diagnostics if isinstance(item, Mapping)),
    )


@dataclass(frozen=True)
class ResourceAssessment:
    """The two Jev/DecisionService judgments for one resource."""

    resource_id: str
    rounds: tuple[DecisionAssessment, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_id", _text(self.resource_id, "resource_id", limit=_MAX_ID_CHARS))
        if len(self.rounds) != 2:
            raise ResourceDiscoveryError("resource_assessment_requires_two_rounds")
        for item in self.rounds:
            if not isinstance(item, DecisionAssessment):
                raise ResourceDiscoveryError("resource_assessment_round_invalid")

    @property
    def round1(self) -> DecisionAssessment:
        return self.rounds[0]

    @property
    def round2(self) -> DecisionAssessment:
        return self.rounds[1]

    @property
    def final(self) -> DecisionAssessment:
        return self.round2

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESOURCE_ASSESSMENT_VERSION,
            "resource_id": self.resource_id,
            "round1": self.round1.to_dict(),
            "round2": self.round2.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ResourceAssessment":
        if not isinstance(value, Mapping):
            raise ResourceDiscoveryError("resource_assessment_must_be_object")
        _schema(value, RESOURCE_ASSESSMENT_VERSION, "resource_assessment")
        _unknown(value, {"schema_version", "resource_id", "round1", "round2"}, "resource_assessment")
        return cls(
            resource_id=str(value.get("resource_id") or ""),
            rounds=(_decode_decision(value.get("round1")), _decode_decision(value.get("round2"))),
        )


@dataclass(frozen=True)
class ResourceCandidate:
    """One normalized repo, skill, tool, or website candidate."""

    resource_id: str
    kind: ResourceType
    name: str
    url: str
    source: str
    description: str = ""
    metadata: Mapping[str, object] = None  # type: ignore[assignment]
    objective_score: float = 0.0
    assessment: ResourceAssessment | None = None
    selected: bool = False
    duplicate_of: str | None = None
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_id", _text(self.resource_id, "resource_id", limit=_MAX_ID_CHARS))
        if not isinstance(self.kind, ResourceType):
            object.__setattr__(self, "kind", ResourceType(self.kind))
        object.__setattr__(self, "name", _text(self.name, "name", limit=_MAX_TITLE_CHARS))
        object.__setattr__(self, "url", _url(self.url))
        object.__setattr__(self, "source", _text(self.source, "source", limit=120))
        object.__setattr__(self, "description", _text(self.description, "description", limit=_MAX_TEXT_CHARS, required=False))
        normalized_metadata = _json_safe(self.metadata or {})
        object.__setattr__(self, "metadata", normalized_metadata if isinstance(normalized_metadata, Mapping) else {})
        score = _number(self.objective_score, default=-1.0)
        if score < 0 or score > 100:
            raise ResourceDiscoveryError("objective_score_out_of_range")
        object.__setattr__(self, "objective_score", float(score))
        if not isinstance(self.selected, bool):
            raise ResourceDiscoveryError("selected_must_be_boolean")
        if self.duplicate_of is not None:
            object.__setattr__(self, "duplicate_of", _text(self.duplicate_of, "duplicate_of", limit=_MAX_ID_CHARS))
        object.__setattr__(self, "source_ids", _string_list(self.source_ids, "source_ids"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, adapter: str = "") -> "ResourceCandidate":
        source = str(value.get("source") or adapter or "unknown").strip()
        kind = _kind(value.get("kind") or value.get("resource_type") or value.get("type"), source, value)
        metadata = _metadata(value)
        full_name = str(metadata.get("full_name") or value.get("full_name") or "").strip()
        raw_url = value.get("url") or value.get("html_url") or value.get("canonical_url")
        if not raw_url and full_name and (kind == ResourceType.REPO or "github" in source.lower()):
            raw_url = f"https://github.com/{full_name}"
        canonical_url = _url(raw_url)
        if source.lower() in {"manual", "paste", "pasted"} or value.get("manual"):
            source = "pasted"
        name = str(value.get("name") or value.get("title") or full_name.rsplit("/", 1)[-1] or canonical_url)
        description = str(value.get("description") or value.get("snippet") or value.get("skill_excerpt") or value.get("does") or "")
        resource_id = _resource_id(kind, canonical_url, metadata, value.get("resource_id") or value.get("id"))
        source_ids = value.get("source_ids") if isinstance(value.get("source_ids"), (list, tuple)) else ()
        if value.get("source_id"):
            source_ids = tuple(source_ids) + (str(value["source_id"]),)
        return cls(
            resource_id=resource_id,
            kind=kind,
            name=name,
            url=canonical_url,
            source=source,
            description=description,
            metadata=metadata,
            objective_score=0.0,
            source_ids=tuple(str(item) for item in source_ids),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ResourceCandidate":
        if not isinstance(value, Mapping):
            raise ResourceDiscoveryError("resource_candidate_must_be_object")
        _schema(value, RESOURCE_CANDIDATE_VERSION, "resource_candidate")
        allowed = {
            "schema_version",
            "resource_id",
            "kind",
            "name",
            "url",
            "source",
            "description",
            "metadata",
            "objective_score",
            "assessment",
            "selected",
            "duplicate_of",
            "source_ids",
        }
        _unknown(value, allowed, "resource_candidate")
        assessment = value.get("assessment")
        return cls(
            resource_id=str(value.get("resource_id") or ""),
            kind=value.get("kind", ResourceType.OTHER.value),
            name=str(value.get("name") or ""),
            url=value.get("url", ""),
            source=str(value.get("source") or ""),
            description=str(value.get("description") or ""),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {},
            objective_score=value.get("objective_score", 0),
            assessment=ResourceAssessment.from_dict(assessment) if assessment is not None else None,
            selected=bool(value.get("selected", False)),
            duplicate_of=value.get("duplicate_of"),
            source_ids=tuple(value.get("source_ids") or ()),
        )

    @property
    def final_assessment(self) -> DecisionAssessment | None:
        return self.assessment.final if self.assessment else None

    @property
    def identity_key(self) -> str:
        return _resource_identity(self.kind, self.url, self.metadata)

    def with_assessment(self, assessment: ResourceAssessment, *, selected: bool) -> "ResourceCandidate":
        final = assessment.final
        duplicate_of = final.duplicate_of
        return replace(self, assessment=assessment, selected=selected, duplicate_of=duplicate_of)

    def merge(self, other: "ResourceCandidate") -> "ResourceCandidate":
        metadata = dict(self.metadata)
        for key, value in other.metadata.items():
            if key not in metadata or metadata[key] in (None, "", [], {}):
                metadata[key] = value
        sources = list(metadata.get("sources") or [])
        for source in (self.source, other.source):
            if source and source not in sources:
                sources.append(source)
        if sources:
            metadata["sources"] = sources
        ids = tuple(dict.fromkeys((*self.source_ids, *other.source_ids)))
        return replace(
            self,
            description=self.description if len(self.description) >= len(other.description) else other.description,
            metadata=metadata,
            source_ids=ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESOURCE_CANDIDATE_VERSION,
            "resource_id": self.resource_id,
            "kind": self.kind.value,
            "name": self.name,
            "url": self.url,
            "source": self.source,
            "description": self.description,
            "metadata": dict(_json_safe(self.metadata)),
            "objective_score": self.objective_score,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "selected": self.selected,
            "duplicate_of": self.duplicate_of,
            "source_ids": list(self.source_ids),
        }

    def legacy_payload(self, *, rank: int = 0) -> dict[str, object]:
        """Keep existing poster/package services usable during UI migration."""
        payload = dict(self.metadata)
        payload.update(
            {
                "rank": rank,
                "name": self.name,
                "url": self.url,
                "source": self.source,
                "kind": self.kind.value,
                "description": self.description,
                "resource_id": self.resource_id,
            }
        )
        return payload


@dataclass(frozen=True)
class ResourceSet:
    """Versioned, persistence-ready result of one discovery run."""

    set_id: str
    query: str
    plan: PlannerResult
    candidates: tuple[ResourceCandidate, ...]
    adapter_status: Mapping[str, Mapping[str, object]]
    status: ResourceStatus
    degraded: bool
    diagnostics: tuple[Mapping[str, object], ...]
    created_at: str
    preset: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "set_id", _text(self.set_id, "set_id", limit=_MAX_ID_CHARS))
        object.__setattr__(self, "query", _text(self.query, "query", limit=_MAX_QUERY_CHARS))
        if not isinstance(self.plan, PlannerResult):
            raise ResourceDiscoveryError("plan_invalid")
        normalized_candidates = tuple(item if isinstance(item, ResourceCandidate) else ResourceCandidate.from_dict(item) for item in self.candidates)
        object.__setattr__(self, "candidates", normalized_candidates)
        if not isinstance(self.status, ResourceStatus):
            object.__setattr__(self, "status", ResourceStatus(self.status))
        if not isinstance(self.degraded, bool):
            raise ResourceDiscoveryError("degraded_must_be_boolean")
        normalized_status: dict[str, Mapping[str, object]] = {}
        for name, value in (self.adapter_status or {}).items():
            if not isinstance(value, Mapping):
                raise ResourceDiscoveryError("adapter_status_must_be_object")
            normalized_status[str(name)] = dict(_json_safe(value))
        object.__setattr__(self, "adapter_status", normalized_status)
        normalized_diagnostics = tuple(dict(item) for item in self.diagnostics if isinstance(item, Mapping))
        object.__setattr__(self, "diagnostics", normalized_diagnostics)
        object.__setattr__(self, "created_at", _text(self.created_at, "created_at", limit=80))
        object.__setattr__(self, "preset", _text(self.preset, "preset", limit=120, required=False))

    @property
    def resource_set_id(self) -> str:
        return self.set_id

    @property
    def counts(self) -> dict[str, int]:
        counts = {kind.value: 0 for kind in ResourceType}
        for candidate in self.candidates:
            counts[candidate.kind.value] += 1
        return counts

    @property
    def selected_candidates(self) -> tuple[ResourceCandidate, ...]:
        return tuple(item for item in self.candidates if item.selected)

    def legacy_records(self, *, kind: ResourceType | str | None = None) -> list[dict[str, object]]:
        requested = ResourceType(kind) if kind is not None and not isinstance(kind, ResourceType) else kind
        rows = [item for item in self.candidates if requested is None or item.kind == requested]
        return [item.legacy_payload(rank=index) for index, item in enumerate(rows, start=1)]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": RESOURCE_SET_VERSION,
            "set_id": self.set_id,
            "query": self.query,
            "plan": self.plan.to_dict(),
            "preset": self.preset,
            "candidates": [item.to_dict() for item in self.candidates],
            "adapter_status": dict(_json_safe(self.adapter_status)),
            "status": self.status.value,
            "degraded": self.degraded,
            "diagnostics": [dict(_json_safe(item)) for item in self.diagnostics],
            "created_at": self.created_at,
            "counts": self.counts,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ResourceSet":
        if not isinstance(value, Mapping):
            raise ResourceDiscoveryError("resource_set_must_be_object")
        _schema(value, RESOURCE_SET_VERSION, "resource_set")
        allowed = {
            "schema_version",
            "set_id",
            "query",
            "plan",
            "preset",
            "candidates",
            "adapter_status",
            "status",
            "degraded",
            "diagnostics",
            "created_at",
            "counts",
        }
        _unknown(value, allowed, "resource_set")
        candidates = value.get("candidates")
        if not isinstance(candidates, (list, tuple)):
            raise ResourceDiscoveryError("resource_candidates_must_be_list")
        diagnostics = value.get("diagnostics", ())
        if not isinstance(diagnostics, (list, tuple)):
            raise ResourceDiscoveryError("resource_diagnostics_must_be_list")
        adapter_status = value.get("adapter_status", {})
        if not isinstance(adapter_status, Mapping):
            raise ResourceDiscoveryError("adapter_status_must_be_object")
        return cls(
            set_id=str(value.get("set_id") or ""),
            query=str(value.get("query") or ""),
            plan=PlannerResult.from_dict(value.get("plan")),
            preset=str(value.get("preset") or ""),
            candidates=tuple(ResourceCandidate.from_dict(item) for item in candidates),
            adapter_status=adapter_status,
            status=value.get("status", ResourceStatus.EMPTY.value),
            degraded=value.get("degraded", False),
            diagnostics=tuple(item for item in diagnostics if isinstance(item, Mapping)),
            created_at=str(value.get("created_at") or ""),
        )


def _response_rows(response: object) -> tuple[list[Mapping[str, object]], bool, list[Mapping[str, object]], str | None]:
    degraded = False
    raw_diagnostics: object = ()
    cause_code: str | None = None
    if response is None:
        raise ResourceDiscoveryError("adapter_response_missing")
    if isinstance(response, Mapping):
        rows = response.get("results", response.get("candidates", response.get("items", ())))
        degraded = bool(response.get("degraded", False))
        raw_diagnostics = response.get("diagnostics", ())
        cause_code = str(response.get("cause_code") or "") or None
    elif isinstance(response, (list, tuple)):
        rows = response
    else:
        rows = getattr(response, "results", None)
        if rows is None:
            rows = getattr(response, "candidates", None)
        if rows is None:
            raise ResourceDiscoveryError("adapter_response_invalid")
        degraded = bool(getattr(response, "degraded", False))
        raw_diagnostics = getattr(response, "diagnostics", ())
        cause_code = str(getattr(response, "cause_code", "") or "") or None
    if not isinstance(rows, (list, tuple)):
        raise ResourceDiscoveryError("adapter_results_must_be_list")
    mappings = [row for row in rows if isinstance(row, Mapping)]
    invalid_count = len(rows) - len(mappings)
    diagnostics: list[Mapping[str, object]] = []
    if invalid_count:
        diagnostics.append(
            make_diagnostic("resource", "normalize", code="resource_candidate_invalid", cause_code="invalid_response", detail=f"{invalid_count} rows were not objects")
        )
        degraded = True
    if isinstance(raw_diagnostics, (list, tuple)):
        diagnostics.extend(item for item in raw_diagnostics if isinstance(item, Mapping))
    elif raw_diagnostics:
        diagnostics.append(make_diagnostic("resource", "search", code="resource_source_failed", cause_code="invalid_response", detail="diagnostics must be a list"))
        degraded = True
    return mappings, degraded, diagnostics, cause_code


def _invoke_search(adapter: object, query: str, *, limit: int, adapter_name: str) -> object:
    method = getattr(adapter, "search", adapter)
    if not callable(method):
        raise ResourceDiscoveryError("adapter_not_callable")
    kwargs: dict[str, object] = {}
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "limit" in parameters or any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        kwargs["limit"] = limit
    if "adapters" in parameters:
        kwargs["adapters"] = (adapter_name,)
    try:
        return method(query, **kwargs)
    except TypeError:
        # Small compatibility seam for simple test/legacy callables that only
        # accept query.  Signature inspection above avoids retrying normal
        # TypeErrors from adapters with declared parameters.
        if kwargs:
            return method(query)
        raise


def _failure_diagnostic(source: str, stage: str, error: BaseException, *, code: str = "resource_source_failed") -> Mapping[str, object]:
    """Turn an adapter exception into the redacted diagnostic contract."""
    cause = getattr(error, "cause_code", None) or getattr(error, "kind", None) or getattr(error, "code", None)
    return make_diagnostic(source, stage, error, code=code, cause_code=str(cause) if cause else None)


def _document_excerpt(value: object, *, limit: int = 2_000) -> str:
    """Extract bounded public text from an existing reader/getter response."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:limit].strip()
    if isinstance(value, str):
        return value[:limit].strip()
    if isinstance(value, Mapping):
        for key in ("text", "content", "body", "skill_md", "readme"):
            candidate = value.get(key)
            if isinstance(candidate, (str, bytes)):
                return _document_excerpt(candidate, limit=limit)
    candidate = getattr(value, "text", None)
    if isinstance(candidate, (str, bytes)):
        return _document_excerpt(candidate, limit=limit)
    return ""


class GitHubResourceAdapter:
    """Production GitHub adapter built on the existing stdlib GitHub client."""

    SEARCH_URL = "https://api.github.com/search/repositories"

    def __init__(
        self,
        getter: Callable[[str], object] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        readme: bool = True,
    ) -> None:
        if getter is None:
            from .github_weekly import default_github_get

            getter = default_github_get
        self.getter = getter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.readme = bool(readme)

    def _readme_excerpt(self, full_name: str) -> tuple[str, list[Mapping[str, object]]]:
        if not self.readme:
            return "", []
        url = f"https://raw.githubusercontent.com/{full_name}/HEAD/README.md"
        try:
            text = _document_excerpt(self.getter(url))
        except Exception as exc:
            return "", [_failure_diagnostic("github", "read", exc)]
        if text:
            return text, []
        return "", [make_diagnostic("github", "read", code="resource_document_missing", cause_code="not_found", detail="README.md was not available")]

    def _row(self, item: Mapping[str, object]) -> tuple[dict[str, object] | None, list[Mapping[str, object]]]:
        full_name = str(item.get("full_name") or "").strip()
        if "/" not in full_name:
            return None, [make_diagnostic("github", "normalize", code="resource_candidate_invalid", cause_code="invalid_response", detail="GitHub item has no owner/repository")]
        readme, diagnostics = self._readme_excerpt(full_name)
        owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
        topics = item.get("topics") if isinstance(item.get("topics"), (list, tuple)) else ()
        row: dict[str, object] = {
            "source": "github",
            "kind": ResourceType.REPO.value,
            "source_id": full_name,
            "full_name": full_name,
            "name": str(item.get("name") or full_name.rsplit("/", 1)[-1]),
            "url": str(item.get("html_url") or f"https://github.com/{full_name}"),
            "description": str(item.get("description") or ""),
            "stars": item.get("stargazers_count", item.get("stars", 0)),
            "stars_week": item.get("stars_week", 0),
            "forks": item.get("forks_count", item.get("forks", 0)),
            "language": str(item.get("language") or ""),
            "topics": [str(topic) for topic in topics if str(topic).strip()],
            "owner": str(owner.get("login") or full_name.split("/", 1)[0]),
            "updated_at": str(item.get("updated_at") or ""),
            "pushed_at": str(item.get("pushed_at") or ""),
            "created_at": str(item.get("created_at") or ""),
            "fresh": bool(item.get("fresh")),
            "docs_exists": bool(readme),
        }
        if readme:
            row["readme_excerpt"] = readme
        return row, diagnostics

    def search(self, query: str, *, limit: int) -> object:
        diagnostics: list[Mapping[str, object]] = []
        rows: list[dict[str, object]] = []
        normalized = " ".join(str(query).split())
        if normalized == GITHUB_WEEKLY_QUERY:
            from .github_weekly import fetch_weekly_ai_repos

            weekly_diagnostics: list[dict[str, object]] = []
            repos = fetch_weekly_ai_repos(
                getter=self.getter,
                now=self.clock(),
                limit=limit,
                diagnostics=weekly_diagnostics,
            )
            diagnostics.extend(weekly_diagnostics)
            source_rows = repos
        else:
            url = f"{self.SEARCH_URL}?{urlencode({'q': normalized + ' fork:false', 'sort': 'stars', 'order': 'desc', 'per_page': str(max(1, limit))})}"
            try:
                payload = self.getter(url)
                items = payload.get("items") if isinstance(payload, Mapping) else None
                if not isinstance(items, list):
                    raise ValueError("github_search_items_missing")
                source_rows = [item for item in items if isinstance(item, Mapping)]
            except Exception as exc:
                return {"results": [], "degraded": True, "cause_code": str(getattr(exc, "kind", "unknown_error")), "diagnostics": [_failure_diagnostic("github", "search", exc)]}
        for item in source_rows[: max(0, int(limit))]:
            row, row_diagnostics = self._row(item)
            diagnostics.extend(row_diagnostics)
            if row is not None:
                rows.append(row)
        cause = ""
        if diagnostics:
            cause = str(diagnostics[-1].get("cause_code") or "")
        return {"results": rows, "degraded": bool(diagnostics), "cause_code": cause, "diagnostics": diagnostics}


class SkillResourceAdapter:
    """Reuse Skill Finder's bounded search and SKILL.md readers."""

    def __init__(self, getter: Callable[[str], object] | None = None) -> None:
        from . import skill_finder

        self.getter = getter or skill_finder._default_get

    def search(self, query: str, *, limit: int) -> object:
        from . import skill_finder

        diagnostics: list[Mapping[str, object]] = []

        def guarded_get(url: str) -> object:
            try:
                return self.getter(url)
            except Exception as exc:
                diagnostics.append(_failure_diagnostic("skills", "fetch", exc))
                raise

        def on_degrade(reason: str) -> None:
            diagnostics.append(make_diagnostic("skills", "search", code="resource_source_failed", cause_code="invalid_response", detail=reason))

        github_rows = skill_finder._github_search(f"SKILL.md {query}", guarded_get)[: max(0, int(limit))]
        registry_rows = skill_finder._skills_sh_search(query, guarded_get, on_degrade=on_degrade)[: max(0, int(limit))]
        rows: list[dict[str, object]] = []
        for raw, source in [*( (item, "github") for item in github_rows), *((item, "skills.sh") for item in registry_rows)]:
            full_name = str(raw.get("full_name") or raw.get("source_repo") or "").strip()
            source_id = str(raw.get("source_id") or full_name).strip()
            name = str(raw.get("name") or raw.get("skillId") or source_id.rsplit("/", 1)[-1]).strip()
            skill_md = str(raw.get("skill_md") or "")
            skill_path = str(raw.get("skill_path") or "")
            if full_name and not skill_md:
                try:
                    skill_md, skill_path = skill_finder.fetch_skill_md(full_name, guarded_get, name)
                except Exception as exc:
                    diagnostics.append(_failure_diagnostic("skills", "read", exc))
            if not skill_md:
                diagnostics.append(make_diagnostic("skills", "read", code="resource_document_missing", cause_code="not_found", detail="SKILL.md was not available"))
            excerpt = skill_finder._skill_excerpt({"skill_md": skill_md}) if skill_md else ""
            url = str(raw.get("url") or raw.get("html_url") or "")
            if not url:
                url = f"https://skills.sh/{source_id}" if source == "skills.sh" else f"https://github.com/{full_name}"
            rows.append(
                {
                    "source": source,
                    "kind": ResourceType.SKILL.value,
                    "source_id": source_id,
                    "full_name": full_name,
                    "name": name,
                    "url": url,
                    "description": str(raw.get("description") or raw.get("does") or ""),
                    "skill_path": skill_path,
                    "skill_excerpt": excerpt,
                    "stars": raw.get("stars", raw.get("stargazers_count", 0)),
                    "installs": raw.get("installs", 0),
                    "pushed_at": str(raw.get("pushed_at") or ""),
                    "topics": raw.get("topics") if isinstance(raw.get("topics"), (list, tuple)) else (),
                    "docs_exists": bool(skill_md),
                }
            )
        return {"results": rows, "degraded": bool(diagnostics), "cause_code": str(diagnostics[-1].get("cause_code") or "") if diagnostics else "", "diagnostics": diagnostics}


class WebResourceAdapter:
    """Use the existing bounded web search and ReaderService for excerpts."""

    def __init__(self, searcher: Callable[..., object] | None = None, *, reader: object | None = None) -> None:
        if searcher is None:
            from .background import search_web

            searcher = search_web
        if reader is None:
            from .reader import ReaderService

            reader = ReaderService()
        self.searcher = searcher
        self.reader = reader

    def search(self, query: str, *, limit: int) -> object:
        diagnostics: list[Mapping[str, object]] = []
        try:
            try:
                response = self.searcher(query, limit=limit)
            except TypeError:
                response = self.searcher(query)
        except Exception as exc:
            return {"results": [], "degraded": True, "cause_code": "unknown_error", "diagnostics": [_failure_diagnostic("web", "search", exc)]}
        if not isinstance(response, (list, tuple)):
            return {"results": [], "degraded": True, "cause_code": "invalid_response", "diagnostics": [make_diagnostic("web", "search", code="resource_source_failed", cause_code="invalid_response", detail="web search did not return a list")]}
        rows: list[dict[str, object]] = []
        for item in response[: max(0, int(limit))]:
            if not isinstance(item, Mapping):
                diagnostics.append(make_diagnostic("web", "normalize", code="resource_candidate_invalid", cause_code="invalid_response", detail="web result was not an object"))
                continue
            url = str(item.get("url") or item.get("link") or "").strip()
            if not url:
                diagnostics.append(make_diagnostic("web", "normalize", code="resource_candidate_invalid", cause_code="invalid_response", detail="web result has no URL"))
                continue
            row: dict[str, object] = {
                "source": "web",
                "kind": ResourceType.WEBSITE.value,
                "source_id": url,
                "name": str(item.get("title") or item.get("name") or url),
                "url": url,
                "description": str(item.get("snippet") or item.get("description") or ""),
            }
            try:
                reader = getattr(self.reader, "read", self.reader)
                document = reader(url, title_hint=str(row["name"]), source_type="resource")
                excerpt = _document_excerpt(document)
                if excerpt:
                    row["page_excerpt"] = excerpt
                    row["docs_exists"] = True
            except Exception as exc:
                diagnostics.append(_failure_diagnostic("web", "read", exc))
            rows.append(row)
        return {"results": rows, "degraded": bool(diagnostics), "cause_code": str(diagnostics[-1].get("cause_code") or "") if diagnostics else "", "diagnostics": diagnostics}


class ResourceDiscoveryService:
    """Search, normalize, judge, dedupe, and optionally persist resources."""

    def __init__(
        self,
        *,
        repo_adapter: object | None = None,
        github_adapter: object | None = None,
        skill_adapter: object | None = None,
        web_adapter: object | None = None,
        planner: object | None = None,
        decision_service: DecisionService | None = None,
        repository: ResourceSetRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if repo_adapter is not None and github_adapter is not None and repo_adapter is not github_adapter:
            raise ValueError("repo_adapter_and_github_adapter_conflict")
        self.adapters: dict[str, object | None] = {
            "github": github_adapter or repo_adapter,
            "skills": skill_adapter,
            "web": web_adapter,
        }
        self.planner = planner
        self.decision_service = decision_service or DecisionService()
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def _plan(self, query: str, source_event_ids: tuple[str, ...]) -> tuple[PlannerResult, list[Mapping[str, object]]]:
        diagnostics: list[Mapping[str, object]] = []
        if self.planner is None:
            diagnostics.append(make_diagnostic("planner", "plan", code="planner_failed", cause_code="not_configured", detail="planner is not configured"))
            return build_fallback_plan(query, source_event_ids=source_event_ids), diagnostics
        try:
            method = getattr(self.planner, "plan", self.planner)
            parameters = inspect.signature(method).parameters
            if "source_event_ids" in parameters:
                result = method(query, source_event_ids=source_event_ids)
            else:
                result = method(query)
            if isinstance(result, Mapping):
                result = PlannerResult.from_dict(result)
            if not isinstance(result, PlannerResult):
                raise ResourceDiscoveryError("planner_result_invalid")
            return result, diagnostics
        except PlannerError as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            if isinstance(diagnostic, Mapping):
                diagnostics.append(normalize_diagnostic(diagnostic, "planner", "plan"))
            else:
                diagnostics.append(make_diagnostic("planner", "plan", exc, code="planner_failed", cause_code=getattr(exc, "cause_code", None)))
        except Exception as exc:
            diagnostics.append(make_diagnostic("planner", "plan", exc, code="planner_failed"))
        fallback = build_fallback_plan(query, source_event_ids=source_event_ids)
        return fallback, diagnostics

    def _assess(self, candidate: ResourceCandidate, round_number: int, intent: object) -> DecisionAssessment:
        payload = candidate.legacy_payload()
        payload.update(
            {
                "resource_id": candidate.resource_id,
                "resource_type": candidate.kind.value,
                "decision_round": round_number,
            }
        )
        if round_number == 2:
            first = getattr(candidate, "assessment", None)
            if isinstance(first, ResourceAssessment):
                payload["round1_assessment"] = first.round1.to_dict()
        try:
            assessment = self.decision_service.assess(
                payload,
                candidate_id=candidate.resource_id,
                objective_score=candidate.objective_score,
                intent=intent,
                rubric_id=RESOURCE_RUBRIC_ID,
            )
            if isinstance(assessment, Mapping):
                assessment = _decode_decision(assessment)
            if not isinstance(assessment, DecisionAssessment):
                raise ResourceDiscoveryError("decision_assessment_invalid")
            return assessment
        except Exception as exc:
            # A custom adapter may fail outside the Jev error boundary.  Keep
            # the candidate and fall back to the same objective-only service.
            fallback = DecisionService().assess(
                payload,
                candidate_id=candidate.resource_id,
                objective_score=candidate.objective_score,
                intent=intent,
                rubric_id=RESOURCE_RUBRIC_ID,
            )
            diagnostic = make_diagnostic("jev", "assess", exc, code="decision_failed")
            return replace(fallback, diagnostics=tuple((*fallback.diagnostics, diagnostic)))

    def _search_one(
        self,
        adapter_name: str,
        query: str,
        limit: int,
    ) -> tuple[list[Mapping[str, object]], bool, list[Mapping[str, object]], str | None]:
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return [], True, [make_diagnostic(adapter_name, "search", code="resource_adapter_not_configured", cause_code="not_configured", detail="adapter is not configured")], "not_configured"
        try:
            response = _invoke_search(adapter, query, limit=limit, adapter_name=adapter_name)
            rows, degraded, diagnostics, cause_code = _response_rows(response)
            normalized = [normalize_diagnostic(item, adapter_name, "search") for item in diagnostics]
            return rows, degraded, normalized, cause_code
        except Exception as exc:
            diagnostic = make_diagnostic(adapter_name, "search", exc, code="resource_source_failed")
            return [], True, [diagnostic], getattr(exc, "cause_code", None) or "unknown_error"

    def discover(
        self,
        query: str,
        *,
        pasted_text: str = "",
        source_event_ids: Sequence[str] = (),
        adapters: Sequence[str] | None = None,
        limit: int | None = None,
        preset: str = "",
        persist: bool = True,
    ) -> ResourceSet:
        normalized_query = _text(query, "query", limit=_MAX_QUERY_CHARS)
        event_ids = _string_list(source_event_ids, "source_event_ids") if source_event_ids else ()
        plan, diagnostics = self._plan(normalized_query, event_ids)
        selected_adapters = tuple(dict.fromkeys(_canonical_adapter(name) for name in (adapters or plan.plan.adapters)))
        if not selected_adapters:
            raise ResourceDiscoveryError("adapters_required")
        max_candidates = plan.plan.max_candidates if limit is None else min(plan.plan.max_candidates, int(limit))
        if max_candidates < 1:
            raise ResourceDiscoveryError("limit_must_be_positive")
        raw_candidates: list[tuple[Mapping[str, object], str]] = []
        adapter_status: dict[str, dict[str, object]] = {}
        degraded = bool(diagnostics)
        for adapter_name in selected_adapters:
            if adapter_name not in self.adapters:
                diagnostic = make_diagnostic(adapter_name, "search", code="resource_adapter_unsupported", cause_code="unsupported_adapter", detail="adapter is not supported")
                adapter_status[adapter_name] = {"requested": True, "attempted": False, "status": "failed", "count": 0, "diagnostics": [diagnostic]}
                diagnostics.append(diagnostic)
                degraded = True
                continue
            rows, source_degraded, source_diagnostics, cause_code = self._search_one(adapter_name, plan.plan.query, max_candidates)
            for row in rows:
                raw_candidates.append((row, adapter_name))
            source_status = "degraded" if source_degraded else ("ok" if rows else "empty")
            adapter_status[adapter_name] = {
                "requested": True,
                "attempted": True,
                "status": source_status,
                "count": len(rows),
                "cause_code": cause_code or "",
                "diagnostics": source_diagnostics,
            }
            diagnostics.extend(source_diagnostics)
            degraded = degraded or source_degraded
        for name in ("github", "skills", "web"):
            if name not in adapter_status:
                adapter_status[name] = {"requested": False, "attempted": False, "status": "skipped", "count": 0, "diagnostics": []}

        if not isinstance(pasted_text, str):
            pasted_text = ""
        pasted, rejects = parse_skill_links(pasted_text)
        for reject in rejects:
            diagnostics.append(make_diagnostic("pasted", "parse", code="pasted_skill_rejected", cause_code=str(reject.get("reason") or "invalid_link"), detail="pasted skill link was rejected"))
            degraded = True
        for item in pasted:
            candidate = dict(item)
            candidate["source"] = "pasted"
            candidate["kind"] = ResourceType.SKILL.value
            candidate["manual"] = True
            raw_candidates.append((candidate, "pasted"))

        candidates_by_identity: dict[str, ResourceCandidate] = {}
        for raw, adapter_name in raw_candidates:
            try:
                candidate = ResourceCandidate.from_mapping(raw, adapter=adapter_name)
            except Exception as exc:
                diagnostics.append(make_diagnostic(adapter_name, "normalize", exc, code="resource_candidate_invalid", cause_code="invalid_response"))
                degraded = True
                continue
            if candidate.identity_key in candidates_by_identity:
                candidates_by_identity[candidate.identity_key] = candidates_by_identity[candidate.identity_key].merge(candidate)
            else:
                candidates_by_identity[candidate.identity_key] = candidate

        scored: list[ResourceCandidate] = []
        for candidate in candidates_by_identity.values():
            scored.append(replace(candidate, objective_score=objective_resource_score(candidate.legacy_payload(), now=self.clock())))

        judged: list[ResourceCandidate] = []
        for candidate in scored:
            first = self._assess(candidate, 1, plan.intent)
            staged = replace(candidate, assessment=ResourceAssessment(candidate.resource_id, (first, first)))
            second = self._assess(staged, 2, plan.intent)
            assessment = ResourceAssessment(candidate.resource_id, (first, second))
            selected = second.decision == DecisionChoice.INCLUDE and not bool(second.duplicate)
            judged.append(candidate.with_assessment(assessment, selected=selected))
            for item in (*first.diagnostics, *second.diagnostics):
                if isinstance(item, Mapping):
                    diagnostics.append(normalize_diagnostic(item, "jev", "assess"))
            degraded = degraded or first.degraded or second.degraded
        judged.sort(key=lambda item: (-(item.final_assessment.final_score if item.final_assessment else item.objective_score), -item.objective_score, item.resource_id))

        status = ResourceStatus.DEGRADED if degraded else (ResourceStatus.OK if judged else ResourceStatus.EMPTY)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        result = ResourceSet(
            set_id=str(self.id_factory()),
            query=normalized_query,
            plan=plan,
            candidates=tuple(judged),
            adapter_status=adapter_status,
            status=status,
            degraded=degraded,
            diagnostics=tuple(diagnostics),
            created_at=now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            preset=preset,
        )
        if persist and self.repository is not None:
            try:
                self.repository.save_resource_set(result)
            except Exception as exc:
                diagnostic = make_diagnostic("resource_repository", "persist", exc, code="resource_persist_failed")
                result = replace(result, status=ResourceStatus.DEGRADED, degraded=True, diagnostics=(*result.diagnostics, diagnostic))
        return result

    def discover_preset(self, preset: str = GITHUB_WEEKLY_PRESET, **kwargs: object) -> ResourceSet:
        normalized = str(preset or "").strip().lower().replace("-", "_")
        if normalized not in {GITHUB_WEEKLY_PRESET, "github_weekly", "weekly_ai_projects"}:
            raise ResourceDiscoveryError("unknown_resource_preset")
        kwargs.setdefault("adapters", ("github",))
        kwargs.setdefault("preset", GITHUB_WEEKLY_PRESET)
        return self.discover(GITHUB_WEEKLY_QUERY, **kwargs)  # type: ignore[arg-type]

    def restore(self, set_id: str) -> ResourceSet:
        if self.repository is None:
            raise ResourceDiscoveryError("resource_repository_not_configured")
        identifier = _text(set_id, "set_id", limit=_MAX_ID_CHARS)
        try:
            raw = self.repository.get_resource_set(identifier)
        except Exception as exc:
            raise ResourceDiscoveryError("resource_restore_failed", exc) from None
        if raw is None:
            raise ResourceDiscoveryError("resource_set_not_found")
        if isinstance(raw, ResourceSet):
            return raw
        try:
            return ResourceSet.from_dict(raw)
        except Exception as exc:
            raise ResourceDiscoveryError("resource_storage_invalid", exc) from None

    def refresh(self, set_id: str) -> ResourceSet:
        """Restore the last durable result without re-running external search."""
        return self.restore(set_id)


def build_resource_discovery_service(
    *,
    repository: ResourceSetRepository | None = None,
    planner: object | None = None,
    decision_service: DecisionService | None = None,
    github_get: Callable[[str], object] | None = None,
    skills_get: Callable[[str], object] | None = None,
    web_search: Callable[..., object] | None = None,
    web_reader: object | None = None,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> ResourceDiscoveryService:
    """Construct the production service while keeping every external seam injectable."""
    return ResourceDiscoveryService(
        github_adapter=GitHubResourceAdapter(github_get, clock=clock),
        skill_adapter=SkillResourceAdapter(skills_get),
        web_adapter=WebResourceAdapter(web_search, reader=web_reader),
        planner=planner,
        decision_service=decision_service,
        repository=repository,
        clock=clock,
        id_factory=id_factory,
    )


__all__ = [
    "GITHUB_WEEKLY_PRESET",
    "GITHUB_WEEKLY_QUERY",
    "GitHubResourceAdapter",
    "ResourceAdapter",
    "ResourceAssessment",
    "ResourceCandidate",
    "ResourceDiscoveryError",
    "ResourceDiscoveryService",
    "ResourceSet",
    "ResourceSetRepository",
    "ResourceStatus",
    "SkillResourceAdapter",
    "WebResourceAdapter",
    "build_resource_discovery_service",
    "objective_resource_score",
]
