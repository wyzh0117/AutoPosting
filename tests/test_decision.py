from __future__ import annotations

import json
import types
import unittest

from autoposting.decision.adapters.jev import JevAdapter
from autoposting.decision.contracts import (
    ContractError,
    DecisionStatus,
    JEVAssessment,
    PlannerResult,
    ResearchIntent,
    SearchPlan,
)
from autoposting.decision.planner import LLMPlanner, PlannerError, build_fallback_plan
from autoposting.decision.rubrics import NEWS_RUBRIC_ID
from autoposting.decision.service import DecisionService


def _planner_payload() -> dict[str, object]:
    return PlannerResult(
        intent=ResearchIntent(
            goal="寻找浏览器自动化工具",
            concepts=("browser automation", "screenshot"),
            search_repo=True,
            search_skill=True,
            search_web=True,
            preferred_signals=("active maintenance",),
        ),
        plan=SearchPlan(
            query="browser automation screenshot",
            adapters=("github", "skills", "web"),
            max_candidates=30,
        ),
    ).to_dict()


class ContractTests(unittest.TestCase):
    def test_planner_result_is_versioned_json_safe(self) -> None:
        result = PlannerResult.from_dict(_planner_payload())
        payload = result.to_dict()
        self.assertEqual("planner-result-v1", payload["schema_version"])
        self.assertEqual("browser automation screenshot", payload["plan"]["query"])

    def test_contract_rejects_unknown_fields_and_invalid_scores(self) -> None:
        payload = _planner_payload()
        payload["unexpected"] = True
        with self.assertRaises(ContractError):
            PlannerResult.from_dict(payload)
        with self.assertRaises(ContractError):
            JEVAssessment.from_dict(
                {
                    "schema_version": "jev-assessment-v1",
                    "score": 101,
                    "confidence": 1,
                    "decision": "include",
                }
            )
        with self.assertRaises(ContractError):
            JEVAssessment.from_dict(
                {
                    "score": 50,
                    "confidence": 0.9,
                    "decision": "include",
                }
            )

    def test_decision_assessment_rejects_invalid_enums_as_contract_errors(self) -> None:
        from autoposting.decision.contracts import DecisionAssessment

        with self.assertRaises(ContractError):
            DecisionAssessment(
                candidate_id="candidate",
                objective_score=1,
                semantic_score=None,
                final_score=1,
                confidence=None,
                decision="not-a-choice",
                resource_type=None,
                relation=None,
                worth_reading=None,
                duplicate=None,
                duplicate_of=None,
                reasons=(),
                rubric_id=NEWS_RUBRIC_ID,
                rubric_version="v1",
                status=DecisionStatus.DEGRADED,
                degraded=True,
            )


class PlannerTests(unittest.TestCase):
    def test_provider_is_injected_and_selected_event_ids_are_authoritative(self) -> None:
        prompts: list[str] = []

        def provider(prompt: str) -> object:
            prompts.append(prompt)
            return _planner_payload()

        result = LLMPlanner(provider).plan("找浏览器工具", source_event_ids=("event-1",))
        self.assertEqual(("event-1",), result.intent.source_event_ids)
        self.assertEqual("找浏览器工具", prompts[0].split("<request>", 1)[1].split("</request>", 1)[0])

    def test_fenced_json_is_supported_but_invalid_shape_is_degraded_error(self) -> None:
        result = LLMPlanner(lambda _prompt: "```json\n" + json.dumps(_planner_payload()) + "\n```").plan("工具")
        self.assertEqual("github", result.plan.adapters[0])
        with self.assertRaises(PlannerError) as context:
            LLMPlanner(lambda _prompt: "[]").plan("工具")
        self.assertEqual("invalid_response", context.exception.cause_code)

    def test_planner_rejects_non_json_scalar_and_missing_fields(self) -> None:
        for payload in ("not json", 7, [], {"intent": {}}):
            with self.subTest(payload=payload), self.assertRaises(PlannerError):
                LLMPlanner(lambda _prompt, payload=payload: payload).plan("工具")

    def test_unconfigured_and_timeout_are_stable_and_redacted(self) -> None:
        with self.assertRaises(PlannerError) as context:
            LLMPlanner().plan("工具")
        self.assertEqual("not_configured", context.exception.cause_code)

        def timeout(_prompt: str) -> object:
            raise TimeoutError("Authorization: Bearer planner-secret https://example.invalid/x")

        with self.assertRaises(PlannerError) as context:
            LLMPlanner(timeout).plan("工具")
        self.assertEqual("timeout", context.exception.cause_code)
        detail = str(context.exception.diagnostic["detail"])
        self.assertNotIn("planner-secret", detail)
        self.assertNotIn("https://example.invalid/x", detail)

    def test_configured_tracks_the_provider_seam_not_provider_presence(self) -> None:
        class RunProvider:
            def run(self, prompt: str) -> object:
                return _planner_payload()

        class EndpointOnlyProvider:
            """Looks configured in Settings but exposes no provider interface."""

            settings = types.SimpleNamespace(base_url="https://example.invalid/v1")

        self.assertTrue(LLMPlanner(RunProvider()).configured)
        self.assertFalse(LLMPlanner(EndpointOnlyProvider()).configured)
        self.assertFalse(LLMPlanner().configured)

    def test_fallback_plan_is_explicit_and_deterministic(self) -> None:
        result = build_fallback_plan("浏览器 截图", source_event_ids=("e1",))
        self.assertEqual(("e1",), result.intent.source_event_ids)
        self.assertEqual(("github", "skills", "web"), result.plan.adapters)


def _jev_payload(score: float = 70, confidence: float = 0.9) -> dict[str, object]:
    return {
        "schema_version": "jev-assessment-v1",
        "score": score,
        "confidence": confidence,
        "decision": "include",
        "reasons": ["fits the stated goal"],
        "resource_type": "tool",
        "worth_reading": True,
        "duplicate": False,
    }


class DecisionServiceTests(unittest.TestCase):
    def test_high_confidence_uses_named_sixty_forty_mix(self) -> None:
        service = DecisionService(JevAdapter(lambda _payload: _jev_payload(50, 0.9)))
        result = service.assess({"event_id": "event-1"}, objective_score=80, rubric_id=NEWS_RUBRIC_ID)
        self.assertEqual(68.0, result.final_score)
        self.assertEqual("ok", result.status.value)
        self.assertFalse(result.degraded)
        self.assertEqual("event-1", result.to_dict()["candidate_id"])

    def test_no_jev_is_objective_only_and_does_not_drop_candidate(self) -> None:
        result = DecisionService().assess({"event_id": "event-1"}, objective_score=73)
        self.assertEqual(73.0, result.final_score)
        self.assertIsNone(result.semantic_score)
        self.assertEqual(DecisionStatus.DEGRADED, result.status)
        self.assertEqual("not_configured", result.diagnostics[0]["cause_code"])

    def test_low_confidence_is_objective_only_with_diagnostics(self) -> None:
        service = DecisionService(JevAdapter(lambda _payload: _jev_payload(100, 0.2)))
        result = service.assess({"event_id": "event-1"}, objective_score=20)
        self.assertEqual(20.0, result.final_score)
        self.assertEqual(100.0, result.semantic_score)
        self.assertEqual("review", result.decision.value)
        self.assertEqual("low_confidence", result.diagnostics[0]["cause_code"])

    def test_timeout_and_invalid_response_are_objective_only(self) -> None:
        def timeout(_payload: object) -> object:
            raise TimeoutError("timeout")

        timed_out = DecisionService(JevAdapter(timeout)).assess({"id": "a"}, objective_score=11)
        self.assertEqual("timeout", timed_out.diagnostics[0]["cause_code"])

        invalid = DecisionService(JevAdapter(lambda _payload: {"score": "bad"})).assess(
            {"id": "b"}, objective_score=12
        )
        self.assertEqual("invalid_response", invalid.diagnostics[0]["cause_code"])

        for payload in ("not json", [], 7, {"schema_version": "jev-assessment-v1"}):
            result = DecisionService(
                JevAdapter(lambda _payload, payload=payload: payload)
            ).assess({"id": "c"}, objective_score=12)
            with self.subTest(payload=payload):
                self.assertIn(
                    result.diagnostics[0]["cause_code"],
                    {"invalid_response", "invalid_json"},
                )

    def test_jev_error_details_are_sanitized(self) -> None:
        def broken(_payload: object) -> object:
            raise ValueError("Authorization: Bearer jev-secret https://example.invalid/private")

        result = DecisionService(JevAdapter(broken)).assess({"id": "a"}, objective_score=10)
        detail = str(result.diagnostics[0]["detail"])
        self.assertNotIn("jev-secret", detail)
        self.assertNotIn("https://example.invalid/private", detail)

    def test_existing_llm_service_shape_can_be_injected_without_credentials(self) -> None:
        class RunProvider:
            def run(self, prompt: str) -> object:
                self.prompt = prompt
                return _jev_payload(55, 0.9)

        provider = RunProvider()
        result = DecisionService(JevAdapter(provider)).assess({"id": "a"}, objective_score=45)
        self.assertEqual(49.0, result.final_score)
        self.assertIn("jev-request-v1", provider.prompt)

    def test_assess_many_preserves_generic_candidate_order(self) -> None:
        service = DecisionService(JevAdapter(lambda _payload: _jev_payload(40, 0.9)))
        results = service.assess_many(
            [{"id": "first", "objective_score": 10}, {"id": "second", "objective_score": 20}]
        )
        self.assertEqual(["first", "second"], [result.candidate_id for result in results])


if __name__ == "__main__":
    unittest.main()
