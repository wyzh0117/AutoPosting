from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .engagement import (
    better_tier,
    effective_tier,
    engagement_from_raw,
    engagement_kind,
)
from .models import FeedItem, Source
from .normalize import content_hash


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap')),
    url_or_handle TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
    topics_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
    last_fetched_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    external_id TEXT,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    error_message TEXT,
    UNIQUE (source_id, external_id),
    UNIQUE (canonical_url),
    UNIQUE (content_hash)
);

CREATE TABLE IF NOT EXISTS item_sources (
    item_id TEXT NOT NULL REFERENCES items(item_id),
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    source_external_id TEXT,
    source_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY (item_id, source_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    current_count INTEGER NOT NULL,
    independent_sources INTEGER NOT NULL,
    baseline_mean REAL NOT NULL,
    baseline_stddev REAL NOT NULL,
    statistical_threshold REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    fact_list_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'NEW',
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    text TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    traceable INTEGER NOT NULL,
    exceeds_evidence INTEGER NOT NULL,
    verbatim_quote_chars INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT,
    processing_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    platform TEXT NOT NULL CHECK (platform IN ('wechat', 'xiaohongshu')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    assets_json TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    generator TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFTED',
    validation_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (event_id, platform, processing_version)
);

CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    platform TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_attempts (
    attempt_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    platform TEXT NOT NULL,
    dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    idempotency_key TEXT NOT NULL UNIQUE,
    retry_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_reference TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_endpoints (
    endpoint_id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    models_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_endpoint_purposes (
    purpose TEXT PRIMARY KEY CHECK (purpose IN ('article', 'chat')),
    endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES drafts(draft_id),
    platform TEXT NOT NULL,
    live INTEGER NOT NULL CHECK (live IN (0, 1)),
    run_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED', 'CANCELLED')),
    error_message TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_item_raw(connection: sqlite3.Connection, item_id: str, patch: dict) -> None:
    row = connection.execute(
        "SELECT raw_json FROM items WHERE item_id = ?", (item_id,)
    ).fetchone()
    current: dict[str, object] = {}
    if row is not None:
        try:
            parsed = json.loads(str(row["raw_json"] or "{}"))
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            current = parsed
    for key, value in patch.items():
        if value is not None:
            current[key] = value
    connection.execute(
        "UPDATE items SET raw_json = ? WHERE item_id = ?",
        (json.dumps(current, ensure_ascii=False, sort_keys=True), item_id),
    )


def model_endpoint_id(base_url: str) -> str:
    """Stable endpoint id derived from a normalized base_url."""
    normalized = base_url.strip().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_sitemap_source_type(connection)

    @staticmethod
    def _ensure_sitemap_source_type(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "sitemap" in sql:
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE sources_new (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap')),
                url_or_handle TEXT NOT NULL,
                tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
                topics_json TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
                last_fetched_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO sources_new SELECT * FROM sources;
            DROP TABLE sources;
            ALTER TABLE sources_new RENAME TO sources;
            """
        )
        connection.execute("PRAGMA foreign_keys = ON")

    def upsert_source(self, source: Source) -> None:
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, name, type, url_or_handle, tier, topics_json,
                    enabled, fetch_interval_minutes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    url_or_handle = excluded.url_or_handle,
                    tier = excluded.tier,
                    topics_json = excluded.topics_json,
                    enabled = excluded.enabled,
                    fetch_interval_minutes = excluded.fetch_interval_minutes,
                    updated_at = excluded.updated_at
                """,
                (
                    source.source_id,
                    source.name,
                    source.type,
                    source.url_or_handle,
                    source.tier,
                    json.dumps(source.topics, ensure_ascii=False),
                    int(source.enabled),
                    source.fetch_interval_minutes,
                    now,
                    now,
                ),
            )

    def get_source(self, source_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return dict(row)

    def insert_item(self, source: Source, item: FeedItem) -> bool:
        digest = content_hash(item.title, item.content_text)
        stable_key = item.external_id or item.canonical_url or digest
        item_id = hashlib.sha256(f"{source.source_id}:{stable_key}".encode()).hexdigest()[:24]
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO items (
                    item_id, source_id, external_id, canonical_url, title,
                    content_text, content_hash, published_at, fetched_at,
                    topic, raw_json, processing_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    source.source_id,
                    item.external_id,
                    item.canonical_url,
                    item.title,
                    item.content_text,
                    digest,
                    item.published_at,
                    _utc_now(),
                    source.topics[0] if source.topics else "unclassified",
                    json.dumps(item.raw_data, ensure_ascii=False, sort_keys=True),
                    "normalize-v1",
                ),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                row = connection.execute(
                    """
                    SELECT item_id FROM items
                    WHERE canonical_url = ? OR content_hash = ?
                       OR (source_id = ? AND external_id = ?)
                    LIMIT 1
                    """,
                    (item.canonical_url, digest, source.source_id, item.external_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Duplicate item could not be resolved")
                item_id = str(row["item_id"])
            if item.raw_data:
                _merge_item_raw(connection, item_id, item.raw_data)
            connection.execute(
                """
                INSERT OR IGNORE INTO item_sources (
                    item_id, source_id, source_external_id, source_url, discovered_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, source.source_id, item.external_id, item.canonical_url, _utc_now()),
            )
            return inserted

    def record_fetch_result(self, source_id: str, error: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE sources SET last_fetched_at = ?, last_error = ?, updated_at = ? WHERE source_id = ?",
                (_utc_now(), error, _utc_now(), source_id),
            )

    def count_items(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM items").fetchone()
        return int(row["count"])

    def cache_get(self, cache_key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT response_json FROM llm_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return str(row["response_json"]) if row is not None else None

    def cache_set(
        self,
        cache_key: str,
        task: str,
        platform: str,
        prompt_version: str,
        content_hash: str,
        response_json: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO llm_cache (
                    cache_key, task, platform, prompt_version, content_hash,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cache_key, task, platform, prompt_version, content_hash, response_json, _utc_now()),
            )

    def get_item_source_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT DISTINCT source_id FROM item_sources").fetchall()
        return {str(row["source_id"]) for row in rows}

    def list_recent_items(self, since: str) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT item_id, source_id, canonical_url, title, content_text,
                       published_at, fetched_at, topic, status
                FROM items
                WHERE COALESCE(published_at, fetched_at) >= ?
                ORDER BY COALESCE(published_at, fetched_at) DESC
                """,
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_event(self, name: str, topic: str, trigger_type: str, stable_key: str) -> str:
        event_id = hashlib.sha256(stable_key.encode()).hexdigest()[:24]
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, topic, name, trigger_type, window_start,
                    triggered_at, current_count, independent_sources,
                    baseline_mean, baseline_stddev, statistical_threshold,
                    evidence_json, rule_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '{}', 'manual-v1', ?)
                """,
                (event_id, topic, name, trigger_type, now, now, now),
            )
        return event_id

    def get_event(self, event_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return dict(row)

    def update_event_status(
        self, event_id: str, status: str, error_message: str | None = None
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET status = ?, error_message = ? WHERE event_id = ?",
                (status, error_message, event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(event_id)

    def registered_source_ids(self, source_ids: set[str]) -> set[str]:
        if not source_ids:
            return set()
        placeholders = ",".join("?" for _ in source_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT source_id FROM sources WHERE source_id IN ({placeholders})",
                tuple(sorted(source_ids)),
            ).fetchall()
        return {str(row["source_id"]) for row in rows}

    def replace_claims(
        self,
        event_id: str,
        facts: object,
        validation: object,
        fact_list_json: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM claims WHERE event_id = ?", (event_id,))
            connection.execute(
                "UPDATE events SET fact_list_json = ? WHERE event_id = ?",
                (fact_list_json, event_id),
            )
            for index, claim in enumerate(facts.claims):
                claim_id = hashlib.sha256(f"{event_id}:{index}:{claim.text}".encode()).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT INTO claims (
                        claim_id, event_id, text, source_ids_json, confidence,
                        traceable, exceeds_evidence, verbatim_quote_chars,
                        status, error_message, processing_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        event_id,
                        claim.text,
                        json.dumps(claim.source_ids, ensure_ascii=False),
                        claim.confidence,
                        int(claim.traceable),
                        int(claim.exceeds_evidence),
                        claim.verbatim_quote_chars,
                        "VERIFIED" if validation.valid else "FAILED",
                        None if validation.valid else ",".join(validation.errors),
                        "facts-v1",
                        _utc_now(),
                    ),
                )

    def count_drafts(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM drafts").fetchone()
        return int(row["count"])

    def get_draft(self, draft_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return dict(row)

    def is_current_draft(self, draft_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT draft_id FROM drafts
                WHERE event_id = (SELECT event_id FROM drafts WHERE draft_id = ?)
                  AND platform = (SELECT platform FROM drafts WHERE draft_id = ?)
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (draft_id, draft_id),
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return str(row["draft_id"]) == draft_id

    def upsert_draft(self, draft: object) -> str:
        """Insert or update a draft; returns 'inserted', 'unchanged' or 'changed'."""
        now = _utc_now()
        digest = hashlib.sha256(f"{draft.title}\n{draft.body}".encode("utf-8")).hexdigest()
        validation = {
            "source_ids_present": bool(draft.source_ids),
            "aigc_disclosure_present": "AIGC" in draft.body,
        }
        tags_json = json.dumps(draft.tags, ensure_ascii=False)
        source_ids_json = json.dumps(draft.source_ids, ensure_ascii=False)
        assets_json = json.dumps(getattr(draft, "assets", ()), ensure_ascii=False)
        validation_json = json.dumps(validation, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT content_hash FROM drafts
                WHERE event_id = ? AND platform = ? AND processing_version = ?
                """,
                (draft.event_id, draft.platform, draft.processing_version),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO drafts (
                        draft_id, event_id, platform, title, body, tags_json,
                        assets_json, source_ids_json, generator, processing_version,
                        status, validation_json, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFTED', ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        draft.event_id,
                        draft.platform,
                        draft.title,
                        draft.body,
                        tags_json,
                        assets_json,
                        source_ids_json,
                        draft.generator,
                        draft.processing_version,
                        validation_json,
                        digest,
                        now,
                        now,
                    ),
                )
                outcome = "inserted"
            elif str(existing["content_hash"]) == digest:
                connection.execute(
                    """
                    UPDATE drafts
                    SET title = ?, body = ?, tags_json = ?, assets_json = ?, source_ids_json = ?,
                        generator = ?, validation_json = ?, updated_at = ?
                    WHERE event_id = ? AND platform = ? AND processing_version = ?
                    """,
                    (
                        draft.title,
                        draft.body,
                        tags_json,
                        assets_json,
                        source_ids_json,
                        draft.generator,
                        validation_json,
                        now,
                        draft.event_id,
                        draft.platform,
                        draft.processing_version,
                    ),
                )
                outcome = "unchanged"
            else:
                connection.execute(
                    """
                    UPDATE drafts
                    SET title = ?, body = ?, tags_json = ?, assets_json = ?, source_ids_json = ?,
                        generator = ?, validation_json = ?, content_hash = ?,
                        status = 'DRAFTED', approved_by = NULL, approved_at = NULL,
                        reviewed_by = NULL, reviewed_at = NULL, review_note = NULL,
                        updated_at = ?
                    WHERE event_id = ? AND platform = ? AND processing_version = ?
                    """,
                    (
                        draft.title,
                        draft.body,
                        tags_json,
                        assets_json,
                        source_ids_json,
                        draft.generator,
                        validation_json,
                        digest,
                        now,
                        draft.event_id,
                        draft.platform,
                        draft.processing_version,
                    ),
                )
                outcome = "changed"
        return outcome

    def approve_draft(self, draft_id: str, approved_by: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT event_id, status FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise KeyError(draft_id)
            if row["status"] not in {"DRAFTED", "APPROVED"}:
                raise ValueError(f"Draft cannot be approved from status {row['status']}")
            connection.execute(
                """
                UPDATE drafts
                SET status = 'APPROVED', approved_by = ?, approved_at = ?, updated_at = ?
                WHERE draft_id = ?
                """,
                (approved_by, _utc_now(), _utc_now(), draft_id),
            )
        return str(row["event_id"])

    def reject_draft(self, draft_id: str, reviewed_by: str, review_note: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT event_id, status FROM drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise KeyError(draft_id)
            if row["status"] != "DRAFTED":
                raise ValueError(f"Draft cannot be rejected from status {row['status']}")
            connection.execute(
                """
                UPDATE drafts
                SET status = 'REJECTED', reviewed_by = ?, reviewed_at = ?,
                    review_note = ?, updated_at = ?
                WHERE draft_id = ?
                """,
                (reviewed_by, _utc_now(), review_note, _utc_now(), draft_id),
            )
        return str(row["event_id"])

    def event_has_all_platforms_approved(
        self, event_id: str, processing_version: str
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                       COUNT(*) AS total
                FROM drafts WHERE event_id = ? AND processing_version = ?
                """,
                (event_id, processing_version),
            ).fetchone()
        total = int(row["total"] or 0)
        return total >= 1 and int(row["approved"] or 0) == total

    def source_tiers(self, source_ids: set[str]) -> dict[str, str]:
        if not source_ids:
            return {}
        placeholders = ", ".join("?" for _ in source_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT source_id, tier FROM sources WHERE source_id IN ({placeholders})",
                tuple(sorted(source_ids)),
            ).fetchall()
        return {str(row["source_id"]): str(row["tier"]) for row in rows}

    def find_item_id_by_title(self, title: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT item_id FROM items
                WHERE title = ?
                ORDER BY fetched_at DESC, rowid DESC
                LIMIT 1
                """,
                (title,),
            ).fetchone()
        return str(row["item_id"]) if row is not None else ""

    def get_item(self, item_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return dict(row)

    def get_item_sources(self, item_id: str) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, source_url FROM item_sources
                WHERE item_id = ? ORDER BY source_id
                """,
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sources(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, name, type, tier, enabled, fetch_interval_minutes,
                       last_fetched_at, last_error
                FROM sources
                ORDER BY name, source_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_hotspot_candidates(
        self, since: str, limit: int = 10, sort: str = "time", pool: int = 40
    ) -> list[dict[str, object]]:
        if sort not in {"time", "heat"}:
            raise ValueError("sort must be time or heat")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT item_id, title, content_text, published_at, fetched_at, raw_json
                FROM items
                WHERE COALESCE(published_at, fetched_at) >= ?
                ORDER BY COALESCE(published_at, fetched_at) DESC, item_id
                """,
                (since,),
            ).fetchall()
            item_ids = [str(row["item_id"]) for row in rows]
            sources_by_item: dict[str, list[tuple[str, str]]] = {}
            if item_ids:
                placeholders = ", ".join("?" for _ in item_ids)
                for row in connection.execute(
                    f"""
                    SELECT item_id, source_id, source_url FROM item_sources
                    WHERE item_id IN ({placeholders})
                    """,
                    item_ids,
                ):
                    sources_by_item.setdefault(str(row["item_id"]), []).append(
                        (str(row["source_id"]), str(row["source_url"]))
                    )
            source_meta: dict[str, tuple[str, str]] = {}
            all_source_ids = {
                source_id for pairs in sources_by_item.values() for source_id, _ in pairs
            }
            if all_source_ids:
                placeholders = ", ".join("?" for _ in all_source_ids)
                for row in connection.execute(
                    f"SELECT source_id, tier, type FROM sources WHERE source_id IN ({placeholders})",
                    tuple(all_source_ids),
                ):
                    source_meta[str(row["source_id"])] = (str(row["tier"]), str(row["type"]))
        from .ingestion import should_hide_candidate

        candidates: list[dict[str, object]] = []
        for row in rows:
            item_id = str(row["item_id"])
            pairs = sources_by_item.get(item_id, [])
            source_ids = [source_id for source_id, _ in pairs]
            if should_hide_candidate(str(row["title"]), source_ids):
                continue
            engagement = engagement_from_raw(row["raw_json"])
            base_tier = "S4"
            item_tier = "S4"
            kinds: list[str] = []
            for source_id, source_url in pairs:
                tier, source_type = source_meta.get(source_id, ("S4", "rss"))
                kind = engagement_kind(source_id, source_url, source_type)
                kinds.append(kind)
                base_tier = better_tier(base_tier, tier)
                item_tier = better_tier(item_tier, effective_tier(tier, engagement, kind))
            candidates.append(
                {
                    "item_id": item_id,
                    "title": str(row["title"]),
                    "content_text": str(row["content_text"]),
                    "observed_at": str(row["published_at"] or row["fetched_at"]),
                    "source_ids": [source_id for source_id, _ in pairs],
                    "source_urls": [source_url for _, source_url in pairs],
                    "engagement": engagement.as_public(),
                    "engagement_weight": engagement.weight,
                    "base_tier": base_tier,
                    "effective_tier": item_tier,
                    "promoted": item_tier != base_tier,
                    "discussion": base_tier == "S4" or any(kind == "x" for kind in kinds),
                }
            )
        candidates = candidates[: max(pool, limit)]
        if sort == "heat":
            candidates.sort(
                key=lambda candidate: (
                    len(candidate["source_ids"]),
                    int(candidate["engagement_weight"]),
                    candidate["observed_at"],
                ),
                reverse=True,
            )
        else:
            candidates.sort(
                key=lambda candidate: (
                    candidate["observed_at"],
                    len(candidate["source_ids"]),
                    int(candidate["engagement_weight"]),
                ),
                reverse=True,
            )
        return candidates[:limit]

    def publication_attempt_id(self, draft_id: str, digest: str, dry_run: bool) -> str:
        key = f"{draft_id}:{digest}:{int(dry_run)}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]

    def record_publication_attempt(
        self,
        attempt_id: str,
        draft_id: str,
        platform: str,
        dry_run: bool,
        status: str,
        error_message: str | None,
        retry_count: int,
        response_reference: str | None = None,
    ) -> None:
        now = _utc_now()
        request = {"draft_id": draft_id, "platform": platform, "dry_run": dry_run}
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO publication_attempts (
                    attempt_id, draft_id, platform, dry_run, idempotency_key,
                    retry_count, status, request_json, response_reference,
                    error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    retry_count = excluded.retry_count,
                    status = excluded.status,
                    response_reference = excluded.response_reference,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                """,
                (
                    attempt_id,
                    draft_id,
                    platform,
                    int(dry_run),
                    attempt_id,
                    retry_count,
                    status,
                    json.dumps(request, sort_keys=True),
                    response_reference,
                    error_message,
                    now,
                    now,
                ),
            )

    def get_publication_attempt(self, attempt_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM publication_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_latest_publication_attempt(self, draft_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM publication_attempts
                WHERE draft_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return dict(row)

    def count_publication_attempts(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM publication_attempts"
            ).fetchone()
        return int(row["count"])

    def record_hotspot(self, decision: object) -> str:
        if not getattr(decision, "triggered"):
            raise ValueError("Only triggered hotspot decisions can create events")
        stable_key = f"{decision.topic}:{decision.evaluated_at.isoformat()}"
        event_id = hashlib.sha256(stable_key.encode()).hexdigest()[:24]
        evidence = {
            "source_ids": decision.source_ids,
            "tiers": decision.tiers,
            "reasons": decision.reasons,
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, topic, name, trigger_type, window_start,
                    triggered_at, current_count, independent_sources,
                    baseline_mean, baseline_stddev, statistical_threshold,
                    evidence_json, rule_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    decision.topic,
                    decision.topic,
                    "HOTSPOT",
                    decision.window_start.isoformat(),
                    decision.evaluated_at.isoformat(),
                    decision.current_count,
                    decision.independent_sources,
                    decision.baseline_mean,
                    decision.baseline_stddev,
                    decision.statistical_threshold,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    decision.rule_version,
                    _utc_now(),
                ),
            )
        return event_id

    def count_events(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        return int(row["count"])

    def upsert_model_endpoint(
        self, endpoint_id: str, base_url: str, models: list[str] | None = None
    ) -> None:
        """Persist endpoint metadata; models=None keeps the stored list."""
        now = _utc_now()
        models_json = json.dumps(models, ensure_ascii=False) if models is not None else None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_endpoints (
                    endpoint_id, base_url, models_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(endpoint_id) DO UPDATE SET
                    models_json = COALESCE(?, models_json),
                    updated_at = excluded.updated_at
                """,
                (endpoint_id, base_url, models_json or "[]", now, now, models_json),
            )

    def get_model_endpoint(self, endpoint_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_endpoints WHERE endpoint_id = ?", (endpoint_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_model_endpoints(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_endpoints ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_model_endpoint(self, endpoint_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM model_endpoint_purposes WHERE endpoint_id = ?", (endpoint_id,)
            )
            cursor = connection.execute(
                "DELETE FROM model_endpoints WHERE endpoint_id = ?", (endpoint_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(endpoint_id)

    def set_model_purpose(
        self, purpose: str, endpoint_id: str, model: str, effort: str
    ) -> None:
        """Persist the active endpoint selection for a purpose ('article'/'chat')."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_endpoint_purposes (purpose, endpoint_id, model, effort, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(purpose) DO UPDATE SET
                    endpoint_id = excluded.endpoint_id,
                    model = excluded.model,
                    effort = excluded.effort,
                    updated_at = excluded.updated_at
                """,
                (purpose, endpoint_id, model, effort, _utc_now()),
            )

    def get_model_purpose(self, purpose: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_endpoint_purposes WHERE purpose = ?", (purpose,)
            ).fetchone()
        return dict(row) if row is not None else None

    def create_scheduled_job(
        self, draft_id: str, platform: str, live: bool, run_at: str
    ) -> dict[str, object]:
        job_id = hashlib.sha256(
            f"{draft_id}:{platform}:{int(live)}:{run_at}".encode()
        ).hexdigest()[:16]
        now = _utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO scheduled_jobs (
                    job_id, draft_id, platform, live, run_at, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (job_id, draft_id, platform, int(live), run_at, now, now),
            )
        return self.get_scheduled_job(job_id)

    def get_scheduled_job(self, job_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list_scheduled_jobs(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_jobs
                ORDER BY run_at ASC, created_at ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def due_scheduled_jobs(self, now: str) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_jobs
                WHERE status = 'PENDING' AND run_at <= ?
                ORDER BY run_at ASC
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_scheduled_job(self, job_id: str) -> bool:
        now = _utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs
                SET status = 'RUNNING', updated_at = ?
                WHERE job_id = ? AND status = 'PENDING'
                """,
                (now, job_id),
            )
        return cursor.rowcount == 1

    def finish_scheduled_job(
        self,
        job_id: str,
        status: str,
        error_message: str | None = None,
        result_json: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_jobs
                SET status = ?, error_message = ?, result_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, error_message, result_json, _utc_now(), job_id),
            )

    def cancel_scheduled_job(self, job_id: str) -> dict[str, object]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs
                SET status = 'CANCELLED', updated_at = ?
                WHERE job_id = ? AND status = 'PENDING'
                """,
                (_utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("schedule_not_cancellable")
        return self.get_scheduled_job(job_id)