"""Planner/Jev provider-seam and provider-error regression tests.

The production failure these cover: an ``LLMService`` injected as the Planner
and Jev provider reported ``configured`` while every call failed with
``not_configured``, because the service exposed no provider interface.
"""

from __future__ import annotations

import json
import socket
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from autoposting.db import Database
from autoposting.decision.adapters.jev import JevAdapter, JevError
from autoposting.decision.contracts import PlannerResult, ResearchIntent, SearchPlan
from autoposting.decision.planner import LLMPlanner, PlannerError
from autoposting.decision.rubrics import NEWS_RUBRIC
from autoposting.decision.service import DecisionService
from autoposting.diagnostics import cause_code_for
from autoposting.llm import HermesRunner, LLMError, LLMService

ENDPOINT = "https://example.invalid/v1"
API_KEY = "sk-provider-secret"


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


def _jev_payload(score: float = 70, confidence: float = 0.9) -> dict[str, object]:
    return {
        "schema_version": "jev-assessment-v1",
        "score": score,
        "confidence": confidence,
        "decision": "include",
        "reasons": ["provider seam regression"],
        "resource_type": "tool",
        "worth_reading": True,
        "duplicate": False,
    }


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{ENDPOINT}/chat/completions", status, "status", {}, None
    )


class _CommandRunner:
    """Offline stand-in for the hermes subprocess runner: one canned payload."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(argv)
        return types.SimpleNamespace(stdout=json.dumps(self.payload), stderr="", returncode=0)


class _SettingsOnlyProvider:
    """LLMService-shaped object: endpoint settings exist, provider interface does not."""

    def __init__(self) -> None:
        self.settings = types.SimpleNamespace(base_url=ENDPOINT, api_key=API_KEY)


class _DatabaseFixture:
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "research-provider.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _service(self, payload: dict[str, object]) -> tuple[LLMService, _CommandRunner]:
        runner = _CommandRunner(payload)
        return LLMService(self.database, runner=runner), runner

    def _endpoint_service(self) -> LLMService:
        return LLMService(self.database, base_url=ENDPOINT, api_key=API_KEY)


class ProviderSeamTests(_DatabaseFixture, unittest.TestCase):
    def test_llm_service_run_seam_plans_through_the_runner(self) -> None:
        service, runner = self._service(_planner_payload())

        planner = LLMPlanner(service)
        self.assertTrue(planner.configured)
        result = planner.plan("浏览器自动化", source_event_ids=("event-1",))

        self.assertIsInstance(result, PlannerResult)
        self.assertEqual(("event-1",), result.intent.source_event_ids)
        self.assertEqual("browser automation screenshot", result.plan.query)
        self.assertEqual(1, len(runner.calls))
        self.assertIn("AutoPosting Query Planner", runner.calls[0][2])

    def test_llm_service_run_seam_assesses_through_the_runner(self) -> None:
        service, runner = self._service(_jev_payload())

        adapter = JevAdapter(service)
        self.assertTrue(adapter.configured)
        assessment = adapter.assess({"event_id": "event-1"}, candidate_id="event-1", rubric=NEWS_RUBRIC)

        self.assertEqual("include", assessment.decision.value)
        self.assertEqual(70.0, assessment.score)
        self.assertEqual(1, len(runner.calls))
        self.assertIn("jev-request-v1", runner.calls[0][2])
        self.assertIn("event-1", runner.calls[0][2])

    def test_provider_without_callable_interface_is_unconfigured_and_provider_error(self) -> None:
        provider = _SettingsOnlyProvider()

        planner = LLMPlanner(provider)
        self.assertFalse(planner.configured)
        with self.assertRaises(PlannerError) as planner_error:
            planner.plan("工具")
        self.assertEqual("planner_invalid_provider", planner_error.exception.code)
        self.assertEqual("provider_error", planner_error.exception.cause_code)
        self.assertEqual("provider_error", planner_error.exception.diagnostic["cause_code"])
        detail = str(planner_error.exception.diagnostic["detail"])
        self.assertNotIn(API_KEY, detail)
        self.assertNotIn(ENDPOINT, detail)

        jev = JevAdapter(provider)
        self.assertFalse(jev.configured)
        with self.assertRaises(JevError) as jev_error:
            jev.assess({"id": "a"}, candidate_id="a", rubric=NEWS_RUBRIC)
        self.assertEqual("jev_invalid_provider", jev_error.exception.code)
        self.assertEqual("provider_error", jev_error.exception.cause_code)
        self.assertEqual("provider_error", jev_error.exception.diagnostic["cause_code"])

    def test_genuinely_unconfigured_providers_stay_not_configured(self) -> None:
        for planner in (LLMPlanner(), LLMPlanner(None)):
            with self.subTest(provider="none"), self.assertRaises(PlannerError) as raised:
                planner.plan("工具")
            self.assertEqual("not_configured", raised.exception.cause_code)

        adapter = JevAdapter(None)
        self.assertFalse(adapter.configured)
        with self.assertRaises(JevError) as raised:
            adapter.assess({"id": "a"}, candidate_id="a", rubric=NEWS_RUBRIC)
        self.assertEqual("not_configured", raised.exception.cause_code)

        for service in (DecisionService(), DecisionService(JevAdapter(None))):
            with self.subTest(decision_service=service):
                result = service.assess({"event_id": "event-1"}, objective_score=73)
                self.assertEqual(73.0, result.final_score)
                self.assertEqual("not_configured", result.diagnostics[0]["cause_code"])


class RunnerErrorCauseTests(unittest.TestCase):
    def _error_for(self, failure: BaseException) -> LLMError:
        runner = HermesRunner(base_url=ENDPOINT, api_key=API_KEY)
        with mock.patch("urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(LLMError) as raised:
                runner.run("prompt")
        return raised.exception

    def test_http_statuses_map_to_specific_causes(self) -> None:
        for status, cause in ((401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"), (500, "provider_error")):
            with self.subTest(status=status):
                error = self._error_for(_http_error(status))
                self.assertEqual("llm_api_failed", error.code)
                self.assertEqual(cause, error.cause_code)

    def test_transport_and_timeout_failures_keep_their_own_causes(self) -> None:
        unreachable = self._error_for(urllib.error.URLError("getaddrinfo failed"))
        self.assertEqual("llm_api_failed", unreachable.code)
        self.assertEqual("unreachable", unreachable.cause_code)

        timed_out = self._error_for(socket.timeout("timed out"))
        self.assertEqual("llm_timeout", timed_out.code)
        self.assertEqual("timeout", timed_out.cause_code)

    def test_non_json_body_maps_to_invalid_response(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return b"<html>not json</html>"

        runner = HermesRunner(base_url=ENDPOINT, api_key=API_KEY)
        with mock.patch("urllib.request.urlopen", return_value=_Response()):
            with self.assertRaises(LLMError) as raised:
                runner.run("prompt")
        self.assertEqual("llm_invalid_output", raised.exception.code)
        self.assertEqual("invalid_response", raised.exception.cause_code)

    def test_http_errors_never_carry_credentials_or_endpoint(self) -> None:
        error = self._error_for(_http_error(429))
        for text in (str(error), error.reason, str(error.detail or "")):
            self.assertNotIn(API_KEY, text)
            self.assertNotIn("example.invalid", text)

    def test_hermes_cli_nonzero_exit_is_provider_error_with_process_detail(self) -> None:
        runner = HermesRunner(
            command_runner=lambda _argv: types.SimpleNamespace(stdout="noise", stderr="", returncode=2)
        )
        with self.assertRaises(LLMError) as raised:
            runner.run("prompt")
        self.assertEqual("llm_command_failed", raised.exception.code)
        self.assertEqual("provider_error", raised.exception.cause_code)
        self.assertEqual("process_exit_2", raised.exception.detail)
        self.assertIn("退出码 2", raised.exception.reason)


class AdapterCausePassthroughTests(_DatabaseFixture, unittest.TestCase):
    def test_http_429_reaches_planner_and_jev_diagnostics_unchanged(self) -> None:
        service = self._endpoint_service()
        self.assertTrue(LLMPlanner(service).configured)
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(429)):
            with self.assertRaises(PlannerError) as planner_error:
                LLMPlanner(service).plan("工具")
            with self.assertRaises(JevError) as jev_error:
                JevAdapter(service).assess({"id": "a"}, candidate_id="a", rubric=NEWS_RUBRIC)

        self.assertEqual("rate_limited", planner_error.exception.cause_code)
        self.assertEqual("rate_limited", planner_error.exception.diagnostic["cause_code"])
        self.assertEqual("rate_limited", jev_error.exception.cause_code)
        self.assertEqual("rate_limited", jev_error.exception.diagnostic["cause_code"])
        detail = str(jev_error.exception.diagnostic["detail"])
        self.assertNotIn(API_KEY, detail)
        self.assertNotIn("example.invalid", detail)

    def test_http_500_is_provider_error_not_not_configured(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(500)):
            result = DecisionService(JevAdapter(self._endpoint_service())).assess(
                {"event_id": "event-1"}, objective_score=60
            )
        self.assertTrue(result.degraded)
        self.assertEqual("provider_error", result.diagnostics[0]["cause_code"])
        self.assertEqual(60.0, result.final_score)

    def test_http_401_degrades_decision_as_auth_failed_without_losing_objective_score(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=_http_error(401)):
            result = DecisionService(JevAdapter(self._endpoint_service())).assess(
                {"event_id": "event-1"}, objective_score=42
            )
        self.assertEqual("auth_failed", result.diagnostics[0]["cause_code"])
        self.assertEqual(42.0, result.final_score)
        self.assertIsNone(result.semantic_score)


class DiagnosticsCauseAllowlistTests(unittest.TestCase):
    def test_system_cause_codes_are_recognized(self) -> None:
        for cause in (
            "not_configured",
            "provider_error",
            "auth_failed",
            "low_confidence",
            "blocked_http",
            "dns_failure",
            "connection_reset",
            "tls_failure",
            "invalid_content",
            "unsupported_content",
            "too_large",
            "reader_internal_error",
            "rate_limited",
            "timeout",
            "unreachable",
        ):
            with self.subTest(cause=cause):
                self.assertEqual(cause, cause_code_for(types.SimpleNamespace(kind=cause)))

    def test_builtin_error_mapping_is_unchanged(self) -> None:
        self.assertEqual("timeout", cause_code_for(TimeoutError("slow")))
        self.assertEqual("unreachable", cause_code_for(OSError("down")))
        self.assertEqual("invalid_response", cause_code_for(ValueError("bad")))
        self.assertEqual("invalid_response", cause_code_for(TypeError("bad")))
        self.assertEqual("unknown_error", cause_code_for(RuntimeError("boom")))


if __name__ == "__main__":
    unittest.main()