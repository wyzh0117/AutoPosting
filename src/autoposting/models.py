from __future__ import annotations

from dataclasses import dataclass


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