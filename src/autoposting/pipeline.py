from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging

from .diagnostics import (
    log_diagnostic,
    make_diagnostic,
    parse_diagnostic,
)
from .db import Database
from .ingestion import (
    AggregateCollector,
    Fetcher,
    RSSCollector,
    RedditCollector,
    SitemapNewsCollector,
    XCollector,
)
from .models import Source


@dataclass(frozen=True)
class CollectionBatchResult:
    inserted: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped_disabled: int = 0
    skipped_not_due: int = 0
    errors: dict[str, dict[str, object]] = field(default_factory=dict)
    diagnostics: dict[str, dict[str, object]] = field(default_factory=dict)
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    requested_sources: tuple[str, ...] = ()
    attempted_sources: tuple[str, ...] = ()
    successful_sources: tuple[str, ...] = ()
    failed_sources: tuple[str, ...] = ()
    items_fetched: int = 0
    items_after_dedupe: int = 0
    clusters_created_or_updated: int = 0
    degraded: bool = False
    source_attempts: tuple[dict[str, object], ...] = ()


LOGGER = logging.getLogger(__name__)


def _is_due(last_fetched_at: object, interval_minutes: int, now: datetime) -> bool:
    if not last_fetched_at:
        return True
    try:
        last_fetched = datetime.fromisoformat(str(last_fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last_fetched.tzinfo is None:
        last_fetched = last_fetched.replace(tzinfo=timezone.utc)
    return now >= last_fetched.astimezone(timezone.utc) + timedelta(minutes=interval_minutes)


def collect_sources(
    database: Database,
    sources: tuple[Source, ...] | list[Source],
    fetcher: Fetcher | None = None,
    force: bool = False,
    now: datetime | None = None,
    only_source_ids: tuple[str, ...] | None = None,
    x_bearer_token: str = "",
) -> CollectionBatchResult:
    current_time = now or datetime.now(timezone.utc)
    inserted = duplicates = failed = skipped_disabled = skipped_not_due = 0
    errors: dict[str, dict[str, object]] = {}
    diagnostics: dict[str, dict[str, object]] = {}
    due: list[Source] = []
    selected = tuple(sources)
    if only_source_ids is not None:
        wanted = set(only_source_ids)
        selected = tuple(source for source in sources if source.source_id in wanted)
        missing = wanted.difference(source.source_id for source in selected)
        if missing:
            raise ValueError(f"unknown_source_id:{sorted(missing)[0]}")

    # Register metadata first, then let the database decide runtime enabled and
    # interval values. This keeps a UI toggle from being overwritten by the
    # source JSON passed at process start.
    for source in selected:
        database.upsert_source(source)
    requested_ids = tuple(source.source_id for source in selected)
    run_id = database.start_collection_run(
        requested_ids, started_at=current_time.isoformat()
    )
    started_at = current_time.isoformat()

    def _record_attempt(
        source_id: str,
        status: str,
        *,
        items_fetched: int = 0,
        items_inserted: int = 0,
        items_after_dedupe: int = 0,
        diagnostic: dict[str, object] | None = None,
    ) -> None:
        database.record_collection_source_attempt(
            run_id,
            source_id,
            status,
            items_fetched=items_fetched,
            items_inserted=items_inserted,
            items_after_dedupe=items_after_dedupe,
            diagnostic=diagnostic,
        )

    try:
        for source in selected:
            stored_source = database.get_source(source.source_id)
            if not bool(stored_source["enabled"]):
                skipped_disabled += 1
                _record_attempt(source.source_id, "SKIPPED_DISABLED")
                continue
            interval = int(stored_source["fetch_interval_minutes"])
            if not force and not _is_due(
                stored_source["last_fetched_at"], interval, current_time
            ):
                skipped_not_due += 1
                _record_attempt(source.source_id, "SKIPPED_NOT_DUE")
                continue
            adapter_type = source.adapter_type or source.type
            if adapter_type == "x" and not x_bearer_token and fetcher is None:
                message = "x_bearer_token_required"
                diagnostic = make_diagnostic(
                    source.source_id,
                    "collect",
                    code="source_fetch_failed",
                    cause_code="credential_required",
                    detail=message,
                )
                database.record_fetch_result(source.source_id, diagnostic)
                errors[source.source_id] = diagnostic
                diagnostics[source.source_id] = diagnostic
                failed += 1
                _record_attempt(source.source_id, "FAILED", diagnostic=diagnostic)
                continue
            if adapter_type not in {"rss", "html", "x", "sitemap", "reddit"}:
                message = f"collector_not_implemented_for_{adapter_type}"
                diagnostic = make_diagnostic(
                    source.source_id,
                    "collect",
                    code="source_fetch_failed",
                    cause_code="unsupported_source",
                    detail=message,
                )
                database.record_fetch_result(source.source_id, diagnostic)
                errors[source.source_id] = diagnostic
                diagnostics[source.source_id] = diagnostic
                failed += 1
                _record_attempt(source.source_id, "FAILED", diagnostic=diagnostic)
                continue
            due.append(source)

        for source in due:
            _record_attempt(source.source_id, "RUNNING")

        workers = 1 if fetcher is not None else min(6, max(len(due), 1))

        def _run(source: Source) -> tuple[str, object]:
            try:
                adapter_type = source.adapter_type or source.type
                if adapter_type == "rss":
                    collector = RSSCollector(database, fetcher=fetcher)
                elif adapter_type == "html":
                    collector = AggregateCollector(database, fetcher=fetcher)
                elif adapter_type == "sitemap":
                    collector = SitemapNewsCollector(database, fetcher=fetcher)
                elif adapter_type == "reddit":
                    collector = RedditCollector(database, fetcher=fetcher)
                else:
                    collector = XCollector(
                        database, fetcher=fetcher, bearer_token=x_bearer_token
                    )
                return source.source_id, collector.collect(source)
            except Exception as exc:
                return source.source_id, exc

        if due:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for source_id, outcome in pool.map(_run, due):
                    if isinstance(outcome, Exception):
                        diagnostic = make_diagnostic(source_id, "collect", outcome)
                        database.record_fetch_result(source_id, diagnostic)
                        diagnostics[source_id] = diagnostic
                        errors[source_id] = diagnostic
                        log_diagnostic(LOGGER, diagnostic, outcome)
                        failed += 1
                        _record_attempt(source_id, "FAILED", diagnostic=diagnostic)
                        continue
                    fetched = max(0, int(outcome.inserted) + int(outcome.duplicates))
                    after_dedupe = max(0, fetched - int(outcome.duplicates))
                    inserted += outcome.inserted
                    duplicates += outcome.duplicates
                    failed += outcome.failed
                    if outcome.failed:
                        stored = database.get_source(source_id)
                        diagnostic = parse_diagnostic(stored.get("last_error")) or make_diagnostic(
                            source_id,
                            "collect",
                            code="source_fetch_failed",
                            cause_code="collector_reported_failure",
                            detail="source_failed",
                        )
                        diagnostics[source_id] = diagnostic
                        errors[source_id] = diagnostic
                        _record_attempt(
                            source_id,
                            "FAILED",
                            items_fetched=fetched,
                            items_inserted=outcome.inserted,
                            items_after_dedupe=after_dedupe,
                            diagnostic=diagnostic,
                        )
                    else:
                        _record_attempt(
                            source_id,
                            "SUCCESS",
                            items_fetched=fetched,
                            items_inserted=outcome.inserted,
                            items_after_dedupe=after_dedupe,
                        )

        final = database.finish_collection_run(
            run_id,
            status="FAILED" if failed else "DONE",
            diagnostics=diagnostics.values(),
        )
    except Exception:
        try:
            final = database.finish_collection_run(
                run_id,
                status="FAILED",
                diagnostics=diagnostics.values(),
            )
        except Exception as finalize_error:  # pragma: no cover - storage failure path
            LOGGER.error(
                "collection run finalize failed run_id=%s error_type=%s",
                run_id,
                type(finalize_error).__name__,
            )
        raise

    attempted = tuple(str(value) for value in final["attempted_sources"])
    successful = tuple(str(value) for value in final["successful_sources"])
    failed_ids = tuple(str(value) for value in final["failed_sources"])
    source_attempts = tuple(
        item for item in final["source_attempts"] if isinstance(item, dict)
    )
    return CollectionBatchResult(
        inserted=inserted,
        duplicates=duplicates,
        failed=failed,
        skipped_disabled=skipped_disabled,
        skipped_not_due=skipped_not_due,
        errors=errors,
        diagnostics=diagnostics,
        run_id=run_id,
        started_at=str(final["started_at"]),
        finished_at=str(final["finished_at"]),
        requested_sources=requested_ids,
        attempted_sources=attempted,
        successful_sources=successful,
        failed_sources=failed_ids,
        items_fetched=int(final["items_fetched"]),
        items_after_dedupe=int(final["items_after_dedupe"]),
        clusters_created_or_updated=int(final["clusters_created_or_updated"]),
        degraded=bool(final["degraded"]),
        source_attempts=source_attempts,
    )
