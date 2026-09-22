"""Faithful offline coverage for the Iteration 2 resource contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from autoposting.decision import DecisionChoice, DecisionService, ResourceType, build_fallback_plan
from autoposting.decision.adapters.jev import JevAdapter
from autoposting.resource_discovery import (
    GITHUB_WEEKLY_PRESET,
    GitHubResourceAdapter,
    ResourceDiscoveryError,
    ResourceDiscoveryService,
    ResourceSet,
    ResourceStatus,
    SkillResourceAdapter,
    WebResourceAdapter,
    build_resource_discovery_service,
    objective_resource_score,
)


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


class _Adapter:
    def __init__(self, rows=None, *, error: Exception | None = None, degraded: bool = False):
        self.rows = list(rows or [])
        self.error = error
        self.degraded = degraded
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return {
            "results": self.rows[:limit],
            "degraded": self.degraded,
            "cause_code": "rate_limited" if self.degraded else "",
            "diagnostics": (
                [{"code": "resource_source_failed", "cause_code": "rate_limited"}]
                if self.degraded
                else []
            ),
        }


class _Planner:
    def __init__(self, adapters=("github", "skills", "web"), max_candidates=10):
        base = build_fallback_plan("ignored")
        self.result = replace(
            base,
            intent=replace(base.intent, goal="planned query", concepts=("planned",)),
            plan=replace(base.plan, query="planned query", adapters=tuple(adapters), max_candidates=max_candidates),
        )
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def plan(self, query: str, *, source_event_ids=()):
        self.calls.append((query, tuple(source_event_ids)))
        return self.result


class _JevProvider:
    def __init__(self, scores=(90.0, 90.0), decisions=("include", "include"), confidence=1.0):
        self.scores = tuple(scores)
        self.decisions = tuple(decisions)
        self.confidence = confidence
        self.calls: list[dict[str, object]] = []

    def __call__(self, payload):
        candidate = dict(payload["candidate"])
        self.calls.append(candidate)
        round_number = int(candidate.get("decision_round") or 1)
        index = max(0, min(round_number - 1, len(self.scores) - 1))
        return {
            "schema_version": "jev-assessment-v1",
            "score": self.scores[index],
            "confidence": self.confidence,
            "decision": self.decisions[index],
            "reasons": [f"round-{round_number}"],
            "resource_type": "repo",
            "relation": "related_project",
            "worth_reading": True,
            "duplicate": False,
            "duplicate_of": None,
        }


class _Repository:
    def __init__(self):
        self.rows: dict[str, dict[str, object]] = {}
        self.saved: list[str] = []

    def save_resource_set(self, resource_set: ResourceSet):
        self.rows[resource_set.set_id] = resource_set.to_dict()
        self.saved.append(resource_set.set_id)
        return None

    def get_resource_set(self, set_id: str):
        return self.rows.get(set_id)

    def list_resource_sets(self, limit: int = 20):
        return list(self.rows.values())[:limit]

    def delete_resource_set(self, set_id: str):
        return self.rows.pop(set_id, None) is not None


def _repo(url="https://github.com/acme/toolkit", *, source="github", stars=100):
    return {
        "source": source,
        "kind": "repo",
        "full_name": "acme/toolkit",
        "source_id": "acme/toolkit",
        "name": "toolkit",
        "url": url,
        "description": "A useful toolkit",
        "stars": stars,
        "forks": 10,
        "updated_at": "2026-09-21T00:00:00Z",
        "has_readme": True,
        "api_key": "must never persist",
    }


class ResourceDiscoveryTests(unittest.TestCase):
    def _service(self, *, repo=None, skills=None, web=None, planner=None, decision=None, repository=None, id_value="set-1"):
        return ResourceDiscoveryService(
            github_adapter=repo,
            skill_adapter=skills,
            web_adapter=web,
            planner=planner or _Planner(),
            decision_service=decision,
            repository=repository,
            clock=lambda: NOW,
            id_factory=lambda: id_value,
        )

    def test_mixed_sources_dedupe_and_two_round_jev(self):
        github = _Adapter([_repo()])
        web = _Adapter([_repo("https://github.com/acme/toolkit?tab=readme", source="web", stars=1)])
        provider = _JevProvider(scores=(55.0, 95.0))
        service = self._service(
            repo=github,
            web=web,
            planner=_Planner(("github", "web")),
            decision=DecisionService(JevAdapter(provider)),
        )

        result = service.discover("ignored", source_event_ids=("event-1",))

        self.assertEqual(ResourceStatus.OK, result.status)
        self.assertFalse(result.degraded)
        self.assertEqual(1, len(result.candidates), "same repository must be one candidate")
        candidate = result.candidates[0]
        self.assertEqual(("acme/toolkit",), candidate.source_ids)
        self.assertEqual(2, len(provider.calls), "each candidate gets exactly two Jev rounds")
        self.assertEqual(95.0, candidate.final_assessment.semantic_score)
        self.assertAlmostEqual(0.6 * candidate.objective_score + 0.4 * 95.0, candidate.final_assessment.final_score, places=3)
        self.assertTrue(candidate.selected)
        self.assertNotIn("api_key", candidate.to_dict())
        self.assertNotIn("api_key", str(result.to_dict()))

    def test_planner_receives_selected_event_ids_and_limits_each_adapter(self):
        planner = _Planner(("github", "skills"), max_candidates=2)
        github = _Adapter([_repo(), _repo("https://github.com/acme/other")])
        skills = _Adapter([])
        result = self._service(repo=github, skills=skills, planner=planner).discover(
            "user query", source_event_ids=("event-2", "event-1"), limit=1
        )

        self.assertEqual(("user query", ("event-2", "event-1")), planner.calls[0])
        self.assertEqual([("planned query", 1)], github.calls)
        self.assertEqual([("planned query", 1)], skills.calls)
        self.assertEqual(1, len(result.candidates))
        self.assertEqual("empty", result.adapter_status["skills"]["status"])

    def test_single_source_failure_keeps_healthy_rows_and_marks_degraded(self):
        github = _Adapter([_repo()])
        skills = _Adapter(error=RuntimeError("skills.sh unavailable"))
        result = self._service(repo=github, skills=skills, planner=_Planner(("github", "skills"))).discover("q")

        self.assertEqual(1, len(result.candidates))
        self.assertEqual(ResourceStatus.DEGRADED, result.status)
        self.assertTrue(result.degraded)
        self.assertEqual("degraded", result.adapter_status["skills"]["status"])
        self.assertEqual("unknown_error", result.adapter_status["skills"]["cause_code"])
        self.assertTrue(any(item.get("source") == "skills" for item in result.diagnostics))

    def test_empty_source_is_distinct_from_failed_source(self):
        result = self._service(
            repo=_Adapter([]),
            planner=_Planner(("github",)),
            decision=DecisionService(JevAdapter(_JevProvider())),
        ).discover("q")

        self.assertEqual(ResourceStatus.EMPTY, result.status)
        self.assertFalse(result.degraded)
        self.assertEqual("empty", result.adapter_status["github"]["status"])

    def test_pasted_monorepo_skill_merges_with_search_result(self):
        skills = _Adapter(
            [
                {
                    "source": "skills.sh",
                    "kind": "skill",
                    "full_name": "acme/skills",
                    "skill_path": "video-edit/SKILL.md",
                    "name": "video-edit",
                    "source_id": "acme/skills/video-edit",
                    "url": "https://skills.sh/acme/skills/video-edit",
                    "description": "search copy",
                    "installs": 100,
                }
            ]
        )
        result = self._service(skills=skills, planner=_Planner(("skills",))).discover(
            "video", pasted_text="https://github.com/acme/skills/tree/main/video-edit"
        )

        self.assertEqual(1, len(result.candidates))
        candidate = result.candidates[0]
        self.assertEqual(ResourceType.SKILL, candidate.kind)
        self.assertEqual("video-edit/SKILL.md", candidate.metadata["skill_path"])
        self.assertEqual(("acme/skills/video-edit", "acme/skills"), candidate.source_ids)
        self.assertIn("pasted", candidate.metadata.get("sources", []))
        self.assertIn("skills.sh", candidate.metadata.get("sources", []))

    def test_pasted_reject_is_safe_and_diagnostic(self):
        result = self._service(planner=_Planner(("github",))).discover("q", pasted_text="https://example.invalid/not-a-skill")
        self.assertEqual(ResourceStatus.DEGRADED, result.status)
        self.assertEqual(0, len(result.candidates))
        self.assertTrue(any(item.get("cause_code") == "unsupported_host" for item in result.diagnostics))

    def test_low_confidence_jev_is_objective_only_and_not_selected(self):
        provider = _JevProvider(scores=(100.0, 100.0), confidence=0.1)
        result = self._service(
            repo=_Adapter([_repo()]),
            planner=_Planner(("github",)),
            decision=DecisionService(JevAdapter(provider), min_confidence=0.8),
        ).discover("q")
        candidate = result.candidates[0]

        self.assertTrue(candidate.final_assessment.degraded)
        self.assertEqual("review", candidate.final_assessment.decision.value)
        self.assertFalse(candidate.selected)
        self.assertTrue(any(item.get("cause_code") == "low_confidence" for item in result.diagnostics))

    def test_custom_decision_failure_falls_back_without_secret(self):
        class BrokenDecision:
            def assess(self, candidate, **kwargs):
                raise RuntimeError("provider token=secret-value")

        result = self._service(repo=_Adapter([_repo()]), planner=_Planner(("github",)), decision=BrokenDecision()).discover("q")
        self.assertEqual(1, len(result.candidates))
        self.assertTrue(result.degraded)
        self.assertNotIn("secret-value", str(result.to_dict()))
        self.assertTrue(any(item.get("code") == "decision_failed" for item in result.diagnostics))

    def test_resource_set_persists_and_refresh_restores_without_network(self):
        repository = _Repository()
        github = _Adapter([_repo()])
        service = self._service(
            repo=github,
            planner=_Planner(("github",)),
            decision=DecisionService(JevAdapter(_JevProvider())),
            repository=repository,
            id_value="durable-1",
        )
        original = service.discover("q")
        calls_before_refresh = len(github.calls)
        github.rows.clear()
        restored = service.refresh(original.set_id)

        self.assertEqual(["durable-1"], repository.saved)
        self.assertEqual(original.to_dict(), restored.to_dict())
        self.assertEqual(calls_before_refresh, len(github.calls), "refresh must not re-run the network adapter")

    def test_weekly_preset_uses_existing_github_lane_contract(self):
        github = _Adapter([_repo()])
        service = self._service(repo=github, planner=_Planner(("github",)), decision=DecisionService(JevAdapter(_JevProvider())))
        result = service.discover_preset(GITHUB_WEEKLY_PRESET, persist=False)

        self.assertEqual(GITHUB_WEEKLY_PRESET, result.preset)
        self.assertEqual("本周 AI 新项目", result.query)
        self.assertEqual(1, len(result.legacy_records()))
        self.assertEqual("repo", result.legacy_records()[0]["kind"])

    def test_contract_round_trip_rejects_unknown_schema_and_bounds_objective_signal(self):
        self.assertGreater(objective_resource_score({"stars": 100, "updated_at": "2026-09-21T00:00:00Z"}, now=NOW), 0)
        result = self._service(repo=_Adapter([_repo()]), planner=_Planner(("github",))).discover("q", persist=False)
        self.assertEqual(result.to_dict(), ResourceSet.from_dict(result.to_dict()).to_dict())
        invalid = dict(result.to_dict())
        invalid["schema_version"] = "resource-set-v0"
        with self.assertRaises(ResourceDiscoveryError):
            ResourceSet.from_dict(invalid)


class ProductionAdapterTests(unittest.TestCase):
    def test_github_adapter_reuses_search_contract_and_reads_readme(self):
        calls: list[str] = []

        def getter(url: str):
            calls.append(url)
            if "/search/repositories?" in url:
                return {"items": [{
                    "full_name": "acme/agent-tools",
                    "name": "agent-tools",
                    "html_url": "https://github.com/acme/agent-tools",
                    "description": "browser automation tools",
                    "stargazers_count": 321,
                    "forks_count": 22,
                    "updated_at": "2026-09-21T00:00:00Z",
                    "topics": ["agent", "browser"],
                    "owner": {"login": "acme"},
                }]}
            if url.startswith("https://raw.githubusercontent.com/acme/agent-tools/"):
                return "# Agent tools\nAutomate a browser and fill forms."
            raise AssertionError(url)

        response = GitHubResourceAdapter(getter, clock=lambda: NOW).search("browser agent", limit=3)
        row = response["results"][0]
        self.assertEqual("repo", row["kind"])
        self.assertEqual("acme/agent-tools", row["source_id"])
        self.assertIn("Automate a browser", row["readme_excerpt"])
        self.assertEqual(321, row["stars"])
        self.assertTrue(any("search/repositories" in url for url in calls))
        self.assertTrue(any("README.md" in url for url in calls))

    def test_github_weekly_query_uses_existing_weekly_fetcher(self):
        def getter(url: str):
            if "/search/repositories?" in url:
                return {"items": [{
                    "full_name": "acme/weekly-ai",
                    "name": "weekly-ai",
                    "html_url": "https://github.com/acme/weekly-ai",
                    "description": "an AI agent",
                    "stargazers_count": 50,
                    "topics": ["ai"],
                    "owner": {"login": "acme"},
                }]}
            if url.startswith("https://raw.githubusercontent.com/acme/weekly-ai/"):
                return "# Weekly AI\nA project README."
            raise AssertionError(url)

        response = GitHubResourceAdapter(getter, clock=lambda: NOW).search("本周 AI 新项目", limit=1)
        self.assertFalse(response["degraded"])
        self.assertEqual("acme/weekly-ai", response["results"][0]["full_name"])

    def test_skill_adapter_reads_skill_md_from_github_and_skills_sh(self):
        calls: list[str] = []

        def getter(url: str):
            calls.append(url)
            if "skills.sh/api/search" in url:
                return {"skills": [{
                    "id": "acme/skills/video-edit",
                    "skillId": "video-edit",
                    "name": "video-edit",
                    "source": "acme/skills",
                    "description": "video editing",
                    "installs": 100,
                }]}
            if "api.github.com/search" in url:
                return {"items": [{
                    "full_name": "acme/browser-skill",
                    "html_url": "https://github.com/acme/browser-skill",
                    "description": "browser skill",
                }]}
            if url.startswith("https://raw.githubusercontent.com/"):
                return "# Skill\nUse this skill to automate browser work."
            if url.startswith("https://api.github.com/repos/"):
                return []
            raise AssertionError(url)

        response = SkillResourceAdapter(getter).search("browser automation", limit=2)
        self.assertFalse(response["degraded"])
        self.assertEqual(2, len(response["results"]))
        self.assertTrue(all(row["kind"] == "skill" for row in response["results"]))
        self.assertTrue(all(row["docs_exists"] for row in response["results"]))
        self.assertTrue(any("SKILL.md" in url for url in calls))
        self.assertTrue(any("skills.sh/api/search" in url for url in calls))

    def test_web_adapter_reads_page_excerpt_and_failure_stays_local(self):
        class Reader:
            def read(self, url: str, **kwargs):
                return {"status": "full", "text": "The page explains the browser workflow."}

        response = WebResourceAdapter(
            lambda query, limit=10: [{"title": "Browser guide", "url": "https://example.com/guide", "snippet": "guide"}],
            reader=Reader(),
        ).search("browser", limit=2)
        row = response["results"][0]
        self.assertFalse(response["degraded"])
        self.assertEqual("website", row["kind"])
        self.assertIn("browser workflow", row["page_excerpt"])

    def test_production_factory_runs_repo_skill_web_together(self):
        def github_get(url: str):
            if "/search/repositories?" in url:
                return {"items": [{
                    "full_name": "acme/factory-repo",
                    "html_url": "https://github.com/acme/factory-repo",
                    "description": "factory repo",
                    "stargazers_count": 10,
                    "owner": {"login": "acme"},
                }]}
            if url.startswith("https://raw.githubusercontent.com/"):
                return "# README\nDocumented resource."
            raise AssertionError(url)

        def skills_get(url: str):
            if "skills.sh/api/search" in url:
                return {"skills": [{"id": "acme/skills/agent", "name": "agent", "source": "acme/skills", "installs": 20}]}
            if "api.github.com/search" in url:
                return {"items": []}
            if url.startswith("https://raw.githubusercontent.com/"):
                return "# SKILL.md\nA skill document."
            if url.startswith("https://api.github.com/repos/"):
                return []
            raise AssertionError(url)

        service = build_resource_discovery_service(
            github_get=github_get,
            skills_get=skills_get,
            web_search=lambda query, limit=10: [{"title": "Guide", "url": "https://example.com/guide", "snippet": "guide"}],
            web_reader=lambda url, **kwargs: {"text": "Guide content"},
            planner=_Planner(("github", "skills", "web")),
            decision_service=DecisionService(JevAdapter(_JevProvider())),
            clock=lambda: NOW,
            id_factory=lambda: "factory-1",
        )
        result = service.discover("browser automation", persist=False)

        self.assertEqual({"repo": 1, "skill": 1, "website": 1, "tool": 0, "other": 0}, result.counts)
        self.assertEqual(ResourceStatus.OK, result.status)


if __name__ == "__main__":
    unittest.main()
