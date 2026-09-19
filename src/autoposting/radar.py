"""On-demand news radar primitives.

The radar deliberately has no scheduler and does not write while scoring.  A
caller that owns an explicit collect action may persist the returned
engagement signals with :class:`autoposting.db.Database`.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .engagement import engagement_from_raw, engagement_kind


FACT_STATUSES = ("SUPPORTED", "INFERRED", "CONFLICTED", "UNSUPPORTED")
_PLATFORM_BASELINES = {
    "hn": 120.0,
    "reddit": 220.0,
    "x": 600.0,
    "github": 1000.0,
    "generic": 120.0,
}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
    "will", "new", "news", "update", "official", "breaking", "report", "reports",
    "latest", "today", "关于", "正式", "最新", "消息", "宣布", "发布", "推出", "上线",
    "报道", "回应", "介绍", "表示", "称", "或将", "以及", "并且",
}
# These words describe how a headline is written rather than which event it
# names.  Removing them from the conflict/fingerprint path lets a source add
# "today", "analysis", or "what developers need to know" without changing
# event identity, while keeping product/entity tokens significant.
_CLUSTER_GENERIC_TOKENS = _STOPWORDS | {
    "announce", "announces", "announced", "announcing", "announcement",
    "launch", "launches", "launched", "launching", "release", "releases",
    "released", "releasing", "introduce", "introduces", "introduced",
    "introducing", "update", "updates", "updated", "updating", "version",
    "versions", "model", "models", "system", "systems", "tool", "tools",
    "platform", "platforms", "product", "products", "company", "companies",
    "today", "yesterday", "now", "what", "why", "how", "details", "detail",
    "analysis", "analyze", "guide", "guides", "developers", "developer",
    "development", "context", "explained", "explainer", "review", "preview",
    "statement", "says", "said", "available", "news", "breaking", "major",
    "new", "first", "内容", "详情", "分析", "讨论", "介绍", "发布", "推出",
    "上线", "更新", "消息", "报道", "官方", "今天", "昨日", "现在", "开发者",
}
_IMPACT_WORDS = {
    "launch", "launched", "release", "released", "model", "acquire", "acquisition",
    "regulation", "regulatory", "policy", "ban", "lawsuit", "court", "security",
    "breach", "outage", "shutdown", "funding", "price", "pricing", "open-source",
    "opensourced", "standard", "研究", "模型", "收购", "监管", "政策", "安全", "漏洞",
    "融资", "价格", "开源", "上线", "停运", "法院", "诉讼", "事故",
}
_SCOPE_WORDS = {
    "industry", "global", "everyone", "developer", "developers", "platform", "ecosystem",
    "worldwide", "行业", "全球", "开发者", "平台", "生态", "全行业", "跨行业",
}
_CONSEQUENCE_WORDS = {
    "change", "changes", "replace", "replacement", "available", "blocked", "risk", "impact",
    "cost", "price", "migration", "consequence", "改变", "替代", "影响", "风险", "成本",
    "迁移", "限制", "禁用", "涨价", "降价",
}
_SOURCE_ROLE_RANK = {"canonical": 4, "supporting": 3, "community": 2, "discussion": 1}
_SOURCE_CATEGORY = {
    "canonical": "Primary / Official",
    "supporting": "Mainstream Media",
    "community": "Technical Community",
    "discussion": "Social Community",
}
_OFFICIAL_DOMAINS = {
    "openai.com", "anthropic.com", "deepmind.google", "ai.google", "google.com",
    "microsoft.com", "meta.com", "ai.meta.com", "nvidia.com",
    "stability.ai", "mistral.ai", "x.ai", "cohere.com",
}
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*|[\u4e00-\u9fff]+", re.IGNORECASE)


def _text(value: object) -> str:
    return str(value or "").strip()


def _parse_time(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now(value: datetime | str | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _parse_time(value)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", match):
            # Character tokens keep short Chinese headlines clusterable, while
            # dropping common connective/reporting words avoids broad merges.
            tokens.update(char for char in match if char not in _STOPWORDS)
            tokens.update(match[index : index + 2] for index in range(len(match) - 1))
        elif match not in _STOPWORDS and len(match) > 1:
            tokens.add(match)
    return tokens


def _title_key(title: str) -> str:
    return " ".join(sorted(_tokens(title)))


def _normalized_title(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", title.casefold()).strip()


def _domain(url: object, source_id: str = "") -> str:
    raw = _text(url)
    try:
        host = (urlsplit(raw).hostname or "").casefold().strip(".")
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return host or source_id.casefold().strip()


def _as_map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _source_role(source: Mapping[str, object]) -> str:
    explicit = _text(
        source.get("role")
        or source.get("source_role")
        or source.get("category")
        or source.get("kind")
        or source.get("classification")
    ).casefold()
    if source.get("official") or source.get("is_official") or source.get("first_party"):
        return "canonical"
    if explicit in {"canonical", "official", "primary", "first-party", "first_party"}:
        return "canonical"
    if explicit in {"supporting", "media", "research"}:
        return "supporting"
    if explicit in {"community", "technical", "code"}:
        return "community"
    if explicit in {"discussion", "social"}:
        return "discussion"
    source_id = _text(source.get("source_id") or source.get("id")).casefold()
    source_type = _text(source.get("source_type") or source.get("type")).casefold()
    url = _text(source.get("url") or source.get("source_url"))
    blob = f"{source_id} {source_type} {url}".casefold()
    host = _domain(url)
    if source_type in {"official", "primary", "first-party", "first_party"}:
        return "canonical"
    if source_type in {"research", "paper", "government", "court"}:
        return "canonical"
    if source_type in {"x", "reddit"} or any(value in blob for value in ("news.ycombinator.com", "reddit.com", "x.com/", "twitter.com/")):
        return "discussion"
    if _text(source.get("tier")).upper() == "S1" or host in _OFFICIAL_DOMAINS or any(host.endswith(f".{domain}") for domain in _OFFICIAL_DOMAINS):
        return "canonical"
    if source_type in {"github", "community", "forum", "technical"} or "github.com" in host:
        return "community"
    if _text(source.get("tier")).upper() == "S1" or any(
        marker in source_id for marker in ("official", "first_party", "first-party", "openai", "anthropic", "google", "meta")
    ):
        return "canonical"
    return "supporting"


def _source_category(source: Mapping[str, object]) -> str:
    role = _text(source.get("role")) or _source_role(source)
    source_type = _text(source.get("source_type") or source.get("type")).casefold()
    if source_type in {"research", "paper"}:
        return "Research"
    if source_type in {"github", "code"}:
        return "Code / GitHub"
    return _SOURCE_CATEGORY.get(role, "Mainstream Media")


def _source_records(candidate: Mapping[str, object]) -> list[dict[str, object]]:
    raw_sources = candidate.get("sources")
    records: list[dict[str, object]] = []
    if isinstance(raw_sources, Sequence) and not isinstance(raw_sources, (str, bytes)):
        for raw in raw_sources:
            if isinstance(raw, Mapping):
                records.append(dict(raw))
    source_ids = candidate.get("source_ids")
    if isinstance(source_ids, str):
        source_ids = [source_ids]
    ids = [str(value).strip() for value in (source_ids or []) if str(value).strip()]
    if not ids and candidate.get("source_id"):
        ids = [_text(candidate.get("source_id"))]
    source_urls = candidate.get("source_urls")
    if isinstance(source_urls, str):
        source_urls = [source_urls]
    urls = [str(value).strip() for value in (source_urls or [])]
    metadata = _as_map(candidate.get("source_metadata"))
    tiers = _as_map(candidate.get("source_tiers"))
    types = _as_map(candidate.get("source_types"))
    default_tier = _text(candidate.get("base_tier")).upper()
    by_key = {(str(row.get("source_id") or row.get("id")), _text(row.get("url") or row.get("source_url"))): row for row in records}
    for index, source_id in enumerate(ids):
        url = urls[index] if index < len(urls) else ""
        existing = dict(by_key.get((source_id, url), {}))
        meta = _as_map(metadata.get(source_id))
        existing.update(meta)
        existing.update({"source_id": source_id, "url": url})
        if source_id in tiers:
            existing["tier"] = tiers[source_id]
        if source_id in types:
            existing["source_type"] = types[source_id]
        if "tier" not in existing and len(ids) == 1 and default_tier:
            existing["tier"] = default_tier
        records.append(existing)
    if not records:
        source_id = _text(candidate.get("source_id")) or "item"
        records.append({"source_id": source_id, "url": _text(candidate.get("canonical_url"))})
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for row in records:
        source_id = _text(row.get("source_id") or row.get("id")) or "source"
        url = _text(row.get("url") or row.get("source_url"))
        normalized = dict(row)
        normalized.update(
            {
                "source_id": source_id,
                "url": url,
                "name": _text(row.get("name")) or source_id,
                "source_type": _text(row.get("source_type") or row.get("type")),
                "tier": _text(row.get("tier")).upper() or "S4",
                "domain": _domain(url, source_id),
            }
        )
        normalized["role"] = _source_role(normalized)
        normalized["category"] = _source_category(normalized)
        unique.setdefault((source_id, url), normalized)
    return list(unique.values())


def _engagement(candidate: Mapping[str, object]) -> dict[str, int]:
    raw = candidate.get("engagement")
    if isinstance(raw, Mapping):
        parsed = engagement_from_raw(dict(raw))
    else:
        parsed = engagement_from_raw(candidate.get("raw_data") or {})
    return parsed.as_public()


def _normal_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    title = _text(candidate.get("title"))
    content = _text(candidate.get("content_text") or candidate.get("summary"))
    observed = _text(candidate.get("observed_at") or candidate.get("published_at") or candidate.get("fetched_at"))
    sources = _source_records(candidate)
    item_id = _text(candidate.get("item_id"))
    raw = dict(candidate)
    raw.pop("_tokens", None)
    return {
        "item_id": item_id,
        "title": title,
        "content_text": content,
        "observed_at": observed,
        "topic": _text(candidate.get("topic")),
        "canonical_url": _text(candidate.get("canonical_url")),
        "source_ids": [str(source["source_id"]) for source in sources],
        "source_urls": [str(source.get("url") or "") for source in sources],
        "sources": sources,
        "engagement": _engagement(candidate),
        "raw_data": candidate.get("raw_data") if isinstance(candidate.get("raw_data"), Mapping) else {},
        "_raw": raw,
        "_tokens": _tokens(title),
        "_content_tokens": _tokens(content[:500]),
        "_normalized_title": _normalized_title(title),
        "_title_key": _title_key(title),
        "_content_hash": _text(candidate.get("content_hash")),
    }


def _distinctive_cluster_tokens(value: object) -> set[str]:
    tokens = set(value if isinstance(value, (set, frozenset, list, tuple)) else _tokens(_text(value)))
    return {
        token
        for token in tokens
        if token not in _CLUSTER_GENERIC_TOKENS and not token.isdigit()
    }


def _has_distinctive_conflict(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Reject Jaccard merges when both titles name different entities/products."""
    left_tokens = _distinctive_cluster_tokens(left.get("_tokens"))
    right_tokens = _distinctive_cluster_tokens(right.get("_tokens"))
    left_only = left_tokens - right_tokens
    right_only = right_tokens - left_tokens
    return bool(left_only and right_only)


def _same_event(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    left_hash = _text(left.get("_content_hash"))
    if left_hash and left_hash == _text(right.get("_content_hash")):
        return True
    if _text(left.get("_normalized_title")) and left.get("_normalized_title") == right.get("_normalized_title"):
        return True
    if _has_distinctive_conflict(left, right):
        return False
    left_tokens = set(left.get("_tokens") or ())
    right_tokens = set(right.get("_tokens") or ())
    shared = left_tokens & right_tokens
    union = left_tokens | right_tokens
    if len(shared) < 2 or not union:
        return False
    similarity = len(shared) / len(union)
    return similarity >= 0.42 or (len(shared) >= 4 and similarity >= 0.30)


def _strip_internal(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "item_id": item.get("item_id", ""),
        "title": item.get("title", ""),
        "content_text": item.get("content_text", ""),
        "observed_at": item.get("observed_at", ""),
        "topic": item.get("topic", ""),
        "canonical_url": item.get("canonical_url", ""),
        "source_ids": list(item.get("source_ids") or []),
        "source_urls": list(item.get("source_urls") or []),
        "sources": [dict(source) for source in (item.get("sources") or [])],
        "engagement": dict(item.get("engagement") or {}),
    }


def _event_id(group: Sequence[Mapping[str, object]]) -> str:
    all_sources: dict[tuple[str, str], dict[str, object]] = {}
    for item in group:
        for source in item.get("sources") or []:
            normalized = dict(source)
            all_sources.setdefault(
                (_text(normalized.get("source_id")), _text(normalized.get("url"))),
                normalized,
            )
    canonical = max(all_sources.values(), key=_source_sort_key) if all_sources else {}
    canonical_id = _text(canonical.get("source_id"))
    canonical_items = [
        item
        for item in group
        if canonical_id
        and canonical_id in {_text(source.get("source_id")) for source in item.get("sources") or []}
    ]
    identity_item = max(
        canonical_items or list(group),
        key=lambda item: _parse_time(item.get("observed_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    distinctive = _distinctive_cluster_tokens(identity_item.get("_tokens"))
    fingerprint = " ".join(sorted(distinctive)) or _text(identity_item.get("_normalized_title")) or _text(identity_item.get("item_id"))
    topic = next((_text(item.get("topic")) for item in group if _text(item.get("topic"))), "")
    identity = f"{topic}:{fingerprint}"
    return hashlib.sha256(f"radar:{identity}".encode("utf-8")).hexdigest()[:24]


def _source_sort_key(source: Mapping[str, object]) -> tuple[int, int, int, str]:
    role = _text(source.get("role"))
    tier_rank = {"S1": 4, "S2": 3, "S3": 2, "S4": 1}.get(_text(source.get("tier")).upper(), 0)
    first_party = int(bool(source.get("identity_verified")))
    return (_SOURCE_ROLE_RANK.get(role, 0), tier_rank, first_party, _text(source.get("source_id")))


def _public_source(source: Mapping[str, object], item_id: str = "") -> dict[str, object]:
    result = {
        "source_id": _text(source.get("source_id") or source.get("id")),
        "name": _text(source.get("name")) or _text(source.get("source_id") or source.get("id")),
        "url": _text(source.get("url") or source.get("source_url")),
        "domain": _text(source.get("domain")),
        "source_type": _text(source.get("source_type") or source.get("type")),
        "tier": _text(source.get("tier")).upper() or "S4",
        "role": _text(source.get("role")) or _source_role(source),
        "category": _text(source.get("category")) or _source_category(source),
    }
    if item_id:
        result["item_id"] = item_id
    return result


def _build_event(group: Sequence[Mapping[str, object]]) -> dict[str, object]:
    all_sources: dict[tuple[str, str], dict[str, object]] = {}
    for item in group:
        for source in item.get("sources") or []:
            normalized = dict(source)
            key = (_text(normalized.get("source_id")), _text(normalized.get("url")))
            all_sources.setdefault(key, normalized)
    sources = list(all_sources.values())
    canonical = max(sources, key=_source_sort_key) if sources else {"source_id": "source", "role": "supporting"}
    canonical_id = _text(canonical.get("source_id"))
    canonical_items = [item for item in group if canonical_id in {_text(source.get("source_id")) for source in item.get("sources") or []}]
    canonical_item = max(canonical_items or list(group), key=lambda item: _parse_time(item.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    times = [_parse_time(item.get("observed_at")) for item in group]
    valid_times = [value for value in times if value is not None]
    item_ids = [_text(item.get("item_id")) for item in group if _text(item.get("item_id"))]
    source_domains = sorted({_text(source.get("domain")) for source in sources if _text(source.get("domain"))})
    public_sources = [_public_source(source) for source in sorted(sources, key=lambda row: (_SOURCE_ROLE_RANK.get(_text(row.get("role")), 0), _text(row.get("source_id"))), reverse=True)]
    supporting = [source for source in public_sources if source.get("role") == "supporting"]
    community = [source for source in public_sources if source.get("role") == "community"]
    discussion = [source for source in public_sources if source.get("role") == "discussion"]
    title = _text(canonical_item.get("title")) or _text(group[0].get("title"))
    entities = sorted(_tokens(title))[:12]
    event = {
        "event_id": _event_id(group),
        "title": title,
        "topic": next((_text(item.get("topic")) for item in group if _text(item.get("topic"))), ""),
        "representative_item_id": _text(canonical_item.get("item_id")),
        "canonical_item_id": _text(canonical_item.get("item_id")),
        "item_id": _text(canonical_item.get("item_id")),
        "item_ids": item_ids,
        "first_seen_at": _iso(min(valid_times)) if valid_times else "",
        "latest_seen_at": _iso(max(valid_times)) if valid_times else "",
        "entities": entities,
        "canonical_source": dict(_public_source(canonical, _text(canonical_item.get("item_id")))),
        "canonical": dict(_public_source(canonical, _text(canonical_item.get("item_id")))),
        "supporting_sources": supporting,
        "community_sources": community,
        "discussion_sources": discussion,
        "supporting": supporting,
        "community": community,
        "discussion": discussion,
        "sources": public_sources,
        "source_ids": sorted({_text(source.get("source_id")) for source in sources}),
        "source_domains": source_domains,
        "independent_source_count": len(source_domains),
        "items": [_strip_internal(item) for item in group],
        "importance_score": 0.0,
        "attention_score": 0.0,
        "priority_score": 0.0,
    }
    return event


def cluster_candidates(candidates: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Cluster hotspot candidates into stable event records.

    Clustering is intentionally conservative: shared title/content signals or
    a repeated content hash are required, so source count alone cannot merge
    unrelated stories.
    """
    normalized = [_normal_candidate(candidate) for candidate in candidates if isinstance(candidate, Mapping)]
    groups: list[list[dict[str, object]]] = []
    for candidate in normalized:
        matching = [index for index, group in enumerate(groups) if any(_same_event(candidate, item) for item in group)]
        if not matching:
            groups.append([candidate])
            continue
        first = matching[0]
        groups[first].append(candidate)
        for index in reversed(matching[1:]):
            groups[first].extend(groups.pop(index))
    return [_build_event(group) for group in groups]


def cluster_events(candidates: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Compatibility alias for :func:`cluster_candidates`."""
    return cluster_candidates(candidates)


def _event_items(event: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = event.get("items")
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []


def _event_sources(event: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = event.get("sources")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [source for source in raw if isinstance(source, Mapping)]
    canonical = event.get("canonical_source")
    return [canonical] if isinstance(canonical, Mapping) else []


def _event_age_hours(event: Mapping[str, object], now: datetime) -> float:
    latest = _parse_time(event.get("latest_seen_at"))
    if latest is None:
        return 0.0
    return max(0.0, (now - latest).total_seconds() / 3600.0)


def _keyword_fraction(text: str, words: set[str]) -> float:
    found = len(_tokens(text) & words)
    return min(1.0, found / 3.0)


def _authority(event: Mapping[str, object]) -> float:
    source = event.get("canonical_source")
    if not isinstance(source, Mapping):
        return 0.25
    role = _text(source.get("role"))
    tier = _text(source.get("tier")).upper()
    return {
        "canonical": 1.0,
        "supporting": 0.62,
        "community": 0.36,
        "discussion": 0.22,
    }.get(role, {"S1": 0.9, "S2": 0.62, "S3": 0.45, "S4": 0.25}.get(tier, 0.25))


def calculate_importance_score(event: Mapping[str, object], *, now: datetime | str | None = None) -> tuple[float, dict[str, float]]:
    """Return a 0--100 importance score and its explainable components."""
    current = _now(now)
    text = f"{_text(event.get('title'))} {_text(event.get('topic'))}"
    age = _event_age_hours(event, current)
    impact = min(1.0, 0.35 + 0.45 * _keyword_fraction(text, _IMPACT_WORDS) + 0.20 * min(1.0, int(event.get("independent_source_count") or 0) / 4))
    authority = _authority(event)
    novelty = max(0.0, min(1.0, math.exp(-age / 168.0)))
    scope = min(1.0, 0.25 + 0.50 * _keyword_fraction(text, _SCOPE_WORDS) + 0.25 * min(1.0, int(event.get("independent_source_count") or 0) / 4))
    consequence = min(1.0, 0.28 + 0.62 * _keyword_fraction(text, _CONSEQUENCE_WORDS))
    components = {"impact": impact, "authority": authority, "novelty": novelty, "scope": scope, "consequence": consequence}
    score = 100.0 * (0.30 * impact + 0.20 * authority + 0.15 * novelty + 0.15 * scope + 0.20 * consequence)
    return round(max(0.0, min(100.0, score)), 4), components


def _platform_metric(item: Mapping[str, object], platform: str) -> float:
    engagement = engagement_from_raw(item.get("engagement") or item.get("raw_data") or {})
    if platform == "hn":
        return float(engagement.score + engagement.comments * 2)
    if platform == "reddit":
        return float(engagement.score + engagement.comments * 2)
    if platform == "x":
        return float(engagement.likes + engagement.reposts * 3 + engagement.replies * 2)
    if platform == "github":
        raw = _as_map(item.get("engagement"))
        return float(raw.get("stars") or raw.get("star_count") or engagement.weight)
    return float(engagement.weight)


def engagement_by_platform(event: Mapping[str, object]) -> dict[str, float]:
    """Aggregate engagement without counting repeated URLs on one platform."""
    platform_values: dict[str, list[float]] = defaultdict(list)
    for item in _event_items(event):
        sources = item.get("sources") if isinstance(item.get("sources"), Sequence) else []
        platforms = set()
        for source in sources or []:
            if isinstance(source, Mapping):
                platform = engagement_kind(
                    _text(source.get("source_id")),
                    _text(source.get("url") or source.get("source_url")),
                    _text(source.get("source_type") or source.get("type")),
                )
                if platform == "generic" and _text(source.get("source_type")).casefold() == "github":
                    platform = "github"
                platforms.add(platform)
        if not platforms:
            platforms.add(_text(item.get("platform")).casefold() or "generic")
        for platform in platforms:
            platform_values[platform].append(_platform_metric(item, platform))
    return {platform: max(values, default=0.0) for platform, values in platform_values.items()}


def _engagement_metrics_by_platform(event: Mapping[str, object]) -> dict[str, dict[str, float]]:
    metrics: dict[str, dict[str, float]] = defaultdict(dict)
    for item in _event_items(event):
        sources = item.get("sources") if isinstance(item.get("sources"), Sequence) else []
        platforms: set[str] = set()
        for source in sources or []:
            if isinstance(source, Mapping):
                platform = engagement_kind(
                    _text(source.get("source_id")),
                    _text(source.get("url") or source.get("source_url")),
                    _text(source.get("source_type") or source.get("type")),
                )
                if platform == "generic" and _text(source.get("source_type") or source.get("type")).casefold() == "github":
                    platform = "github"
                platforms.add(platform)
        if not platforms:
            platforms.add(_text(item.get("platform")).casefold() or "generic")
        engagement = engagement_from_raw(item.get("engagement") or item.get("raw_data") or {})
        raw = _as_map(item.get("engagement"))
        for platform in platforms:
            values = {
                "score": float(engagement.score),
                "comments": float(engagement.comments),
                "likes": float(engagement.likes),
                "reposts": float(engagement.reposts),
                "replies": float(engagement.replies),
                "engagement": _platform_metric(item, platform),
            }
            if platform == "github":
                values["stars"] = float(raw.get("stars") or raw.get("star_count") or engagement.weight)
            for metric, value in values.items():
                metrics[platform][metric] = max(metrics[platform].get(metric, 0.0), value)
    return {platform: dict(values) for platform, values in metrics.items()}


def _normalized_engagement(event: Mapping[str, object]) -> tuple[float, dict[str, float]]:
    current = engagement_by_platform(event)
    normalized = {
        platform: min(1.0, math.log1p(value) / math.log1p(_PLATFORM_BASELINES.get(platform, _PLATFORM_BASELINES["generic"]) * 10))
        for platform, value in current.items()
    }
    return (max(normalized.values(), default=0.0), normalized)


def _snapshot_momentum(event: Mapping[str, object], current_normalized: float, coverage: float, snapshots: Sequence[Mapping[str, object]], now: datetime) -> float:
    age_hours = _event_age_hours(event, now)
    baseline_momentum = min(1.0, (current_normalized + coverage) / math.sqrt(age_hours + 1.0))
    event_id = _text(event.get("event_id"))
    current_values = engagement_by_platform(event)
    current_metrics = _engagement_metrics_by_platform(event)
    history: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for row in snapshots:
        if _text(row.get("event_id")) != event_id:
            continue
        observed = _parse_time(row.get("observed_at"))
        if observed is None:
            continue
        platform = _text(row.get("platform")).casefold() or "generic"
        metric = _text(row.get("metric")).casefold() or "engagement"
        try:
            value = max(0.0, float(row.get("value") or 0))
        except (TypeError, ValueError):
            continue
        history[(platform, metric)].append((observed, value))
    changes: list[float] = []
    for (platform, _metric), values in history.items():
        values.sort()
        current = current_metrics.get(platform, {}).get(_metric, current_values.get(platform))
        if current is None or not values:
            continue
        prior_time, prior_value = values[-1]
        elapsed = max(1.0 / 60.0, (now - prior_time).total_seconds() / 3600.0)
        baseline = _PLATFORM_BASELINES.get(platform, _PLATFORM_BASELINES["generic"])
        changes.append(max(-1.0, min(1.0, (current - prior_value) / baseline)) / math.sqrt(elapsed))
    if changes:
        return max(0.0, min(1.0, baseline_momentum * 0.5 + max(changes) * 0.5 + 0.25))
    return baseline_momentum


def calculate_attention_score(
    event: Mapping[str, object],
    *,
    snapshots: Iterable[Mapping[str, object]] = (),
    now: datetime | str | None = None,
) -> tuple[float, dict[str, object]]:
    """Return a 0--100 attention score using current signals only."""
    current = _now(now)
    domains = {_text(value) for value in (event.get("source_domains") or []) if _text(value)}
    if not domains:
        domains = {_text(source.get("source_id")) for source in _event_sources(event) if _text(source.get("source_id"))}
    coverage = min(1.0, max(0, len(domains) - 1) / 4.0)
    normalized_engagement, platform_values = _normalized_engagement(event)
    categories = {_text(source.get("category")) for source in _event_sources(event) if _text(source.get("category"))}
    source_diversity = min(1.0, len(categories) / 4.0)
    freshness = math.exp(-_event_age_hours(event, current) / 72.0)
    snapshot_rows = [row for row in snapshots if isinstance(row, Mapping)]
    momentum = _snapshot_momentum(event, normalized_engagement, coverage, snapshot_rows, current)
    components: dict[str, object] = {
        "cross_source_coverage": round(coverage, 6),
        "normalized_engagement": round(normalized_engagement, 6),
        "normalized_engagement_by_platform": {key: round(value, 6) for key, value in platform_values.items()},
        "source_diversity": round(source_diversity, 6),
        "freshness": round(freshness, 6),
        "snapshot_momentum": round(momentum, 6),
    }
    score = 100.0 * (0.25 * coverage + 0.30 * normalized_engagement + 0.20 * source_diversity + 0.15 * freshness + 0.10 * momentum)
    return round(max(0.0, min(100.0, score)), 4), components


def build_evidence_pack(event: Mapping[str, object]) -> dict[str, object]:
    """Build a typed evidence payload; inferred signals never enter facts."""
    sources = _event_sources(event)
    canonical = event.get("canonical_source") if isinstance(event.get("canonical_source"), Mapping) else {}
    canonical_item_id = _text(event.get("canonical_item_id") or event.get("representative_item_id"))
    facts: list[dict[str, object]] = []
    inferences: list[dict[str, object]] = []
    uncertainty: list[dict[str, object]] = []
    for item in _event_items(event):
        item_id = _text(item.get("item_id"))
        title = _text(item.get("title"))
        if not title:
            continue
        roles = {_text(source.get("role")) for source in (item.get("sources") or []) if isinstance(source, Mapping)}
        if item_id == canonical_item_id or "canonical" in roles or "supporting" in roles:
            facts.append({"text": title, "status": "SUPPORTED", "item_id": item_id, "source_ids": list(item.get("source_ids") or [])})
        elif roles & {"community", "discussion"}:
            inferences.append({"text": title, "status": "INFERRED", "item_id": item_id, "source_ids": list(item.get("source_ids") or [])})
    if _text(canonical.get("role")) != "canonical":
        uncertainty.append({"text": "No official or first-party source was found; community signals are not confirmation.", "status": "UNSUPPORTED"})
    supported_facts = [fact for fact in facts if fact.get("status") == "SUPPORTED"]
    attention_signals: list[dict[str, object]] = []
    for platform, value in engagement_by_platform(event).items():
        attention_signals.append({"platform": platform, "metric": "engagement", "value": value, "status": "SUPPORTED"})
    pack = {
        "event_id": _text(event.get("event_id")),
        "event": {
            "title": _text(event.get("title")),
            "first_seen_at": _text(event.get("first_seen_at")),
            "latest_seen_at": _text(event.get("latest_seen_at")),
            "entities": list(event.get("entities") or []),
        },
        "canonical_source": dict(canonical),
        "supported_facts": supported_facts,
        "facts": facts,
        "supporting_reports": [dict(source) for source in (event.get("supporting_sources") or [])],
        "attention_signals": attention_signals,
        "uncertainty": uncertainty,
        "conflicts": [],
        "inference": inferences,
        "inferences": inferences,
        "fact_statuses": list(FACT_STATUSES),
    }
    return pack


def score_events(
    events: Iterable[Mapping[str, object]],
    *,
    snapshots: Iterable[Mapping[str, object]] = (),
    now: datetime | str | None = None,
) -> list[dict[str, object]]:
    current = _now(now)
    snapshot_rows = [row for row in snapshots if isinstance(row, Mapping)]
    scored: list[dict[str, object]] = []
    for raw_event in events:
        event = dict(raw_event)
        importance, importance_breakdown = calculate_importance_score(event, now=current)
        attention, attention_breakdown = calculate_attention_score(event, snapshots=snapshot_rows, now=current)
        priority = round(0.55 * importance + 0.45 * attention, 4)
        event.update(
            {
                "importance_score": importance,
                "attention_score": attention,
                "priority_score": priority,
                "engagement_by_platform": engagement_by_platform(event),
                "importance_breakdown": importance_breakdown,
                "attention_breakdown": attention_breakdown,
                "high_importance": importance >= 70.0,
                "high_attention": attention >= 70.0,
                "selection_reasons": [
                    reason for reason, enabled in (("high_importance", importance >= 70.0), ("high_attention", attention >= 70.0)) if enabled
                ],
            }
        )
        event["evidence_pack"] = build_evidence_pack(event)
        scored.append(event)
    scored.sort(key=lambda event: (float(event.get("priority_score") or 0), float(event.get("importance_score") or 0), _text(event.get("latest_seen_at"))), reverse=True)
    for index, event in enumerate(scored, start=1):
        event["rank"] = index
    return scored


def rank_events(
    candidates: Iterable[Mapping[str, object]],
    *,
    snapshots: Iterable[Mapping[str, object]] = (),
    now: datetime | str | None = None,
) -> list[dict[str, object]]:
    return score_events(cluster_candidates(candidates), snapshots=snapshots, now=now)


def build_radar(
    candidates: Iterable[Mapping[str, object]],
    *,
    snapshots: Iterable[Mapping[str, object]] = (),
    now: datetime | str | None = None,
) -> list[dict[str, object]]:
    """Run the complete in-memory on-demand radar pipeline."""
    return rank_events(candidates, snapshots=snapshots, now=now)


class EventScoringService:
    """Small façade useful to an explicit collect command or future API."""

    def cluster(self, candidates: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
        return cluster_candidates(candidates)

    def score(
        self,
        events: Iterable[Mapping[str, object]],
        *,
        snapshots: Iterable[Mapping[str, object]] = (),
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        return score_events(events, snapshots=snapshots, now=now)

    def rank(
        self,
        candidates: Iterable[Mapping[str, object]],
        *,
        snapshots: Iterable[Mapping[str, object]] = (),
        now: datetime | str | None = None,
    ) -> list[dict[str, object]]:
        return build_radar(candidates, snapshots=snapshots, now=now)


NewsRadar = EventScoringService


__all__ = [
    "FACT_STATUSES",
    "EventScoringService",
    "NewsRadar",
    "build_evidence_pack",
    "build_radar",
    "calculate_attention_score",
    "calculate_importance_score",
    "cluster_candidates",
    "cluster_events",
    "engagement_by_platform",
    "rank_events",
    "score_events",
]
