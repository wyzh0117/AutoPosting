"""Unit tests for autoposting.llm (runner, parser, cache, LLMService)."""

import json
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autoposting.db import Database
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


class FakeSecurityRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.secret = ""

    def __call__(self, argv: list[str], **kwargs: object) -> object:
        input_value = kwargs.get("input")
        self.calls.append((argv, input_value if isinstance(input_value, str) else None))
        if "find-generic-password" in argv:
            return types.SimpleNamespace(returncode=0, stdout=self.secret + "\n", stderr="")
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
        self.assertEqual("process_exit_2", raised.exception.cause_code)
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

    def test_keychain_store_keeps_secret_out_of_process_arguments(self) -> None:
        runner = FakeSecurityRunner()
        store = MacOSKeychainCredentialStore(runner=runner)

        store.set("endpoint-1", "secret-key")
        self.assertEqual("secret-key", store.get("endpoint-1"))
        store.delete("endpoint-1")

        self.assertTrue(all("secret-key" not in argument for call, _ in runner.calls for argument in call))
        self.assertEqual("secret-key\n", runner.calls[0][1])
        self.assertEqual("-w", runner.calls[0][0][-1])


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
        self.assertEqual("AutoPosting/0.3.4", requests[0][2])
        self.assertEqual("remote-model", requests[0][3]["model"])
        self.assertEqual("REAL PROMPT", requests[0][3]["messages"][0]["content"])


class SettingsTests(unittest.TestCase):
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

    def test_rejects_garbage(self) -> None:
        for garbage in ("", "not json", "Sure, here is the result: {", "text before {\"a\": 1}"):
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

    def test_extra_or_missing_ids_invalidate_batch(self) -> None:
        payload = _translations_payload()
        payload["translations"].append(
            {"item_id": "id-999", "title_zh": "凭空多出的条目", "summary_zh": "不应存在"}
        )
        service = LLMService(self.db, runner=FakeRunner(stdout=json.dumps(payload)))

        results = service.translate_batch(_items())

        self.assertEqual("failed", results["id-1"]["status"])
        self.assertEqual("translation_unavailable", results["id-1"]["error"])

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
        self.assertIn("某某称", prompt)
        self.assertIn("大家在聊什么", prompt)
        self.assertIn("前因与脉络", prompt)
        self.assertIn("普通人", prompt)
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
                    + "\n\n{}",
                ),
            )
        )
        self.assertIsNotNone(cached)

    def test_xiaohongshu_article_prompt_constraints(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps({"title": "模型一速览", "body": "钩子！\n要点一。\n要点二。\n#AI资讯\n来源 lab_blog media_feed\nAIGC"})
        )
        service = LLMService(self.db, runner=fake)

        result = service.generate_article("xiaohongshu", self.title, self.content, self.source_ids)

        self.assertEqual("模型一速览", result["title"])
        self.assertEqual(1, len(fake.calls))
        self.assertNotIn("不超过", fake.calls[0][2])

    def test_xiaohongshu_article_accepts_title_over_20_characters(self) -> None:
        fake = FakeRunner(
            stdout=json.dumps(
                {"title": "超" * 21, "body": "正文 lab_blog media_feed AIGC"}
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

    def test_extract_hook_candidates_parses_and_fails_soft(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = FakeRunner(
            stdout='{"candidates":[{"phrase":"协和医学院4+4试点班","kind":"program"}]}'
        )
        service = LLMService(
            _db(tmp),
            runner=fake,
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        found = service.extract_hook_candidates("……协和医学院4+4试点班……")
        self.assertEqual("program", found[0]["kind"])
        self.assertIn("候选切口JSON", fake.calls[0][2])

        broken = FakeRunner(stdout="not-json")
        service = LLMService(
            _db(tmp),
            runner=broken,
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        self.assertEqual([], service.extract_hook_candidates("正文"))
        empty = LLMService(_db(tmp), runner=FakeRunner(stdout="{}"))
        self.assertEqual([], empty.extract_hook_candidates("正文"))


class CjkTests(unittest.TestCase):
    def test_has_cjk(self) -> None:
        self.assertTrue(_has_cjk("中文标题"))
        self.assertTrue(_has_cjk("Model One 中文混合"))
        self.assertFalse(_has_cjk("Pure English title"))
        self.assertFalse(_has_cjk("12345 !!!"))


if __name__ == "__main__":
    unittest.main()
