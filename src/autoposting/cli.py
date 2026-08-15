from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .config import load_sources
from .content import Claim, Conflict, ContentWorkflow, FactList, FactValidationError
from .db import Database
from .hotspots import HotspotConfig, HotspotDetector, Observation
from .pipeline import collect_sources
from .publishing import PublicationService
from .selfcheck import run_self_check


DEFAULT_DATABASE = "data/autoposting.db"
DEFAULT_SOURCES = "config/sources.example.json"
DEFAULT_PACKAGES = "output/packages"


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DATABASE, help="SQLite database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autoposting",
        description="Local-first AI news collection and drafting MVP",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize SQLite and register sources")
    _add_database_argument(init_parser)
    init_parser.add_argument("--sources", default=DEFAULT_SOURCES)

    collect_parser = subparsers.add_parser("collect", help="Run one incremental RSS collection")
    _add_database_argument(collect_parser)
    collect_parser.add_argument("--sources", default=DEFAULT_SOURCES)
    collect_parser.add_argument("--force", action="store_true", help="Ignore fetch intervals")

    candidates_parser = subparsers.add_parser(
        "daily-candidates", help="List items observed in a recent time window"
    )
    _add_database_argument(candidates_parser)
    candidates_parser.add_argument("--hours", type=int, default=24)

    hotspot_parser = subparsers.add_parser(
        "hotspot-check", help="Evaluate transparent hotspot rules from JSON"
    )
    hotspot_parser.add_argument("--input", required=True, help="Observation JSON path")
    hotspot_parser.add_argument("--db", help="SQLite path; required only with --record")
    hotspot_parser.add_argument("--record", action="store_true")

    event_parser = subparsers.add_parser("event-create", help="Create an idempotent event")
    _add_database_argument(event_parser)
    event_parser.add_argument("--name", required=True)
    event_parser.add_argument("--topic", required=True)
    event_parser.add_argument("--stable-key", required=True)
    event_parser.add_argument("--trigger-type", default="DAILY")

    draft_parser = subparsers.add_parser(
        "draft", help="Validate facts and generate both platform drafts"
    )
    _add_database_argument(draft_parser)
    draft_parser.add_argument("--event-id", required=True)
    draft_parser.add_argument("--facts", required=True, help="Fact-list JSON path")

    approve_parser = subparsers.add_parser("approve", help="Approve one draft")
    _add_database_argument(approve_parser)
    approve_parser.add_argument("--draft-id", required=True)
    approve_parser.add_argument("--reviewer", required=True)

    reject_parser = subparsers.add_parser("reject", help="Reject one draft with a reason")
    _add_database_argument(reject_parser)
    reject_parser.add_argument("--draft-id", required=True)
    reject_parser.add_argument("--reviewer", required=True)
    reject_parser.add_argument("--reason", required=True)

    package_parser = subparsers.add_parser(
        "package", help="Write an approved draft to a local dry-run package"
    )
    _add_database_argument(package_parser)
    package_parser.add_argument("--draft-id", required=True)
    package_parser.add_argument("--output", default=DEFAULT_PACKAGES)
    package_parser.add_argument("--max-retries", type=int, default=3)

    self_check_parser = subparsers.add_parser(
        "self-check", help="Run the complete offline acceptance fixture"
    )
    self_check_parser.add_argument("--workspace", default="data/self-check")

    web_parser = subparsers.add_parser("web", help="Start the local HTTP API on 127.0.0.1")
    _add_database_argument(web_parser)
    web_parser.add_argument("--sources", default=DEFAULT_SOURCES)
    web_parser.add_argument("--packages", default=DEFAULT_PACKAGES)
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8080)

    return parser


def _database(path: str) -> Database:
    database = Database(path)
    database.initialize()
    return database


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(value)


def _load_fact_list(path: str | Path) -> FactList:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Fact list must be a JSON object")
    claims_payload = payload.get("claims")
    conflicts_payload = payload.get("conflicts", [])
    if not isinstance(payload.get("event"), str):
        raise ValueError("Fact list event must be a string")
    if not isinstance(claims_payload, list):
        raise ValueError("Fact list claims must be an array")
    if not isinstance(conflicts_payload, list):
        raise ValueError("Fact list conflicts must be an array")

    claims: list[Claim] = []
    for index, item in enumerate(claims_payload):
        if not isinstance(item, dict):
            raise ValueError(f"claims[{index}] must be an object")
        if not isinstance(item.get("text"), str) or not isinstance(item.get("confidence"), str):
            raise ValueError(f"claims[{index}] requires text and confidence strings")
        claims.append(
            Claim(
                text=item["text"],
                source_ids=_string_tuple(item.get("source_ids"), f"claims[{index}].source_ids"),
                confidence=item["confidence"],
                traceable=item.get("traceable", True),
                exceeds_evidence=item.get("exceeds_evidence", False),
                verbatim_quote_chars=item.get("verbatim_quote_chars", 0),
            )
        )

    conflicts: list[Conflict] = []
    for index, item in enumerate(conflicts_payload):
        if not isinstance(item, dict):
            raise ValueError(f"conflicts[{index}] must be an object")
        description = item.get("description")
        resolution = item.get("resolution")
        if not isinstance(description, str) or not isinstance(resolution, str):
            raise ValueError(f"conflicts[{index}] requires description and resolution strings")
        conflicts.append(
            Conflict(
                description=description,
                source_ids=_string_tuple(item.get("source_ids"), f"conflicts[{index}].source_ids"),
                resolution=resolution,
            )
        )

    return FactList(
        event=payload["event"],
        claims=tuple(claims),
        unknowns=_string_tuple(payload.get("unknowns", []), "unknowns"),
        conflicts=tuple(conflicts),
    )


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _hotspot_decision(path: str | Path):
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Hotspot input must be a JSON object")
    config_payload = payload.get("config", {})
    if not isinstance(config_payload, dict):
        raise ValueError("Hotspot config must be an object")
    allowed_config = set(HotspotConfig.__dataclass_fields__)
    unknown_config = set(config_payload) - allowed_config
    if unknown_config:
        raise ValueError(f"Unknown hotspot config fields: {', '.join(sorted(unknown_config))}")
    observations_payload = payload.get("observations")
    if not isinstance(observations_payload, list):
        raise ValueError("Hotspot observations must be an array")
    observations: list[Observation] = []
    for index, item in enumerate(observations_payload):
        if not isinstance(item, dict):
            raise ValueError(f"observations[{index}] must be an object")
        observations.append(
            Observation(
                source_id=str(item.get("source_id", "")),
                tier=str(item.get("tier", "")),
                observed_at=_parse_datetime(
                    item.get("observed_at"), f"observations[{index}].observed_at"
                ),
            )
        )
    baseline_counts = payload.get("baseline_counts")
    if not isinstance(baseline_counts, list) or not all(
        isinstance(value, int) and value >= 0 for value in baseline_counts
    ):
        raise ValueError("baseline_counts must be an array of non-negative integers")
    last_triggered = payload.get("last_triggered_at")
    return HotspotDetector(HotspotConfig(**config_payload)).evaluate(
        topic=str(payload.get("topic", "")),
        observations=observations,
        baseline_counts=baseline_counts,
        now=_parse_datetime(payload.get("now"), "now"),
        last_triggered_at=(
            _parse_datetime(last_triggered, "last_triggered_at")
            if last_triggered is not None
            else None
        ),
    )


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "init":
        database = _database(args.db)
        sources = load_sources(args.sources)
        for source in sources:
            database.upsert_source(source)
        return {"ok": True, "database": str(database.path), "sources_registered": len(sources)}

    if args.command == "collect":
        result = collect_sources(
            _database(args.db),
            load_sources(args.sources),
            force=args.force,
            x_bearer_token=os.environ.get("AUTOPOSTING_X_BEARER", ""),
        )
        return {"ok": result.failed == 0, **asdict(result)}

    if args.command == "daily-candidates":
        if args.hours <= 0:
            raise ValueError("hours must be positive")
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        items = _database(args.db).list_recent_items(since.isoformat())
        return {"ok": True, "since": since.isoformat(), "count": len(items), "items": items}

    if args.command == "hotspot-check":
        decision = _hotspot_decision(args.input)
        event_id = None
        if args.record:
            if not args.db:
                raise ValueError("--db is required with --record")
            if decision.triggered:
                event_id = _database(args.db).record_hotspot(decision)
        return {"ok": True, "decision": asdict(decision), "event_id": event_id}

    if args.command == "event-create":
        database = _database(args.db)
        event_id = database.create_event(
            args.name, args.topic, args.trigger_type, args.stable_key
        )
        return {"ok": True, "event_id": event_id, "status": database.get_event(event_id)["status"]}

    if args.command == "draft":
        database = _database(args.db)
        workflow = ContentWorkflow(database)
        facts = _load_fact_list(args.facts)
        validation = workflow.save_fact_list(args.event_id, facts)
        if not validation.valid:
            raise FactValidationError(",".join(validation.errors))
        drafts = workflow.generate_drafts(args.event_id, facts)
        return {
            "ok": True,
            "event_id": args.event_id,
            "validation": asdict(validation),
            "drafts": [asdict(draft) for draft in drafts],
        }

    if args.command == "approve":
        database = _database(args.db)
        ContentWorkflow(database).approve_draft(args.draft_id, args.reviewer)
        draft = database.get_draft(args.draft_id)
        return {
            "ok": True,
            "draft_id": args.draft_id,
            "draft_status": draft["status"],
            "event_status": database.get_event(str(draft["event_id"]))["status"],
        }

    if args.command == "reject":
        database = _database(args.db)
        ContentWorkflow(database).reject_draft(args.draft_id, args.reviewer, args.reason)
        draft = database.get_draft(args.draft_id)
        return {
            "ok": True,
            "draft_id": args.draft_id,
            "draft_status": draft["status"],
            "event_status": database.get_event(str(draft["event_id"]))["status"],
        }

    if args.command == "package":
        result = PublicationService(
            _database(args.db),
            args.output,
            dry_run=True,
            max_retries=args.max_retries,
        ).publish(args.draft_id)
        return {
            "ok": True,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "package_path": str(result.package_path),
            "archive_path": str(result.archive_path),
            "dry_run": True,
        }

    if args.command == "self-check":
        return run_self_check(args.workspace)

    if args.command == "web":
        from .web import WebApp, run_web

        app = WebApp(_database(args.db), load_sources(args.sources), args.packages)
        run_web(app, args.host, args.port)
        return {"ok": True, "stopped": True}

    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _execute(args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(main())