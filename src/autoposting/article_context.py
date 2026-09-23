"""Article generation input contract.

The single official research capability is ``Research Service -> Topic
Dossier``.  Article Service receives an already-finished research context and
only assembles facts, sources, and writing material before calling the article
model.  It never starts a second research run: no hook hunt, no related search,
no topic expansion of its own.

``ArticleGenerationContext`` is a runtime contract.  It is deliberately not a
database table: a dossier is already persisted, and the context is rebuilt from
it on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit

ARTICLE_CONTEXT_VERSION = "article-generation-context-v1"

SOURCE_TYPE_TOPIC_DOSSIER = "topic_dossier"
SOURCE_TYPE_RESOURCE_SET = "resource_set"
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPES = frozenset(
    {SOURCE_TYPE_TOPIC_DOSSIER, SOURCE_TYPE_RESOURCE_SET, SOURCE_TYPE_MANUAL}
)

DOSSIER_SCHEMA_VERSION = "topic-dossier-v1"

MAX_MATERIAL_CHARS = 20_000
MAX_SUMMARY_CHARS = 4_000
MAX_EVIDENCE_ITEMS = 100
MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 1_000
MAX_URL_CHARS = 4_000


_REASONS = {
    "dossier_id_required": "缺少 Topic Dossier 标识",
    "dossier_not_found": "找不到该 Topic Dossier",
    "dossier_invalid": "Topic Dossier 结构无效，请重新研究",
    "dossier_evidence_required": "Topic Dossier 没有可用证据，无法直接生成文章，请重新研究",
    "dossier_evidence_invalid": "Topic Dossier 证据字段无效，请重新研究",
    "dossier_sources_required": "Topic Dossier 没有可引用的官方来源，无法生成带来源索引的文章",
    "context_source_type_invalid": "文章上下文来源类型无效",
    "context_subject_required": "文章上下文缺少主题",
    "context_material_required": "文章上下文没有可写素材",
}


class ArticleContextError(ValueError):
    """Stable, user-facing failure for an unusable article context.

    ``code`` is the machine-readable reason; ``reason`` is the message shown in
    the UI.  Both are stable so tests can assert exact behaviour.
    """

    def __init__(self, code: str, reason: str | None = None) -> None:
        self.code = str(code)
        self.reason = str(reason or _REASONS.get(self.code, "研究上下文不可用"))
        super().__init__(self.code)

    def to_payload(self) -> dict[str, str]:
        return {"error": self.code, "reason": self.reason}


def _url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_URL_CHARS:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def _clean_list(value: object, *, limit: int = MAX_EVIDENCE_ITEMS) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _positional_list(value: object, *, limit: int = MAX_EVIDENCE_ITEMS) -> list[str]:
    """Like :func:`_clean_list` but never reorders or drops a duplicate.

    ``source_urls`` is positional: entry *i* belongs to ``source_ids[i]``.  Two
    sources legitimately share one url (the same story on two feeds), so
    de-duplicating the url list would silently shift every later url onto the
    wrong source.  One slot per input entry, always.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item or "").strip() for item in value[:limit]]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _evidence_decision(item: Mapping[str, object]) -> str:
    assessment = _mapping(item.get("assessment"))
    decision = assessment.get("decision")
    if isinstance(decision, str) and decision.strip():
        return decision.strip()
    return ""


def _evidence_row(item: object, *, index: int) -> dict[str, object]:
    """Normalize one dossier evidence entry into the context shape.

    Every field the article writer may cite has to survive this boundary, and a
    structurally broken entry fails loudly instead of silently disappearing.
    """
    data = _mapping(item)
    if not data:
        raise ArticleContextError("dossier_evidence_invalid")
    evidence_id = str(data.get("evidence_id") or "").strip()
    title = str(data.get("title") or "").strip()
    url = _url(data.get("url"))
    if not evidence_id or not title or not url:
        raise ArticleContextError("dossier_evidence_invalid")
    relation = str(data.get("relation") or "topic_only").strip() or "topic_only"
    reader_status = str(data.get("reader_status") or "unread").strip() or "unread"
    content = str(data.get("content") or "").strip()
    snippet = str(data.get("snippet") or "").strip()
    source = str(data.get("source") or "search").strip() or "search"
    source_scope = str(data.get("source_scope") or "evidence").strip() or "evidence"
    quality = data.get("reader_quality")
    if isinstance(quality, bool) or not isinstance(quality, (int, float)):
        quality = None
    return {
        "rank": index,
        "evidence_id": evidence_id[:400],
        "title": title[:MAX_TITLE_CHARS],
        "url": url,
        "snippet": snippet[:2_000],
        "source": source[:120],
        "source_scope": source_scope[:80],
        "relation": relation[:80],
        "reader_status": reader_status[:80],
        "reader_quality": quality,
        "decision": _evidence_decision(data),
        "content": content[:MAX_TEXT_CHARS],
    }


def _topic_rows(dossier: Mapping[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    topics = dossier.get("topics")
    if not isinstance(topics, (list, tuple)):
        return rows
    for topic in topics:
        data = _mapping(topic)
        title = str(data.get("title") or "").strip()
        if not title:
            continue
        rows.append(
            {
                "event_id": str(data.get("event_id") or "").strip(),
                "title": title[:MAX_TITLE_CHARS],
                "topic": str(data.get("topic") or "").strip()[:200],
                "source_ids": _clean_list(data.get("source_ids")),
                # Positional: one slot per id, so the pairing survives duplicate
                # urls and the ids are never shifted onto a neighbour's url.
                "source_urls": [
                    url
                    for url in (_url(item) for item in _positional_list(data.get("source_urls")))
                ],
                "item_ids": _clean_list(data.get("item_ids")),
            }
        )
    return rows


def _source_refs_from_topics(topics: list[dict[str, object]]) -> list[dict[str, object]]:
    """Derive legal citation refs from the dossier's own event sources.

    ``source_ids`` and ``source_urls`` are positional: index *i* of one belongs
    to index *i* of the other.  A short url list is therefore never zipped onto
    a longer id list — that would hand one source's url to another — and the
    source simply carries no url.  Evidence URLs are *not* subscription sources
    and never become citation ids; they travel in the research context instead.
    """
    refs: list[dict[str, object]] = []
    seen: set[str] = set()
    for topic in topics:
        source_ids = list(topic.get("source_ids") or ())
        source_urls = list(topic.get("source_urls") or ())
        aligned = len(source_urls) == len(source_ids)
        for index, source_id in enumerate(source_ids):
            url = source_urls[index] if aligned else ""
            key = f"{source_id}|{url}"
            if key in seen:
                continue
            seen.add(key)
            refs.append({"source_id": source_id, "source_url": url, "event_id": topic.get("event_id") or ""})
    return refs


def _evidence_block(index: int, row: Mapping[str, object]) -> str:
    """Render one evidence entry as prompt material the writer can quote."""
    lines = [f"[{index}] {row.get('title') or row.get('url')}"]
    lines.append(f"来源：{row.get('source')} · 关系：{row.get('relation')} · 阅读状态：{row.get('reader_status')}")
    if row.get("decision"):
        lines.append(f"判断：{row.get('decision')}")
    lines.append(f"链接：{row.get('url')}")
    body = str(row.get("content") or "").strip() or str(row.get("snippet") or "").strip()
    if body:
        lines.append(body)
    return "\n".join(lines)


@dataclass(frozen=True)
class ArticleGenerationContext:
    """Everything the article writer is allowed to consume."""

    source_type: str
    primary_subject: str
    research_summary: str = ""
    evidence: tuple[Mapping[str, object], ...] = ()
    source_refs: tuple[Mapping[str, object], ...] = ()
    selected_images: tuple[Mapping[str, object], ...] = ()
    resource_refs: tuple[Mapping[str, object], ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    material: str = ""
    background: Mapping[str, object] = field(default_factory=dict)
    resource_set_id: str = ""

    def __post_init__(self) -> None:
        if self.source_type not in SOURCE_TYPES:
            raise ArticleContextError("context_source_type_invalid")
        if not str(self.primary_subject or "").strip():
            raise ArticleContextError("context_subject_required")
        if not str(self.material or "").strip():
            raise ArticleContextError("context_material_required")

    @property
    def evidence_count(self) -> int:
        return len(self.evidence)

    @property
    def dossier_id(self) -> str:
        value = self.metadata.get("dossier_id")
        return str(value or "")

    @property
    def source_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for ref in self.source_refs:
            source_id = str(_mapping(ref).get("source_id") or "").strip()
            if source_id and source_id not in seen:
                seen.append(source_id)
        return tuple(seen)

    @property
    def item_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for topic in self.metadata.get("topics") or ():
            for item_id in _mapping(topic).get("item_ids") or ():
                text = str(item_id or "").strip()
                if text and text not in seen:
                    seen.append(text)
        return tuple(seen)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe projection; the article prompt consumes this verbatim."""
        payload: dict[str, object] = {
            "schema_version": ARTICLE_CONTEXT_VERSION,
            "source_type": self.source_type,
            "primary_subject": self.primary_subject,
            "research_summary": self.research_summary,
            "evidence": [dict(item) for item in self.evidence],
            "source_refs": [dict(item) for item in self.source_refs],
            "selected_images": [dict(item) for item in self.selected_images],
            "resource_refs": [dict(item) for item in self.resource_refs],
            "metadata": dict(self.metadata),
        }
        if self.resource_set_id:
            payload["resource_set_id"] = self.resource_set_id
        return payload

    def prompt_background(self) -> dict[str, object]:
        """Background payload handed to ``LLMService.generate_article``."""
        return dict(self.background)


def _with_article_context(
    context: ArticleGenerationContext, background: Mapping[str, object]
) -> ArticleGenerationContext:
    """Return a copy whose background carries the serialized context itself."""
    payload = dict(background)
    payload["article_context"] = context.to_dict()
    return ArticleGenerationContext(
        source_type=context.source_type,
        primary_subject=context.primary_subject,
        research_summary=context.research_summary,
        evidence=context.evidence,
        source_refs=context.source_refs,
        selected_images=context.selected_images,
        resource_refs=context.resource_refs,
        metadata=context.metadata,
        material=context.material,
        background=payload,
        resource_set_id=context.resource_set_id,
    )


def _summary_from(
    dossier: Mapping[str, object],
    topics: list[dict[str, object]],
    evidence: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    title = str(dossier.get("title") or "").strip()
    if title:
        lines.append(f"主题：{title}")
    query = str(dossier.get("query") or "").strip()
    if query:
        lines.append(f"检索主题：{query}")
    for topic in topics:
        label = topic.get("topic") or "事件"
        lines.append(f"事件簇（{label}）：{topic.get('title')}")
    for row in evidence:
        detail = str(row.get("snippet") or row.get("content") or "").strip()
        detail = " ".join(detail.split())[:200]
        if detail:
            lines.append(f"证据：{row.get('title')} — {detail}")
        else:
            lines.append(f"证据：{row.get('title')}")
    summary = "\n".join(lines).strip()
    return summary[:MAX_SUMMARY_CHARS]


def build_dossier_context(
    dossier: object,
    *,
    selected_images: object = None,
) -> ArticleGenerationContext:
    """Map a persisted Topic Dossier onto the article input contract.

    Raises :class:`ArticleContextError` — never falls back to searching — when
    the dossier is missing, malformed, or carries no usable evidence.
    """
    data = _mapping(dossier)
    if not data:
        raise ArticleContextError("dossier_invalid")
    schema = data.get("schema_version")
    if schema is not None and schema != DOSSIER_SCHEMA_VERSION:
        raise ArticleContextError("dossier_invalid")
    dossier_id = str(data.get("dossier_id") or "").strip()
    if not dossier_id:
        raise ArticleContextError("dossier_invalid")
    title = str(data.get("title") or "").strip()
    if not title:
        raise ArticleContextError("dossier_invalid")
    event_ids = _clean_list(data.get("event_ids"))
    if not event_ids:
        raise ArticleContextError("dossier_invalid")

    raw_evidence = data.get("evidence")
    if not isinstance(raw_evidence, (list, tuple)):
        raise ArticleContextError("dossier_evidence_invalid")
    evidence = [
        _evidence_row(item, index=index)
        for index, item in enumerate(raw_evidence[:MAX_EVIDENCE_ITEMS])
    ]
    if not evidence:
        raise ArticleContextError("dossier_evidence_required")

    topics = _topic_rows(data)
    source_refs = _source_refs_from_topics(topics)
    if not source_refs:
        raise ArticleContextError("dossier_sources_required")
    summary = _summary_from(data, topics, evidence)
    images: list[dict[str, object]] = []
    for item in selected_images if isinstance(selected_images, (list, tuple)) else ():
        row = _mapping(item)
        url = _url(row.get("url"))
        if url:
            images.append({"url": url, "kind": str(row.get("kind") or "cover")})

    blocks = [f"# {title}", f"研究检索主题：{data.get('query') or title}", ""]
    for topic in topics:
        blocks.append(f"## 事件簇：{topic.get('title')}")
        if topic.get("topic"):
            blocks.append(f"话题：{topic.get('topic')}")
        blocks.append("")
    blocks.append("## 研究证据（Topic Dossier）")
    for index, row in enumerate(evidence, start=1):
        blocks.append(_evidence_block(index, row))
        blocks.append("")
    material = "\n".join(blocks).strip()[:MAX_MATERIAL_CHARS]
    if not material:
        raise ArticleContextError("context_material_required")

    plain_evidence = [
        {
            key: row[key]
            for key in (
                "evidence_id",
                "title",
                "url",
                "snippet",
                "source",
                "source_scope",
                "relation",
                "reader_status",
                "reader_quality",
                "decision",
            )
        }
        for row in evidence
    ]
    context = ArticleGenerationContext(
        source_type=SOURCE_TYPE_TOPIC_DOSSIER,
        primary_subject=title,
        research_summary=summary,
        evidence=tuple(plain_evidence),
        source_refs=tuple(source_refs),
        selected_images=tuple(images),
        resource_refs=(),
        metadata={
            "dossier_id": dossier_id,
            "event_ids": event_ids,
            "topics": topics,
            "status": str(data.get("status") or ""),
            "degraded": bool(data.get("degraded")),
            "query": str(data.get("query") or ""),
            "created_at": str(data.get("created_at") or ""),
        },
        material=material,
        background={
            "focus": title,
            "query": str(data.get("query") or ""),
            "research_context": {
                "dossier_id": dossier_id,
                "title": title,
                "query": str(data.get("query") or ""),
                "status": str(data.get("status") or ""),
                "degraded": bool(data.get("degraded")),
                "research_summary": summary,
                "topics": topics,
                "evidence": plain_evidence,
            },
            "source_excerpt": material,
        },
    )
    return _with_article_context(context, context.background)


def build_manual_context(
    *,
    primary_subject: str,
    material: str,
    research_summary: str = "",
    source_refs: object = None,
    background: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
    selected_images: object = None,
) -> ArticleGenerationContext:
    """Wrap the retained manual/legacy item path in the same contract.

    Manual creation stays available: not every article has to come from a Topic
    Dossier.  This builder adds no research of its own.
    """
    subject = str(primary_subject or "").strip()
    if not subject:
        raise ArticleContextError("context_subject_required")
    text = str(material or "").strip()
    if not text:
        raise ArticleContextError("context_material_required")
    refs: list[dict[str, object]] = []
    for item in source_refs if isinstance(source_refs, (list, tuple)) else ():
        row = _mapping(item)
        source_id = str(row.get("source_id") or "").strip()
        if not source_id:
            continue
        refs.append(
            {
                "source_id": source_id,
                "source_url": _url(row.get("source_url")),
                "citation_id": str(row.get("citation_id") or ""),
            }
        )
    images: list[dict[str, object]] = []
    for item in selected_images if isinstance(selected_images, (list, tuple)) else ():
        row = _mapping(item)
        url = _url(row.get("url"))
        if url:
            images.append({"url": url, "kind": str(row.get("kind") or "cover")})
    base_background = dict(background or {})
    # The prompt reads background.focus; a caller that did not restate the
    # subject must not end up with a background block that has no subject.
    base_background.setdefault("focus", subject)
    context = ArticleGenerationContext(
        source_type=SOURCE_TYPE_MANUAL,
        primary_subject=subject,
        research_summary=str(research_summary or "")[:MAX_SUMMARY_CHARS],
        evidence=(),
        source_refs=tuple(refs),
        selected_images=tuple(images),
        resource_refs=(),
        metadata=dict(metadata or {}),
        material=text[:MAX_MATERIAL_CHARS],
        background=base_background,
    )
    return _with_article_context(context, base_background)


def build_resource_set_context(
    resource_set: object,
    *,
    primary_subject: str = "",
    material: str = "",
) -> ArticleGenerationContext:
    """Minimal ResourceSet adapter; Resource Discovery keeps its own lane.

    This is a thin mapping only — it adds no product capability and no search of
    its own.
    """
    data = _mapping(resource_set)
    if not data:
        raise ArticleContextError("dossier_invalid")
    set_id = str(data.get("set_id") or data.get("resource_set_id") or "").strip()
    if not set_id:
        raise ArticleContextError("dossier_invalid")
    candidates = data.get("candidates")
    if not isinstance(candidates, (list, tuple)):
        candidates = []
    refs: list[dict[str, object]] = []
    for item in candidates[:MAX_EVIDENCE_ITEMS]:
        row = _mapping(item)
        url = _url(row.get("url") or row.get("html_url"))
        if not url:
            continue
        refs.append(
            {
                "name": str(row.get("name") or row.get("title") or url)[:MAX_TITLE_CHARS],
                "url": url,
                "kind": str(row.get("kind") or "")[:80],
                "source": str(row.get("source") or "")[:120],
            }
        )
    subject = str(primary_subject or data.get("query") or set_id).strip()
    text = str(material or "").strip()
    if not text:
        lines = [f"# {subject}", f"资源集：{set_id}", "## 候选资源"]
        for index, ref in enumerate(refs, start=1):
            lines.append(f"[{index}] {ref['name']} — {ref['url']}")
            if ref.get("source"):
                lines.append(f"来源：{ref['source']} · 类型：{ref.get('kind') or 'other'}")
        text = "\n".join(lines)
    context = ArticleGenerationContext(
        source_type=SOURCE_TYPE_RESOURCE_SET,
        primary_subject=subject,
        research_summary=str(data.get("summary") or "")[:MAX_SUMMARY_CHARS],
        evidence=(),
        source_refs=(),
        selected_images=(),
        resource_refs=tuple(refs),
        metadata={"resource_set_id": set_id},
        material=text[:MAX_MATERIAL_CHARS],
        background={
            "focus": subject,
            "resource_context": {"resource_set_id": set_id, "candidates": refs},
        },
        resource_set_id=set_id,
    )
    return _with_article_context(context, context.background)