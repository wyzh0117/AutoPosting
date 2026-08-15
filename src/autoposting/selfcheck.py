from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .content import Claim, ContentWorkflow, FactList, FactValidationError
from .db import Database
from .hotspots import HotspotConfig, HotspotDetector, Observation
from .ingestion import RSSCollector
from .models import Source
from .publishing import PublicationService


SELF_CHECK_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Self Check Lab</title><item>
<guid>self-check-release-1</guid><title>Self Check Model released</title>
<link>https://example.invalid/news/model?utm_source=rss</link>
<description><![CDATA[<p>The model is available in the self-check fixture.</p>]]></description>
<pubDate>Sat, 08 Aug 2026 08:00:00 GMT</pubDate>
</item></channel></rss>
"""


def run_self_check(workspace: str | Path) -> dict[str, object]:
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    database_path = root / "self-check.db"
    database_path.unlink(missing_ok=True)
    database = Database(database_path)
    database.initialize()
    source = Source(
        source_id="self_check_lab",
        name="Self Check Lab",
        type="rss",
        url_or_handle="https://example.invalid/feed.xml",
        tier="S1",
        topics=("models",),
        enabled=True,
        fetch_interval_minutes=15,
    )
    database.upsert_source(source)
    collector = RSSCollector(database, fetcher=lambda _: SELF_CHECK_FEED)
    first_collection = collector.collect(source)
    second_collection = collector.collect(source)

    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    observations = [
        Observation(
            source_id=("self_check_lab", "staff_verified", "media_verified")[index % 3],
            tier=("S1", "S2", "S3")[index % 3],
            observed_at=now - timedelta(minutes=index * 5),
        )
        for index in range(6)
    ]
    detector = HotspotDetector(HotspotConfig())
    triggered = detector.evaluate("models", observations, [1] * 7, now)
    not_triggered = detector.evaluate("models", observations, [8] * 7, now)

    invalid_event_id = database.create_event(
        "Untraceable event", "models", "SELF_CHECK", "self-check:invalid"
    )
    invalid_facts = FactList(
        event="Untraceable event",
        claims=(Claim("An unsupported statement.", (), "high"),),
        unknowns=(),
        conflicts=(),
    )
    workflow = ContentWorkflow(database)
    invalid_validation = workflow.save_fact_list(invalid_event_id, invalid_facts)
    missing_sources_blocked = False
    try:
        workflow.generate_drafts(invalid_event_id, invalid_facts)
    except FactValidationError:
        missing_sources_blocked = True
    invalid_event = database.get_event(invalid_event_id)

    valid_event_id = database.create_event(
        "Self Check Model release", "models", "SELF_CHECK", "self-check:valid"
    )
    valid_facts = FactList(
        event="Self Check Model release",
        claims=(
            Claim(
                "The fixture says the model is available.",
                ("self_check_lab",),
                "high",
            ),
        ),
        unknowns=("Real-world availability is not checked by this offline fixture.",),
        conflicts=(),
    )
    valid_validation = workflow.save_fact_list(valid_event_id, valid_facts)
    drafts = workflow.generate_drafts(valid_event_id, valid_facts)
    for draft in drafts:
        workflow.approve_draft(draft.draft_id, "self-check-human")
    publisher = PublicationService(database, root / "packages", dry_run=True, max_retries=3)
    first_package = publisher.publish(drafts[0].draft_id)
    second_package = publisher.publish(drafts[0].draft_id)
    package_payload = json.loads(first_package.package_path.read_text(encoding="utf-8"))

    checks = {
        "new_content_persisted": first_collection.inserted == 1 and database.count_items() == 1,
        "duplicate_collection_idempotent": (
            second_collection.inserted == 0 and second_collection.duplicates == 1
        ),
        "hotspot_trigger_stable": triggered.triggered and not not_triggered.triggered,
        "missing_sources_blocked": (
            not invalid_validation.valid
            and missing_sources_blocked
            and invalid_event["status"] == "FAILED"
        ),
        "failure_has_error": bool(invalid_event["error_message"]),
        "facts_and_platform_drafts_created": valid_validation.valid and len(drafts) == 2,
        "platform_drafts_are_distinct": drafts[0].body != drafts[1].body,
        "approval_required": database.get_event(valid_event_id)["status"] == "APPROVED",
        "dry_run_is_idempotent": first_package.attempt_id == second_package.attempt_id,
        "dry_run_has_no_publish_transition": (
            package_payload["dry_run"] is True
            and database.get_draft(drafts[0].draft_id)["status"] == "APPROVED"
            and database.get_event(valid_event_id)["status"] == "APPROVED"
        ),
    }
    metrics = {
        "items_after_duplicate_collection": database.count_items(),
        "events": database.count_events(),
        "drafts": database.count_drafts(),
        "dry_run_attempts": database.count_publication_attempts(),
    }
    return {"ok": all(checks.values()), "checks": checks, "metrics": metrics}