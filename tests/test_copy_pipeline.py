"""Fail-fast poster copy: LLM output is the only body, validator is format-only."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autoposting.copy_validate import require_enriched_copy, validate_copy_items
from autoposting.github_weekly import apply_leaderboard_copy, fetch_weekly_ai_repos
from autoposting.llm import LLMError, LLMService
from autoposting.skill_finder import render_skill_package, skills_as_repos
from autoposting.visual.adapter import project_from_repo

from test_github_weekly import TRENDING_HTML
from test_llm import FakeRunner, _db


def _item(full_name: str = "acme/llm-cli", **fields: str) -> dict[str, object]:
    row = {
        "full_name": full_name,
        "summary": "在终端里直接把提示词交给本地模型并回显结果。",
        "does": "把提示词发给本地模型。\n把模型输出打到终端。\n只覆盖这一层对话。",
        "how": "读入命令行参数。\n调用已配置的模型接口。\n把返回文本原样打印。",
        "audience": "习惯在项目目录里试验提示词的人。",
        "scene": "需要对着仓库立刻问模型的时候。",
    }
    row.update(fields)
    return row


class CopyValidatorTests(unittest.TestCase):
    def test_missing_field_raises_llm_enrichment_failed(self) -> None:
        repos = [{"full_name": "acme/llm-cli"}]
        with self.assertRaises(LLMError) as raised:
            validate_copy_items(repos, [_item(audience="")])
        self.assertEqual("llm_enrichment_failed", raised.exception.code)
        self.assertIn("LLM_ENRICHMENT_FAILED", raised.exception.reason)
        self.assertIn('missing field "audience"', raised.exception.reason)

    def test_missing_repo_item_raises(self) -> None:
        repos = [{"full_name": "acme/llm-cli"}, {"full_name": "acme/other"}]
        with self.assertRaises(LLMError) as raised:
            validate_copy_items(repos, [_item()])
        self.assertIn("acme/other", raised.exception.reason)

    def test_short_skill_name_matches_full_name(self) -> None:
        repos = [
            {
                "full_name": "prime-skills/runcomfy-agent-skills/video-inpainting",
                "name": "video-inpainting",
            }
        ]
        incoming = _item(full_name="video-inpainting")
        matched = validate_copy_items(repos, [incoming])
        self.assertEqual(
            incoming["summary"],
            matched["prime-skills/runcomfy-agent-skills/video-inpainting"]["summary"],
        )

    def test_name_only_item_matches_full_name(self) -> None:
        repos = [
            {
                "full_name": "prime-skills/runcomfy-agent-skills/video-inpainting",
                "name": "video-inpainting",
            }
        ]
        incoming = _item(full_name="", name="video-inpainting")
        incoming.pop("full_name", None)
        matched = validate_copy_items(repos, [incoming])
        self.assertEqual(
            incoming["summary"],
            matched["prime-skills/runcomfy-agent-skills/video-inpainting"]["summary"],
        )

    def test_ambiguous_short_name_still_requires_full_name(self) -> None:
        repos = [
            {"full_name": "acme/one/video-edit", "name": "video-edit"},
            {"full_name": "other/two/video-edit", "name": "video-edit"},
        ]
        with self.assertRaises(LLMError) as raised:
            validate_copy_items(repos, [_item(full_name="video-edit")])
        self.assertIn("missing item", raised.exception.reason)

    def test_unenriched_snapshot_fails_render_gate(self) -> None:
        with self.assertRaises(LLMError) as raised:
            require_enriched_copy(
                [{"full_name": "acme/llm-cli", "summary": "x", "enriched": False}]
            )
        self.assertIn("enrichment not completed", raised.exception.reason)


class FetchDoesNotPrefillCopyTests(unittest.TestCase):
    def test_weekly_fetch_keeps_facts_without_kind_templates(self) -> None:
        repos = fetch_weekly_ai_repos(
            getter=lambda url: TRENDING_HTML if "trending" in url else {"items": []},
            now=datetime(2026, 8, 29, tzinfo=timezone.utc),
            limit=2,
        )
        self.assertEqual("llm-cli", repos[0]["name"])
        self.assertFalse(repos[0].get("enriched"))
        self.assertFalse(str(repos[0].get("summary") or "").strip())
        self.assertFalse(str(repos[0].get("does") or "").strip())
        self.assertNotIn("给", str(repos[0].get("summary") or ""))
        self.assertIn("tiny LLM CLI", str(repos[0].get("description") or ""))


class ApplyLeaderboardCopyTests(unittest.TestCase):
    def test_apply_requires_every_copy_field(self) -> None:
        repos = [{"full_name": "acme/llm-cli", "name": "llm-cli", "enriched": False}]
        with self.assertRaises(LLMError):
            apply_leaderboard_copy(repos, [_item(how="")])

    def test_apply_accepts_short_skill_name(self) -> None:
        repos = [
            {
                "full_name": "prime-skills/runcomfy-agent-skills/video-inpainting",
                "name": "video-inpainting",
                "enriched": False,
            }
        ]
        updated = apply_leaderboard_copy(repos, [_item(full_name="video-inpainting")])
        self.assertTrue(updated[0]["enriched"])
        self.assertEqual("prime-skills/runcomfy-agent-skills/video-inpainting", updated[0]["full_name"])

    def test_apply_writes_llm_fields_and_marks_enriched(self) -> None:
        repos = [{"full_name": "acme/llm-cli", "name": "llm-cli", "stars": 9, "enriched": False}]
        updated = apply_leaderboard_copy(repos, [_item()])
        self.assertEqual("在终端里直接把提示词交给本地模型并回显结果。", updated[0]["summary"])
        self.assertTrue(updated[0]["enriched"])
        self.assertEqual(9, updated[0]["stars"])
        self.assertEqual("llm-cli", updated[0]["name"])


class EnrichFailFastTests(unittest.TestCase):
    def test_enrich_without_model_raises(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LLMService(_db(temp))
        with self.assertRaises(LLMError) as raised:
            service.enrich_github_repos([{"full_name": "acme/llm-cli", "name": "llm-cli"}])
        self.assertEqual("llm_enrichment_failed", raised.exception.code)
        self.assertIn("model not configured", raised.exception.reason)

    def test_enrich_missing_audience_raises_instead_of_keeping_old_copy(self) -> None:
        repos = [
            {
                "rank": 1,
                "full_name": "acme/llm-cli",
                "name": "llm-cli",
                "description": "A tiny LLM CLI agent.",
                "kind": "CLI",
                "language": "Python",
                "topics": [],
            }
        ]
        fake = FakeRunner(
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "full_name": "acme/llm-cli",
                            "summary": "在终端里直接把提示词交给本地模型。",
                            "does": "把提示词发给本地模型。",
                            "scene": "对着仓库立刻问模型的时候。",
                            "how": "调用已配置的模型接口。",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LLMService(_db(temp), runner=fake)
        service.configure(model="qwen3.8-max", base_url="", api_key="")
        with self.assertRaises(LLMError) as raised:
            service.enrich_github_repos(repos)
        self.assertEqual("llm_enrichment_failed", raised.exception.code)
        self.assertIn('missing field "audience"', raised.exception.reason)


class AdapterDoesNotRewriteCopyTests(unittest.TestCase):
    def test_project_from_repo_keeps_llm_english_and_it_led_lines(self) -> None:
        payload = project_from_repo(
            {
                "rank": 1,
                "name": "agency-agents",
                "kind": "Agent",
                "summary": "它把不同职责的角色编成一支小团队。",
                "does": "It organizes frontend and ops agents.\n它帮你把任务交给下一位。",
                "how": "先定角色和各自职责",
                "audience": "想搭小型 AI 团队的人",
                "scene": "本地把开发、内容和运营串成一条流水线",
            }
        )
        self.assertEqual("它把不同职责的角色编成一支小团队。", payload["keyMessage"])
        self.assertTrue(any("It organizes" in item for item in payload["features"]))
        self.assertTrue(any(item.lstrip().startswith("它") for item in payload["features"]))

    def test_empty_copy_is_not_replaced_with_kind_templates(self) -> None:
        payload = project_from_repo({"rank": 1, "name": "alpha-cli", "kind": "CLI"})
        self.assertEqual("", payload["keyMessage"])
        self.assertEqual([], payload["features"])
        self.assertEqual([], payload["how"])
        self.assertEqual([], payload["audience"])
        self.assertEqual([], payload["scene"])
        self.assertNotIn("给想先看懂再动手的人", payload["keyMessage"])


class SkillFactsOnlyTests(unittest.TestCase):
    def test_skills_as_repos_does_not_prefill_boards(self) -> None:
        rows = skills_as_repos(
            [
                {
                    "name": "douyin-video",
                    "source_id": "yzfly/douyin-mcp-server/douyin-video",
                    "description": "video editing automation tool",
                    "stage_name": "剪辑",
                    "installs": 10,
                }
            ]
        )
        self.assertEqual("douyin-video", rows[0]["name"])
        self.assertTrue(rows[0]["kind"])
        self.assertFalse(rows[0].get("enriched"))
        self.assertFalse(str(rows[0].get("summary") or "").strip())
        self.assertNotIn("接手", str(rows[0].get("does") or ""))
        self.assertNotIn("这一步要用的材料备齐", str(rows[0].get("how") or ""))

    def test_copy_writer_failure_stops_render(self) -> None:
        def boom(repos: list[dict[str, object]]) -> list[dict[str, object]]:
            raise LLMError("llm_api_failed", "模型挂了")

        with tempfile.TemporaryDirectory() as raw:
            def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
                png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return png_path

            with self.assertRaises(LLMError) as raised:
                render_skill_package(
                    {
                        "title": "t",
                        "stages": [],
                        "skills": [
                            {
                                "name": "douyin-video",
                                "source_id": "yzfly/douyin-mcp-server/douyin-video",
                                "description": "剪辑",
                            }
                        ],
                    },
                    Path(raw),
                    screenshot_fn=fake_shot,
                    copy_writer=boom,
                )
        self.assertEqual("llm_api_failed", raised.exception.code)


class HookExtractionFailFastTests(unittest.TestCase):
    def test_invalid_json_raises_instead_of_empty_list(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LLMService(
            _db(temp),
            runner=FakeRunner(stdout="not-json"),
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        with self.assertRaises(LLMError) as raised:
            service.extract_hook_candidates("正文")
        self.assertIn("LLM_HOOK_FAILED", raised.exception.reason)

    def test_missing_candidates_array_raises(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LLMService(
            _db(temp),
            runner=FakeRunner(stdout="{}"),
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        with self.assertRaises(LLMError) as raised:
            service.extract_hook_candidates("正文")
        self.assertIn("candidates", raised.exception.reason)


if __name__ == "__main__":
    unittest.main()
