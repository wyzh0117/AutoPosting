from __future__ import annotations

import hashlib
import json
import sqlite3
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
from .models import FeedItem, Source
from .normalize import content_hash


DB_SCHEMA_VERSION = 1
_JSON_VERSIONS = {
    "github_weekly_repos": "github-weekly-repos-v1",
    "github_weekly_images": "github-weekly-images-v1",
    "github_weekly_visual": "github-weekly-visual-v1",
    "github_weekly_diagnostics": "github-weekly-diagnostics-v1",
    "skill_finder_snapshot": "skill-finder-snapshot-v1",
    "favorite_payload": "favorite-payload-v1",
    "scheduled_result": "scheduled-result-v1",
}


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
    type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit')),
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
                type TEXT NOT NULL CHECK (type IN ('rss', 'web', 'x', 'sitemap', 'reddit')),
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
                SELECT source_id, name, type, tier, enabled, fetch_interval_minutes,
                       last_fetched_at, last_error
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
