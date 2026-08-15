from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .db import Database


class PublicationError(RuntimeError):
    pass


class LivePublishingDisabled(PublicationError):
    pass


@dataclass(frozen=True)
class PublicationResult:
    attempt_id: str
    status: str
    package_path: Path
    archive_path: Path


class PublicationService:
    def __init__(
        self,
        database: Database,
        output_directory: str | Path,
        dry_run: bool = True,
        max_retries: int = 3,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.database = database
        self.output_directory = Path(output_directory)
        self.dry_run = dry_run
        self.max_retries = max_retries

    def publish(self, draft_id: str) -> PublicationResult:
        draft = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise PublicationError("draft_superseded")
        attempt_id = self.database.publication_attempt_id(
            draft_id,
            str(draft["content_hash"]),
            self.dry_run,
        )
        if draft["status"] != "APPROVED":
            self.database.record_publication_attempt(
                attempt_id,
                draft_id,
                str(draft["platform"]),
                self.dry_run,
                "FAILED",
                "draft_not_approved",
                min(1, self.max_retries),
            )
            raise PublicationError("draft_not_approved")
        event = self.database.get_event(str(draft["event_id"]))
        if event["status"] != "APPROVED":
            self.database.record_publication_attempt(
                attempt_id,
                draft_id,
                str(draft["platform"]),
                self.dry_run,
                "FAILED",
                "event_not_approved",
                min(1, self.max_retries),
            )
            raise PublicationError("event_not_approved")
        if not self.dry_run:
            self.database.record_publication_attempt(
                attempt_id,
                draft_id,
                str(draft["platform"]),
                False,
                "FAILED",
                "live_publishing_disabled",
                min(1, self.max_retries),
            )
            raise LivePublishingDisabled("live_publishing_disabled")
        archive_path = self.archive_draft(draft_id)
        existing = self.database.get_publication_attempt(attempt_id)
        if existing and existing["status"] == "DRY_RUN":
            return PublicationResult(
                attempt_id=attempt_id,
                status="DRY_RUN",
                package_path=Path(str(existing["response_reference"])),
                archive_path=archive_path,
            )

        self.output_directory.mkdir(parents=True, exist_ok=True)
        package_path = self.output_directory / f"{draft_id}-{draft['platform']}.json"
        payload = {
            "schema_version": "publication-package-v1",
            "dry_run": True,
            "draft_id": draft_id,
            "platform": draft["platform"],
            "title": draft["title"],
            "body": draft["body"],
            "tags": json.loads(str(draft["tags_json"])),
            "assets": json.loads(str(draft["assets_json"])),
            "source_ids": json.loads(str(draft["source_ids_json"])),
            "content_hash": draft["content_hash"],
            "aigc_disclosure_required": True,
        }
        temporary_path = package_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(package_path)
        self.database.record_publication_attempt(
            attempt_id,
            draft_id,
            str(draft["platform"]),
            True,
            "DRY_RUN",
            None,
            0,
            str(package_path),
        )
        return PublicationResult(attempt_id, "DRY_RUN", package_path, archive_path)

    def archive_draft(self, draft_id: str) -> Path:
        """Write the approved final article to a local, content-addressed archive."""
        draft = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise PublicationError("draft_superseded")
        if draft["status"] != "APPROVED":
            raise PublicationError("draft_not_approved")
        event = self.database.get_event(str(draft["event_id"]))
        if event["status"] != "APPROVED":
            raise PublicationError("event_not_approved")

        archive_directory = self.output_directory.parent / "archives"
        archive_directory.mkdir(parents=True, exist_ok=True)
        archive_path = archive_directory / (
            f"{draft_id}-{draft['platform']}-{draft['content_hash']}.json"
        )
        payload = {
            "schema_version": "article-archive-v1",
            "draft_id": draft_id,
            "event_id": draft["event_id"],
            "platform": draft["platform"],
            "title": draft["title"],
            "body": draft["body"],
            "tags": json.loads(str(draft["tags_json"])),
            "assets": json.loads(str(draft["assets_json"])),
            "source_ids": json.loads(str(draft["source_ids_json"])),
            "content_hash": draft["content_hash"],
        }
        temporary_path = archive_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(archive_path)
        return archive_path