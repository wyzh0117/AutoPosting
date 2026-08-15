from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .models import Source


class ConfigError(ValueError):
    pass


REQUIRED_SOURCE_FIELDS = {
    "source_id",
    "name",
    "type",
    "url_or_handle",
    "tier",
    "topics",
    "enabled",
    "fetch_interval",
}


def _fetch_interval_minutes(value: object) -> int:
    if isinstance(value, bool):
        raise ConfigError("fetch_interval must be a positive integer or duration")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        match = re.fullmatch(r"([1-9][0-9]*)(m|h)", value.strip().lower())
        if not match:
            raise ConfigError("fetch_interval must look like '15m' or '1h'")
        amount = int(match.group(1))
        minutes = amount if match.group(2) == "m" else amount * 60
    else:
        raise ConfigError("fetch_interval must be a positive integer or duration")
    if minutes <= 0:
        raise ConfigError("fetch_interval must be positive")
    return minutes


def _source_from_mapping(data: object, index: int) -> Source:
    if not isinstance(data, dict):
        raise ConfigError(f"sources[{index}] must be an object")
    missing = sorted(REQUIRED_SOURCE_FIELDS - data.keys())
    if missing:
        raise ConfigError(f"sources[{index}] missing fields: {', '.join(missing)}")

    source_id = data["source_id"]
    name = data["name"]
    source_type = data["type"]
    location = data["url_or_handle"]
    tier = data["tier"]
    topics = data["topics"]
    enabled = data["enabled"]
    identity_verified = data.get("identity_verified", False)
    content_filter = data.get("content_filter") or ""

    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", source_id):
        raise ConfigError(f"sources[{index}].source_id is invalid")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"sources[{index}].name is required")
    if source_type not in {"rss", "web", "x", "sitemap"}:
        raise ConfigError(f"sources[{index}].type must be rss, web, x, or sitemap")
    if tier not in {"S1", "S2", "S3", "S4"}:
        raise ConfigError(f"sources[{index}].tier must be S1, S2, S3, or S4")
    if not isinstance(location, str) or not location.strip():
        raise ConfigError(f"sources[{index}].url_or_handle is required")
    if source_type in {"rss", "web", "sitemap"}:
        parts = urlsplit(location)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigError(f"sources[{index}].url_or_handle must be an HTTP(S) URL")
    if not isinstance(topics, list) or not topics or not all(
        isinstance(topic, str) and topic.strip() for topic in topics
    ):
        raise ConfigError(f"sources[{index}].topics must be a non-empty string list")
    if not isinstance(enabled, bool):
        raise ConfigError(f"sources[{index}].enabled must be boolean")
    if not isinstance(identity_verified, bool):
        raise ConfigError(f"sources[{index}].identity_verified must be boolean")
    if source_type == "x" and enabled and not identity_verified and tier in {"S1", "S2"}:
        raise ConfigError(
            f"sources[{index}].identity_verified must be true before enabling an S1/S2 X account"
        )
    if not isinstance(content_filter, str) or content_filter not in {"", "ai"}:
        raise ConfigError(f"sources[{index}].content_filter must be empty or 'ai'")

    return Source(
        source_id=source_id,
        name=name.strip(),
        type=source_type,
        url_or_handle=location.strip(),
        tier=tier,
        topics=tuple(dict.fromkeys(topic.strip() for topic in topics)),
        enabled=enabled,
        fetch_interval_minutes=_fetch_interval_minutes(data["fetch_interval"]),
        identity_verified=identity_verified,
        content_filter=content_filter,
    )


def load_sources(path: str | Path) -> tuple[Source, ...]:
    source_path = Path(path)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"source registry not found: {source_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in source registry: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ConfigError("source registry must contain a sources array")
    sources = tuple(
        _source_from_mapping(item, index) for index, item in enumerate(payload["sources"])
    )
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise ConfigError("source_id values must be unique")
    return sources