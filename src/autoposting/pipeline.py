from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .db import Database
from .ingestion import (
    AggregateCollector,
    Fetcher,
    RSSCollector,
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
    errors: dict[str, str] = field(default_factory=dict)


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
    errors: dict[str, str] = {}
    due: list[Source] = []
    selected = tuple(sources)
    if only_source_ids is not None:
        wanted = set(only_source_ids)
        selected = tuple(source for source in sources if source.source_id in wanted)
        missing = wanted.difference(source.source_id for source in selected)
        if missing:
            raise ValueError(f"unknown_source_id:{sorted(missing)[0]}")

    for source in selected:
        database.upsert_source(source)
        if not source.enabled:
            skipped_disabled += 1
            continue
        stored_source = database.get_source(source.source_id)
        if not force and not _is_due(
            stored_source["last_fetched_at"], source.fetch_interval_minutes, current_time
        ):
            skipped_not_due += 1
            continue
        if source.type == "x" and not x_bearer_token and fetcher is None:
            message = "x_bearer_token_required"
            database.record_fetch_result(source.source_id, message)
            errors[source.source_id] = message
            failed += 1
            continue
        if source.type not in {"rss", "web", "x", "sitemap"}:
            message = f"collector_not_implemented_for_{source.type}"
            database.record_fetch_result(source.source_id, message)
            errors[source.source_id] = message
            failed += 1
            continue
        due.append(source)

    workers = 1 if fetcher is not None else min(6, max(len(due), 1))

    def _run(source: Source) -> tuple[str, object]:
        try:
            if source.type == "rss":
                collector = RSSCollector(database, fetcher=fetcher)
            elif source.type == "web":
                collector = AggregateCollector(database, fetcher=fetcher)
            elif source.type == "sitemap":
                collector = SitemapNewsCollector(database, fetcher=fetcher)
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
                    errors[source_id] = str(outcome)
                    failed += 1
                    continue
                inserted += outcome.inserted
                duplicates += outcome.duplicates

    return CollectionBatchResult(
        inserted=inserted,
        duplicates=duplicates,
        failed=failed,
        skipped_disabled=skipped_disabled,
        skipped_not_due=skipped_not_due,
        errors=errors,
    )