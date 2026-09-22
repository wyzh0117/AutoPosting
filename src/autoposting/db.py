from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .engagement import (
    better_tier,
    effective_tier,
    engagement_from_raw,
    engagement_kind,
)
from .diagnostics import (
    normalize_diagnostic,
    parse_diagnostic,
    sanitize_detail,
    serialize_diagnostic,
)
from .models import FeedItem, Source, canonical_source_types
from .normalize import content_hash


DB_SCHEMA_VERSION = 4
_JSON_VERSIONS = {
    "github_weekly_repos": "github-weekly-repos-v1",
    "github_weekly_images": "github-weekly-images-v1",
    "github_weekly_visual": "github-weekly-visual-v1",
    "github_weekly_diagnostics": "github-weekly-diagnostics-v1",
    "skill_finder_snapshot": "skill-finder-snapshot-v1",
    "favorite_payload": "favorite-payload-v1",
    "scheduled_result": "scheduled-result-v1",
    "collection_run_source_ids": "collection-run-source-ids-v1",
    "collection_run_diagnostics": "collection-run-diagnostics-v1",
    "collection_run_source_attempt_diagnostic": "collection-run-source-attempt-diagnostic-v1",
    "topic_dossier": "topic-dossier-envelope-v1",
    "topic_dossier_event_ids": "topic-dossier-event-ids-v1",
    "resource_set": "resource-set-envelope-v1",
}

_RESOURCE_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|apikey|token|secret|password|authorization|cookie|headers?)"
)
_RESOURCE_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)(?:authorization|access[_-]?token|api[_-]?key|appsecret|secret|"
    r"password|passwd|pwd|bearer|token|signature|sig|auth)\s*[:=]\s*[^\s,;]+"
)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non_finite_json:{value}")


def _json_envelope(version: str, data: object, expected_type: type | tuple[type, ...]) -> str:
    if not isinstance(data, expected_type):
        raise TypeError(f"{version}_data_type")
    return json.dumps(
        {"schema_version": version, "data": data},
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_issue(field: str, reason: str) -> dict[str, object]:
    cause_code = f"{field}_{reason}"
    return {
        "field": field,
        "cause_code": cause_code,
        "detail": reason,
        "raw_preserved": True,
    }


def _decode_json_envelope(
    raw: object,
    *,
    field: str,
    expected_type: type | tuple[type, ...],
    allow_none: bool = False,
) -> tuple[object, dict[str, object] | None]:
    fallback: object = None if allow_none else ({} if expected_type is dict else [])
    if raw is None and allow_none:
        return None, None
    try:
        parsed = json.loads(str(raw), parse_constant=_reject_json_constant)
    except (TypeError, ValueError):
        return fallback, _json_issue(field, "invalid_json")
    if allow_none and parsed is None:
        return None, None
    if isinstance(parsed, dict) and "schema_version" in parsed:
        if parsed.get("schema_version") != _JSON_VERSIONS[field]:
            return fallback, _json_issue(field, "unknown_schema_version")
        if "data" not in parsed:
            return fallback, _json_issue(field, "missing_data")
        parsed = parsed["data"]
        if allow_none and parsed is None:
            return None, None
        if not isinstance(parsed, expected_type):
            return fallback, _json_issue(field, "wrong_data_type")
        return parsed, None
    if not isinstance(parsed, expected_type):
        return fallback, _json_issue(field, "wrong_top_level")
    return parsed, None


def _attach_json_issues(payload: dict[str, object], issues: list[dict[str, object]]) -> dict[str, object]:
    if not issues:
        return payload
    reasons = [str(issue["cause_code"]) for issue in issues]
    payload["degraded"] = True
    payload["degraded_reasons"] = reasons
    payload["cause_code"] = reasons[0]
    payload["degraded_details"] = issues
    return payload


def _legacy_source_error(value: object) -> str:
    return f"legacy_source_error:{sanitize_detail(value)}"


def _schema_statements(script: str) -> Iterable[str]:
    """Yield complete schema statements without executescript's implicit COMMIT."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        text = statement.strip()
        if text:
            yield text
        statement = ""
    if statement.strip():
        raise RuntimeError("incomplete_schema_statement")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit', 'html', 'github', 'api', 'account')),
    url_or_handle TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
    topics_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
    source_type TEXT NOT NULL DEFAULT 'other' CHECK (source_type IN ('official', 'media', 'community', 'developer', 'research', 'repository', 'other')),
    adapter_type TEXT NOT NULL DEFAULT 'rss' CHECK (adapter_type IN ('rss', 'sitemap', 'html', 'reddit', 'github', 'api', 'account', 'x')),
    identity_verified INTEGER NOT NULL DEFAULT 0 CHECK (identity_verified IN (0, 1)),
    content_filter TEXT NOT NULL DEFAULT '' CHECK (content_filter IN ('', 'ai')),
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

-- Radar history is intentionally separate from the publishing/event-trigger
-- table above.  It records only snapshots explicitly saved by a collect path.
CREATE TABLE IF NOT EXISTS engagement_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL CHECK (value >= 0),
    observed_at TEXT NOT NULL,
    UNIQUE (event_id, platform, metric, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_engagement_snapshots_event_time
    ON engagement_snapshots (event_id, observed_at);

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
    purpose TEXT PRIMARY KEY CHECK (purpose IN ('article', 'chat', 'translation', 'leaderboard', 'image')),
    endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS github_weekly (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    week_start TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    repos_json TEXT NOT NULL,
    xhs_title TEXT NOT NULL,
    xhs_body TEXT NOT NULL,
    model_used TEXT NOT NULL,
    images_json TEXT NOT NULL DEFAULT '[]',
    visual_json TEXT NOT NULL DEFAULT '{}',
    diagnostics_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS skill_finder (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    query TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'DONE', 'FAILED')),
    requested_sources_json TEXT NOT NULL,
    attempted_sources_json TEXT NOT NULL,
    successful_sources_json TEXT NOT NULL,
    failed_sources_json TEXT NOT NULL,
    items_fetched INTEGER NOT NULL DEFAULT 0 CHECK (items_fetched >= 0),
    items_inserted INTEGER NOT NULL DEFAULT 0 CHECK (items_inserted >= 0),
    items_after_dedupe INTEGER NOT NULL DEFAULT 0 CHECK (items_after_dedupe >= 0),
    clusters_created_or_updated INTEGER NOT NULL DEFAULT 0 CHECK (clusters_created_or_updated >= 0),
    degraded INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0, 1)),
    diagnostics_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_run_sources (
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED_DISABLED', 'SKIPPED_NOT_DUE')
    ),
    started_at TEXT,
    finished_at TEXT,
    items_fetched INTEGER NOT NULL DEFAULT 0 CHECK (items_fetched >= 0),
    items_inserted INTEGER NOT NULL DEFAULT 0 CHECK (items_inserted >= 0),
    items_after_dedupe INTEGER NOT NULL DEFAULT 0 CHECK (items_after_dedupe >= 0),
    diagnostic_json TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_started
    ON collection_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_collection_run_sources_status
    ON collection_run_sources (run_id, status);

CREATE TABLE IF NOT EXISTS topic_dossiers (
    dossier_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    query TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'empty')),
    degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
    dossier_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_topic_dossiers_created
    ON topic_dossiers (created_at DESC, dossier_id DESC);

CREATE TABLE IF NOT EXISTS resource_sets (
    set_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    preset TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'empty')),
    degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
    resource_set_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resource_sets_created
    ON resource_sets (created_at DESC, set_id DESC);
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
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DB_SCHEMA_VERSION:
                raise RuntimeError(f"unsupported_database_schema_version:{version}")
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN")
                for statement in _schema_statements(SCHEMA):
                    # The schema's PRAGMA would be a no-op inside this transaction;
                    # foreign keys are restored after the schema and migration commit.
                    if statement.rstrip(";").strip().upper() == "PRAGMA FOREIGN_KEYS = ON":
                        continue
                    connection.execute(statement)
                migrated = False
                while version < DB_SCHEMA_VERSION:
                    next_version = version + 1
                    if next_version == 1:
                        self._migrate_v1(connection)
                    elif next_version == 2:
                        self._migrate_v2(connection)
                    elif next_version == 3:
                        self._migrate_v3(connection)
                    elif next_version == 4:
                        self._migrate_v4(connection)
                    else:  # pragma: no cover - guarded by DB_SCHEMA_VERSION
                        raise RuntimeError(f"missing_database_migration:{next_version}")
                    connection.execute(f"PRAGMA user_version = {next_version}")
                    version = next_version
                    migrated = True
                if version == DB_SCHEMA_VERSION and not migrated:
                    # user_version records completed migrations, but cannot detect
                    # a partially restored/edited table with an already-current
                    # version.  The existing ensure helpers are idempotent, so use
                    # them as a cheap repair pass without changing the version.
                    self._migrate_v1(connection)
                    self._migrate_v2(connection)
                    self._migrate_v3(connection)
                    self._migrate_v4(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        """Apply the first idempotent migration and record it only on commit."""
        self._ensure_sitemap_source_type(connection)
        self._ensure_reddit_source_type(connection)
        self._ensure_translation_model_purpose(connection)
        self._ensure_leaderboard_model_purpose(connection)
        self._ensure_image_model_purpose(connection)
        self._ensure_github_weekly_images(connection)
        self._ensure_github_weekly_visual(connection)
        self._ensure_github_weekly_diagnostics(connection)
        self._ensure_skill_finder(connection)
        self._ensure_hotspot_favorites(connection)
        self._ensure_scheduled_result(connection)
        self._ensure_engagement_snapshots(connection)
        self._migrate_legacy_source_errors(connection)

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Add canonical source fields while preserving the legacy ``type`` alias."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(sources)").fetchall()
        }
        if "source_type" not in columns:
            connection.execute(
                "ALTER TABLE sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'other' "
                "CHECK (source_type IN ('official', 'media', 'community', 'developer', "
                "'research', 'repository', 'other'))"
            )
        if "adapter_type" not in columns:
            connection.execute(
                "ALTER TABLE sources ADD COLUMN adapter_type TEXT NOT NULL DEFAULT 'rss' "
                "CHECK (adapter_type IN ('rss', 'sitemap', 'html', 'reddit', 'github', "
                "'api', 'account', 'x'))"
            )
        if "identity_verified" not in columns:
            connection.execute(
                "ALTER TABLE sources ADD COLUMN identity_verified INTEGER NOT NULL DEFAULT 0 "
                "CHECK (identity_verified IN (0, 1))"
            )
        if "content_filter" not in columns:
            connection.execute(
                "ALTER TABLE sources ADD COLUMN content_filter TEXT NOT NULL DEFAULT '' "
                "CHECK (content_filter IN ('', 'ai'))"
            )

        # Rows created by the v1 schema have no canonical fields.  Keep the
        # legacy type readable while filling the new columns deterministically.
        connection.execute(
            """
            UPDATE sources
            SET source_type = CASE type
                WHEN 'sitemap' THEN 'official'
                WHEN 'reddit' THEN 'community'
                WHEN 'x' THEN 'developer'
                ELSE 'other'
            END,
            adapter_type = CASE type
                WHEN 'web' THEN 'html'
                ELSE type
            END
            WHERE source_type = 'other' AND adapter_type = 'rss'
            """
        )

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Create the durable Topic Dossier store without touching sources."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_dossiers (
                dossier_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                query TEXT NOT NULL,
                event_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'empty')),
                degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
                dossier_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_dossiers_created
            ON topic_dossiers (created_at DESC, dossier_id DESC)
            """
        )

    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        """Create the durable ResourceSet store without storing credentials."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS resource_sets (
                set_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                preset TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'empty')),
                degraded INTEGER NOT NULL CHECK (degraded IN (0, 1)),
                resource_set_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_resource_sets_created
            ON resource_sets (created_at DESC, set_id DESC)
            """
        )

    @staticmethod
    def _migrate_legacy_source_errors(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT source_id, last_error FROM sources "
            "WHERE last_error IS NOT NULL AND TRIM(last_error) <> ''"
        ).fetchall()
        for row in rows:
            current = str(row["last_error"])
            if current == "legacy_source_error":
                continue
            if current.startswith("legacy_source_error:"):
                normalized = _legacy_source_error(current.split(":", 1)[1])
            elif current.startswith("code="):
                parsed = parse_diagnostic(current)
                normalized = (
                    serialize_diagnostic(parsed)
                    if parsed is not None
                    else _legacy_source_error(current)
                )
            else:
                normalized = _legacy_source_error(current)
            connection.execute(
                "UPDATE sources SET last_error = ? WHERE source_id = ?",
                (normalized, str(row["source_id"])),
            )

    @staticmethod
    def _ensure_sitemap_source_type(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "sitemap" in sql:
            return
        connection.execute(
            """
            CREATE TABLE sources_new (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit', 'html', 'github', 'api', 'account')),
                url_or_handle TEXT NOT NULL,
                tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
                topics_json TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
                last_fetched_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO sources_new SELECT * FROM sources")
        connection.execute("DROP TABLE sources")
        connection.execute("ALTER TABLE sources_new RENAME TO sources")

    @staticmethod
    def _ensure_reddit_source_type(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sources'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "reddit" in sql:
            return
        connection.execute(
            """
            CREATE TABLE sources_new (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit', 'html', 'github', 'api', 'account')),
                url_or_handle TEXT NOT NULL,
                tier TEXT NOT NULL CHECK (tier IN ('S1', 'S2', 'S3', 'S4')),
                topics_json TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                fetch_interval_minutes INTEGER NOT NULL CHECK (fetch_interval_minutes > 0),
                last_fetched_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("INSERT INTO sources_new SELECT * FROM sources")
        connection.execute("DROP TABLE sources")
        connection.execute("ALTER TABLE sources_new RENAME TO sources")

    @staticmethod
    def _ensure_translation_model_purpose(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'model_endpoint_purposes'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "translation" in sql:
            return
        connection.execute(
            """
            CREATE TABLE model_endpoint_purposes_new (
                purpose TEXT PRIMARY KEY CHECK (
                    purpose IN ('article', 'chat', 'translation')
                ),
                endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO model_endpoint_purposes_new SELECT * FROM model_endpoint_purposes"
        )
        connection.execute("DROP TABLE model_endpoint_purposes")
        connection.execute("ALTER TABLE model_endpoint_purposes_new RENAME TO model_endpoint_purposes")

    @staticmethod
    def _ensure_leaderboard_model_purpose(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'model_endpoint_purposes'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "leaderboard" in sql:
            return
        connection.execute(
            """
            CREATE TABLE model_endpoint_purposes_new (
                purpose TEXT PRIMARY KEY CHECK (
                    purpose IN ('article', 'chat', 'translation', 'leaderboard', 'image')
                ),
                endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO model_endpoint_purposes_new SELECT * FROM model_endpoint_purposes"
        )
        connection.execute("DROP TABLE model_endpoint_purposes")
        connection.execute("ALTER TABLE model_endpoint_purposes_new RENAME TO model_endpoint_purposes")

    @staticmethod
    def _ensure_image_model_purpose(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'model_endpoint_purposes'"
        ).fetchone()
        sql = str(row["sql"] if row is not None else "")
        if "'image'" in sql:
            return
        connection.execute(
            """
            CREATE TABLE model_endpoint_purposes_new (
                purpose TEXT PRIMARY KEY CHECK (
                    purpose IN ('article', 'chat', 'translation', 'leaderboard', 'image')
                ),
                endpoint_id TEXT NOT NULL REFERENCES model_endpoints(endpoint_id),
                model TEXT NOT NULL,
                effort TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO model_endpoint_purposes_new SELECT * FROM model_endpoint_purposes"
        )
        connection.execute("DROP TABLE model_endpoint_purposes")
        connection.execute("ALTER TABLE model_endpoint_purposes_new RENAME TO model_endpoint_purposes")

    @staticmethod
    def _ensure_github_weekly_images(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(github_weekly)").fetchall()
        }
        if "images_json" not in columns:
            connection.execute(
                "ALTER TABLE github_weekly ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _ensure_github_weekly_visual(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(github_weekly)").fetchall()
        }
        if "visual_json" not in columns:
            connection.execute(
                "ALTER TABLE github_weekly ADD COLUMN visual_json TEXT NOT NULL DEFAULT '{}'"
            )

    @staticmethod
    def _ensure_github_weekly_diagnostics(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(github_weekly)").fetchall()
        }
        if "diagnostics_json" not in columns:
            connection.execute(
                "ALTER TABLE github_weekly ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _ensure_skill_finder(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_finder (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                query TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _ensure_hotspot_favorites(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hotspot_favorites (
                item_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                favorited_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _ensure_scheduled_result(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(scheduled_jobs)").fetchall()
        }
        if "result_json" not in columns:
            connection.execute("ALTER TABLE scheduled_jobs ADD COLUMN result_json TEXT")

    @staticmethod
    def _ensure_engagement_snapshots(connection: sqlite3.Connection) -> None:
        """Idempotently add radar-only engagement history to old databases."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS engagement_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL CHECK (value >= 0),
                observed_at TEXT NOT NULL,
                UNIQUE (event_id, platform, metric, observed_at)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_engagement_snapshots_event_time "
            "ON engagement_snapshots (event_id, observed_at)"
        )

    def save_github_weekly(
        self,
        week_start: str,
        repos: list[dict[str, object]],
        xhs_title: str,
        xhs_body: str,
        model_used: str = "",
        images: list[dict[str, object]] | None = None,
        visual: dict[str, object] | None = None,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        now = _utc_now()
        repos_payload = list(repos)
        images_payload = list(images or [])
        visual_payload = visual or {}
        diagnostics_payload = list(diagnostics or [])
        repos_json = _json_envelope(
            _JSON_VERSIONS["github_weekly_repos"], repos_payload, list
        )
        images_json = _json_envelope(
            _JSON_VERSIONS["github_weekly_images"], images_payload, list
        )
        visual_json = _json_envelope(
            _JSON_VERSIONS["github_weekly_visual"], visual_payload, dict
        )
        diagnostics_json = _json_envelope(
            _JSON_VERSIONS["github_weekly_diagnostics"], diagnostics_payload, list
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO github_weekly (
                    id, week_start, fetched_at, repos_json, xhs_title, xhs_body,
                    model_used, images_json, visual_json, diagnostics_json
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    week_start = excluded.week_start,
                    fetched_at = excluded.fetched_at,
                    repos_json = excluded.repos_json,
                    xhs_title = excluded.xhs_title,
                    xhs_body = excluded.xhs_body,
                    model_used = excluded.model_used,
                    images_json = excluded.images_json,
                    visual_json = excluded.visual_json,
                    diagnostics_json = excluded.diagnostics_json
                """,
                (
                    week_start,
                    now,
                    repos_json,
                    xhs_title,
                    xhs_body,
                    model_used,
                    images_json,
                    visual_json,
                    diagnostics_json,
                ),
            )
        return {
            "week_start": week_start,
            "fetched_at": now,
            "repos": repos_payload,
            "xhs_title": xhs_title,
            "xhs_body": xhs_body,
            "model_used": model_used,
            "images": images_payload,
            "visual": visual_payload,
        }

    def get_github_weekly(self) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM github_weekly WHERE id = 1").fetchone()
        if row is None:
            return None
        issues: list[dict[str, object]] = []
        repos, issue = _decode_json_envelope(
            row["repos_json"],
            field="github_weekly_repos",
            expected_type=list,
        )
        if issue:
            issues.append(issue)
        images_raw = row["images_json"] if "images_json" in row.keys() else "[]"
        images, issue = _decode_json_envelope(
            images_raw,
            field="github_weekly_images",
            expected_type=list,
        )
        if issue:
            issues.append(issue)
        visual_raw = row["visual_json"] if "visual_json" in row.keys() else "{}"
        visual, issue = _decode_json_envelope(
            visual_raw,
            field="github_weekly_visual",
            expected_type=dict,
        )
        if issue:
            issues.append(issue)
        diagnostics_raw = (
            row["diagnostics_json"] if "diagnostics_json" in row.keys() else "[]"
        )
        diagnostics, issue = _decode_json_envelope(
            diagnostics_raw,
            field="github_weekly_diagnostics",
            expected_type=list,
        )
        if issue:
            issues.append(issue)
        payload = {
            "week_start": str(row["week_start"]),
            "fetched_at": str(row["fetched_at"]),
            "repos": repos,
            "xhs_title": str(row["xhs_title"]),
            "xhs_body": str(row["xhs_body"]),
            "model_used": str(row["model_used"]),
            "images": images,
            "visual": visual,
        }
        if diagnostics:
            payload["degraded"] = True
            payload["degraded_reasons"] = diagnostics
        return _attach_json_issues({
            **payload,
        }, issues)

    def save_skill_finder(self, payload: dict[str, object]) -> dict[str, object]:
        now = _utc_now()
        body = dict(payload)
        body["fetched_at"] = now
        query = str(body.get("query") or "")
        with self.connect() as connection:
            self._ensure_skill_finder(connection)
            connection.execute(
                """
                INSERT INTO skill_finder (id, query, fetched_at, payload_json)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    query = excluded.query,
                    fetched_at = excluded.fetched_at,
                    payload_json = excluded.payload_json
                """,
                (
                    query,
                    now,
                    _json_envelope(
                        _JSON_VERSIONS["skill_finder_snapshot"], body, dict
                    ),
                ),
            )
        return body

    def get_skill_finder(self) -> dict[str, object] | None:
        with self.connect() as connection:
            self._ensure_skill_finder(connection)
            row = connection.execute("SELECT * FROM skill_finder WHERE id = 1").fetchone()
        if row is None:
            return None
        payload, issue = _decode_json_envelope(
            row["payload_json"],
            field="skill_finder_snapshot",
            expected_type=dict,
        )
        issues = [issue] if issue else []
        payload.setdefault("query", str(row["query"]))
        payload.setdefault("fetched_at", str(row["fetched_at"]))
        return _attach_json_issues(payload, issues)

    def add_hotspot_favorite(
        self,
        item_id: str,
        title: str,
        summary: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("item_id required")
        now = _utc_now()
        body = dict(payload)
        body.setdefault("item_id", item_id)
        body.setdefault("title", title)
        body.setdefault("summary", summary)
        with self.connect() as connection:
            self._ensure_hotspot_favorites(connection)
            connection.execute(
                """
                INSERT INTO hotspot_favorites (
                    item_id, title, summary, payload_json, favorited_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    payload_json = excluded.payload_json,
                    favorited_at = excluded.favorited_at
                """,
                (
                    item_id,
                    str(title or ""),
                    str(summary or ""),
                    _json_envelope(
                        _JSON_VERSIONS["favorite_payload"], body, dict
                    ),
                    now,
                ),
            )
        return {
            "item_id": item_id,
            "title": str(title or ""),
            "summary": str(summary or ""),
            "payload": body,
            "favorited_at": now,
        }

    def list_hotspot_favorites(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            self._ensure_hotspot_favorites(connection)
            rows = connection.execute(
                """
                SELECT item_id, title, summary, payload_json, favorited_at
                FROM hotspot_favorites
                ORDER BY favorited_at DESC, item_id DESC
                """
            ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            payload, issue = _decode_json_envelope(
                row["payload_json"],
                field="favorite_payload",
                expected_type=dict,
            )
            issues = [issue] if issue else []
            item = dict(payload)
            item.update(
                {
                    "item_id": str(row["item_id"]),
                    "title": str(row["title"]),
                    "summary": str(row["summary"]),
                    "favorited_at": str(row["favorited_at"]),
                    "payload": payload,
                }
            )
            items.append(_attach_json_issues(item, issues))
        return items

    def remove_hotspot_favorite(self, item_id: str) -> bool:
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("item_id required")
        with self.connect() as connection:
            self._ensure_hotspot_favorites(connection)
            cursor = connection.execute(
                "DELETE FROM hotspot_favorites WHERE item_id = ?",
                (item_id,),
            )
        return cursor.rowcount > 0

    def upsert_source(self, source: Source) -> None:
        now = _utc_now()
        source_type, adapter_type = canonical_source_types(
            source.type, source.source_type, source.adapter_type
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, name, type, url_or_handle, tier, topics_json,
                    enabled, fetch_interval_minutes, source_type, adapter_type,
                    identity_verified, content_filter, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    name = excluded.name,
                    type = excluded.type,
                    url_or_handle = excluded.url_or_handle,
                    tier = excluded.tier,
                    topics_json = excluded.topics_json,
                    source_type = excluded.source_type,
                    adapter_type = excluded.adapter_type,
                    identity_verified = excluded.identity_verified,
                    content_filter = excluded.content_filter,
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
                    source_type,
                    adapter_type,
                    int(source.identity_verified),
                    source.content_filter,
                    now,
                    now,
                ),
            )

    def set_source_enabled(self, source_id: str, enabled: bool) -> None:
        """Update the runtime registry state without rewriting source metadata."""
        source_id = str(source_id or "").strip()
        if not source_id:
            raise ValueError("source_id required")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE sources SET enabled = ?, updated_at = ? WHERE source_id = ?",
                (int(enabled), _utc_now(), source_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(source_id)

    def set_source_fetch_interval(self, source_id: str, minutes: int) -> None:
        """Update a source interval in the authoritative runtime registry."""
        source_id = str(source_id or "").strip()
        if not source_id:
            raise ValueError("source_id required")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("fetch interval must be a positive integer")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE sources SET fetch_interval_minutes = ?, updated_at = ? "
                "WHERE source_id = ?",
                (minutes, _utc_now(), source_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(source_id)

    @staticmethod
    def _collection_ids_json(values: Iterable[object]) -> str:
        ids = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        return _json_envelope(_JSON_VERSIONS["collection_run_source_ids"], ids, list)

    @staticmethod
    def _collection_diagnostics_json(values: Iterable[object]) -> str:
        diagnostics: list[dict[str, object]] = []
        for value in values:
            if not isinstance(value, Mapping):
                continue
            diagnostics.append(
                normalize_diagnostic(
                    value,
                    str(value.get("source") or "collection"),
                    str(value.get("stage") or "collect"),
                )
            )
        return _json_envelope(
            _JSON_VERSIONS["collection_run_diagnostics"], diagnostics, list
        )

    def start_collection_run(
        self,
        requested_sources: Iterable[object],
        *,
        started_at: str | None = None,
    ) -> str:
        """Create a durable collection run and one pending attempt per source."""
        source_ids = list(
            dict.fromkeys(
                str(value).strip() for value in requested_sources if str(value).strip()
            )
        )
        run_id = f"run-{uuid.uuid4().hex[:24]}"
        started = started_at or _utc_now()
        empty_ids = self._collection_ids_json(())
        empty_diagnostics = self._collection_diagnostics_json(())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_runs (
                    run_id, started_at, status, requested_sources_json,
                    attempted_sources_json, successful_sources_json,
                    failed_sources_json, diagnostics_json
                ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started,
                    self._collection_ids_json(source_ids),
                    empty_ids,
                    empty_ids,
                    empty_ids,
                    empty_diagnostics,
                ),
            )
            for source_id in source_ids:
                connection.execute(
                    """
                    INSERT INTO collection_run_sources (
                        run_id, source_id, status
                    ) VALUES (?, ?, 'PENDING')
                    """,
                    (run_id, source_id),
                )
        return run_id

    def record_collection_source_attempt(
        self,
        run_id: str,
        source_id: str,
        status: str,
        *,
        items_fetched: int = 0,
        items_inserted: int = 0,
        items_after_dedupe: int = 0,
        diagnostic: object | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        allowed = {
            "PENDING",
            "RUNNING",
            "SUCCESS",
            "FAILED",
            "SKIPPED_DISABLED",
            "SKIPPED_NOT_DUE",
        }
        if status not in allowed:
            raise ValueError(f"invalid collection source status: {status}")
        values = (items_fetched, items_inserted, items_after_dedupe)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("collection item counts must be non-negative integers")
        run_id = str(run_id or "").strip()
        source_id = str(source_id or "").strip()
        if not run_id or not source_id:
            raise ValueError("run_id and source_id required")
        diagnostic_json: str | None = None
        if diagnostic is not None:
            normalized = normalize_diagnostic(diagnostic, source_id, "collect")
            diagnostic_json = _json_envelope(
                _JSON_VERSIONS["collection_run_source_attempt_diagnostic"],
                normalized,
                dict,
            )
        now = _utc_now()
        attempt_started = started_at or (now if status == "RUNNING" else None)
        attempt_finished = finished_at or (
            now if status in {"SUCCESS", "FAILED", "SKIPPED_DISABLED", "SKIPPED_NOT_DUE"} else None
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_run_sources
                SET status = ?, started_at = COALESCE(started_at, ?),
                    finished_at = ?, items_fetched = ?, items_inserted = ?,
                    items_after_dedupe = ?, diagnostic_json = ?
                WHERE run_id = ? AND source_id = ?
                """,
                (
                    status,
                    attempt_started,
                    attempt_finished,
                    items_fetched,
                    items_inserted,
                    items_after_dedupe,
                    diagnostic_json,
                    run_id,
                    source_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"{run_id}:{source_id}")

    @staticmethod
    def _collection_attempt_diagnostic(raw: object, source_id: str) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        if raw is None:
            return None, None
        decoded, issue = _decode_json_envelope(
            raw,
            field="collection_run_source_attempt_diagnostic",
            expected_type=dict,
            allow_none=True,
        )
        if issue is not None:
            return None, issue
        return (
            normalize_diagnostic(decoded, source_id, "collect")
            if isinstance(decoded, Mapping)
            else None,
            None,
        )

    def _collection_run_payload(self, row: Mapping[str, object]) -> dict[str, object]:
        issues: list[dict[str, object]] = []
        decoded_lists: dict[str, list[object]] = {}
        for field in (
            "requested_sources",
            "attempted_sources",
            "successful_sources",
            "failed_sources",
        ):
            raw_field = f"{field}_json"
            decoded, issue = _decode_json_envelope(
                row.get(raw_field),
                field="collection_run_source_ids",
                expected_type=list,
            )
            decoded_lists[field] = list(decoded) if isinstance(decoded, list) else []
            if issue is not None:
                issues.append({**issue, "field": raw_field, "cause_code": f"{raw_field}_{issue['cause_code'].split('_', 2)[-1]}"})
        diagnostics, issue = _decode_json_envelope(
            row.get("diagnostics_json"),
            field="collection_run_diagnostics",
            expected_type=list,
        )
        if issue is not None:
            issues.append(issue)
        payload: dict[str, object] = {
            "run_id": str(row["run_id"]),
            "started_at": str(row["started_at"]),
            "finished_at": str(row.get("finished_at") or ""),
            "status": str(row["status"]),
            **decoded_lists,
            "items_fetched": int(row.get("items_fetched") or 0),
            "items_inserted": int(row.get("items_inserted") or 0),
            "items_after_dedupe": int(row.get("items_after_dedupe") or 0),
            "clusters_created_or_updated": int(row.get("clusters_created_or_updated") or 0),
            "degraded": bool(row.get("degraded")),
            "diagnostics": list(diagnostics) if isinstance(diagnostics, list) else [],
            "source_attempts": [],
        }
        if issues:
            _attach_json_issues(payload, issues)
        return payload

    def get_collection_run(self, run_id: str) -> dict[str, object]:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id required")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            attempts = connection.execute(
                """
                SELECT source_id, status, started_at, finished_at, items_fetched,
                       items_inserted, items_after_dedupe, diagnostic_json
                FROM collection_run_sources
                WHERE run_id = ? ORDER BY source_id
                """,
                (run_id,),
            ).fetchall()
        payload = self._collection_run_payload(dict(row))
        attempt_issues: list[dict[str, object]] = []
        source_attempts: list[dict[str, object]] = []
        for attempt in attempts:
            source_id = str(attempt["source_id"])
            diagnostic, issue = self._collection_attempt_diagnostic(
                attempt["diagnostic_json"], source_id
            )
            if issue is not None:
                attempt_issues.append({**issue, "field": f"{source_id}.diagnostic"})
            source_attempts.append(
                {
                    "source_id": source_id,
                    "status": str(attempt["status"]),
                    "started_at": str(attempt["started_at"] or ""),
                    "finished_at": str(attempt["finished_at"] or ""),
                    "items_fetched": int(attempt["items_fetched"] or 0),
                    "items_inserted": int(attempt["items_inserted"] or 0),
                    "items_after_dedupe": int(attempt["items_after_dedupe"] or 0),
                    "diagnostic": diagnostic,
                }
            )
        payload["source_attempts"] = source_attempts
        if attempt_issues:
            _attach_json_issues(payload, attempt_issues)
        return payload

    def list_collection_runs(self, limit: int = 20) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM collection_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.get_collection_run(str(row["run_id"])) for row in rows]

    def finish_collection_run(
        self,
        run_id: str,
        *,
        status: str = "DONE",
        finished_at: str | None = None,
        clusters_created_or_updated: int = 0,
        diagnostics: Iterable[object] = (),
    ) -> dict[str, object]:
        if status not in {"DONE", "FAILED"}:
            raise ValueError("collection run status must be DONE or FAILED")
        if isinstance(clusters_created_or_updated, bool) or not isinstance(
            clusters_created_or_updated, int
        ) or clusters_created_or_updated < 0:
            raise ValueError("clusters_created_or_updated must be non-negative integer")
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id required")
        now = finished_at or _utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM collection_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            attempts = connection.execute(
                """
                SELECT source_id, status, items_fetched, items_inserted,
                       items_after_dedupe, diagnostic_json
                FROM collection_run_sources WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
            attempted = [
                str(item["source_id"])
                for item in attempts
                if str(item["status"]) in {"RUNNING", "SUCCESS", "FAILED"}
            ]
            successful = [
                str(item["source_id"])
                for item in attempts
                if str(item["status"]) == "SUCCESS"
            ]
            failed = [
                str(item["source_id"])
                for item in attempts
                if str(item["status"]) == "FAILED"
            ]
            diagnostic_rows: list[dict[str, object]] = []
            for item in attempts:
                if item["diagnostic_json"] is None:
                    continue
                diagnostic, _issue = self._collection_attempt_diagnostic(
                    item["diagnostic_json"], str(item["source_id"])
                )
                if diagnostic is not None:
                    diagnostic_rows.append(diagnostic)
            diagnostic_rows.extend(
                value for value in diagnostics if isinstance(value, Mapping)
            )
            diagnostic_rows = [
                normalize_diagnostic(value, str(value.get("source") or "collection"), "collect")
                for value in diagnostic_rows
            ]
            # Stable de-duplication keeps repeated diagnostics from inflating UI counts.
            unique_diagnostics: list[dict[str, object]] = []
            seen_diagnostics: set[str] = set()
            for diagnostic in diagnostic_rows:
                key = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
                if key not in seen_diagnostics:
                    seen_diagnostics.add(key)
                    unique_diagnostics.append(diagnostic)
            items_fetched = sum(int(item["items_fetched"] or 0) for item in attempts)
            items_inserted = sum(int(item["items_inserted"] or 0) for item in attempts)
            items_after_dedupe = sum(
                int(item["items_after_dedupe"] or 0) for item in attempts
            )
            degraded = bool(failed or unique_diagnostics)
            connection.execute(
                """
                UPDATE collection_runs SET finished_at = ?, status = ?,
                    attempted_sources_json = ?, successful_sources_json = ?,
                    failed_sources_json = ?, items_fetched = ?, items_inserted = ?,
                    items_after_dedupe = ?, clusters_created_or_updated = ?,
                    degraded = ?, diagnostics_json = ?
                WHERE run_id = ?
                """,
                (
                    now,
                    status,
                    self._collection_ids_json(attempted),
                    self._collection_ids_json(successful),
                    self._collection_ids_json(failed),
                    items_fetched,
                    items_inserted,
                    items_after_dedupe,
                    clusters_created_or_updated,
                    int(degraded),
                    self._collection_diagnostics_json(unique_diagnostics),
                    run_id,
                ),
            )
        return self.get_collection_run(run_id)

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

    def record_fetch_result(self, source_id: str, error: object | None = None) -> None:
        stored_error = None
        if error is not None:
            stored_error = serialize_diagnostic(
                normalize_diagnostic(error, source_id, "collect")
            )
        with self.connect() as connection:
            connection.execute(
                "UPDATE sources SET last_fetched_at = ?, last_error = ?, updated_at = ? WHERE source_id = ?",
                (_utc_now(), stored_error, _utc_now(), source_id),
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

    def record_engagement_snapshot(
        self,
        event_id: str | Mapping[str, object],
        platform: str | None = None,
        metric: str | None = None,
        value: float | int | None = None,
        observed_at: str | None = None,
    ) -> dict[str, object]:
        """Persist one explicit radar snapshot idempotently.

        This method is intentionally opt-in; initialization and radar scoring
        never call it, so idle processes do not create background history.
        """
        if isinstance(event_id, Mapping):
            payload = event_id
            observed_at = str(payload.get("observed_at") or observed_at or "") or None
            platform = str(payload.get("platform") or platform or "")
            metric = str(payload.get("metric") or metric or "")
            value = payload.get("value") if value is None else value
            event_id = str(payload.get("event_id") or "")
        event = str(event_id or "").strip()
        channel = str(platform or "").strip().lower()
        name = str(metric or "").strip().lower()
        if not event or not channel or not name:
            raise ValueError("event_id, platform and metric are required")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("snapshot value must be numeric") from exc
        if numeric < 0 or numeric != numeric:
            raise ValueError("snapshot value must be non-negative")
        observed = str(observed_at or _utc_now()).strip()
        snapshot_id = hashlib.sha256(
            f"radar-snapshot:{event}:{channel}:{name}:{observed}".encode("utf-8")
        ).hexdigest()[:32]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO engagement_snapshots (
                    snapshot_id, event_id, platform, metric, value, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, platform, metric, observed_at) DO UPDATE SET
                    value = excluded.value,
                    snapshot_id = excluded.snapshot_id
                """,
                (snapshot_id, event, channel, name, numeric, observed),
            )
            row = connection.execute(
                """
                SELECT snapshot_id, event_id, platform, metric, value, observed_at
                FROM engagement_snapshots
                WHERE event_id = ? AND platform = ? AND metric = ? AND observed_at = ?
                """,
                (event, channel, name, observed),
            ).fetchone()
        if row is None:
            raise RuntimeError("engagement snapshot write did not persist")
        return dict(row)

    def save_engagement_snapshot(
        self,
        event_id: str | Mapping[str, object],
        platform: str | None = None,
        metric: str | None = None,
        value: float | int | None = None,
        observed_at: str | None = None,
    ) -> dict[str, object]:
        """Alias used by explicit collect callers."""
        return self.record_engagement_snapshot(event_id, platform, metric, value, observed_at)

    def upsert_engagement_snapshot(
        self,
        event_id: str | Mapping[str, object],
        platform: str | None = None,
        metric: str | None = None,
        value: float | int | None = None,
        observed_at: str | None = None,
    ) -> dict[str, object]:
        return self.record_engagement_snapshot(event_id, platform, metric, value, observed_at)

    def record_engagement_snapshots(
        self, snapshots: Iterable[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        """Save a caller-provided batch; no scheduler or implicit writes."""
        return [
            self.record_engagement_snapshot(
                str(snapshot.get("event_id") or ""),
                str(snapshot.get("platform") or ""),
                str(snapshot.get("metric") or ""),
                snapshot.get("value") or 0,
                str(snapshot.get("observed_at") or "") or None,
            )
            for snapshot in snapshots
            if isinstance(snapshot, Mapping)
        ]

    def list_engagement_snapshots(
        self,
        event_id: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if event_id:
            clauses.append("event_id = ?")
            parameters.append(str(event_id))
        if platform:
            clauses.append("platform = ?")
            parameters.append(str(platform).lower())
        if metric:
            clauses.append("metric = ?")
            parameters.append(str(metric).lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshot_id, event_id, platform, metric, value, observed_at
                FROM engagement_snapshots
                {where}
                ORDER BY observed_at ASC, snapshot_id ASC
                """,
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_engagement_snapshots(
        self,
        event_id: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
    ) -> list[dict[str, object]]:
        return self.list_engagement_snapshots(event_id, platform, metric)

    def latest_engagement_snapshot(
        self, event_id: str, platform: str, metric: str
    ) -> dict[str, object] | None:
        rows = self.list_engagement_snapshots(event_id, platform, metric)
        return rows[-1] if rows else None

    def get_engagement_snapshot(
        self, event_id: str, platform: str, metric: str
    ) -> dict[str, object] | None:
        return self.latest_engagement_snapshot(event_id, platform, metric)

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

    @staticmethod
    def _dossier_payload(dossier: object) -> dict[str, object]:
        """Validate the JSON boundary without importing the research service."""
        serializer = getattr(dossier, "to_dict", None)
        payload = serializer() if callable(serializer) else dossier
        if not isinstance(payload, Mapping):
            raise TypeError("topic_dossier_must_be_object")
        normalized = dict(payload)
        if normalized.get("schema_version") != "topic-dossier-v1":
            raise ValueError("topic_dossier_unknown_schema_version")
        for field in ("dossier_id", "title", "query", "created_at"):
            value = normalized.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"topic_dossier_{field}_required")
        event_ids = normalized.get("event_ids")
        if (
            not isinstance(event_ids, list)
            or not event_ids
            or any(not isinstance(item, str) or not item.strip() for item in event_ids)
        ):
            raise ValueError("topic_dossier_event_ids_invalid")
        status = normalized.get("status")
        if status not in {"ok", "degraded", "empty"}:
            raise ValueError("topic_dossier_status_invalid")
        if not isinstance(normalized.get("degraded"), bool):
            raise ValueError("topic_dossier_degraded_invalid")
        diagnostics = normalized.get("diagnostics", ())
        if not isinstance(diagnostics, (list, tuple)):
            raise ValueError("topic_dossier_diagnostics_invalid")
        normalized["diagnostics"] = [
            normalize_diagnostic(item, "dossier_store", "dossier_persist")
            for item in diagnostics
        ]
        evidence = normalized.get("evidence", ())
        if not isinstance(evidence, (list, tuple)):
            raise ValueError("topic_dossier_evidence_invalid")
        safe_evidence: list[object] = []
        for item in evidence:
            if not isinstance(item, Mapping):
                raise ValueError("topic_dossier_evidence_item_invalid")
            candidate = dict(item)
            assessment = candidate.get("assessment")
            if isinstance(assessment, Mapping):
                assessment_copy = dict(assessment)
                assessment_diagnostics = assessment_copy.get("diagnostics", ())
                if not isinstance(assessment_diagnostics, (list, tuple)):
                    raise ValueError("topic_dossier_assessment_diagnostics_invalid")
                assessment_copy["diagnostics"] = [
                    normalize_diagnostic(item, "dossier_store", "dossier_persist")
                    for item in assessment_diagnostics
                ]
                candidate["assessment"] = assessment_copy
            safe_evidence.append(candidate)
        normalized["evidence"] = safe_evidence
        return normalized

    def save_dossier(self, dossier: object) -> None:
        """Persist one Topic Dossier; evidence URLs never become sources."""
        payload = self._dossier_payload(dossier)
        event_ids = list(dict.fromkeys(str(item).strip() for item in payload["event_ids"]))
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO topic_dossiers (
                    dossier_id, title, query, event_ids_json, status, degraded,
                    dossier_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dossier_id) DO UPDATE SET
                    title = excluded.title,
                    query = excluded.query,
                    event_ids_json = excluded.event_ids_json,
                    status = excluded.status,
                    degraded = excluded.degraded,
                    dossier_json = excluded.dossier_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["dossier_id"],
                    payload["title"],
                    payload["query"],
                    _json_envelope(
                        _JSON_VERSIONS["topic_dossier_event_ids"], event_ids, list
                    ),
                    payload["status"],
                    int(payload["degraded"]),
                    _json_envelope(_JSON_VERSIONS["topic_dossier"], payload, dict),
                    payload["created_at"],
                    now,
                ),
            )

    def get_dossier(self, dossier_id: str) -> dict[str, object] | None:
        dossier_id = str(dossier_id or "").strip()
        if not dossier_id:
            raise ValueError("dossier_id_required")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT dossier_json FROM topic_dossiers WHERE dossier_id = ?",
                (dossier_id,),
            ).fetchone()
        if row is None:
            return None
        payload, issue = _decode_json_envelope(
            row["dossier_json"],
            field="topic_dossier",
            expected_type=dict,
        )
        if issue or not isinstance(payload, dict):
            raise ValueError(str(issue["cause_code"] if issue else "topic_dossier_invalid_json"))
        return payload

    def list_dossiers(self, limit: int = 20) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("dossier_limit_invalid")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT dossier_json FROM topic_dossiers
                ORDER BY created_at DESC, dossier_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload, issue = _decode_json_envelope(
                row["dossier_json"], field="topic_dossier", expected_type=dict
            )
            if issue or not isinstance(payload, dict):
                continue
            result.append(payload)
        return result

    def delete_dossier(self, dossier_id: str) -> bool:
        dossier_id = str(dossier_id or "").strip()
        if not dossier_id:
            raise ValueError("dossier_id_required")
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM topic_dossiers WHERE dossier_id = ?", (dossier_id,)
            )
        return cursor.rowcount == 1

    @staticmethod
    def _sanitize_resource_value(value: object, *, section: str = "", depth: int = 0) -> object:
        """Keep ResourceSet JSON safe while dropping credential-shaped state."""
        if depth > 8:
            return str(value)[:500]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if value and (
                section in {"adapter_status", "diagnostics", "detail"}
                or _RESOURCE_CREDENTIAL_VALUE_RE.search(value)
            ):
                return sanitize_detail(value)
            return value
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in list(value.items())[:100]:
                name = str(key)
                normalized = name.strip().lower().replace("-", "_")
                if _RESOURCE_SECRET_KEY_RE.search(normalized):
                    continue
                child_section = section
                if normalized in {"adapter_status", "diagnostics", "detail"}:
                    child_section = normalized
                result[name] = Database._sanitize_resource_value(
                    item, section=child_section, depth=depth + 1
                )
            return result
        if isinstance(value, (list, tuple, set)):
            return [
                Database._sanitize_resource_value(item, section=section, depth=depth + 1)
                for item in list(value)[:100]
            ]
        return sanitize_detail(value)

    @staticmethod
    def _resource_set_payload(resource_set: object) -> dict[str, object]:
        """Validate and canonicalize a ResourceSet at the persistence boundary."""
        serializer = getattr(resource_set, "to_dict", None)
        raw = serializer() if callable(serializer) else resource_set
        if not isinstance(raw, Mapping):
            raise TypeError("resource_set_must_be_object")
        # Keep Database independent from the resource service at import time;
        # callers may still pass either its dataclass or its JSON mapping.
        from .resource_discovery import ResourceSet

        payload = ResourceSet.from_dict(raw).to_dict()
        sanitized = Database._sanitize_resource_value(payload)
        if not isinstance(sanitized, dict):  # pragma: no cover - canonical serializer
            raise TypeError("resource_set_must_be_object")
        return sanitized

    def save_resource_set(self, resource_set: object) -> None:
        """Persist one canonical ResourceSet and its adapter state."""
        payload = self._resource_set_payload(resource_set)
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO resource_sets (
                    set_id, query, preset, status, degraded, resource_set_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(set_id) DO UPDATE SET
                    query = excluded.query,
                    preset = excluded.preset,
                    status = excluded.status,
                    degraded = excluded.degraded,
                    resource_set_json = excluded.resource_set_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["set_id"],
                    payload["query"],
                    payload["preset"],
                    payload["status"],
                    int(payload["degraded"]),
                    _json_envelope(_JSON_VERSIONS["resource_set"], payload, dict),
                    payload["created_at"],
                    now,
                ),
            )

    def get_resource_set(self, set_id: str) -> dict[str, object] | None:
        set_id = str(set_id or "").strip()
        if not set_id:
            raise ValueError("resource_set_id_required")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT resource_set_json FROM resource_sets WHERE set_id = ?", (set_id,)
            ).fetchone()
        if row is None:
            return None
        payload, issue = _decode_json_envelope(
            row["resource_set_json"], field="resource_set", expected_type=dict
        )
        if issue or not isinstance(payload, dict):
            raise ValueError(str(issue["cause_code"] if issue else "resource_set_invalid_json"))
        return payload

    def list_resource_sets(self, limit: int = 20) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("resource_set_limit_invalid")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_set_json FROM resource_sets
                ORDER BY created_at DESC, set_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            payload, issue = _decode_json_envelope(
                row["resource_set_json"], field="resource_set", expected_type=dict
            )
            if issue or not isinstance(payload, dict):
                raise ValueError(str(issue["cause_code"] if issue else "resource_set_invalid_json"))
            result.append(payload)
        return result

    def delete_resource_set(self, set_id: str) -> bool:
        set_id = str(set_id or "").strip()
        if not set_id:
            raise ValueError("resource_set_id_required")
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM resource_sets WHERE set_id = ?", (set_id,)
            )
        return cursor.rowcount == 1

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

    def list_drafts(self, limit: int = 20) -> list[dict[str, object]]:
        """Recent drafts with their latest publication attempt, newest activity first.

        Basic persistence query only: ordering/status semantics stay in the
        application layer (publishing center).
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("draft_limit_invalid")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.draft_id, d.event_id, d.platform, d.title, d.status,
                       d.approved_by, d.approved_at, d.created_at, d.updated_at,
                       a.status AS attempt_status, a.created_at AS attempt_at
                FROM drafts AS d
                LEFT JOIN publication_attempts AS a
                  ON a.attempt_id = (
                      SELECT attempt_id FROM publication_attempts
                      WHERE draft_id = d.draft_id
                      ORDER BY updated_at DESC, rowid DESC LIMIT 1
                  )
                ORDER BY d.updated_at DESC, d.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def get_source_urls(self, source_ids: object) -> dict[str, set[str]]:
        ids = sorted({str(value).strip() for value in source_ids if str(value).strip()})
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT source_id, source_url FROM item_sources WHERE source_id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        urls: dict[str, set[str]] = {source_id: set() for source_id in ids}
        for row in rows:
            urls[str(row["source_id"])].add(str(row["source_url"]))
        return urls

    def list_sources(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, name, type, source_type, adapter_type, tier,
                       identity_verified, content_filter, enabled,
                       fetch_interval_minutes, last_fetched_at, last_error,
                       created_at, updated_at
                FROM sources
                ORDER BY name, source_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_hotspot_candidates(
        self,
        since: str,
        limit: int = 10,
        sort: str = "time",
        pool: int = 40,
        lane: str = "hotspots",
    ) -> list[dict[str, object]]:
        if sort not in {"time", "heat"}:
            raise ValueError("sort must be time or heat")
        if lane not in {"hotspots", "viewpoints"}:
            raise ValueError("lane must be hotspots or viewpoints")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT item_id, source_id, title, content_text, published_at, fetched_at, raw_json
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
            } | {str(row["source_id"]) for row in rows}
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
            if not pairs:
                pairs = [(str(row["source_id"]), "")]
            source_ids = [source_id for source_id, _ in pairs]
            has_reddit = any(
                source_meta.get(source_id, ("S4", "rss"))[1] == "reddit"
                for source_id in source_ids
            )
            if lane == "viewpoints" and not has_reddit:
                continue
            if lane == "hotspots" and has_reddit:
                continue
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
        """Persist the active endpoint selection for article/chat/translation/leaderboard."""
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
                return self._scheduled_job_view(existing)
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

    @staticmethod
    def _scheduled_job_view(row: sqlite3.Row | Mapping[str, object]) -> dict[str, object]:
        job = dict(row)
        result, issue = _decode_json_envelope(
            job.get("result_json"),
            field="scheduled_result",
            expected_type=dict,
            allow_none=True,
        )
        job["result"] = result
        return _attach_json_issues(job, [issue] if issue else [])

    def get_scheduled_job(self, job_id: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._scheduled_job_view(row)

    def list_scheduled_jobs(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scheduled_jobs
                ORDER BY run_at ASC, created_at ASC
                """
            ).fetchall()
        return [self._scheduled_job_view(row) for row in rows]

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
        return [self._scheduled_job_view(row) for row in rows]

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
    ) -> bool:
        if status not in {"DONE", "FAILED"}:
            return False
        normalized_result = self._normalize_scheduled_result(result_json)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_jobs
                SET status = ?, error_message = ?, result_json = ?, updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING'
                """,
                (status, error_message, normalized_result, _utc_now(), job_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _normalize_scheduled_result(result_json: str | None) -> str | None:
        if result_json is None:
            return None
        result, issue = _decode_json_envelope(
            result_json,
            field="scheduled_result",
            expected_type=dict,
            allow_none=True,
        )
        if issue:
            raise ValueError(str(issue["cause_code"]))
        if result is None:
            return None
        return _json_envelope(_JSON_VERSIONS["scheduled_result"], result, dict)

    def reconcile_stale_running_jobs(
        self,
        cutoff: str,
        active_job_ids: Iterable[str] = (),
    ) -> list[str]:
        """Close abandoned RUNNING rows without touching active or terminal jobs."""
        active = {str(job_id) for job_id in active_job_ids}
        repaired: list[str] = []
        now = _utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id
                FROM scheduled_jobs
                WHERE status = 'RUNNING' AND updated_at < ?
                ORDER BY updated_at ASC, job_id ASC
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                if job_id in active:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE scheduled_jobs
                    SET status = 'FAILED',
                        error_message = 'scheduler_finish_failed',
                        result_json = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'RUNNING' AND updated_at < ?
                    """,
                    (now, job_id, cutoff),
                )
                if cursor.rowcount == 1:
                    repaired.append(job_id)
        return repaired

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
