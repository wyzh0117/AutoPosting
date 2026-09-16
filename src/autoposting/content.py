from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass

from .db import Database
from .images import (
    apply_selection,
    normalize_assets,
    strip_image_credits,
)
from .engagement import frame_article_body
from .normalize import contains_source_id


class FactValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    text: str
    source_ids: tuple[str, ...]
    confidence: str
    traceable: bool = True
    exceeds_evidence: bool = False
    verbatim_quote_chars: int = 0


@dataclass(frozen=True)
class Conflict:
    description: str
    source_ids: tuple[str, ...]
    resolution: str


@dataclass(frozen=True)
class FactList:
    event: str
    claims: tuple[Claim, ...]
    unknowns: tuple[str, ...]
    conflicts: tuple[Conflict, ...]


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class Draft:
    draft_id: str
    event_id: str
    platform: str
    title: str
    body: str
    tags: tuple[str, ...]
    source_ids: tuple[str, ...]
    generator: str = "deterministic-mock-v1"
    processing_version: str = "draft-v1"
    assets: object = ()


def validate_fact_list(facts: FactList) -> ValidationResult:
    errors: list[str] = []
    if not facts.event.strip():
        errors.append("event_missing")
    if not facts.claims:
        errors.append("claims_missing")
    for claim in facts.claims:
        if not claim.text.strip():
            errors.append("claim_text_missing")
        if not claim.source_ids:
            errors.append("claim_missing_sources")
        if claim.confidence not in {"high", "medium", "low"}:
            errors.append("claim_invalid_confidence")
        if not claim.traceable:
            errors.append("claim_not_traceable")
        if claim.exceeds_evidence:
            errors.append("claim_exceeds_evidence")
        if claim.verbatim_quote_chars > 280:
            errors.append("claim_excessive_verbatim_quote")
    for conflict in facts.conflicts:
        if len(set(conflict.source_ids)) < 2 or not conflict.resolution.strip():
            errors.append("conflict_unresolved")
    unique_errors = tuple(dict.fromkeys(errors))
    return ValidationResult(valid=not unique_errors, errors=unique_errors)


def _fact_list_json(facts: FactList) -> str:
    return json.dumps(
        asdict(facts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _claim_lines(facts: FactList, prefix: str) -> list[str]:
    return [
        f"{prefix}{claim.text}（来源：{', '.join(claim.source_ids)}；置信度：{claim.confidence}）"
        for claim in facts.claims
    ]


def _wechat_draft(event_id: str, facts: FactList, source_ids: tuple[str, ...]) -> Draft:
    sections = [
        facts.event,
        "",
        "已确认事实",
        *_claim_lines(facts, "- "),
    ]
    if facts.unknowns:
        sections.extend(["", "仍待确认", *[f"- {item}" for item in facts.unknowns]])
    sections.extend(
        [
            "",
            f"来源索引：{', '.join(source_ids)}",
            "AIGC 声明：本文由自动化工具依据所列来源辅助生成，发布前需人工审核。",
        ]
    )
    body = frame_article_body(
        "\n".join(sections),
        [{"source_id": source_id, "name": source_id} for source_id in source_ids],
    )
    return Draft(
        draft_id=hashlib.sha256(f"{event_id}:wechat:draft-v1".encode()).hexdigest()[:24],
        event_id=event_id,
        platform="wechat",
        title=f"{facts.event}：已确认信息与待解问题",
        body=body,
        tags=("AI资讯", "事实核查"),
        source_ids=source_ids,
    )


def _xiaohongshu_draft(event_id: str, facts: FactList, source_ids: tuple[str, ...]) -> Draft:
    sections = [
        f"📌 {facts.event}",
        "",
        "关键信息",
        *_claim_lines(facts, "✓ "),
    ]
    if facts.unknowns:
        sections.extend(["", "还不知道什么？", *[f"？{item}" for item in facts.unknowns]])
    sections.extend(
        [
            "",
            f"信息源：{', '.join(source_ids)}",
            "AIGC 声明：内容由 AI 辅助整理，已保留来源，发布前需人工确认。",
            "#AI资讯 #科技动态 #事实核查",
        ]
    )
    body = frame_article_body(
        "\n".join(sections),
        [{"source_id": source_id, "name": source_id} for source_id in source_ids],
    )
    return Draft(
        draft_id=hashlib.sha256(f"{event_id}:xiaohongshu:draft-v1".encode()).hexdigest()[:24],
        event_id=event_id,
        platform="xiaohongshu",
        title=f"{facts.event}｜重点速览",
        body=body,
        tags=("AI资讯", "科技动态", "事实核查"),
        source_ids=source_ids,
    )


class ContentWorkflow:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_fact_list(self, event_id: str, facts: FactList) -> ValidationResult:
        result = validate_fact_list(facts)
        referenced_source_ids = {
            source_id
            for claim in facts.claims
            for source_id in claim.source_ids
        } | {
            source_id
            for conflict in facts.conflicts
            for source_id in conflict.source_ids
        }
        unknown_source_ids = referenced_source_ids - self.database.registered_source_ids(
            referenced_source_ids
        )
        extra_errors: list[str] = []
        if unknown_source_ids:
            extra_errors.extend(
                f"claim_unknown_source:{source_id}" for source_id in sorted(unknown_source_ids)
            )
        if extra_errors:
            result = ValidationResult(
                valid=False,
                errors=tuple(dict.fromkeys(result.errors + tuple(extra_errors))),
            )
        fact_list_json = _fact_list_json(facts)
        event = self.database.get_event(event_id)
        if (
            result.valid
            and event["fact_list_json"] == fact_list_json
            and event["status"] in {"DRAFTED", "APPROVED", "REJECTED"}
        ):
            return result
        self.database.replace_claims(event_id, facts, result, fact_list_json)
        self.database.update_event_status(
            event_id,
            "VERIFIED" if result.valid else "FAILED",
            None if result.valid else ",".join(result.errors),
        )
        return result

    def generate_draft(self, event_id: str, facts: FactList, platform: str) -> Draft:
        if platform not in {"wechat", "xiaohongshu"}:
            raise ValueError(f"unsupported platform: {platform}")
        result = validate_fact_list(facts)
        if not result.valid:
            raise FactValidationError(",".join(result.errors))
        event = self.database.get_event(event_id)
        if event["status"] not in {"VERIFIED", "DRAFTED", "APPROVED"}:
            raise FactValidationError("event_not_ready_for_drafting")
        if event["fact_list_json"] != _fact_list_json(facts):
            raise FactValidationError("fact_list_not_verified")
        source_ids = tuple(
            sorted({source_id for claim in facts.claims for source_id in claim.source_ids})
        )
        draft = (
            _wechat_draft(event_id, facts, source_ids)
            if platform == "wechat"
            else _xiaohongshu_draft(event_id, facts, source_ids)
        )
        outcome = self.database.upsert_draft(draft)
        if outcome != "unchanged":
            self.database.update_event_status(event_id, "DRAFTED")
        return draft

    def generate_drafts(self, event_id: str, facts: FactList) -> tuple[Draft, ...]:
        return (
            self.generate_draft(event_id, facts, "wechat"),
            self.generate_draft(event_id, facts, "xiaohongshu"),
        )

    def edit_draft(self, draft_id: str, title: str, body: str) -> dict[str, object]:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("draft_title_required")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("draft_body_required")
        draft_row = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        platform = str(draft_row["platform"])
        source_ids = tuple(json.loads(str(draft_row["source_ids_json"])))
        if "AIGC" not in body:
            raise ValueError("aigc_disclosure_required")
        source_urls = self.database.get_source_urls(source_ids)
        if any(
            not contains_source_id(body, source_id)
            and not any(url and url in body for url in source_urls.get(source_id, set()))
            for source_id in source_ids
        ):
            raise ValueError("source_ids_required")
        tags = tuple(json.loads(str(draft_row["tags_json"])))
        try:
            parsed_assets = json.loads(str(draft_row["assets_json"]))
        except json.JSONDecodeError:
            parsed_assets = ()
        assets = parsed_assets if isinstance(parsed_assets, (dict, list, tuple)) else ()
        draft = Draft(
            draft_id=draft_id,
            event_id=str(draft_row["event_id"]),
            platform=platform,
            title=title.strip(),
            body=body,
            tags=tags,
            source_ids=source_ids,
            generator=str(draft_row["generator"]),
            processing_version=str(draft_row["processing_version"]),
            assets=assets,
        )
        outcome = self.database.upsert_draft(draft)
        if outcome != "unchanged":
            self.database.update_event_status(str(draft_row["event_id"]), "DRAFTED")
        return self.database.get_draft(draft_id)

    def update_draft_images(
        self, draft_id: str, cover_id: str, body_ids: list[str]
    ) -> dict[str, object]:
        draft_row = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        bundle = apply_selection(
            normalize_assets(draft_row["assets_json"]), cover_id, body_ids
        )
        body = strip_image_credits(str(draft_row["body"]))
        tags = tuple(json.loads(str(draft_row["tags_json"])))
        source_ids = tuple(json.loads(str(draft_row["source_ids_json"])))
        draft = Draft(
            draft_id=draft_id,
            event_id=str(draft_row["event_id"]),
            platform=str(draft_row["platform"]),
            title=str(draft_row["title"]),
            body=body,
            tags=tags,
            source_ids=source_ids,
            generator=str(draft_row["generator"]),
            processing_version=str(draft_row["processing_version"]),
            assets=bundle,
        )
        self.database.upsert_draft(draft)
        return self.database.get_draft(draft_id)

    def replace_draft_image_bundle(
        self, draft_id: str, bundle: dict[str, object]
    ) -> dict[str, object]:
        draft_row = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        normalized = normalize_assets(bundle)
        body = strip_image_credits(str(draft_row["body"]))
        tags = tuple(json.loads(str(draft_row["tags_json"])))
        source_ids = tuple(json.loads(str(draft_row["source_ids_json"])))
        draft = Draft(
            draft_id=draft_id,
            event_id=str(draft_row["event_id"]),
            platform=str(draft_row["platform"]),
            title=str(draft_row["title"]),
            body=body,
            tags=tags,
            source_ids=source_ids,
            generator=str(draft_row["generator"]),
            processing_version=str(draft_row["processing_version"]),
            assets=normalized,
        )
        self.database.upsert_draft(draft)
        return self.database.get_draft(draft_id)

    def approve_draft(self, draft_id: str, approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("approved_by is required")
        draft = self.database.get_draft(draft_id)
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        event = self.database.get_event(str(draft["event_id"]))
        if event["status"] not in {"DRAFTED", "APPROVED"}:
            raise ValueError("event_not_ready_for_approval")
        event_id = self.database.approve_draft(draft_id, approved_by)
        if self.database.event_has_all_platforms_approved(
            event_id, str(draft["processing_version"])
        ):
            self.database.update_event_status(event_id, "APPROVED")

    def reject_draft(self, draft_id: str, reviewed_by: str, reason: str) -> None:
        if not reviewed_by.strip():
            raise ValueError("reviewed_by is required")
        if not reason.strip():
            raise ValueError("rejection reason is required")
        if not self.database.is_current_draft(draft_id):
            raise ValueError("draft_superseded")
        event_id = self.database.reject_draft(draft_id, reviewed_by, reason)
        self.database.update_event_status(event_id, "REJECTED", reason)
