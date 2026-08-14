import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autoposting.db import Database
from autoposting.llm import LLMService
from autoposting.models import FeedItem, Source
from autoposting.publishing import PublicationError, PublicationService
from autoposting.web import WebApp, create_server

from test_workbench import FakeModel, WechatStubHandler, _feed


class ChatStubHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict[str, object]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        ChatStubHandler.requests.append(
            (self.path, self.headers.get("Authorization", ""), payload)
        )
        body = json.dumps(
            {"choices": [{"message": {"content": "第二轮回复"}}]},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class ModelStubHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict[str, object] | None]] = []
    reject_model = ""

    def _send(self, code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        ModelStubHandler.requests.append(
            (self.path, self.headers.get("Authorization", ""), None)
        )
        self._send(200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        ModelStubHandler.requests.append(
            (self.path, self.headers.get("Authorization", ""), payload)
        )
        if payload.get("model") == ModelStubHandler.reject_model:
            self._send(400, {"error": {"message": "model rejected"}})
            return
        self._send(200, {"choices": [{"message": {"content": "OK"}}]})

    def log_message(self, *args: object) -> None:
        pass


class FakeCredentialStore:
    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    def get(self, endpoint_id: str) -> str | None:
        return self.secrets.get(endpoint_id)

    def set(self, endpoint_id: str, api_key: str) -> None:
        self.secrets[endpoint_id] = api_key

    def delete(self, endpoint_id: str) -> None:
        self.secrets.pop(endpoint_id, None)


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        WechatStubHandler.requests = []
        WechatStubHandler.fail_errcode = None
        cls.wechat_stub = ThreadingHTTPServer(("127.0.0.1", 0), WechatStubHandler)
        cls.stub_thread = threading.Thread(target=cls.wechat_stub.serve_forever, daemon=True)
        cls.stub_thread.start()
        cls.wechat_stub_url = f"http://127.0.0.1:{cls.wechat_stub.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.wechat_stub.shutdown()
        cls.wechat_stub.server_close()

    def setUp(self) -> None:
        WechatStubHandler.requests = []
        WechatStubHandler.fail_errcode = None
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        self.sources = (
            Source("lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15),
            Source("media_feed", "AI Media", "rss", "https://media.example/feed.xml", "S3", ("models",), True, 15),
        )
        for source in self.sources:
            self.db.upsert_source(source)
        self.feed = _feed(
            [("w1", "Model One news", "https://lab.example/w1", 5), ("w2", "Model Two news", "https://lab.example/w2", 60)]
        )
        self.credentials = FakeCredentialStore()
        self.app = WebApp(
            self.db,
            self.sources,
            self.root / "packages",
            fetcher=lambda _: self.feed,
            wechat_base_url=self.wechat_stub_url,
            llm_service=LLMService(self.db, runner=FakeModel()),
            credential_store=self.credentials,
            page_fetcher=lambda _: b"",
        )
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, dict[str, object]]:
        body = None
        headers = {}
        if isinstance(payload, str):
            body = payload.encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _insert_item(self, source: Source, external_id: str, title: str, url: str) -> str:
        self.db.insert_item(
            source,
            FeedItem(external_id, url, title, f"{title} 内容", None, {}),
        )
        return str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])

    def test_status_and_index_placeholder(self) -> None:
        status, payload = self._request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("0.4.0", payload["version"])
        self.assertEqual("unconfigured", payload["llm"]["model"])
        self.assertEqual("openai-compatible", payload["llm"]["provider"])
        self.assertEqual("none", payload["llm"]["effort"])
        self.assertTrue(payload["llm"]["enabled"])
        self.assertFalse(payload["platforms"]["wechat"]["configured"])
        self.assertTrue(payload["platforms"]["xiaohongshu"]["configured"])
        self.assertFalse(payload["platforms"]["x"]["configured"])
        self.assertTrue(payload["sources"])
        self.assertIn(payload["sources"][0]["status"], {"ok", "error", "stale", "never", "disabled"})

        request = urllib.request.Request(self.base_url + "/", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("text/html", response.headers["Content-Type"])
        self.assertIn("autoposting", html.lower())

    def test_collect_forces_collection_and_returns_candidates(self) -> None:
        status, payload = self._request("POST", "/api/collect", {"hours": 24})
        self.assertEqual(200, status)
        self.assertEqual(2, payload["count"])
        self.assertEqual(2, self.db.count_items())
        self.assertIn("rankings", payload)
        self.assertEqual(payload["candidates"], payload["rankings"]["time"])
        self.assertEqual({"heat", "time"}, set(payload["rankings"]))
        self.assertTrue(payload["sources"])
        self.assertIn("inserted", payload["collection"])
        self.assertEqual(
            {
                "item_id",
                "title",
                "summary",
                "observed_at",
                "source_ids",
                "source_urls",
                "source_count",
                "title_zh",
                "summary_zh",
                "translation_status",
                "translation_error",
                "engagement",
                "engagement_weight",
                "base_tier",
                "effective_tier",
                "promoted",
                "discussion",
            },
            set(payload["candidates"][0]),
        )
        self.assertIn(
            payload["candidates"][0]["translation_status"],
            {"unavailable", "original", "translated", "failed"},
        )

        status, payload = self._request("POST", "/api/collect", {"hours": 7})
        self.assertEqual(200, status)
        status, payload = self._request("POST", "/api/collect", {"hours": 0})
        self.assertEqual(400, status)
        self.assertIn("hours", payload["error"])
        status, payload = self._request("POST", "/api/collect", {"hours": 721})
        self.assertEqual(400, status)

    def test_hotspots_get_lists_existing_items_without_collect(self) -> None:
        self._insert_item(self.sources[0], "now-1", "最新模型发布", "https://lab.example/now-1")
        status, payload = self._request("GET", "/api/hotspots?hours=4")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        titles = [item["title"] for item in payload["candidates"]]
        self.assertIn("最新模型发布", titles)
        self.assertEqual(payload["candidates"], payload["rankings"]["time"])
        status, payload = self._request("GET", "/api/hotspots?hours=nope")
        self.assertEqual(400, status)

    def test_hotspots_translate_query_uses_model(self) -> None:
        self._insert_item(self.sources[0], "en-1", "English model news", "https://lab.example/en-1")
        status, payload = self._request("GET", "/api/hotspots?hours=4")
        self.assertEqual(200, status)
        self.assertEqual("unavailable", payload["candidates"][0]["translation_status"])
        status, payload = self._request("GET", "/api/hotspots?hours=4&translate=1")
        self.assertEqual(200, status)
        self.assertEqual("translated", payload["candidates"][0]["translation_status"])
        self.assertTrue(payload["candidates"][0]["title_zh"])

    def test_articles_endpoint_is_idempotent_per_item(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")

        status, first = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        status2, second = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})

        self.assertEqual(200, status)
        self.assertEqual(200, status2)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual("DRAFTED", first["status"])
        self.assertIn("AIGC", first["body"])
        self.assertIn("lab_blog", first["body"])
        self.assertIn("【信息来源 1 条 · 原文 1 篇】", first["body"])
        self.assertIn("https://lab.example/w1", first["body"])
        self.assertEqual(
            "openai-compatible/unconfigured:none:article-v4",
            self.db.get_draft(first["draft_id"])["generator"],
        )

        status, payload = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "douyin"})
        self.assertEqual(400, status)
        status, payload = self._request("POST", "/api/articles", {"item_id": "missing", "platform": "wechat"})
        self.assertEqual(404, status)

    def test_find_hooks_endpoint_and_articles_accept_chosen_hooks(self) -> None:
        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "hook-api",
                "https://lab.example/hook-api",
                "中国医生用 GPT-5.6 破解数学题",
                "本科经协和医学院4+4试点班转入临床医学。",
                None,
                {},
            ),
        )
        item_id = str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])

        def fake_fetch(url: str) -> bytes:
            if "4" in url and "试点" in url or "4%2B4" in url or "%E8%AF%95%E7%82%B9" in url:
                return (
                    "<html><body>"
                    '<a class="result__a" href="https://news.example/4plus4">协和4+4试点班争议</a>'
                    '<a class="result__snippet">该项目曾引发舆论质疑。</a>'
                    "</body></html>"
                ).encode("utf-8")
            return b"<html></html>"

        self.app.workbench.page_fetcher = fake_fetch

        status, payload = self._request("POST", "/api/hooks/find", {"item_id": "missing"})
        self.assertEqual(404, status)

        status, payload = self._request("POST", "/api/hooks/find", {"item_id": item_id})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertIn("hooks", payload)

        status, article = self._request(
            "POST",
            "/api/articles",
            {
                "item_id": item_id,
                "platform": "wechat",
                "chosen_hooks": [{"phrase": "协和4+4试点班", "why": "公开检索出现争议"}],
            },
        )
        self.assertEqual(200, status)
        self.assertIn("AIGC", article["body"])

    def test_articles_returns_502_with_stable_code_when_llm_unavailable(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        app = WebApp(
            self.db,
            self.sources,
            self.root / "packages",
            fetcher=lambda _: self.feed,
            llm_service=LLMService(self.db, runner=FakeModel(fail_with=TimeoutError("hung"))),
            page_fetcher=lambda _: b"",
        )
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            request = urllib.request.Request(
                base_url + "/api/articles",
                data=json.dumps({"item_id": item_id, "platform": "wechat"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            self.assertEqual(502, raised.exception.code)
            body = json.load(raised.exception)
            self.assertEqual("article_generation_failed", body["error"])
            self.assertEqual("llm_timeout", body["error_code"])
            self.assertIn("模型调用超过 90 秒", body["error_reason"])
            self.assertNotIn("hung", json.dumps(body))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(0, self.db.count_drafts())

    def test_config_wechat_stays_in_memory_only(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/config",
            {"platform": "wechat", "app_id": "wx123", "app_secret": "top-secret-value", "thumb_media_id": "thumb-1", "author": "测试作者"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        self.assertEqual("测试作者", payload["author"])
        self.assertNotIn("top-secret-value", json.dumps(payload))
        self.assertNotIn("wx123", json.dumps(payload))

        status, status_payload = self._request("GET", "/api/status")
        self.assertTrue(status_payload["platforms"]["wechat"]["configured"])
        self.assertNotIn("top-secret-value", json.dumps(status_payload))
        database_bytes = self.root.joinpath("autoposting.db").read_bytes()
        self.assertNotIn(b"top-secret-value", database_bytes)

    def test_config_xiaohongshu_and_unknown_platform(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/config",
            {
                "platform": "xiaohongshu",
                "account_name": "测试账号",
                "creator_url": "https://creator.xiaohongshu.com/publish/publish",
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        _, status_payload = self._request("GET", "/api/status")
        self.assertTrue(status_payload["platforms"]["xiaohongshu"]["account_configured"])

        status, payload = self._request(
            "POST",
            "/api/config",
            {"platform": "x", "bearer_token": "x-secret-token"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        self.assertNotIn("x-secret-token", json.dumps(payload))
        _, status_payload = self._request("GET", "/api/status")
        self.assertTrue(status_payload["platforms"]["x"]["configured"])
        self.assertNotIn("x-secret-token", json.dumps(status_payload))

        status, payload = self._request("POST", "/api/config", {"platform": "douyin"})
        self.assertEqual(400, status)

    def test_model_settings_are_validated_and_used_by_article_request(self) -> None:
        ModelStubHandler.requests = []
        ModelStubHandler.reject_model = "broken-model"
        stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        remote_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
        try:
            status, discovered = self._request(
                "POST", "/api/models", {"base_url": remote_url, "api_key": "secret-key"}
            )
            save_status, saved = self._request(
                "POST",
                "/api/model",
                {
                    "model": "model-b",
                    "effort": "high",
                    "base_url": remote_url,
                    "api_key": "secret-key",
                },
            )
            failed_status, failed = self._request(
                "POST",
                "/api/model",
                {
                    "model": "broken-model",
                    "effort": "low",
                    "base_url": remote_url,
                    "api_key": "secret-key",
                },
            )
        finally:
            stub.shutdown()
            stub.server_close()

        self.assertEqual(200, status)
        self.assertEqual(["model-a", "model-b"], discovered["models"])
        self.assertEqual(200, save_status)
        self.assertEqual("model-b", saved["llm"]["model"])
        self.assertEqual("high", saved["llm"]["effort"])
        self.assertEqual(502, failed_status)
        self.assertEqual("llm_api_failed", failed["error"])
        self.assertEqual("model-b", self.app.status()["llm"]["model"])
        self.assertEqual("high", self.app.status()["llm"]["effort"])
        self.assertEqual(
            ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"],
            [request[0] for request in ModelStubHandler.requests],
        )
        validation_body = ModelStubHandler.requests[1][2]
        self.assertEqual("model-b", validation_body["model"])
        self.assertEqual("high", validation_body["reasoning_effort"])
        self.assertNotIn("secret-key", json.dumps(self.app.status()))

    def test_chat_models_are_discovered_validated_and_saved(self) -> None:
        ModelStubHandler.requests = []
        ModelStubHandler.reject_model = ""
        stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        thread = threading.Thread(target=stub.serve_forever, daemon=True)
        thread.start()
        remote_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
        try:
            status, discovered = self._request(
                "POST",
                "/api/models",
                {"purpose": "chat", "base_url": remote_url, "api_key": "chat-key"},
            )
            self.credentials.secrets[discovered["endpoint_id"]] = ""
            save_status, saved = self._request(
                "POST",
                "/api/chat/model",
                {
                    "endpoint_id": discovered["endpoint_id"],
                    "model": "model-b",
                    "base_url": remote_url,
                    "api_key": "chat-key",
                },
            )
        finally:
            stub.shutdown()
            stub.server_close()

        self.assertEqual(200, status)
        self.assertEqual(["model-a", "model-b"], discovered["models"])
        self.assertEqual(200, save_status)
        self.assertEqual("model-b", saved["chat_llm"]["model"])
        self.assertEqual(
            ["/v1/models", "/v1/chat/completions"],
            [request[0] for request in ModelStubHandler.requests],
        )

    def test_model_endpoints_persist_switch_and_delete(self) -> None:
        first_stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        second_stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        for stub in (first_stub, second_stub):
            threading.Thread(target=stub.serve_forever, daemon=True).start()
        first_url = f"http://127.0.0.1:{first_stub.server_address[1]}/v1"
        second_url = f"http://127.0.0.1:{second_stub.server_address[1]}/v1"
        try:
            self._request(
                "POST",
                "/api/models",
                {"base_url": first_url, "api_key": "first-key"},
            )
            self._request(
                "POST",
                "/api/model",
                {
                    "model": "model-a",
                    "effort": "low",
                    "base_url": first_url,
                    "api_key": "first-key",
                },
            )
            self._request(
                "POST",
                "/api/chat/model",
                {"model": "model-b", "base_url": first_url, "api_key": "first-key"},
            )
            self._request(
                "POST",
                "/api/models",
                {"base_url": second_url, "api_key": "second-key"},
            )
            self._request(
                "POST",
                "/api/model",
                {
                    "model": "model-b",
                    "effort": "high",
                    "base_url": second_url,
                    "api_key": "second-key",
                },
            )

            restarted = WebApp(
                self.db,
                self.sources,
                self.root / "restarted-packages",
                credential_store=self.credentials,
                page_fetcher=lambda _: b"",
            )
            restored = restarted.status()
            first = next(
                endpoint for endpoint in restored["model_endpoints"] if endpoint["base_url"] == first_url
            )
            second = next(
                endpoint for endpoint in restored["model_endpoints"] if endpoint["base_url"] == second_url
            )

            switched = restarted.set_model(
                {"endpoint_id": first["id"], "model": "model-a", "effort": "minimal"}
            )
            deleted = restarted.delete_model_endpoint({"endpoint_id": first["id"]})
        finally:
            for stub in (first_stub, second_stub):
                stub.shutdown()
                stub.server_close()

        self.assertEqual(second_url, restored["llm"]["base_url"])
        self.assertEqual(first_url, restored["chat_llm"]["base_url"])
        self.assertEqual(2, len(restored["model_endpoints"]))
        self.assertTrue(all(endpoint["credential_available"] for endpoint in restored["model_endpoints"]))
        self.assertEqual(first_url, switched["llm"]["base_url"])
        self.assertEqual([second["id"]], [endpoint["id"] for endpoint in deleted["model_endpoints"]])
        self.assertFalse(deleted["llm"]["configured"])
        self.assertFalse(deleted["chat_llm"]["configured"])
        serialized = json.dumps(restored)
        self.assertNotIn("first-key", serialized)
        self.assertNotIn("second-key", serialized)
        database_bytes = self.root.joinpath("autoposting.db").read_bytes()
        self.assertNotIn(b"first-key", database_bytes)
        self.assertNotIn(b"second-key", database_bytes)

    def test_articles_require_successfully_configured_remote_model(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "w1", "Model One news", "https://lab.example/w1"
        )
        app = WebApp(
            self.db,
            self.sources,
            self.root / "unconfigured-packages",
            page_fetcher=lambda _: b"",
        )
        server = create_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/articles",
                data=json.dumps({"item_id": item_id, "platform": "wechat"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=10)
            status = raised.exception.code
            payload = json.load(raised.exception)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(400, status)
        self.assertEqual("article_model_not_configured", payload["error"])

    def test_chat_model_is_independent_and_forwards_native_history(self) -> None:
        ChatStubHandler.requests = []
        chat_stub = ThreadingHTTPServer(("127.0.0.1", 0), ChatStubHandler)
        chat_thread = threading.Thread(target=chat_stub.serve_forever, daemon=True)
        chat_thread.start()
        chat_url = f"http://127.0.0.1:{chat_stub.server_address[1]}/v1"
        messages = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ]
        try:
            status, configured = self._request(
                "POST",
                "/api/chat/model",
                {
                    "model": "chat-only-model",
                    "base_url": chat_url,
                    "api_key": "chat-secret-key",
                },
            )
            chat_status, reply = self._request(
                "POST", "/api/chat", {"messages": messages}
            )
        finally:
            chat_stub.shutdown()
            chat_stub.server_close()

        self.assertEqual(200, status)
        self.assertEqual("chat-only-model", configured["chat_llm"]["model"])
        self.assertEqual(200, chat_status)
        self.assertEqual("第二轮回复", reply["message"]["content"])
        self.assertEqual("assistant", reply["message"]["role"])
        self.assertEqual(2, len(ChatStubHandler.requests))
        path, authorization, request_payload = ChatStubHandler.requests[-1]
        self.assertEqual("/v1/chat/completions", path)
        self.assertEqual("Bearer chat-secret-key", authorization)
        self.assertEqual(messages, request_payload["messages"])
        self.assertEqual("chat-only-model", request_payload["model"])
        app_status = self.app.status()
        self.assertEqual("unconfigured", app_status["llm"]["model"])
        self.assertEqual("chat-only-model", app_status["chat_llm"]["model"])
        self.assertNotIn("chat-secret-key", json.dumps(app_status))
        self.assertNotIn(b"chat-secret-key", self.root.joinpath("autoposting.db").read_bytes())

    def test_chat_validates_messages_and_requires_configuration(self) -> None:
        status, payload = self._request("POST", "/api/chat", {"messages": []})
        self.assertEqual(400, status)
        self.assertIn("at least one", payload["error"])

        status, payload = self._request("POST", "/api/chat", {"messages": "nope"})
        self.assertEqual(400, status)
        self.assertIn("messages", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/chat",
            {"messages": [{"role": "system", "content": "hi"}]},
        )
        self.assertEqual(400, status)
        self.assertIn("role", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/chat",
            {"messages": [{"role": "user", "content": "   "}]},
        )
        self.assertEqual(400, status)
        self.assertIn("non-empty", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/chat",
            {"messages": [{"role": "user", "content": "x" * 20_001}]},
        )
        self.assertEqual(400, status)
        self.assertIn("20000", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/chat",
            {"messages": [{"role": "user", "content": "m"} for _ in range(51)]},
        )
        self.assertEqual(400, status)
        self.assertIn("50", payload["error"])

    def test_chat_model_requires_complete_first_config_and_retains_blank_key(self) -> None:
        status, payload = self._request(
            "POST", "/api/chat/model", {"model": "chat-model", "base_url": "", "api_key": ""}
        )
        self.assertEqual(400, status)
        self.assertIn("base_url", payload["error"])

        stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        remote_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
        try:
            status, configured = self._request(
                "POST",
                "/api/chat/model",
                {
                    "model": "chat-model",
                    "base_url": remote_url,
                    "api_key": "chat-secret-key",
                },
            )
            self.assertEqual(200, status)
            self.assertNotIn("chat-secret-key", json.dumps(configured))

            status, updated = self._request(
                "POST",
                "/api/chat/model",
                {
                    "model": "chat-model-2",
                    "base_url": remote_url,
                    "api_key": "",
                },
            )
        finally:
            stub.shutdown()
            stub.server_close()
        self.assertEqual(200, status)
        self.assertEqual("chat-model-2", updated["chat_llm"]["model"])
        self.assertEqual("chat-secret-key", self.app.chat_service.settings.api_key)

    def test_style_settings_are_used_for_article_generation(self) -> None:
        status, payload = self._request(
            "POST", "/api/style", {"style": "像资深科技编辑一样克制"}
        )
        self.assertEqual(200, status)
        self.assertEqual("像资深科技编辑一样克制", payload["style"])
        self.assertEqual("像资深科技编辑一样克制", self.app.status()["style"])

    def test_article_edit_persists_and_requires_new_approval(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "w1", "Model One news", "https://lab.example/w1"
        )
        _, article = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "wechat"}
        )
        self._request(
            "POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"}
        )
        edited_body = article["body"] + "\n人工编辑后的补充。"

        status, edited = self._request(
            "POST",
            "/api/articles/edit",
            {"draft_id": article["draft_id"], "title": "人工编辑标题", "body": edited_body},
        )

        self.assertEqual(200, status)
        self.assertEqual("DRAFTED", edited["status"])
        self.assertEqual("人工编辑标题", self.db.get_draft(article["draft_id"])["title"])
        self.assertEqual(edited_body, self.db.get_draft(article["draft_id"])["body"])
        publish_status, publish_payload = self._request(
            "POST",
            "/api/publish",
            {"draft_id": article["draft_id"], "platform": "wechat", "live": True},
        )
        self.assertEqual(400, publish_status)
        self.assertEqual("draft_not_approved", publish_payload["error"])

    def test_approve_endpoint(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})

        status, payload = self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        self.assertEqual(200, status)
        self.assertEqual("APPROVED", payload["status"])
        self.assertEqual("APPROVED", self.db.get_event(article["event_id"])["status"])

    def test_publish_wechat_live_requires_explicit_config(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "wechat", "live": True}
        )

        self.assertEqual(400, status)
        self.assertEqual("wechat_configuration_required", payload["error"])

    def test_publish_wechat_live_archives_final_edit_without_leaking_credentials(self) -> None:
        self._request(
            "POST",
            "/api/config",
            {"platform": "wechat", "app_id": "wx123", "app_secret": "top-secret-value", "thumb_media_id": "thumb-1", "author": ""},
        )
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        edited_title = "人工编辑后的归档标题"
        edited_body = article["body"] + "\n人工编辑后的归档正文。"
        self._request(
            "POST",
            "/api/articles/edit",
            {"draft_id": article["draft_id"], "title": edited_title, "body": edited_body},
        )
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "wechat", "live": True}
        )

        self.assertEqual(200, status)
        self.assertEqual("SUBMITTED", payload["status"])
        self.assertEqual("stub-publish-1", payload["reference"])
        archive_path = Path(str(payload["archive_path"]))
        self.assertTrue(archive_path.exists())
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        self.assertEqual(edited_title, archived["title"])
        self.assertEqual(edited_body, archived["body"])
        self.assertNotIn("top-secret-value", json.dumps(payload))
        self.assertNotIn("stub-token-123", json.dumps(payload))
        database_bytes = self.root.joinpath("autoposting.db").read_bytes()
        self.assertNotIn(b"top-secret-value", database_bytes)
        self.assertNotIn(b"stub-token-123", database_bytes)
        attempt = self.db.get_latest_publication_attempt(article["draft_id"])
        self.assertEqual("SUBMITTED", attempt["status"])
        self.assertEqual("stub-publish-1", attempt["response_reference"])
        for _, _, body in WechatStubHandler.requests:
            if body and "top-secret-value" in body:
                self.fail("secret leaked into API request body")

    def test_schedule_publish_creates_pending_job(self) -> None:
        item_id = self._insert_item(self.sources[0], "sched-1", "Model One news", "https://lab.example/sched-1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST",
            "/api/schedule",
            {
                "draft_id": article["draft_id"],
                "platform": "wechat",
                "live": False,
                "run_at": "2099-01-01T12:00:00+00:00",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("PENDING", payload["status"])
        self.assertTrue(payload["job_id"])
        listed = self._request("GET", "/api/schedule")[1]
        self.assertEqual(1, len(listed["jobs"]))
        cancel, cancelled = self._request(
            "POST", "/api/schedule/cancel", {"job_id": payload["job_id"]}
        )
        self.assertEqual(200, cancel)
        self.assertEqual("CANCELLED", cancelled["status"])

    def test_article_image_selection_and_proxy_reject_local_file(self) -> None:
        item_id = self._insert_item(self.sources[0], "img-sel", "Model One news", "https://lab.example/img-sel")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        assets = article["assets"]
        self.assertEqual("article-assets-v2", assets["schema"])
        self.assertEqual([], assets["candidates"])
        status, payload = self._request(
            "POST",
            "/api/articles/images",
            {"draft_id": article["draft_id"], "cover_id": "", "body_ids": []},
        )
        self.assertEqual(200, status)
        self.assertEqual([], payload["assets"]["body_ids"])
        request = urllib.request.Request(self.base_url + "/api/image-proxy?url=file:///etc/passwd")
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("proxy should reject file URLs")
        except urllib.error.HTTPError as exc:
            self.assertEqual(400, exc.code)

    def test_repeated_wechat_live_publish_reuses_submitted_attempt(self) -> None:
        self._request(
            "POST",
            "/api/config",
            {"platform": "wechat", "app_id": "wx123", "app_secret": "secret", "thumb_media_id": "thumb-1"},
        )
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        first_status, first = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "wechat", "live": True}
        )
        requests_after_first = len(WechatStubHandler.requests)
        second_status, second = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "wechat", "live": True}
        )

        self.assertEqual((200, 200), (first_status, second_status))
        self.assertEqual(first["reference"], second["reference"])
        self.assertEqual(requests_after_first, len(WechatStubHandler.requests))

    def test_publish_wechat_dry_run_writes_local_package(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "wechat"}
        )

        self.assertEqual(200, status)
        self.assertEqual("DRY_RUN", payload["status"])
        self.assertTrue(Path(str(payload["package_path"])).exists())

    def test_publish_xiaohongshu_requires_manual_publish(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "xiaohongshu"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "xiaohongshu", "live": True}
        )
        self.assertEqual(400, status)
        self.assertEqual("draft_not_approved", payload["error"])

        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})
        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "xiaohongshu", "live": True}
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["manual_required"])
        self.assertEqual("https://creator.xiaohongshu.com/publish/publish", payload["creator_url"])
        package = json.loads(Path(str(payload["package_path"])).read_text(encoding="utf-8"))
        self.assertEqual("xiaohongshu", package["platform"])
        self.assertTrue(package["dry_run"])

    def test_platform_drafts_are_independently_approvable_and_publishable(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "w1", "Model One news", "https://lab.example/w1"
        )
        _, wechat = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "wechat"}
        )
        _, xiaohongshu = self._request(
            "POST",
            "/api/articles",
            {"item_id": item_id, "platform": "xiaohongshu"},
        )
        self.assertNotEqual(wechat["event_id"], xiaohongshu["event_id"])

        self._request(
            "POST",
            "/api/approve",
            {"draft_id": xiaohongshu["draft_id"], "reviewer": "human"},
        )
        status, payload = self._request(
            "POST",
            "/api/publish",
            {
                "draft_id": xiaohongshu["draft_id"],
                "platform": "xiaohongshu",
                "live": True,
            },
        )

        self.assertEqual(200, status)
        self.assertTrue(payload["manual_required"])
        self.assertEqual("DRAFTED", self.db.get_draft(wechat["draft_id"])["status"])

    def test_publish_platform_mismatch_is_rejected(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, article = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        self._request("POST", "/api/approve", {"draft_id": article["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": article["draft_id"], "platform": "xiaohongshu"}
        )

        self.assertEqual(400, status)
        self.assertEqual("platform_mismatch", payload["error"])

    def test_superseded_draft_cannot_be_published(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")
        _, current = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts (
                    draft_id, event_id, platform, title, body, tags_json,
                    assets_json, source_ids_json, generator, processing_version,
                    status, validation_json, content_hash, approved_by,
                    approved_at, created_at, updated_at
                ) VALUES (?, ?, 'wechat', '旧稿', '旧正文 lab_blog AIGC', '[]',
                          '[]', '[\"lab_blog\"]', 'old', 'draft-v1',
                          'APPROVED', '{}', 'old-hash', 'human', 'now', '2000', '2000')
                """,
                ("superseded-draft", current["event_id"]),
            )
        self._request("POST", "/api/approve", {"draft_id": current["draft_id"], "reviewer": "human"})

        status, payload = self._request(
            "POST", "/api/publish", {"draft_id": "superseded-draft", "platform": "wechat"}
        )

        self.assertEqual(400, status)
        self.assertEqual("draft_superseded", payload["error"])
        with self.assertRaisesRegex(PublicationError, "draft_superseded"):
            PublicationService(self.db, self.root / "packages", dry_run=True).publish(
                "superseded-draft"
            )

    def test_stable_json_errors(self) -> None:
        status, payload = self._request("POST", "/api/collect", "not json")
        self.assertEqual(400, status)
        self.assertEqual("invalid_json", payload["error"])

        status, payload = self._request(
            "POST",
            "/api/publish",
            {"draft_id": "missing", "platform": "wechat", "live": "false"},
        )
        self.assertEqual(400, status)
        self.assertEqual("live must be boolean", payload["error"])

        status, payload = self._request("GET", "/api/unknown")
        self.assertEqual(404, status)
        self.assertFalse(payload["ok"])

        status, payload = self._request("POST", "/api/articles", {})
        self.assertEqual(400, status)
        self.assertIn("item_id", payload["error"])

        with socket.create_connection(self.server.server_address, timeout=10) as connection:
            connection.sendall(
                b"POST /api/model HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 1048577\r\n"
                b"Connection: close\r\n\r\n"
            )
            response = b""
            while chunk := connection.recv(4096):
                response += chunk
        status_line, _, body = response.partition(b"\r\n\r\n")
        self.assertIn(b" 400 ", status_line)
        self.assertEqual("request_body_too_large", json.loads(body)["error"])

    def test_post_rejects_non_json_and_cross_origin_requests(self) -> None:
        request = urllib.request.Request(
            self.base_url + "/api/config",
            data=b'{"platform":"xiaohongshu"}',
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as wrong_type:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(400, wrong_type.exception.code)
        self.assertEqual("content_type_must_be_application_json", json.load(wrong_type.exception)["error"])

        request = urllib.request.Request(
            self.base_url + "/api/config",
            data=b'{"platform":"xiaohongshu"}',
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cross_origin:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(403, cross_origin.exception.code)
        self.assertEqual("cross_origin_request_rejected", json.load(cross_origin.exception)["error"])

    def test_non_local_host_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.app, "0.0.0.0", 0)
        server = create_server(self.app, "localhost", 0)
        server.server_close()

    def _index(self) -> str:
        request = urllib.request.Request(self.base_url + "/", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            return response.read().decode("utf-8")

    def test_find_hooks_workbench_contract(self) -> None:
        html = self._index()
        self.assertIn("找切口", html)
        self.assertIn('id="find-hooks-btn"', html)
        self.assertIn('id="hook-list"', html)
        self.assertIn('api("/api/hooks/find"', html)
        self.assertIn("chosen_hooks", html)
        self.assertIn("主切口", html)
        self.assertIn("须进开头", html)
        self.assertIn("state.chosenHookIds", html)
        self.assertIn("function clearHooks()", html)
        self.assertIn("clearHooks()", html)
        self.assertLess(html.index("找切口"), html.index("生成文章"))
        self.assertNotIn('api("/api/hooks/find"', html.split("function generate()")[1].split("function persistArticle()")[0])


class IndexPageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        sources = (
            Source("lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15),
        )
        for source in sources:
            self.db.upsert_source(source)
        self.app = WebApp(
            self.db,
            sources,
            self.root / "packages",
            fetcher=lambda _: b"",
            page_fetcher=lambda _: b"",
        )
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _index(self) -> str:
        request = urllib.request.Request(self.base_url + "/", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            return response.read().decode("utf-8")

    def test_hours_select_offers_fresh_windows_and_custom(self) -> None:
        html = self._index()
        self.assertIn('id="hours-select"', html)
        for value in ("4", "6", "24", "72", "168", "720"):
            self.assertIn(f'<option value="{value}"', html)
        self.assertIn('<option value="custom">自定义</option>', html)
        self.assertIn('id="hours-custom"', html)
        self.assertRegex(html, r'<option value="4"[^>]*selected')
        self.assertIn("function currentHours()", html)

    def test_action_buttons_and_dialog_contract(self) -> None:
        html = self._index()
        for button_id in (
            "collect-btn",
            "generate-btn",
            "save-article-btn",
            "publish-btn",
            "config-btn",
        ):
            self.assertIn(f'id="{button_id}"', html)
        self.assertIn('id="publish-btn" disabled', html)
        self.assertIn('id="schedule-btn" disabled', html)
        self.assertIn('id="schedule-at"', html)
        self.assertIn("<dialog", html)
        self.assertIn('id="config-dialog"', html)
        self.assertIn('name="app_secret"', html)
        self.assertIn('type="password"', html)
        self.assertIn('id="config-cancel"', html)
        self.assertIn("showModal", html)
        self.assertIn('config-dialog").close()', html)
        self.assertIn("form.reset()", html)
        self.assertIn("aria-label", html)

    def test_platform_segmented_control(self) -> None:
        html = self._index()
        self.assertIn('data-platform="wechat"', html)
        self.assertIn('data-platform="xiaohongshu"', html)

    def test_api_paths_and_approve_before_publish(self) -> None:
        html = self._index()
        for path in (
            "/api/status",
            "/api/collect",
            "/api/articles",
            "/api/articles/edit",
            "/api/config",
            "/api/model",
            "/api/approve",
            "/api/publish",
        ):
            self.assertIn(path, html)
        self.assertIn("live: true", html)
        self.assertIn('reviewer: "web-user"', html)
        self.assertLess(html.index('"/api/approve"'), html.index('"/api/publish"'))

    def test_changing_topic_or_platform_clears_stale_draft(self) -> None:
        html = self._index()
        self.assertIn("function resetArticle()", html)
        self.assertIn("state.draft = null", html)
        self.assertIn("state.item = item;\n  resetArticle();", html)
        self.assertIn("state.platform = platform;\n  resetArticle();", html)

    def test_manual_required_handling(self) -> None:
        html = self._index()
        self.assertIn("manual_required", html)
        self.assertIn("creator_url", html)
        self.assertIn("package_path", html)
        self.assertIn("官方创作中心", html)
        self.assertIn("人工提交", html)

    def test_publish_result_is_unambiguous_and_complete(self) -> None:
        html = self._index()
        self.assertIn("微信公众号 API：未配置", html)
        self.assertEqual(1, html.count("仍需在官方创作中心人工提交"))
        self.assertIn("#package-path{overflow-wrap:anywhere; word-break:normal", html)
        self.assertIn('var completed = false;', html)
        self.assertIn('button.dataset.originalLabel = result.manual_required ? "已生成发布包" : "已提交";', html)
        self.assertIn("if (completed) { button.disabled = true; }", html)

    def test_first_wechat_configuration_resumes_pending_publish(self) -> None:
        html = self._index()
        self.assertIn("pendingPublish: false", html)
        self.assertIn("openSettings(true)", html)
        self.assertIn("var resumePublish = state.pendingPublish", html)
        self.assertIn("state.pendingPublish = false", html)
        self.assertIn("if (resumePublish) { publish(); }", html)

    def test_hotspot_and_article_render_fields(self) -> None:
        html = self._index()
        for field in ("item_id", "title", "summary", "observed_at", "source_count", "title_zh", "summary_zh"):
            self.assertIn(field, html)
        for element in ("article-title", "article-body", "article-sources", "draft_id"):
            self.assertIn(element, html)

    def test_hotspot_candidate_wording_and_note(self) -> None:
        html = self._index()
        self.assertIn("热点候选", html)
        self.assertIn("热度看独立来源数和讨论量", html)

    def test_bilingual_card_contract(self) -> None:
        html = self._index()
        self.assertIn("原文", html)
        self.assertIn("中文（AI翻译，非官方表述）", html)
        self.assertIn("未翻译（未配置模型）", html)

    def test_version_badge(self) -> None:
        html = self._index()
        self.assertIn("0.4.0", html)

    def test_patch_020_compact_chat_contract(self) -> None:
        html = self._index()
        for element_id in (
            "chat-input",
            "chat-model",
            "chat-send-btn",
            "chat-output",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="chat-output"', html)
        self.assertIn('hidden aria-live="polite"', html)
        self.assertIn('name="chat_model"', html)
        self.assertIn('id="endpoint-use-chat"', html)
        self.assertIn('api("/api/chat"', html)
        self.assertIn('"/api/chat/model"', html)
        self.assertIn("min-height:100dvh", html)
        self.assertIn("overflow-y:auto", html)
        self.assertIn(".article-scroll[hidden]{display:none;}", html)
        self.assertIn("if (button.disabled) { return; }", html)
        self.assertIn("state.chatMessages.slice(-48)", html)
        self.assertIn("if (!state.chat.configured)", html)
        self.assertIn("@media (max-width: 360px)", html)

    def test_patch_021_chat_output_expands_page_without_message_bubbles(self) -> None:
        html = self._index()
        self.assertIn('document.body.classList.add("chat-expanded")', html)
        self.assertIn("function fitArticleBody", html)
        self.assertIn("min-height:calc(100dvh - 8.1rem)", html)
        self.assertIn("body.chat-expanded{min-height:100dvh; overflow-y:auto;}", html)
        self.assertIn("body.chat-expanded main{flex:1 0 auto;}", html)
        self.assertIn("body.chat-expanded #article-view{flex:1 0 auto;}", html)
        self.assertIn(".chat-section.chat-active{flex:0 0 auto; overflow:visible;}", html)
        self.assertIn("#chat-output{", html)
        self.assertIn("overflow:visible", html)
        chat_content = html[html.index(".chat-content{") : html.index(".chat-msg-error")]
        self.assertIn("background:transparent", chat_content)
        self.assertIn("border-top:1px solid var(--border)", chat_content)
        self.assertIn("border-bottom:1px solid var(--border)", chat_content)
        self.assertIn("border-radius:0", chat_content)
        self.assertNotIn("border:1px solid", chat_content)

    def test_patch_013_article_layout_and_model_controls(self) -> None:
        html = self._index()
        self.assertIn("grid-template-columns:1fr 2fr", html)
        self.assertIn('id="article-scroll" class="article-scroll"', html)
        self.assertIn("overflow-y:auto", html)
        self.assertIn('<textarea id="article-body"', html)
        self.assertIn('id="article-title" type="text"', html)
        actions = html[html.index('class="article-actions"') : html.index('id="publish-status"')]
        self.assertLess(actions.index('id="article-model"'), actions.index('id="publish-btn"'))
        self.assertNotIn('id="h-publish"', html)
        self.assertIn("justify-content:space-between", html)

    def test_patch_013_settings_contract(self) -> None:
        html = self._index()
        for name in (
            "model",
            "base_url",
            "api_key",
            "style",
            "app_id",
            "app_secret",
            "thumb_media_id",
            "account_name",
            "creator_url",
        ):
            self.assertIn(f'name="{name}"', html)
        self.assertIn('type="file"', html)
        self.assertIn("FileReader", html)
        self.assertIn('api("/api/style"', html)
        self.assertIn("form.elements.style.value", html)
        self.assertIn("grid-template-columns:minmax(0, 1fr)", html)
        self.assertIn('data-settings-platform="wechat"', html)
        self.assertIn('data-settings-platform="xiaohongshu"', html)

    def test_patch_021_model_discovery_and_effort_contract(self) -> None:
        html = self._index()
        self.assertIn('name="model"', html)
        self.assertIn('<select name="model"', html)
        self.assertIn('name="effort"', html)
        self.assertIn('id="endpoint-connect"', html)
        self.assertIn('api("/api/models"', html)
        self.assertIn(".dialog-content{display:flex; flex-direction:column; gap:.8rem; min-height:0; overflow-y:auto", html)

    def test_patch_022_model_management_contract(self) -> None:
        html = self._index()
        self.assertIn('<select name="chat_model"', html)
        self.assertIn('id="endpoint-connect"', html)
        self.assertIn('id="endpoint-use-article"', html)
        self.assertIn('id="endpoint-use-chat"', html)
        self.assertIn('id="endpoint-list"', html)
        self.assertIn("编辑模型", html)
        self.assertIn('api("/api/model-endpoints/delete"', html)
        self.assertNotIn("本次指定模型", html)
        self.assertNotIn("本次使用指定模型", html)
        self.assertNotIn('id="article-custom-model"', html)
        self.assertNotIn('id="chat-custom-model"', html)
        self.assertNotIn('id="model-refresh"', html)
        self.assertNotIn('name="chat_base_url"', html)

    def test_patch_023_publish_auto_saves_edits_and_aligns_same_row_controls(self) -> None:
        html = self._index()
        self.assertIn("state.articleDirty = true", html)
        self.assertIn('state.articleDirty = true;\n    $("publish-btn").disabled = false;', html)
        self.assertIn("function persistArticle()", html)
        self.assertIn("state.articleDirty ? persistArticle() : Promise.resolve()", html)
        self.assertIn('api("/api/articles/edit"', html)
        self.assertNotIn('status("publish-status", "请先保存修改"', html)
        self.assertIn(
            ".chat-row > .model-picker,.chat-row > input,.chat-row > button{height:40px; min-height:40px; padding-top:0; padding-bottom:0;}",
            html,
        )
        self.assertIn("display:inline-flex; align-items:center; justify-content:center", html)
        height_rule = (
            "select,input[type=text],input[type=password]{height:40px; padding-top:0; padding-bottom:0;}"
        )
        padding_rule = (
            "input[type=text], input[type=password], textarea{width:100%; border-radius:var(--radius-md); padding:.8rem 1rem;}"
        )
        self.assertIn(height_rule, html)
        self.assertIn(padding_rule, html)
        self.assertGreater(html.index(height_rule), html.index(padding_rule))
        self.assertIn("#config-dialog fieldset > .row{align-items:flex-end; flex-wrap:nowrap;}", html)

    def test_patch_024_images_hours_schedule_and_hermes_api_flow(self) -> None:
        html = self._index()
        self.assertIn("function currentHours()", html)
        self.assertIn("function schedulePublish()", html)
        self.assertIn("function connectEndpoint()", html)
        self.assertIn('api("/api/schedule"', html)
        self.assertIn("连接并拉取模型", html)
        self.assertIn("用于文章", html)
        self.assertIn("和 Hermes 一样", html)

    def test_patch_025_source_health_and_dual_rank_tabs(self) -> None:
        html = self._index()
        self.assertIn('data-rank="heat"', html)
        self.assertIn('data-rank="time"', html)
        self.assertIn("热度优先", html)
        self.assertIn("时间优先", html)
        self.assertIn('id="source-health"', html)
        self.assertIn("刷新全部", html)
        self.assertIn("function renderSourceHealth", html)
        self.assertIn("function setRank", html)
        self.assertIn("payload.source_id", html)
        self.assertIn("新入库", html)

    def test_patch_026_image_picker_contract(self) -> None:
        html = self._index()
        self.assertIn('id="image-picker"', html)
        self.assertIn("function renderImagePicker", html)
        self.assertIn("function persistImages()", html)
        self.assertIn('api("/api/articles/images"', html)
        self.assertIn("/api/image-proxy?url=", html)
        self.assertIn("设为封面", html)
        self.assertIn("加入正文", html)
        self.assertIn("上移", html)
        self.assertIn("下移", html)
        self.assertIn("封面和正文分开选", html)
        self.assertIn('id="image-rescan"', html)
        self.assertIn('api("/api/articles/images/rescan"', html)
        self.assertIn("未从原文找到配图", html)
        self.assertIn("点击预览可看大图", html)

    def test_patch_027_lightbox_and_first_party_copy(self) -> None:
        html = self._index()
        self.assertIn('id="image-lightbox"', html)
        self.assertIn('id="lightbox-image"', html)
        self.assertIn('id="lightbox-close"', html)
        self.assertIn('id="lightbox-prev"', html)
        self.assertIn('id="lightbox-next"', html)
        self.assertIn("function openImageLightbox", html)
        self.assertIn("function closeImageLightbox", html)
        self.assertIn("function stepImageLightbox", html)
        self.assertIn("点击查看大图", html)
        self.assertIn("cursor:zoom-in", html)

    def test_patch_028_source_frame_and_x_settings(self) -> None:
        html = self._index()
        self.assertIn("讨论观察", html)
        self.assertIn("function setSettingsPlatform", html)
        self.assertIn('data-settings-platform="x"', html)
        self.assertIn('name="x_bearer_token"', html)
        self.assertIn("社区帖过阈会升温", html)
        self.assertIn("某某称", html)
        self.assertIn("function loadHotspots", html)
        self.assertIn("/api/hotspots?hours=", html)
        self.assertNotIn("renderHotspots([])", html)

    def test_patch_029_candidates_sit_above_collapsed_source_health(self) -> None:
        html = self._index()
        self.assertLess(html.index('id="hotspot-list"'), html.index('id="source-health"'))
        self.assertIn('id="source-health-toggle"', html)
        self.assertIn("只刷这个", html)
        self.assertIn("翻译标题", html)
        self.assertIn("function translateTitles", html)
        self.assertIn("抓取成功", html)
        self.assertIn("这些不是新闻", html)
        self.assertIn("function widenIfEmpty", html)
        self.assertIn("只重新抓取这一路订阅", html)
        self.assertIn('id="hotspot-pane"', html)
        source_health = html[html.index('id="source-health"') :]
        self.assertIn("hidden", source_health[:120])

    def test_patch_0210_panels_grow_from_029_floor(self) -> None:
        html = self._index()
        self.assertIn("min-height:calc(100dvh - 8.1rem)", html)
        self.assertIn("function fitArticleBody", html)
        self.assertIn("#hotspot-list{", html)

    def test_patch_0211_fixed_hotspot_scroll_sources_open_chat_flow_and_third(self) -> None:
        html = self._index()
        self.assertIn("grid-template-columns:1fr 2fr", html)
        self.assertIn('id="hotspots-panel"', html)
        self.assertIn("function setSourcesOpen", html)
        self.assertIn('panel.classList.toggle("sources-open"', html)
        self.assertIn("#hotspots-panel.sources-open", html)
        hotspot = html[html.index("#hotspot-list{") : html.index("#hotspot-list li")]
        self.assertIn("overflow-y:auto", hotspot)
        self.assertNotIn("overflow:visible", hotspot)
        pane = html[html.index("#hotspot-pane{") : html.index("#hotspots-panel.sources-open #hotspot-pane")]
        self.assertIn("overflow:hidden", pane)
        panel = html[html.index("#hotspots-panel{") : html.index("#hotspots-panel.sources-open{")]
        self.assertIn("overflow:hidden", panel)
        opened = html[html.index("#hotspots-panel.sources-open{") : html.index("#hotspots-panel.sources-open{") + 120]
        self.assertIn("max-height:none", opened)
        self.assertLess(html.index('id="article-model"'), html.index('class="chat-section"'))
        self.assertLess(html.index('id="article-scroll"'), html.index('class="chat-section"'))
        self.assertIn("#article-view{flex:0 0 auto", html)
        self.assertIn("align-items:start", html)
        self.assertIn("点开看来源", html)

    def test_patch_033_image_panel_two_hotspots_and_motion(self) -> None:
        html = self._index()
        self.assertIn("function fitHotspotList", html)
        self.assertIn("Math.min(2, items.length)", html)
        self.assertIn("overscroll-behavior:contain", html)
        self.assertIn("--ease:", html)
        self.assertIn("--t-fast:", html)
        self.assertIn("--t-mid:", html)
        self.assertIn("animation:rise var(--t-mid) var(--ease)", html)
        self.assertIn('class="brand-kicker"', html)
        self.assertIn("本地工作台", html)
        picker = html[html.index("#image-picker{") : html.index("#image-picker[hidden]")]
        self.assertIn("overflow:hidden", picker)
        self.assertIn("max-height:20.5rem", picker)
        scroller = html[html.index("#image-picker-list{") : html.index(".image-card{")]
        self.assertIn("overflow-y:auto", scroller)
        self.assertIn("overscroll-behavior:contain", scroller)
        self.assertIn('class="image-picker-head"', html)
        list_rule = html[html.index("#hotspot-list{") : html.index("#hotspot-list li")]
        self.assertIn("overflow-y:auto", list_rule)
        self.assertIn("height:var(--hotspot-list-h)", list_rule)
        self.assertNotIn("emoji", html.lower())

    def test_responsive_and_no_external_resources(self) -> None:
        html = self._index()
        self.assertIn("@media (max-width: 900px)", html)
        self.assertNotIn("<link ", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("http://", html)

    def test_no_plaintext_example_secret(self) -> None:
        html = self._index()
        self.assertNotIn("top-secret", html)
        self.assertNotIn("s3cr3t", html)
        self.assertNotIn("placeholder", html)
        self.assertNotIn('name="app_secret" value', html)


if __name__ == "__main__":
    unittest.main()
