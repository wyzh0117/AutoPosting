from __future__ import annotations

from dataclasses import dataclass


SOURCE_TYPES = frozenset(
    {"official", "media", "community", "developer", "research", "repository", "other"}
)
ADAPTER_TYPES = frozenset(
    {"rss", "sitemap", "html", "reddit", "github", "api", "account", "x"}
)

_LEGACY_SOURCE_TYPES = {
    "rss": ("other", "rss"),
    "web": ("other", "html"),
    "sitemap": ("official", "sitemap"),
    "reddit": ("community", "reddit"),
    "x": ("developer", "x"),
}


def canonical_source_types(
    legacy_type: object = "",
    source_type: object = "",
    adapter_type: object = "",
) -> tuple[str, str]:
    """Resolve the v3 ``type`` field into canonical source and adapter types.

    ``type`` remains a compatibility alias because existing collectors and
    callers still construct ``Source`` positionally. New config should use
    ``source_type`` and ``adapter_type`` explicitly.
    """
    legacy = str(legacy_type or "").strip().lower()
    mapped_source, mapped_adapter = _LEGACY_SOURCE_TYPES.get(legacy, ("other", legacy))
    canonical_source = str(source_type or mapped_source).strip().lower()
    canonical_adapter = str(adapter_type or mapped_adapter).strip().lower()
    if canonical_source not in SOURCE_TYPES:
        raise ValueError(f"unsupported source_type: {canonical_source}")
    if canonical_adapter not in ADAPTER_TYPES:
        raise ValueError(f"unsupported adapter_type: {canonical_adapter}")
    return canonical_source, canonical_adapter


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    type: str
    url_or_handle: str
    tier: str
    topics: tuple[str, ...]
    enabled: bool
    fetch_interval_minutes: int
    identity_verified: bool = False
    content_filter: str = ""
    source_type: str = ""
    adapter_type: str = ""
    topic_scope: str = ""
    language: str = ""

    def __post_init__(self) -> None:
        source_type, adapter_type = canonical_source_types(
            self.type, self.source_type, self.adapter_type
        )
        # New callers may omit the legacy alias (passing an empty placeholder);
        # retain a usable adapter alias for older collectors and DB constraints.
        if not str(self.type or "").strip():
            object.__setattr__(self, "type", adapter_type)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "adapter_type", adapter_type)
        # Registry scope/language are advisory metadata, but both columns are
        # NOT NULL: normalise here so a None or padded value cannot reject an
        # otherwise valid source at write time.
        object.__setattr__(self, "topic_scope", str(self.topic_scope or "").strip().lower())
        object.__setattr__(self, "language", str(self.language or "").strip().lower())


@dataclass(frozen=True)
class FeedItem:
    external_id: str | None
    canonical_url: str
    title: str
    content_text: str
    published_at: str | None
    raw_data: dict[str, str | None]


@dataclass(frozen=True)
class CollectionResult:
    inserted: int
    duplicates: int
    failed: int = 0
