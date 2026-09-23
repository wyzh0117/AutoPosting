"""ArticleGenerationContext: the one input contract for article generation.

Article Service consumes a finished research result. It must never invent
material and it must never start a second research run, so the interesting
cases here are the failures: a dossier that cannot support an article has to
fail loudly with a stable reason instead of silently searching again.
"""

import json
import tempfile
import unittest
from pathlib import Path

from autoposting.article_context import (
    ARTICLE_CONTEXT_VERSION,
    DOSSIER_SCHEMA_VERSION,
    MAX_EVIDENCE_ITEMS,
    SOURCE_TYPE_MANUAL,
    SOURCE_TYPE_RESOURCE_SET,
    SOURCE_TYPE_TOPIC_DOSSIER,
    ArticleContextError,
    build_dossier_context,
    build_manual_context,
    build_resource_set_context,
)
from autoposting.db import Database


def _dossier(**overrides: object) -> dict[str, object]:
    dossier: dict[str, object] = {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "dossier_id": "dossier-1",
        "title": "模型发布研究",
        "query": "模型发布",
        "event_ids": ["event-1"],
        "status": "ok",
        "degraded": False,
        "created_at": "2026-01-01T00:00:00+00:00",
        "topics": [
            {
                "title": "模型发布",
                "topic": "新模型",
                "source_ids": ["lab_blog"],
            }
        ],
        "evidence": [
            {
                "evidence_id": "e1",
                "title": "官方研究说明",
                "url": "https://example.com/research",
                "snippet": "研究摘要",
                "source": "lab_blog",
                "relation": "official",
                "reader_status": "full",
                "reader_quality": 0.9,
                "assessment": {"decision": "include"},
            }
        ],
        "source_refs": [
            {
                "source_id": "lab_blog",
                "citation_id": "lab_blog",
                "source_url": "https://lab.example/model-one",
            }
        ],
    }
    dossier.update(overrides)
    return dossier


class DossierContextTests(unittest.TestCase):
    def test_dossier_maps_to_the_versioned_context_contract(self) -> None:
        context = build_dossier_context(_dossier())

        self.assertEqual(SOURCE_TYPE_TOPIC_DOSSIER, context.source_type)
        self.assertEqual("dossier-1", context.dossier_id)
        self.assertEqual("模型发布研究", context.primary_subject)
        self.assertEqual(1, context.evidence_count)
        self.assertEqual(("lab_blog",), context.source_ids)
        self.assertIn("模型发布研究", context.material)
        self.assertIn("官方研究说明", context.material)
        self.assertEqual("模型发布研究", context.background["focus"])

        payload = context.to_dict()
        self.assertEqual(ARTICLE_CONTEXT_VERSION, payload["schema_version"])
        self.assertEqual("dossier-1", payload["metadata"]["dossier_id"])

        # The model-facing payload carries the context itself, so the prompt can
        # state which contract produced the material.
        prompt_background = context.prompt_background()
        self.assertEqual(
            ARTICLE_CONTEXT_VERSION,
            prompt_background["article_context"]["schema_version"],
        )
        self.assertEqual(
            "dossier-1", prompt_background["article_context"]["metadata"]["dossier_id"]
        )

    def test_context_is_json_serialisable_for_the_prompt_payload(self) -> None:
        context = build_dossier_context(_dossier())
        text = json.dumps(context.background, ensure_ascii=False)
        self.assertIn("dossier-1", text)

    def test_missing_or_malformed_dossier_fails_without_searching(self) -> None:
        cases = [
            (None, "dossier_invalid"),
            ({}, "dossier_invalid"),
            (_dossier(schema_version="topic-dossier-v0"), "dossier_invalid"),
            (_dossier(dossier_id=""), "dossier_invalid"),
            (_dossier(title=""), "dossier_invalid"),
            (_dossier(event_ids=[]), "dossier_invalid"),
            (_dossier(evidence="not-a-list"), "dossier_evidence_invalid"),
        ]
        for dossier, code in cases:
            with self.subTest(code=code, dossier=dossier):
                with self.assertRaises(ArticleContextError) as raised:
                    build_dossier_context(dossier)
                self.assertEqual(code, raised.exception.code)
                self.assertTrue(raised.exception.reason.strip())

    def test_dossier_without_evidence_or_official_sources_fails_loudly(self) -> None:
        with self.assertRaises(ArticleContextError) as raised:
            build_dossier_context(_dossier(evidence=[]))
        self.assertEqual("dossier_evidence_required", raised.exception.code)

        # A dossier whose topics carry no official source cannot produce the
        # citation ids the article body is required to contain.
        with self.assertRaises(ArticleContextError) as raised:
            build_dossier_context(
                _dossier(topics=[{"title": "无来源", "source_ids": []}], source_refs=[])
            )
        self.assertEqual("dossier_sources_required", raised.exception.code)

    def test_error_payload_is_stable_and_user_facing(self) -> None:
        with self.assertRaises(ArticleContextError) as raised:
            build_dossier_context(None)
        payload = raised.exception.to_payload()
        self.assertEqual({"error", "reason"}, set(payload))
        self.assertEqual("dossier_invalid", payload["error"])
        self.assertIn("Topic Dossier", payload["reason"])

    def test_evidence_overflow_is_capped_instead_of_unbounded(self) -> None:
        rows = [
            {
                "evidence_id": f"e{index}",
                "title": f"证据 {index}",
                "url": f"https://example.com/{index}",
                "source": "lab_blog",
            }
            for index in range(MAX_EVIDENCE_ITEMS + 25)
        ]
        context = build_dossier_context(_dossier(evidence=rows))
        self.assertEqual(MAX_EVIDENCE_ITEMS, context.evidence_count)

    def test_selected_images_travel_with_the_context(self) -> None:
        context = build_dossier_context(
            _dossier(), selected_images=[{"url": "https://img.example/a.png", "kind": "cover"}]
        )
        self.assertEqual(
            ({"url": "https://img.example/a.png", "kind": "cover"},), context.selected_images
        )

    def test_failed_reader_evidence_still_reaches_the_material(self) -> None:
        # A degraded reader is a fact about the material, not a reason to drop
        # the row: the writer has to be able to see what could not be read.
        context = build_dossier_context(
            _dossier(
                evidence=[
                    {
                        "evidence_id": "e1",
                        "title": "读不到的页面",
                        "url": "https://example.com/unreadable",
                        "source": "lab_blog",
                        "reader_status": "failed",
                        "assessment": {"decision": "include"},
                    }
                ]
            )
        )
        self.assertEqual(1, context.evidence_count)
        self.assertEqual("failed", context.evidence[0]["reader_status"])
        self.assertIn("读不到的页面", context.material)


class ManualContextTests(unittest.TestCase):
    def test_manual_context_wraps_an_item_without_researching(self) -> None:
        context = build_manual_context(
            primary_subject="Model One news",
            material="Model One news 内容",
            research_summary="摘要",
            source_refs=[{"source_id": "lab_blog", "citation_id": "lab_blog"}],
        )
        self.assertEqual(SOURCE_TYPE_MANUAL, context.source_type)
        self.assertEqual(("lab_blog",), context.source_ids)
        self.assertEqual((), context.evidence)
        self.assertEqual("Model One news", context.background["focus"])
        self.assertEqual(
            ARTICLE_CONTEXT_VERSION,
            context.background["article_context"]["schema_version"],
        )

    def test_manual_context_keeps_caller_background_fields(self) -> None:
        context = build_manual_context(
            primary_subject="主题",
            material="素材",
            background={"query": "查询", "source_excerpt": "摘录", "pasted_page": "粘贴正文"},
        )
        self.assertEqual("查询", context.background["query"])
        self.assertEqual("粘贴正文", context.background["pasted_page"])

    def test_manual_context_rejects_empty_subject_or_material(self) -> None:
        with self.assertRaises(ArticleContextError) as raised:
            build_manual_context(primary_subject="  ", material="素材")
        self.assertEqual("context_subject_required", raised.exception.code)

        with self.assertRaises(ArticleContextError) as raised:
            build_manual_context(primary_subject="主题", material="   ")
        self.assertEqual("context_material_required", raised.exception.code)

    def test_manual_context_drops_source_refs_without_an_id(self) -> None:
        context = build_manual_context(
            primary_subject="主题",
            material="素材",
            source_refs=[{"source_id": ""}, {"source_id": "lab_blog"}],
        )
        self.assertEqual(("lab_blog",), context.source_ids)


class ResourceSetContextTests(unittest.TestCase):
    def test_resource_set_maps_to_a_thin_reference_block(self) -> None:
        context = build_resource_set_context(
            {
                "set_id": "set-1",
                "query": "agent 框架",
                "summary": "资源摘要",
                "candidates": [
                    {"name": "框架 A", "url": "https://github.com/a/a", "source": "github"},
                    {"name": "无链接", "url": ""},
                ],
            }
        )
        self.assertEqual(SOURCE_TYPE_RESOURCE_SET, context.source_type)
        self.assertEqual("set-1", context.resource_set_id)
        self.assertEqual(1, len(context.resource_refs))
        self.assertEqual("框架 A", context.resource_refs[0]["name"])
        self.assertIn("框架 A", context.material)
        self.assertIn("set-1", context.material)

    def test_resource_set_requires_an_identifier(self) -> None:
        for payload in ({}, None, {"set_id": ""}):
            with self.subTest(payload=payload):
                with self.assertRaises(ArticleContextError) as raised:
                    build_resource_set_context(payload)
                self.assertEqual("dossier_invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()