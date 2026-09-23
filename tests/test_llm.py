"""Unit tests for autoposting.llm (runner, parser, cache, LLMService)."""

import json
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autoposting.db import Database
from autoposting.article_prompt import load_article_style_prompt
from autoposting.llm import (
    ARTICLE_PROMPT_VERSION,
    HERMES_BINARY,
    LLMError,
    LLMService,
    MAX_OUTPUT_CHARS,
    HermesRunner,
    MacOSKeychainCredentialStore,
    _has_cjk,
    parse_model_json,
    validate_llm_settings,
)
from autoposting.normalize import content_hash


class FakeRunner:
    def __init__(self, stdout: str = "", returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(stdout=self.stdout, stderr="", returncode=self.returncode)


def _keychain_find_stdout(secret: str) -> str:
    """Match `security find-generic-password -w`: hex-encode non-ASCII bytes."""
    blob = secret.encode("utf-8")
    if all(32 <= byte <= 126 for byte in blob):
        return secret + "\n"
    return blob.hex() + "\n"


class FakeSecurityRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.secret = ""

    def __call__(self, argv: list[str], **kwargs: object) -> object:
        input_value = kwargs.get("input")
        self.calls.append((argv, input_value if isinstance(input_value, str) else None))
        if "find-generic-password" in argv:
            return types.SimpleNamespace(returncode=0, stdout=_keychain_find_stdout(self.secret), stderr="")
        if "add-generic-password" in argv:
            self.secret = str(input_value or "").rstrip("\n")
        elif "delete-generic-password" in argv:
            self.secret = ""
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def _db(tmp: tempfile.TemporaryDirectory) -> Database:
    database = Database(Path(tmp.name) / "llm-test.db")
    database.initialize()
    return database


def _items():
    return [
        {"item_id": "id-1", "title": "Model One news", "summary": "First summary text."},
        {"item_id": "id-2", "title": "Model Two news", "summary": "Second summary text."},
    ]


def _translations_payload(items=None):
    items = items or _items()
    return {
        "translations": [
            {"item_id": item["item_id"], "title_zh": f"中文标题{item['item_id']}", "summary_zh": f"中文摘要{item['item_id']}"}
            for item in items
        ]
    }


class HermesRunnerTests(unittest.TestCase):
    def test_runner_builds_exact_command_and_returns_stdout(self) -> None:
        fake = FakeRunner(stdout='{"ok": true}')
        runner = HermesRunner(command_runner=fake)

        output = runner.run("PROMPT-文本")

        self.assertEqual('{"ok": true}', output)
        self.assertEqual(
            [
                HERMES_BINARY,
                "-z",
                "PROMPT-文本",
                "-m",
                "unconfigured",
                "--provider",
                "openai-compatible",
                "--reasoning",
                "none",
                "--ignore-rules",
                "-t",
                "todo",
            ],
            fake.calls[0],
        )

    def test_runner_timeout_maps_to_stable_code(self) -> None:
        runner = HermesRunner(command_runner=FakeRunner(error=TimeoutError("hung")))
        with self.assertRaises(LLMError) as raised:
            runner.run("p")
        self.assertEqual("llm_timeout", str(raised.exception))

    def test_runner_nonzero_exit_maps_to_stable_code(self) -> None:
        runner = HermesRunner(command_runner=FakeRunner(stdout="noise", returncode=2))
        with self.assertRaises(LLMError) as raised:
            runner.run("p")
        self.assertEqual("llm_command_failed", str(raised.exception))
        # 3.2.2: a non-zero CLI exit is a provider failure, not a cause of its
        # own; the exit token is preserved on .detail for support.
        self.assertEqual("provider_error", raised.exception.cause_code)
        self.assertEqual("process_exit_2", raised.exception.detail)
        self.assertIn("退出码 2", raised.exception.reason)

    def test_runner_accepts_per_call_model_provider_and_effort(self) -> None:
        fake = FakeRunner(stdout='{"ok": true}')
        runner = HermesRunner(command_runner=fake)

        runner.run("p", model="custom-model", provider="custom-provider", effort="high")

        self.assertEqual("custom-model", fake.calls[0][fake.calls[0].index("-m") + 1])
        self.assertEqual(
            "custom-provider", fake.calls[0][fake.calls[0].index("--provider") + 1]
        )
        self.assertEqual("high", fake.calls[0][fake.calls[0].index("--reasoning") + 1])

    def test_runner_oversized_output_rejected(self) -> None:
        runner = HermesRunner(command_runner=FakeRunner(stdout="x" * (MAX_OUTPUT_CHARS + 1)))
        with self.assertRaises(LLMError) as raised:
            runner.run("p")
        self.assertEqual("llm_output_too_large", str(raised.exception))
        self.assertEqual("invalid_response", raised.exception.cause_code)
        self.assertEqual("output_too_large", raised.exception.detail)

    def test_runner_launch_failure_is_a_provider_error(self) -> None:
        # The process never started, so this is the same class of problem as a
        # non-zero exit and must not be left without a cause_code.
        runner = HermesRunner(command_runner=FakeRunner(error=OSError("no such binary")))
        with self.assertRaises(LLMError) as raised:
            runner.run("p")
        self.assertEqual("llm_command_failed", str(raised.exception))
        self.assertEqual("provider_error", raised.exception.cause_code)
        self.assertEqual("process_spawn_failed", raised.exception.detail)

    def test_keychain_store_keeps_secret_out_of_process_arguments(self) -> None:
        runner = FakeSecurityRunner()
        store = MacOSKeychainCredentialStore(runner=runner)

        store.set("endpoint-1", "secret-key")
        self.assertEqual("secret-key", store.get("endpoint-1"))
        store.delete("endpoint-1")

        self.assertTrue(all("secret-key" not in argument for call, _ in runner.calls for argument in call))
        self.assertEqual("secret-key\n", runner.calls[0][1])
        self.assertEqual("-w", runner.calls[0][0][-1])

    def test_keychain_store_roundtrips_non_ascii_secret(self) -> None:
        runner = FakeSecurityRunner()
        store = MacOSKeychainCredentialStore(runner=runner)
        payload = json.dumps(
            {
                "app_id": "wx123",
                "app_secret": "top-secret-value",
                "author": "测试作者",
                "thumb_media_id": "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

        store.set("account:wechat", payload)

        self.assertEqual(payload, store.get("account:wechat"))

    def test_keychain_store_keeps_printable_hex_secret(self) -> None:
        runner = FakeSecurityRunner()
        store = MacOSKeychainCredentialStore(runner=runner)

        store.set("endpoint-1", "deadbeef")

        self.assertEqual("deadbeef", store.get("endpoint-1"))


class OpenAICompatibleRunnerTests(unittest.TestCase):
    def test_configured_endpoint_receives_key_model_and_prompt(self) -> None:
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.headers.get("User-Agent"),
                        json.loads(body),
                    )
                )
                payload = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            runner = HermesRunner(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-api-key",
                model="remote-model",
            )
            self.assertEqual('{"ok": true}', runner.run("REAL PROMPT"))
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual("/v1/chat/completions", requests[0][0])
        self.assertEqual("Bearer test-api-key", requests[0][1])
        self.assertEqual("AutoPosting/3.2.2", requests[0][2])
        self.assertEqual("remote-model", requests[0][3]["model"])
        self.assertEqual("REAL PROMPT", requests[0][3]["messages"][0]["content"])

    def test_list_content_parts_are_joined(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": [
                                        {"type": "text", "text": '{"ok": true}'},
                                    ]
                                }
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            runner = HermesRunner(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-api-key",
                model="remote-model",
            )
            self.assertEqual('{"ok": true}', runner.run("PROMPT"))
        finally:
            server.shutdown()
            server.server_close()

    def test_empty_content_falls_back_to_reasoning_content(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "reasoning_content": '{"ok": true}',
                                }
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            runner = HermesRunner(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-api-key",
                model="remote-model",
            )
            self.assertEqual('{"ok": true}', runner.run("PROMPT"))
        finally:
            server.shutdown()
            server.server_close()


class SettingsTests(unittest.TestCase):
    def test_qwen38_none_is_sent_explicitly_to_disable_default_reasoning(self) -> None:
        settings = validate_llm_settings(
            "qwen3.8-max",
            "openai-compatible",
            "none",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "sk-test",
        )

        body = HermesRunner._completion_body(
            settings, [{"role": "user", "content": "write"}]
        )

        self.assertEqual("none", body["reasoning_effort"])
        self.assertEqual(False, body["enable_thinking"])

    def test_none_stays_omitted_for_other_compatible_endpoints(self) -> None:
        settings = validate_llm_settings(
            "remote-model",
            "openai-compatible",
            "none",
            "https://api.example/v1",
            "sk-test",
        )

        body = HermesRunner._completion_body(
            settings, [{"role": "user", "content": "write"}]
        )

        self.assertNotIn("reasoning_effort", body)

        lookalike = validate_llm_settings(
            "qwen3.8-max",
            "openai-compatible",
            "none",
            "https://notaliyuncs.com/v1",
            "sk-test",
        )
        self.assertNotIn(
            "reasoning_effort",
            HermesRunner._completion_body(
                lookalike, [{"role": "user", "content": "write"}]
            ),
        )

    def test_deepseek_v4_none_disables_default_thinking(self) -> None:
        settings = validate_llm_settings(
            "deepseek-v4-flash",
            "openai-compatible",
            "none",
            "https://api.deepseek.com/v1",
            "sk-test",
        )
        body = HermesRunner._completion_body(
            settings, [{"role": "user", "content": "write"}]
        )
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertNotIn("reasoning_effort", body)

        proxied = validate_llm_settings(
            "deepseek-v4-pro",
            "openai-compatible",
            "none",
            "https://geekspace.cloud/v1",
            "sk-test",
        )
        self.assertEqual(
            {"type": "disabled"},
            HermesRunner._completion_body(
                proxied, [{"role": "user", "content": "write"}]
            )["thinking"],
        )

    def test_deepseek_v4_effort_enables_thinking(self) -> None:
        settings = validate_llm_settings(
            "deepseek-v4-flash",
            "openai-compatible",
            "high",
            "https://api.deepseek.com/v1",
            "sk-test",
        )
        body = HermesRunner._completion_body(
            settings, [{"role": "user", "content": "write"}]
        )
        self.assertEqual({"type": "enabled"}, body["thinking"])
        self.assertEqual("high", body["reasoning_effort"])

    def test_configured_reasoning_effort_is_forwarded(self) -> None:
        settings = validate_llm_settings(
            "qwen3.8-max",
            "openai-compatible",
            "max",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "sk-test",
        )

        body = HermesRunner._completion_body(
            settings, [{"role": "user", "content": "write"}]
        )

        self.assertEqual("max", body["reasoning_effort"])

    def test_deepseek_root_url_gains_v1(self) -> None:
        settings = validate_llm_settings(
            "deepseek-chat",
            "openai-compatible",
            "none",
            "https://api.deepseek.com",
            "sk-test",
        )
        self.assertEqual("https://api.deepseek.com/v1", settings.base_url)

    def test_deepseek_v1_url_is_left_alone(self) -> None:
        settings = validate_llm_settings(
            "deepseek-chat",
            "openai-compatible",
            "none",
            "https://api.deepseek.com/v1",
            "sk-test",
        )
        self.assertEqual("https://api.deepseek.com/v1", settings.base_url)


class ParserTests(unittest.TestCase):
    def test_accepts_bare_json(self) -> None:
        self.assertEqual({"a": 1}, parse_model_json('{"a": 1}'))

    def test_accepts_fenced_json(self) -> None:
        self.assertEqual({"a": 1}, parse_model_json('```json\n{"a": 1}\n```'))
        self.assertEqual({"a": 1}, parse_model_json('```\n{"a": 1}\n```'))

    def test_extracts_json_object_after_preamble(self) -> None:
        self.assertEqual({"a": 1}, parse_model_json('Sure, here is the result:\n{"a": 1}'))
        self.assertEqual(
            {"title": "中文标题"},
            parse_model_json("<think>plan</think>\n{\"title\": \"中文标题\"}\n"),
        )

    def test_rejects_garbage(self) -> None:
        for garbage in ("", "not json", "Sure, here is the result: {"):
            with self.assertRaises(LLMError) as raised:
                parse_model_json(garbage)
            self.assertEqual("llm_invalid_output", str(raised.exception))


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = _db(self.temp_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cache_roundtrip(self) -> None:
        self.db.cache_set("k1", "translate", "general", "translate-v1", "h1", '{"a": 1}')
        self.assertEqual('{"a": 1}', self.db.cache_get("k1"))
        self.assertIsNone(self.db.cache_get("missing"))

    def test_cache_key_scopes_task_platform_version_and_content(self) -> None:
        service = LLMService(self.db)
        base = service._cache_key("translate", "general", "translate-v1", "hash-1")
        self.assertEqual(base, service._cache_key("translate", "general", "translate-v1", "hash-1"))
        keys = {
            "task": service._cache_key("article", "general", "translate-v1", "hash-1"),
            "platform": service._cache_key("translate", "wechat", "translate-v1", "hash-1"),
            "version": service._cache_key("translate", "general", "translate-v2", "hash-1"),
            "content": service._cache_key("translate", "general", "translate-v1", "hash-2"),
        }
        self.assertEqual(5, len({base, *keys.values()}))

    def test_existing_database_initialize_creates_llm_cache(self) -> None:
        table = self.db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_cache'"
        ).fetchone()
        self.assertIsNotNone(table)


class TranslateBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = _db(self.temp_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_translate_batch_calls_runner_once_for_all_missing(self) -> None:
        fake = FakeRunner(stdout=json.dumps(_translations_payload()))
        service = LLMService(self.db, runner=fake)

        results = service.translate_batch(_items())

        self.assertEqual(1, len(fake.calls))
        prompt = fake.calls[0][2]
        self.assertIn('{"item_id": "...", "title_zh": "...", "summary_zh": "..."}', prompt)
        self.assertIn("id-1", prompt)
        self.assertIn("id-2", prompt)
        for item in _items():
            result = results[item["item_id"]]
            self.assertEqual("translated", result["status"])
            self.assertEqual(f"中文标题{item['item_id']}", result["title_zh"])
            self.assertTrue(result["summary_zh"])

    def test_cjk_items_are_copied_without_calling_runner(self) -> None:
        fake = FakeRunner(stdout="{}")
        service = LLMService(self.db, runner=fake)
        items = [{"item_id": "cn-1", "title": "中文新闻标题", "summary": "中文摘要内容"}]

        results = service.translate_batch(items)

        self.assertEqual(0, len(fake.calls))
        self.assertEqual("original", results["cn-1"]["status"])
        self.assertEqual("中文新闻标题", results["cn-1"]["title_zh"])
        self.assertEqual("中文摘要内容", results["cn-1"]["summary_zh"])

    def test_cache_hit_skips_runner(self) -> None:
        fake = FakeRunner(stdout=json.dumps(_translations_payload()))
        service = LLMService(self.db, runner=fake)
        service.translate_batch(_items())
        fake.stdout = "{}"

        results = service.translate_batch(_items())

        self.assertEqual(1, len(fake.calls))
        self.assertEqual("translated", results["id-1"]["status"])
        self.assertEqual("中文标题id-1", results["id-1"]["title_zh"])

    def test_translation_cache_is_scoped_by_endpoint(self) -> None:
        class EndpointRunner(HermesRunner):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def run(self, prompt: str, **kwargs: object) -> str:
                del prompt, kwargs
                self.calls += 1
                return json.dumps(_translations_payload())

        first = EndpointRunner()
        second = EndpointRunner()
        service_a = LLMService(self.db, runner=first)
        service_b = LLMService(self.db, runner=second)
        service_a.configure("same-model", base_url="https://a.example/v1", api_key="a")
        service_b.configure("same-model", base_url="https://b.example/v1", api_key="b")

        service_a.translate_batch(_items())
        service_b.translate_batch(_items())

        self.assertEqual(1, first.calls)
        self.assertEqual(1, second.calls)

    def test_runner_failure_degrades_to_original_without_raising(self) -> None:
        fake = FakeRunner(error=TimeoutError("hung"))
        service = LLMService(self.db, runner=fake)

        results = service.translate_batch(_items())

        for item in _items():
            self.assertEqual("failed", results[item["item_id"]]["status"])
            self.assertEqual("translation_unavailable", results[item["item_id"]]["error"])
            self.assertEqual("llm_timeout", results[item["item_id"]]["error_code"])
            self.assertIn("90 秒", results[item["item_id"]]["error_reason"])
            self.assertEqual("", results[item["item_id"]]["title_zh"])

    def test_extra_ids_are_ignored_without_failing_valid_items(self) -> None:
        payload = _translations_payload()
        payload["translations"].append(
            {"item_id": "id-999", "title_zh": "凭空多出的条目", "summary_zh": "不应存在"}
        )
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(_items())

        self.assertEqual("translated", results["id-1"]["status"])
        self.assertEqual("translated", results["id-2"]["status"])
        self.assertNotIn("id-999", results)

    def test_missing_id_fails_only_that_item(self) -> None:
        payload = {
            "translations": [
                {"item_id": "id-1", "title_zh": "中文标题id-1", "summary_zh": "中文摘要id-1"},
            ]
        }
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(_items())

        self.assertEqual("translated", results["id-1"]["status"])
        self.assertEqual("failed", results["id-2"]["status"])
        self.assertEqual("translation_unavailable", results["id-2"]["error"])

    def test_non_cjk_translation_invalidates_batch(self) -> None:
        payload = {
            "translations": [
                {"item_id": "id-1", "title_zh": "English title", "summary_zh": "English summary"},
                {"item_id": "id-2", "title_zh": "Also english", "summary_zh": "Also english"},
            ]
        }
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(_items())

        self.assertEqual("failed", results["id-1"]["status"])
        self.assertEqual("translation_unavailable", results["id-1"]["error"])

    def test_one_invalid_item_does_not_drop_the_rest(self) -> None:
        payload = {
            "translations": [
                {"item_id": "id-1", "title_zh": "中文标题id-1", "summary_zh": "中文摘要id-1"},
                {"item_id": "id-2", "title_zh": "English title", "summary_zh": "English summary"},
            ]
        }
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(_items())

        self.assertEqual("translated", results["id-1"]["status"])
        self.assertEqual("中文标题id-1", results["id-1"]["title_zh"])
        self.assertEqual("failed", results["id-2"]["status"])

    def test_empty_summary_accepts_empty_summary_zh(self) -> None:
        items = [{"item_id": "id-1", "title": "Gemini Omni 1.1 Flash", "summary": ""}]
        payload = {
            "translations": [
                {"item_id": "id-1", "title_zh": "Gemini Omni 1.1 Flash 现已推出", "summary_zh": ""}
            ]
        }
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(items)

        self.assertEqual("translated", results["id-1"]["status"])
        self.assertEqual("Gemini Omni 1.1 Flash 现已推出", results["id-1"]["title_zh"])
        self.assertEqual("", results["id-1"]["summary_zh"])

    def test_stub_summary_keeps_cjk_title(self) -> None:
        items = [
            {
                "item_id": "id-1",
                "title": "Bild AI (YC W25) Is Hiring Product and AI Engineers",
                "summary": "Comments",
            }
        ]
        payload = {
            "translations": [
                {
                    "item_id": "id-1",
                    "title_zh": "Bild AI（YC W25）正在招聘产品和 AI 工程师",
                    "summary_zh": "Comments",
                }
            ]
        }
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(items)

        self.assertEqual("translated", results["id-1"]["status"])
        self.assertIn("工程师", results["id-1"]["title_zh"])


class ArticleGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = _db(self.temp_dir)
        self.source_ids = ("lab_blog", "media_feed")
        self.title = "Model One news"
        self.content = "Model One news 内容正文"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _wechat_body(self) -> str:
        return (
            "导语：模型一发布了。\n核心信息：官方博客宣布。\n影响与观察：值得关注。\n"
            "来源与未知项：lab_blog, media_feed\nAIGC 声明：本文由 AI 辅助生成。"
        )

    def _xhs_caption(self) -> str:
        return (
            "官方刚刚确认模型一已经发布。\n"
            "- 博客公开了上线消息\n"
            "- 目前仍缺具体时间表\n"
            "来源 lab_blog media_feed\nAIGC"
        )

    def _xhs_payload(self, title: str = "模型一速览") -> dict[str, str]:
        return {
            "title": title,
            "body": self._wechat_body(),
            "caption": self._xhs_caption(),
        }

    def test_wechat_article_prompt_and_validation(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertEqual("模型一正式发布", result["title"])
        self.assertIn("AIGC", result["body"])
        self.assertEqual(1, len(fake.calls))
        prompt = fake.calls[0][2]
        self.assertNotIn("不超过", prompt)
        self.assertIn("lab_blog", prompt)
        self.assertIn("不可信", prompt)
        self.assertIn("结构留给写作者，不要留给读者", prompt)
        self.assertIn("真正变了什么", prompt)
        self.assertIn("一般人容易忽略的角度", prompt)
        self.assertIn("没持续关注此事的普通读者", prompt)
        self.assertIn("背景资料", prompt)
        cached = self.db.cache_get(
            service._cache_key(
                "article",
                "wechat",
                ARTICLE_PROMPT_VERSION,
                content_hash(
                    self.title,
                    self.content
                    + "\n"
                    + json.dumps(sorted(self.source_ids), ensure_ascii=False)
                    + "\n\n"
                    + load_article_style_prompt("AI专家")
                    + "\n{}",
                ),
            )
        )
        self.assertIsNotNone(cached)

    def test_xiaohongshu_article_prompt_constraints(self) -> None:
        fake = FakeRunner(stdout=json.dumps(self._xhs_payload()))
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("xiaohongshu", self.title, self.content, self.source_ids)

        self.assertEqual("模型一速览", result["title"])
        self.assertEqual(self._wechat_body(), result["body"])
        self.assertIn("导语：模型一发布了", result["caption"])
        self.assertEqual(1, len(fake.calls))
        self.assertNotIn("不超过", fake.calls[0][2])
        self.assertIn("caption", fake.calls[0][2])
        self.assertIn("- ", fake.calls[0][2])

    def test_xiaohongshu_article_still_generates_without_caption(self) -> None:
        service = LLMService(
            self.db,
            runner=FakeRunner(
                stdout=json.dumps(
                    {
                        "title": "模型一速览",
                        "body": "正文已经发生。lab_blog media_feed AIGC",
                    }
                )
            ),
        )
        result = service.generate_article("xiaohongshu", self.title, self.content, self.source_ids)
        self.assertEqual("模型一速览", result["title"])
        self.assertIn("正文已经发生", result["caption"])
        self.assertIn("lab_blog", result["caption"])
        self.assertIn("AIGC", result["caption"])

    def test_xiaohongshu_article_accepts_title_over_20_characters(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps(
                {
                    "title": "超" * 21,
                    "body": "正文 lab_blog media_feed AIGC",
                    "caption": self._xhs_caption(),
                }
            )
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article(
            "xiaohongshu", self.title, self.content, self.source_ids
        )

        self.assertEqual("超" * 21, result["title"])

    def test_article_style_is_transmitted_and_scopes_cache(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)

        service.generate_article(
            "wechat", self.title, self.content, self.source_ids, style="像资深科技编辑一样克制"
        )
        service.generate_article(
            "wechat", self.title, self.content, self.source_ids, style="使用轻松口语"
        )

        self.assertEqual(2, len(fake.calls))
        self.assertIn("像资深科技编辑一样克制", fake.calls[0][2])
        self.assertIn("使用轻松口语", fake.calls[1][2])

    def test_preset_style_is_transmitted_and_scopes_cache(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)

        first_style = load_article_style_prompt("AI专家")
        second_style = load_article_style_prompt("经验分享")
        service.generate_article(
            "wechat", self.title, self.content, self.source_ids, preset_style=first_style
        )
        service.generate_article(
            "wechat", self.title, self.content, self.source_ids, preset_style=second_style
        )

        self.assertEqual(2, len(fake.calls))
        self.assertIn(f"写作风格要求：\n{first_style}", fake.calls[0][2])
        self.assertIn(f"写作风格要求：\n{second_style}", fake.calls[1][2])

    def test_article_cache_hit_skips_runner(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)
        service.generate_article("wechat", self.title, self.content, self.source_ids)
        fake.stdout = "{}"

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertEqual(1, len(fake.calls))
        self.assertEqual("模型一正式发布", result["title"])

    def test_article_cache_scoped_by_platform_and_content(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)
        service.generate_article("wechat", self.title, self.content, self.source_ids)

        service.generate_article("wechat", self.title, self.content + " 更新", self.source_ids)
        fake.stdout = json.dumps(self._xhs_payload())
        service.generate_article("xiaohongshu", self.title, self.content, self.source_ids)

        self.assertEqual(3, len(fake.calls))

    def test_article_cache_is_scoped_by_sources(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)
        service.generate_article("wechat", self.title, self.content, ("lab_blog",))
        fake.stdout = json.dumps(
            {
                "title": "模型一正式发布",
                "body": self._wechat_body().replace("lab_blog, media_feed", "lab_blog, new_source"),
            }
        )

        result = service.generate_article(
            "wechat", self.title, self.content, ("lab_blog", "new_source")
        )

        self.assertEqual(2, len(fake.calls))
        self.assertIn("new_source", result["body"])

    def test_article_cache_is_scoped_by_model_and_effort(self) -> None:
        fake = FakeRunner(stdout=json.dumps({"title": "模型一正式发布", "body": self._wechat_body()}))
        service = LLMService(self.db, runner=fake)

        service.generate_article("wechat", self.title, self.content, self.source_ids)
        service.generate_article(
            "wechat", self.title, self.content, self.source_ids, model="another-model", effort="high"
        )

        self.assertEqual(2, len(fake.calls))
        self.assertIn("another-model", fake.calls[1])
        self.assertIn("high", fake.calls[1])

    def test_accepts_chinese_title_and_body_keys(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps({"标题": "模型一正式发布", "正文": self._wechat_body()})
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertEqual("模型一正式发布", result["title"])
        self.assertIn("AIGC", result["body"])

    def test_accepts_nested_article_object(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps(
                {"article": {"title": "模型一正式发布", "body": self._wechat_body()}}
            )
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertEqual("模型一正式发布", result["title"])
        self.assertIn("lab_blog", result["body"])

    def test_joins_list_body_paragraphs(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps(
                {
                    "title": "模型一正式发布",
                    "body": self._wechat_body().split("\n"),
                }
            )
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertIn("AIGC", result["body"])
        self.assertIn("lab_blog", result["body"])

    def test_accepts_content_as_body_alias(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps({"title": "模型一正式发布", "content": self._wechat_body()})
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("wechat", self.title, self.content, self.source_ids)

        self.assertEqual("模型一正式发布", result["title"])
        self.assertIn("AIGC", result["body"])

    def test_summary_is_not_treated_as_body(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps({"title": "模型一正式发布", "summary": self._wechat_body()})
        )
        service = LLMService(self.db, runner=fake)
        with self.assertRaises(LLMError) as raised:
            service.generate_article("wechat", self.title, self.content, self.source_ids)
        self.assertEqual("article_generation_failed", str(raised.exception))
        self.assertIn("title 或 body", raised.exception.reason)

    def test_validation_failures_raise_stable_code(self) -> None:
        cases = [
            {"title": "模型一正式发布", "body": self._wechat_body().replace("lab_blog", "other")},
            {"title": "模型一正式发布", "body": self._wechat_body().replace("AIGC", "AI生成")},
            {"title": "English title", "body": self._wechat_body()},
            {"title": 42, "body": self._wechat_body()},
        ]
        for payload in cases:
            service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))
            with self.assertRaises(LLMError) as raised:
                service.generate_article("wechat", self.title, self.content, self.source_ids)
            self.assertEqual("article_generation_failed", str(raised.exception))

    def test_runner_failure_raises_stable_code(self) -> None:
        service = LLMService(self.db, runner=FakeRunner(error=TimeoutError("hung")))
        with self.assertRaises(LLMError) as raised:
            service.generate_article("wechat", self.title, self.content, self.source_ids)
        self.assertEqual("article_generation_failed", str(raised.exception))
        self.assertEqual("llm_timeout", raised.exception.cause_code)
        self.assertIn("模型调用超过 90 秒", raised.exception.reason)

class CjkTests(unittest.TestCase):
    def test_has_cjk(self) -> None:
        self.assertTrue(_has_cjk("中文标题"))
        self.assertTrue(_has_cjk("Model One 中文混合"))
        self.assertFalse(_has_cjk("Pure English title"))
        self.assertFalse(_has_cjk("12345 !!!"))


if __name__ == "__main__":
    unittest.main()
