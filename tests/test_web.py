import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from autoposting.db import Database
from autoposting.images import default_page_fetcher
from autoposting.llm import LLMService
from autoposting.models import FeedItem, Source
from autoposting.publishing import PublicationError, PublicationService
from autoposting.reddit import reddit_hot_url, reddit_rss_url
from autoposting.web import INDEX_HTML, WebApp, create_server

from test_github_weekly import TRENDING_HTML
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


class RelatedSearchModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(argv)
        return types.SimpleNamespace(
            stdout=json.dumps({"query": "模型发布 最新进展 新闻"}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )


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
        self.model = FakeModel()
        self.app = WebApp(
            self.db,
            self.sources,
            self.root / "packages",
            fetcher=lambda _: self.feed,
            wechat_base_url=self.wechat_stub_url,
            llm_service=LLMService(self.db, runner=self.model),
            translation_service=LLMService(self.db, runner=self.model),
            credential_store=self.credentials,
            page_fetcher=lambda _: b"",
        )
        self.app._wechat_poster_renderer = lambda _title: b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
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

    def test_github_weekly_refresh_uses_fixed_template(self) -> None:
        self.app.github_fetcher = (
            lambda url: TRENDING_HTML if "trending" in url else {"items": []}
        )
        status, payload = self._request("POST", "/api/github-weekly", {})
        self.assertEqual(200, status)
        self.assertGreaterEqual(int(payload["count"]), 2)
        self.assertEqual("本周值得关注的github ai项目前10", payload["xhs_title"])
        self.assertIn("llm-cli", payload["xhs_body"])
        self.assertIn("https://github.com/acme/llm-cli", payload["xhs_body"])
        self.assertEqual("llm-cli", payload["repos"][0]["name"])
        self.assertFalse(str(payload["repos"][0].get("summary") or "").strip())
        self.assertFalse(payload["repos"][0]["enriched"])
        self.assertEqual(len(payload["repos"]) + 1, len(payload["images"]))
        self.assertEqual("cover", payload["images"][0]["kind"])
        self.assertEqual("本周爆火ai工具", payload["images"][0]["title"])
        # D5/D8: the plan is render-only; no image-model prompt may survive.
        for image in payload["images"]:
            self.assertNotIn("prompt", image)
        status2, saved = self._request("GET", "/api/github-weekly")
        self.assertEqual(200, status2)
        self.assertEqual(payload["repos"][0]["full_name"], saved["repos"][0]["full_name"])

    def test_resource_discovery_loopback_mixed_set_persists_and_refreshes(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if "api.github.com/search/repositories" in url:
                return {
                    "items": [
                        {
                            "full_name": "acme/ppt-agent",
                            "html_url": "https://github.com/acme/ppt-agent",
                            "description": "AI agent for making presentations",
                            "stargazers_count": 42,
                            "topics": ["ai", "ppt"],
                        }
                    ]
                }
            return {"skills": []}

        self.app.github_fetcher = getter
        self.app.skill_web_search = lambda _query: [
            {
                "title": "Presentation workflow guide",
                "url": "https://docs.example/ppt-agent",
                "snippet": "A practical web guide.",
            }
        ]
        status, payload = self._request(
            "POST",
            "/api/resources/discover",
            {
                "query": "制作 PPT 的 AI agent",
                "pasted_text": "https://github.com/acme/ppt-agent/tree/main/skills/slides",
                "adapters": ["repo", "skill", "web"],
                "limit": 10,
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        resource_set = payload["resource_set"]
        self.assertEqual("resource-set-v1", resource_set["schema_version"])
        self.assertEqual("制作 PPT 的 AI agent", resource_set["query"])
        self.assertEqual({"github", "skills", "web"}, set(resource_set["adapter_status"]))
        self.assertGreaterEqual(resource_set["counts"]["repo"], 1)
        self.assertGreaterEqual(resource_set["counts"]["skill"], 1)
        self.assertGreaterEqual(resource_set["counts"]["website"], 1)
        self.assertTrue(any(row["source"] == "pasted" for row in resource_set["candidates"]))
        self.assertNotIn("token", json.dumps(resource_set, ensure_ascii=False).lower())
        set_id = resource_set["set_id"]

        call_count = len(calls)
        restored_status, restored = self._request("GET", "/api/resources/" + set_id)
        self.assertEqual(200, restored_status)
        self.assertEqual(resource_set, restored["resource_set"])
        listed_status, listed = self._request("GET", "/api/resources?limit=20")
        self.assertEqual(200, listed_status)
        self.assertEqual(set_id, listed["resource_sets"][0]["set_id"])

        refresh_status, refreshed = self._request(
            "POST", "/api/resources/" + set_id + "/refresh", {}
        )
        self.assertEqual(200, refresh_status)
        self.assertEqual(resource_set, refreshed["resource_set"])
        self.assertEqual(call_count, len(calls), "refresh must restore, not re-run adapters")

    def test_resource_discovery_github_weekly_preset_is_a_resource_set(self) -> None:
        def getter(_url: str) -> object:
            return {
                "items": [
                    {
                        "full_name": "acme/weekly-ai",
                        "html_url": "https://github.com/acme/weekly-ai",
                        "description": "A weekly AI project",
                        "stargazers_count": 9,
                    }
                ]
            }

        self.app.github_fetcher = getter
        status, payload = self._request(
            "POST",
            "/api/resources/discover",
            {"preset": "github_weekly", "adapters": ["repo"], "limit": 5},
        )
        self.assertEqual(200, status)
        resource_set = payload["resource_set"]
        self.assertEqual("github_weekly", resource_set["preset"])
        self.assertEqual("本周 AI 新项目", resource_set["query"])
        self.assertEqual(1, resource_set["counts"]["repo"])
        self.assertEqual(0, resource_set["counts"]["skill"])

    def test_github_weekly_refresh_does_not_call_image_model(self) -> None:
        """D5/D8 regression guard: no image-model surface may come back."""
        self.app.github_fetcher = (
            lambda url: TRENDING_HTML if "trending" in url else {"items": []}
        )
        # The image model is torn out entirely: it must not be reachable at all.
        self.assertFalse(hasattr(self.app, "image_service"))
        self.assertFalse(hasattr(self.app, "image_generator"))
        self.assertFalse(hasattr(self.app, "_image_model_configured"))
        status, payload = self._request("POST", "/api/github-weekly", {})
        self.assertEqual(200, status)
        self.assertTrue(payload["images"])
        self.assertTrue(all(not image.get("url") for image in payload["images"]))
        self.assertTrue(all("prompt" not in image for image in payload["images"]))
        # The configuration endpoint is gone too.
        status2, _ = self._request("POST", "/api/image/model", {"model": "x"})
        self.assertNotEqual(200, status2)

    def test_no_image_model_call_site_can_come_back(self) -> None:
        """D5/D8 teardown guard: the model-image layer must stay deleted.

        Both image lanes render through HTML/CSS + browser screenshot only, and
        the image-model prompt/config surface is gone. Reintroducing any of these
        names would silently re-create a call path that has no consumer.
        """
        root = Path(__file__).resolve().parents[1]
        forbidden = (
            "generate_image",
            "IMAGE_ASPECT_RATIO",
            "configure_verified_image",
            "load_image_prompt",
            "render_weekly_images",
            "generate_missing_images",
            "image_prompt_dir",
            "IMAGE_PROMPT_FILES",
        )
        offenders: list[str] = []
        for path in sorted((root / "src" / "autoposting").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                if name in text:
                    offenders.append(f"{path.relative_to(root)}:{name}")
        self.assertEqual([], offenders, msg=f"图像模型调用面又回来了：{offenders}")
        self.assertFalse((root / "src" / "autoposting" / "skill_cover.py").exists())
        for gone in ("STYLE.md", "cover.md", "poster.md", "skill_cover.md"):
            self.assertFalse((root / "docs" / "image_prompts" / gone).exists())
        # The config route is gone, not merely disabled.
        self.assertNotIn("/api/image/model", INDEX_HTML)
        self.assertNotIn("image_llm", INDEX_HTML)

    def test_github_weekly_render_applies_theme_and_palette(self) -> None:
        self.app.github_fetcher = (
            lambda url: TRENDING_HTML if "trending" in url else {"items": []}
        )
        seen: dict[str, object] = {}

        def fake_render(repos, jobs, out_dir, theme="dark", palette=None, **kwargs):
            seen["theme"] = theme
            seen["palette"] = palette
            seen["count"] = len(repos)
            out_dir.mkdir(parents=True, exist_ok=True)
            rendered = []
            for job in jobs:
                filename = f"{job['id']}.png"
                (out_dir / filename).write_bytes(b"png")
                row = dict(job)
                row["file"] = filename
                row["url"] = f"/api/generated/github-weekly/{filename}"
                rendered.append(row)
            return rendered

        self.app.visual_renderer = fake_render
        from copy_fixtures import attach_stub_poster_copy

        attach_stub_poster_copy(self.app)
        self._request("POST", "/api/github-weekly", {})
        status, payload = self._request(
            "POST",
            "/api/github-weekly/render",
            {
                "theme": "light",
                "palette": {"main": "#F3EFE4", "secondary": "#FFFCF6", "accent": "#C45C26"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("light", seen["theme"])
        self.assertEqual("#C45C26", seen["palette"]["accent"])
        self.assertTrue(payload["images"][0]["url"])
        self.assertEqual("light", payload["visual"]["theme"])
        self.assertEqual("#C45C26", payload["visual"]["palette"]["accent"])

    def test_github_weekly_render_without_enrichment_returns_error(self) -> None:
        self.app.github_fetcher = (
            lambda url: TRENDING_HTML if "trending" in url else {"items": []}
        )
        self._request("POST", "/api/github-weekly", {})
        status, payload = self._request("POST", "/api/github-weekly/render", {"theme": "dark"})
        self.assertEqual(502, status)
        self.assertEqual("llm_enrichment_failed", payload["error"])
        self.assertIn("LLM_ENRICHMENT_FAILED", str(payload.get("error_reason") or ""))

    def test_github_weekly_image_renders_one_job_and_accepts_palette(self) -> None:
        self.app.github_fetcher = (
            lambda url: TRENDING_HTML if "trending" in url else {"items": []}
        )
        seen: dict[str, object] = {}

        def fake_render(repos, jobs, out_dir, theme="dark", palette=None, **kwargs):
            seen["theme"] = theme
            seen["palette"] = palette
            seen["job_ids"] = [job["id"] for job in jobs]
            out_dir.mkdir(parents=True, exist_ok=True)
            rendered = []
            for job in jobs:
                filename = f"{job['id']}.png"
                (out_dir / filename).write_bytes(b"png")
                row = dict(job)
                row["file"] = filename
                row["url"] = f"/api/generated/github-weekly/{filename}"
                rendered.append(row)
            return rendered

        self.app.visual_renderer = fake_render
        status, weekly = self._request("POST", "/api/github-weekly", {})
        self.assertEqual(200, status)
        image_id = str(weekly["images"][0]["id"])
        later_id = str(weekly["images"][1]["id"])
        status, payload = self._request(
            "POST",
            "/api/github-weekly/image",
            {
                "id": image_id,
                "theme": "light",
                "palette": {"main": "#F3EFE4", "secondary": "#FFFCF6", "accent": "#C45C26"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("light", seen["theme"])
        self.assertEqual("#C45C26", seen["palette"]["accent"])
        self.assertEqual([image_id], seen["job_ids"])
        self.assertEqual(image_id, payload["id"])
        self.assertTrue(payload["url"])
        saved = self._request("GET", "/api/github-weekly")[1]
        by_id = {str(image["id"]): image for image in saved["images"]}
        self.assertTrue(by_id[image_id]["url"])
        self.assertFalse(by_id[later_id].get("url"))
        self.assertEqual("light", saved["visual"]["theme"])
        self.assertEqual("#C45C26", saved["visual"]["palette"]["accent"])

    def test_status_and_index_placeholder(self) -> None:
        status, payload = self._request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual("3.2.2", payload["version"])
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

    def test_status_source_health_exposes_redacted_diagnostic(self) -> None:
        secret = "token=ghp_do_not_leak https://private.example/feed SELECT * FROM secrets"
        self.db.record_fetch_result("lab_blog", secret)

        status, payload = self._request("GET", "/api/status")
        self.assertEqual(200, status)
        source = next(item for item in payload["sources"] if item["source_id"] == "lab_blog")
        diagnostic = source["diagnostic"]
        self.assertEqual("source_fetch_failed", diagnostic["code"])
        self.assertEqual("unknown_error", diagnostic["cause_code"])
        self.assertTrue(diagnostic["degraded"])
        self.assertEqual("lab_blog", diagnostic["source"])
        self.assertEqual("collect", diagnostic["stage"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("ghp_do_not_leak", rendered)
        self.assertNotIn("https://private.example/feed", rendered)
        self.assertNotIn("SELECT * FROM secrets", rendered)

    def test_source_registry_routes_use_runtime_db_state(self) -> None:
        status, payload = self._request("GET", "/api/sources")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        source = next(item for item in payload["sources"] if item["source_id"] == "lab_blog")
        self.assertEqual("rss", source["adapter_type"])
        self.assertIn("source_type", source)
        self.assertTrue(source["enabled"])

        status, payload = self._request(
            "POST", "/api/sources/enabled", {"source_id": "lab_blog", "enabled": False}
        )
        self.assertEqual(200, status)
        source = next(item for item in payload["sources"] if item["source_id"] == "lab_blog")
        self.assertFalse(source["enabled"])

        status, payload = self._request(
            "POST", "/api/sources/fetch-interval", {"source_id": "lab_blog", "minutes": 45}
        )
        self.assertEqual(200, status)
        source = next(item for item in payload["sources"] if item["source_id"] == "lab_blog")
        self.assertEqual(45, source["fetch_interval_minutes"])
        self.assertFalse(self.db.get_source("lab_blog")["enabled"])
        self.assertEqual(45, self.db.get_source("lab_blog")["fetch_interval_minutes"])

        status, payload = self._request(
            "POST", "/api/sources/enabled", {"source_id": "lab_blog", "enabled": "false"}
        )
        self.assertEqual(400, status)
        self.assertEqual("enabled must be boolean", payload["error"])

    def test_collection_run_routes_expose_actual_source_attempts(self) -> None:
        status, collected = self._request("POST", "/api/collect", {"hours": 24})
        self.assertEqual(200, status)
        self.assertIn("collection", collected)

        status, payload = self._request("GET", "/api/collection-runs?limit=5")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["runs"]), 1)
        run = payload["runs"][0]
        self.assertTrue(run["run_id"])
        self.assertIn("source_attempts", run)
        self.assertEqual(2, len(run["source_attempts"]))

        status, detail = self._request("GET", "/api/collection-runs/" + run["run_id"])
        self.assertEqual(200, status)
        self.assertEqual(run["run_id"], detail["run"]["run_id"])
        self.assertEqual(run["source_attempts"], detail["run"]["source_attempts"])

    def test_mixed_collection_api_returns_unified_candidates_and_run(self) -> None:
        reddit = Source(
            "reddit_ai",
            "Reddit AI",
            "reddit",
            "r/LocalLLaMA",
            "S4",
            ("models",),
            True,
            15,
        )
        self.db.upsert_source(reddit)
        reddit_feed = _feed(
            [
                (
                    "reddit1",
                    "Model One news",
                    "https://www.reddit.com/r/LocalLLaMA/comments/reddit1/discussion",
                    5,
                )
            ]
        )
        fetcher = {
            "https://lab.example/feed.xml": _feed(
                [("lab1", "Model One news", "https://lab.example/model-one", 10)]
            ),
            reddit_hot_url("LocalLLaMA"): b"not-json",
            reddit_rss_url("LocalLLaMA"): reddit_feed,
        }
        self.app.hotspots.sources = (self.sources[0], reddit)
        self.app.hotspots.fetcher = fetcher.get

        status, payload = self._request(
            "POST", "/api/collect", {"hours": 24, "lane": "mixed"}
        )

        self.assertEqual(200, status)
        self.assertEqual("DONE", payload["collection_run"]["status"])
        self.assertEqual(
            {"lab_blog", "reddit_ai"},
            set(payload["collection_run"]["requested_sources"]),
        )
        self.assertEqual(2, len(payload["collection_run"]["source_attempts"]))
        event = payload["radar"]["events"][0]
        self.assertEqual({"lab_blog", "reddit_ai"}, set(event["source_ids"]))
        self.assertEqual(2, event["independent_source_count"])

    def test_research_routes_run_and_restore_multi_selected_dossier(self) -> None:
        collect_status, collected = self._request("POST", "/api/collect", {"hours": 24})
        self.assertEqual(200, collect_status)
        radar_events = (collected.get("radar") or {}).get("events") or []
        event_ids = [
            str(item["event_id"])
            for item in radar_events
            if isinstance(item, dict) and item.get("event_id")
        ][:2]
        if len(event_ids) < 2:
            event_ids = [
                self.db.create_event("模型发布", "models", "HOTSPOT_ITEM", "web-research-1"),
                self.db.create_event("行业反应", "models", "HOTSPOT_ITEM", "web-research-2"),
            ]
        first, second = event_ids

        class Search:
            def search(self, query: str, *, adapters: tuple[str, ...], limit: int) -> dict[str, object]:
                return {
                    "query": query,
                    "adapters": list(adapters),
                    "results": [
                        {
                            "title": "官方研究说明",
                            "url": "https://example.com/research",
                            "snippet": "研究摘要",
                        }
                    ],
                }

        class Reader:
            def read(self, candidate: object) -> dict[str, object]:
                return {
                    "status": "full",
                    "title": getattr(candidate, "title", "证据"),
                    "text": "研究正文",
                    "quality_score": 0.9,
                }

        service = self.app.workbench.research_service
        service.search_adapter = Search()
        service.reader = Reader()
        status, created = self._request(
            "POST",
            "/api/research",
            {"event_ids": [first, second], "query": "模型发布研究"},
        )
        self.assertEqual(200, status)
        self.assertTrue(created["ok"])
        dossier = created["dossier"]
        self.assertEqual([first, second], dossier["event_ids"])
        self.assertIn(dossier["status"], {"ok", "degraded"})
        self.assertTrue(dossier["evidence"])

        status, restored = self._request(
            "GET", "/api/research/" + dossier["dossier_id"]
        )
        self.assertEqual(200, status)
        self.assertEqual(dossier["dossier_id"], restored["dossier"]["dossier_id"])
        self.assertEqual(dossier["event_ids"], restored["dossier"]["event_ids"])

        status, listed = self._request("GET", "/api/research?limit=5")
        self.assertEqual(200, status)
        self.assertTrue(any(row["dossier_id"] == dossier["dossier_id"] for row in listed["dossiers"]))

    def test_research_routes_reject_invalid_selection_without_internal_error(self) -> None:
        status, payload = self._request("POST", "/api/research", {"event_ids": []})
        self.assertEqual(400, status)
        self.assertEqual("event_ids_count_invalid", payload["error"])

    def test_settings_status_reports_only_github_token_configuration(self) -> None:
        token = "ghp_web_settings_secret"
        status, payload = self._request(
            "POST", "/api/skills/github-token", {"token": token}
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        self.assertNotIn(token, json.dumps(payload))
        status, status_payload = self._request("GET", "/api/status")
        self.assertEqual(200, status)
        self.assertTrue(status_payload["github_token_configured"])
        self.assertNotIn(token, json.dumps(status_payload))
        self._request("POST", "/api/skills/github-token", {"token": ""})

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

    def test_mixed_collect_route_returns_collection_run_contract(self) -> None:
        status, payload = self._request(
            "POST", "/api/collect", {"hours": 24, "lane": "mixed"}
        )
        self.assertEqual(200, status)
        collection = payload["collection"]
        self.assertTrue(collection["run_id"])
        self.assertEqual({"lab_blog", "media_feed"}, set(collection["requested_sources"]))
        self.assertIn("source_attempts", collection)
        self.assertIn("collection_run", payload)
        self.assertEqual(collection["run_id"], payload["collection_run"]["run_id"])

        status, clusters = self._request(
            "GET", "/api/hotspots?hours=24&lane=mixed"
        )
        self.assertEqual(200, status)
        self.assertTrue(clusters["ok"])
        self.assertIn("radar", clusters)
        self.assertTrue(clusters["radar"]["events"])

        status, reopened = self._request(
            "GET", "/api/collection-runs/" + collection["run_id"]
        )
        self.assertEqual(200, status)
        self.assertEqual(collection["run_id"], reopened["run"]["run_id"])
        self.assertTrue(reopened["run"]["source_attempts"])

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

    def test_radar_is_nested_and_get_does_not_write_snapshots(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "radar-1",
                "https://lab.example/radar-1",
                "OpenAI announces Orion model",
                "OpenAI announces Orion model 内容",
                now,
                {"score": "80"},
            ),
        )
        before = {
            source.source_id: self.db.get_source(source.source_id).get("updated_at")
            for source in self.sources
        }
        self.assertEqual([], self.db.list_engagement_snapshots())

        status, payload = self._request("GET", "/api/hotspots?hours=4")

        self.assertEqual(200, status)
        self.assertEqual([], self.db.list_engagement_snapshots())
        self.assertEqual(
            before,
            {
                source.source_id: self.db.get_source(source.source_id).get("updated_at")
                for source in self.sources
            },
        )
        self.assertIn("radar", payload)
        event = payload["radar"]["events"][0]
        self.assertIn("event_id", event)
        self.assertIn("importance_score", event)
        self.assertIn("attention_score", event)
        self.assertIn("priority_score", event)
        self.assertIn("evidence_pack", event)

        status, collected = self._request("POST", "/api/collect", {"hours": 4})

        self.assertEqual(200, status)
        snapshots = self.db.list_engagement_snapshots()
        self.assertTrue(snapshots)
        self.assertEqual(
            {row["event_id"] for row in snapshots},
            {
                row["event_id"]
                for row in collected["radar"]["events"]
                if any(float(value or 0) > 0 for value in (row.get("engagement_by_platform") or {}).values())
            },
        )
        self.model.calls = []
        article_status, _article = self._request(
            "POST",
            "/api/articles",
            {"item_id": collected["candidates"][0]["item_id"], "platform": "wechat"},
        )
        self.assertEqual(200, article_status)
        self.assertTrue(self.model.calls)
        self.assertIn("evidence_pack", self.model.calls[-1][2])
        self.assertIn("priority_score", self.model.calls[-1][2])

    def test_hotspots_translate_query_uses_model(self) -> None:
        self._insert_item(self.sources[0], "en-1", "English model news", "https://lab.example/en-1")
        status, payload = self._request("GET", "/api/hotspots?hours=4")
        self.assertEqual(200, status)
        self.assertEqual("unavailable", payload["candidates"][0]["translation_status"])
        status, payload = self._request("GET", "/api/hotspots?hours=4&translate=1")
        self.assertEqual(200, status)
        self.assertEqual("translated", payload["candidates"][0]["translation_status"])
        self.assertTrue(payload["candidates"][0]["title_zh"])

    def _insert_named(self, source: Source, external_id: str, title: str, url: str) -> str:
        self.db.insert_item(
            source,
            FeedItem(external_id, url, title, f"{title} 内容", None, {}),
        )
        return str(
            self.db.connect()
            .execute("SELECT item_id FROM items WHERE title = ?", (title,))
            .fetchone()["item_id"]
        )

    def test_favorites_api_add_list_remove_round_trip(self) -> None:
        item_id = self._insert_named(
            self.sources[0], "fav-1", "Favorite news", "https://lab.example/fav-1"
        )
        status, added = self._request("POST", "/api/favorites", {"item_id": item_id})
        self.assertEqual(200, status)
        self.assertTrue(added["ok"])
        status, listed = self._request("GET", "/api/favorites")
        self.assertEqual(200, status)
        self.assertEqual([item_id], [item["item_id"] for item in listed["items"]])
        self.assertEqual("Favorite news", listed["items"][0]["title"])
        status, deleted = self._request("DELETE", "/api/favorites", {"item_id": item_id})
        self.assertEqual(200, status)
        self.assertTrue(deleted["ok"])
        status, empty = self._request("GET", "/api/favorites")
        self.assertEqual([], empty["items"])

    def test_articles_item_ids_length_two_hits_fuse_path(self) -> None:
        first_id = self._insert_named(
            self.sources[0], "fuse-a", "Alpha launch", "https://lab.example/fuse-a"
        )
        second_id = self._insert_named(
            self.sources[1], "fuse-b", "Beta debate", "https://media.example/fuse-b"
        )
        status, payload = self._request(
            "POST",
            "/api/articles",
            {"item_ids": [second_id, first_id], "platform": "wechat"},
        )
        self.assertEqual(200, status)
        prompt = self.model.calls[-1][2]
        data, _ = json.JSONDecoder().raw_decode(prompt.rsplit("数据：\n", 1)[1].strip())
        self.assertIn("Alpha launch", data["summary"])
        self.assertIn("Beta debate", data["summary"])
        self.assertIn("Alpha launch 内容", data["summary"])
        self.assertIn("Beta debate 内容", data["summary"])
        self.assertEqual({"lab_blog", "media_feed"}, set(payload["source_ids"]))

    def test_articles_endpoint_is_idempotent_per_item(self) -> None:
        item_id = self._insert_item(self.sources[0], "w1", "Model One news", "https://lab.example/w1")

        status, first = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})
        status2, second = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "wechat"})

        self.assertEqual(200, status)
        self.assertEqual(200, status2)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual("AI专家", first["prompt_style"])
        self.assertEqual("DRAFTED", first["status"])
        self.assertIn("AIGC", first["body"])
        self.assertIn("https://lab.example/w1", first["body"])
        self.assertNotIn("【信息来源", first["body"])
        self.assertEqual(
            "openai-compatible/unconfigured:none:article-v13",
            self.db.get_draft(first["draft_id"])["generator"],
        )

        status, payload = self._request("POST", "/api/articles", {"item_id": item_id, "platform": "douyin"})
        self.assertEqual(400, status)
        status, payload = self._request("POST", "/api/articles", {"item_id": "missing", "platform": "wechat"})
        self.assertEqual(404, status)

    def test_article_prompt_style_selects_file_and_scopes_cache(self) -> None:
        item_id = self._insert_item(self.sources[0], "style", "Style news", "https://lab.example/style")

        status, first = self._request(
            "POST",
            "/api/articles",
            {"item_id": item_id, "platform": "wechat", "prompt_style": "经验分享"},
        )
        call_count = len(self.model.calls)
        status2, second = self._request(
            "POST",
            "/api/articles",
            {"item_id": item_id, "platform": "wechat", "prompt_style": "经验分享"},
        )
        status3, third = self._request(
            "POST",
            "/api/articles",
            {"item_id": item_id, "platform": "wechat", "prompt_style": "AI专家"},
        )
        bad_status, _ = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "wechat", "prompt_style": "8"}
        )

        self.assertEqual((200, 200, 200, 400), (status, status2, status3, bad_status))
        self.assertEqual("经验分享", first["prompt_style"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(first["draft_id"], third["draft_id"])
        self.assertEqual(call_count, len(self.model.calls) - 1)
        prompts = [call[2] for call in self.model.calls]
        self.assertTrue(any("拆解" in prompt for prompt in prompts))
        self.assertTrue(any("一般人容易看漏的角度" in prompt for prompt in prompts))

        k_status, k_payload = self._request(
            "POST",
            "/api/articles",
            {"item_id": item_id, "platform": "wechat", "prompt_style": "klinkenborg_style"},
        )
        self.assertEqual(200, k_status, k_payload)
        self.assertEqual("klinkenborg_style", k_payload["prompt_style"])
        self.assertTrue(any("句子这一最小可控单位" in call[2] for call in self.model.calls))

    def test_xiaohongshu_article_returns_caption_and_9x16_cards(self) -> None:
        item_id = self._insert_item(self.sources[0], "xhs1", "Model One news", "https://lab.example/xhs1")

        status, payload = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "xiaohongshu"}
        )

        self.assertEqual(200, status)
        self.assertIn("导语：今日要点", payload["body"])
        self.assertEqual(payload["body"], payload["caption"])
        self.assertIn("导语：今日要点", payload["manuscript"])
        self.assertEqual("cover", payload["cards"][0]["kind"])
        self.assertEqual(1080, payload["cards"][0]["width"])
        self.assertEqual(1920, payload["cards"][0]["height"])
        self.assertEqual("@小红书号", payload["cards"][0]["handle"])

    def test_original_endpoint_fetches_article_and_translates_after_it(self) -> None:
        item_id = self._insert_item(self.sources[0], "original", "Original news", "https://lab.example/original")
        paragraph = "This is the complete English article body with verified context. " * 8
        self.app.workbench.page_fetcher = lambda _: (
            f"<html><nav>menu noise</nav><article><h1>Original news</h1>"
            f"<p>{paragraph}</p><script>bad()</script></article></html>"
        ).encode()

        status, payload = self._request("POST", "/api/original", {"item_id": item_id})
        translated_status, translated = self._request(
            "POST", "/api/original/translate", {"item_id": item_id}
        )
        missing_status, _ = self._request("POST", "/api/original", {"item_id": "missing"})

        self.assertEqual(200, status)
        self.assertEqual(200, translated_status)
        self.assertEqual(404, missing_status)
        self.assertEqual("Original news", payload["title"])
        self.assertIn("complete English article body", payload["content"])
        self.assertNotIn("menu noise", payload["content"])
        self.assertEqual("page", payload["content_source"])
        self.assertEqual("full", payload["reader_status"])
        self.assertEqual("full", payload["status"])
        self.assertGreaterEqual(float(payload["quality_score"]), 0.55)
        self.assertIsNone(payload["failure_reason"])
        self.assertEqual("https://lab.example/original", payload["canonical_url"])
        self.assertEqual("http", payload["fetch_method"])
        self.assertEqual("html", payload["extractor"])
        self.assertTrue(payload["translatable"])
        self.assertIn("中文摘要", translated["translation"])
        self.assertEqual("https://lab.example/original", payload["sources"][0]["url"])

        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "unsafe-original",
                "javascript:alert(1)",
                '<img src=x onerror="alert(1)">',
                "<script>alert(1)</script> visible text",
                None,
                {},
            ),
        )
        unsafe_id = str(
            self.db.connect()
            .execute("SELECT item_id FROM items WHERE external_id = ?", ("unsafe-original",))
            .fetchone()["item_id"]
        )
        unsafe_status, unsafe = self._request("POST", "/api/original", {"item_id": unsafe_id})
        self.assertEqual(200, unsafe_status)
        self.assertEqual('<img src=x onerror="alert(1)">', unsafe["title"])
        self.assertEqual("<script>alert(1)</script> visible text", unsafe["content"])
        self.assertEqual([], unsafe["sources"])
        for index, url in enumerate(("data:text/plain,unsafe", "file:///tmp/unsafe"), start=1):
            external_id = f"unsafe-scheme-{index}"
            self.db.insert_item(
                self.sources[0],
                FeedItem(external_id, url, f"unsafe {index}", f"stored text {index}", None, {}),
            )
            item_id = str(
                self.db.connect()
                .execute("SELECT item_id FROM items WHERE external_id = ?", (external_id,))
                .fetchone()["item_id"]
            )
            scheme_status, scheme_payload = self._request(
                "POST", "/api/original", {"item_id": item_id}
            )
            self.assertEqual(200, scheme_status)
            self.assertEqual([], scheme_payload["sources"])

    def test_original_endpoint_reuses_full_feed_content_without_fetching(self) -> None:
        body = "The feed already contains the complete article and its verified context. " * 10
        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "full-feed-original",
                "https://lab.example/full-feed-original",
                "Full feed article",
                body,
                None,
                {},
            ),
        )
        item_id = str(
            self.db.connect()
            .execute(
                "SELECT item_id FROM items WHERE external_id = ?",
                ("full-feed-original",),
            )
            .fetchone()["item_id"]
        )
        calls: list[str] = []

        def should_not_fetch(url: str) -> bytes:
            calls.append(url)
            raise AssertionError("full RSS content should not trigger a page fetch")

        self.app.workbench.page_fetcher = should_not_fetch

        result = self.app.original(item_id)

        self.assertEqual("feed", result["content_source"])
        self.assertEqual(body, result["content"])
        self.assertEqual("full", result["reader_status"])
        self.assertEqual("rss", result["fetch_method"])
        self.assertEqual("rss", result["extractor"])
        self.assertEqual("https://lab.example/full-feed-original", result["canonical_url"])
        self.assertEqual([], calls)

    def test_original_endpoint_uses_injected_dynamic_reader(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "dynamic-original", "Dynamic report", "https://lab.example/dynamic"
        )
        static_shell = b'<html><body><div id="app"></div><script>boot()</script></body></html>'
        rendered = (
            "<article><h1>Dynamic report</h1><p>"
            + "The rendered page contains the complete verified report and context. " * 10
            + "</p></article>"
        )
        calls: list[str] = []

        def render_fetcher(url: str) -> str:
            calls.append(url)
            return rendered

        app = WebApp(
            self.db,
            self.sources,
            self.root / "dynamic-reader",
            page_fetcher=lambda _url: static_shell,
            render_fetcher=render_fetcher,
            credential_store=self.credentials,
        )

        result = app.original(item_id)

        self.assertEqual("page", result["content_source"])
        self.assertEqual("full", result["reader_status"])
        self.assertEqual("dynamic", result["fetch_method"])
        self.assertIn("complete verified report", result["content"])
        self.assertEqual(["https://lab.example/dynamic"], calls)

    def test_original_endpoint_uses_lazy_default_dynamic_reader(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "default-dynamic-original", "Dynamic report", "https://lab.example/default-dynamic"
        )
        static_shell = b'<html><body><div id="app"></div><script>boot()</script></body></html>'
        rendered = (
            "<article><h1>Dynamic report</h1><p>"
            + "The default rendered page contains the complete verified report. " * 10
            + "</p></article>"
        )
        calls: list[str] = []

        def default_renderer(url: str) -> str:
            calls.append(url)
            return rendered

        with patch("autoposting.web.playwright_render_fetcher", default_renderer):
            app = WebApp(
                self.db,
                self.sources,
                self.root / "default-dynamic-reader",
                page_fetcher=lambda _url: static_shell,
                credential_store=self.credentials,
            )
            result = app.original(item_id)

        self.assertEqual("full", result["reader_status"])
        self.assertEqual("dynamic", result["fetch_method"])
        self.assertIn("complete verified report", result["content"])
        self.assertEqual(["https://lab.example/default-dynamic"], calls)

    def test_original_translation_requires_translation_model(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "translation-required", "English report", "https://lab.example/en"
        )
        app = WebApp(
            self.db,
            self.sources,
            self.root / "unconfigured-translation",
            page_fetcher=lambda _: b"<article><p>English body text.</p></article>",
        )
        with self.assertRaisesRegex(ValueError, "translation_model_not_configured"):
            app.translate_original(item_id)

    def test_original_fetch_failure_is_retried(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "retry-original", "Retry English report", "https://lab.example/retry"
        )
        calls = 0

        def flaky_fetcher(_: str) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary timeout")
            return ("<article><p>" + "Recovered full article text. " * 15 + "</p></article>").encode()

        self.app.workbench.page_fetcher = flaky_fetcher

        first = self.app.original(item_id)
        second = self.app.original(item_id)

        self.assertEqual("feed", first["content_source"])
        self.assertEqual("page", second["content_source"])
        self.assertEqual(2, calls)

    def test_english_body_with_cjk_brand_in_title_is_translatable(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "mixed-title", "千问 Qwen model report", "https://lab.example/mixed"
        )
        self.app.workbench.page_fetcher = lambda _: (
            "<article><p>" + "This English report contains detailed benchmark context. " * 12
            + "</p></article>"
        ).encode()

        original = self.app.original(item_id)
        translated = self.app.translate_original(item_id)

        self.assertTrue(original["translatable"])
        self.assertIn("中文摘要", translated["translation"])

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
        self.assertNotIn("author", payload)
        self.assertNotIn("top-secret-value", json.dumps(payload))
        self.assertNotIn("wx123", json.dumps(payload))

        status, status_payload = self._request("GET", "/api/status")
        self.assertTrue(status_payload["platforms"]["wechat"]["configured"])
        self.assertNotIn("top-secret-value", json.dumps(status_payload))
        database_bytes = self.root.joinpath("autoposting.db").read_bytes()
        self.assertNotIn(b"top-secret-value", database_bytes)
        self.assertIn("account:wechat", self.credentials.secrets)

    def test_wechat_config_restores_after_restart_and_can_be_deleted(self) -> None:
        self._request(
            "POST",
            "/api/config",
            {
                "platform": "wechat",
                "app_id": "wx123",
                "app_secret": "top-secret-value",
                "thumb_media_id": "thumb-1",
                "author": "测试作者",
            },
        )
        restarted = WebApp(
            self.db,
            self.sources,
            self.root / "restarted-packages",
            credential_store=self.credentials,
            page_fetcher=lambda _: b"",
        )
        self.assertIsNotNone(restarted.wechat_config)
        self.assertEqual("wx123", restarted.wechat_config["app_id"])
        self.assertEqual("top-secret-value", restarted.wechat_config["app_secret"])
        self.assertTrue(restarted.status()["platforms"]["wechat"]["configured"])
        serialized = json.dumps(restarted.status())
        self.assertNotIn("top-secret-value", serialized)
        self.assertNotIn("wx123", serialized)

        status, payload = self._request(
            "POST", "/api/config/delete", {"platform": "wechat"}
        )
        self.assertEqual(200, status)
        self.assertFalse(payload["configured"])
        self.assertNotIn("account:wechat", self.credentials.secrets)
        self.assertIsNone(self.app.wechat_config)

    def test_config_wechat_allows_empty_thumb_media_id(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/config",
            {"platform": "wechat", "app_id": "wx123", "app_secret": "top-secret-value", "author": ""},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        self.assertNotIn("top-secret-value", json.dumps(payload))

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
            translation_status, translation_saved = self._request(
                "POST",
                "/api/translation/model",
                {"model": "model-a", "base_url": first_url, "api_key": "first-key"},
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
        self.assertEqual(200, translation_status)
        self.assertEqual("model-a", translation_saved["translation_llm"]["model"])
        self.assertEqual(first_url, restored["translation_llm"]["base_url"])
        self.assertEqual(2, len(restored["model_endpoints"]))
        self.assertTrue(all(endpoint["credential_available"] for endpoint in restored["model_endpoints"]))
        self.assertEqual(first_url, switched["llm"]["base_url"])
        self.assertEqual([second["id"]], [endpoint["id"] for endpoint in deleted["model_endpoints"]])
        self.assertFalse(deleted["llm"]["configured"])
        self.assertFalse(deleted["chat_llm"]["configured"])
        self.assertFalse(deleted["translation_llm"]["configured"])
        serialized = json.dumps(restored)
        self.assertNotIn("first-key", serialized)
        self.assertNotIn("second-key", serialized)
        database_bytes = self.root.joinpath("autoposting.db").read_bytes()
        self.assertNotIn(b"first-key", database_bytes)
        self.assertNotIn(b"second-key", database_bytes)

    def test_models_probe_keeps_saved_model_list(self) -> None:
        stub = ThreadingHTTPServer(("127.0.0.1", 0), ModelStubHandler)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        remote_url = f"http://127.0.0.1:{stub.server_address[1]}/v1"
        try:
            self._request(
                "POST",
                "/api/models",
                {"base_url": remote_url, "api_key": "keep-key", "models": ["kept-model"]},
            )
            status, probed = self._request(
                "POST",
                "/api/models",
                {
                    "base_url": remote_url,
                    "api_key": "keep-key",
                    "persist": False,
                },
            )
            stored = self.db.get_model_endpoint(probed["endpoint_id"])
        finally:
            stub.shutdown()
            stub.server_close()
        self.assertEqual(200, status)
        self.assertEqual(["model-a", "model-b"], probed["models"])
        self.assertEqual(["kept-model"], json.loads(str(stored["models_json"])))

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
        self.assertEqual("DRAFT_SAVED", payload["status"])
        self.assertEqual("stub-media-1", payload["reference"])
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
        self.assertEqual("DRAFT_SAVED", attempt["status"])
        self.assertEqual("stub-media-1", attempt["response_reference"])
        for _, _, body in WechatStubHandler.requests:
            if not body:
                continue
            text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else body
            if "top-secret-value" in text:
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

    def test_publishing_center_lists_drafts_and_opens_them(self) -> None:
        item_id = self._insert_item(
            self.sources[0], "w1", "Model One news", "https://lab.example/w1"
        )
        _, wechat = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "wechat"}
        )
        _, xiaohongshu = self._request(
            "POST", "/api/articles", {"item_id": item_id, "platform": "xiaohongshu"}
        )
        self._request(
            "POST", "/api/approve", {"draft_id": wechat["draft_id"], "reviewer": "human"}
        )
        self._request(
            "POST",
            "/api/publish",
            {"draft_id": xiaohongshu["draft_id"], "platform": "xiaohongshu", "live": True},
        )

        status, payload = self._request("GET", "/api/drafts?limit=20")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        drafts = {row["draft_id"]: row for row in payload["drafts"]}
        self.assertEqual({wechat["draft_id"], xiaohongshu["draft_id"]}, set(drafts))
        self.assertEqual("APPROVED", drafts[wechat["draft_id"]]["status"])
        self.assertEqual("wechat", drafts[wechat["draft_id"]]["platform"])
        self.assertEqual("human", drafts[wechat["draft_id"]]["approved_by"])
        self.assertEqual("DRAFTED", drafts[xiaohongshu["draft_id"]]["status"])
        self.assertIn("attempt_status", drafts[xiaohongshu["draft_id"]])
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        for secret in ("app_secret", "access_token", "bearer", "api_key"):
            self.assertNotIn(secret, serialized)

        open_status, opened = self._request("GET", "/api/drafts/" + wechat["draft_id"])
        self.assertEqual(200, open_status)
        self.assertTrue(opened["ok"])
        self.assertEqual(wechat["draft_id"], opened["draft_id"])
        self.assertEqual(wechat["title"], opened["title"])
        self.assertEqual(wechat["body"], opened["body"])
        self.assertEqual(wechat["source_ids"], opened["source_ids"])
        self.assertEqual("wechat", opened["platform"])

        missing_status, _ = self._request("GET", "/api/drafts/does-not-exist")
        self.assertEqual(404, missing_status)

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

    def test_unknown_http_errors_are_sanitized_and_logged(self) -> None:
        cases = (
            ("GET", "/api/status", None, "status"),
            ("POST", "/api/collect", {}, "collect"),
            ("DELETE", "/api/favorites", {"item_id": "item-1"}, "delete_favorite"),
        )
        for method, path, payload, method_name in cases:
            secret = "token=super-secret https://private.example/sql?key=hidden"
            with self.subTest(method=method), patch.object(
                self.app,
                method_name,
                side_effect=RuntimeError(secret),
            ), self.assertLogs("autoposting.web", level="ERROR") as captured:
                status, response = self._request(method, path, payload)

            self.assertEqual(500, status)
            self.assertFalse(response["ok"])
            self.assertEqual("internal_error", response["error"])
            self.assertEqual("internal_error", response["error_code"])
            self.assertEqual("InternalServerError", response["error_type"])
            request_id = response.get("request_id")
            self.assertIsInstance(request_id, str)
            self.assertTrue(request_id)

            log_output = "\n".join(captured.output)
            self.assertIn(method, log_output)
            self.assertIn(path, log_output)
            self.assertIn(request_id, log_output)
            self.assertIn("RuntimeError", log_output)
            self.assertIn("Traceback", log_output)
            self.assertNotIn(secret, log_output)
            self.assertNotIn("super-secret", log_output)
            self.assertNotIn("private.example", log_output)

    def test_web_json_serializes_known_non_basic_values(self) -> None:
        when = datetime(2026, 9, 19, 12, 34, 56, tzinfo=timezone.utc)
        with patch.object(
            self.app,
            "status",
            return_value={
                "ok": True,
                "path": self.root / "snapshot.json",
                "day": when.date(),
                "when": when,
            },
        ):
            status, payload = self._request("GET", "/api/status")

        self.assertEqual(200, status)
        self.assertEqual(str(self.root / "snapshot.json"), payload["path"])
        self.assertEqual(when.date().isoformat(), payload["day"])
        self.assertEqual(when.isoformat(), payload["when"])

    def test_unknown_web_json_value_becomes_sanitized_500(self) -> None:
        with patch.object(self.app, "status", return_value={"ok": True, "value": object()}):
            with self.assertLogs("autoposting.web", level="ERROR") as captured:
                status, payload = self._request("GET", "/api/status")

        self.assertEqual(500, status)
        self.assertEqual("internal_error", payload["error"])
        self.assertTrue(payload["request_id"])
        self.assertIn("TypeError", "\n".join(captured.output))

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


class SkillPosterSelectionTests(unittest.TestCase):
    """A poster tile checkbox is the same selection as the skill checkbox.

    The render request is built from the collected skills, so toggling only the
    image flag left 生成图片 reporting 「超过条目上限」 with no way to lower the
    count from the gallery.
    """

    def test_the_tile_checkbox_moves_the_skill_selection(self) -> None:
        self.assertIn("function skillsForImage(image)", INDEX_HTML)
        self.assertIn('if (!image || image.kind !== "poster") { return []; }', INDEX_HTML)

    def test_both_gallery_controls_use_the_mapping(self) -> None:
        for name in ("function toggleSkillImage(imageId)", "function selectAllSkillImages()"):
            start = INDEX_HTML.index(name)
            body = INDEX_HTML[start : INDEX_HTML.index("\n}", start)]
            self.assertIn("skillsForImage(", body, name)

    def test_the_render_request_still_comes_from_the_skills(self) -> None:
        # One source of truth: whatever the gallery does, the request reads the
        # collected skills.
        self.assertIn("selected_ids: selectedSkills(pack).map(skillIdentity)", INDEX_HTML)


class HomeActivityCardTests(unittest.TestCase):
    """The home card reports the latest persisted run on a cold page load."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "home.db")
        self.db.initialize()
        self.app = WebApp(self.db, Path("config/sources.example.json"), Path(self.tmp.name) / "packages")

    def test_the_home_card_is_filled_without_opening_the_runs_page(self) -> None:
        html = INDEX_HTML
        # The boot path loads the runs feed ...
        self.assertIn("loadCollectionRuns().catch(function () {});", html)
        # ... and the renderer writes the card for the latest persisted run,
        # instead of leaving the "loading" placeholder on screen forever.
        self.assertIn("最近一次采集：", html)
        self.assertIn('homeSummary.textContent = "最近一次采集："', html)
        self.assertIn('"还没有采集运行记录，去「资讯探索」刷新一次来源。"', html)

    def test_the_card_still_names_the_placeholder_before_any_run(self) -> None:
        html = INDEX_HTML
        self.assertIn("加载状态后，这里会显示最近的资讯运行、研究和草稿。", html)

    def test_an_empty_run_list_does_not_leave_the_placeholder(self) -> None:
        html = INDEX_HTML
        # An empty history is reported as an instruction, not as a loading state.
        self.assertIn("还没有采集运行记录", html)


class IndexPageContractTests(unittest.TestCase):
    def _index(self) -> str:
        request = urllib.request.Request(self.base_url + "/", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            return response.read().decode("utf-8")

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
        self.assertIn('id="publish-btn" disabled class="primary">到草稿</button>', html)
        self.assertIn('id="schedule-btn" disabled>定时到草稿</button>', html)
        self.assertIn("function publishButtonLabel()", html)
        self.assertIn('return state.platform === "wechat" ? "到草稿" : "发布"', html)
        self.assertIn('id="schedule-at"', html)
        self.assertIn("<dialog", html)
        self.assertIn('id="config-dialog"', html)
        self.assertIn('name="app_secret"', html)
        self.assertIn('type="password"', html)
        self.assertIn('id="config-cancel"', html)
        self.assertIn("showModal", html)
        self.assertIn('config-dialog").close()', html)
        self.assertIn("form.app_secret.value = \"\"", html)
        self.assertIn("aria-label", html)

    def test_platform_segmented_control(self) -> None:
        html = self._index()
        self.assertIn('data-platform="wechat"', html)
        self.assertIn('data-platform="xiaohongshu"', html)

    def test_top_nav_holds_primary_pages_and_platform_switcher(self) -> None:
        """Iteration 2: the four legacy lane tabs are gone; the primary nav and
        the platform switcher are the only top-bar controls, while the retained
        weekly/skill capabilities live inside the creation page studios."""
        html = self._index()
        self.assertIn('id="app-nav"', html)
        self.assertNotIn('id="legacy-lane-tabs"', html)
        self.assertNotIn('data-lane="hotspots"', html)
        self.assertNotIn('data-lane="viewpoints"', html)
        self.assertNotIn('data-lane="github"', html)
        self.assertNotIn('data-lane="skills"', html)
        nav = html[html.index('id="app-nav"') : html.index('id="wait-banner"')]
        for page in ("home", "news", "resources", "creation", "publishing", "sources", "runs", "settings"):
            self.assertIn(f'data-app-page-target="{page}"', nav)
        self.assertIn('data-platform="wechat"', nav)
        self.assertIn('data-platform="xiaohongshu"', nav)
        self.assertLess(html.index('id="app-nav"'), html.index('id="wait-banner"'))
        self.assertGreater(html.index('id="wait-banner"'), html.index("</header>"))
        self.assertLess(html.index('id="wait-banner"'), html.index("<main>"))
        # Retained capabilities: the unified search API and the poster galleries.
        self.assertIn("/api/skills/search", html)
        # Skill search/paste input lives on the resource page; the studio only
        # renders the collected package.
        self.assertIn('id="resource-query"', html)
        self.assertIn('id="resource-pasted-text"', html)
        self.assertIn('id="resource-studio-btn"', html)
        self.assertNotIn('id="skill-query"', html)
        self.assertIn('id="skills-gallery"', html)
        self.assertIn('id="github-studio"', html)
        self.assertIn('id="skills-studio"', html)

    def test_iteration_one_information_architecture_and_compatibility(self) -> None:
        html = self._index()
        pages = ("home", "news", "resources", "creation", "publishing", "sources", "runs", "settings")
        for page in pages:
            self.assertIn(f'data-app-page-target="{page}"', html)
            self.assertIn(f'id="page-{page}"', html)
        primary = html[html.index('id="primary-nav"') : html.index('id="platform-switcher"')]
        self.assertNotIn("热点候选", primary)
        self.assertNotIn("观点候选", primary)
        # Iteration 2 removed the retired lane tab bar entirely instead of
        # leaving it hidden in the DOM.
        self.assertNotIn('legacy-lane-tabs', html)
        self.assertIn('id="source-health-wrap"', html)
        # 3.2.2: the strip is a visible disclosure on the news page instead of a
        # hidden legacy hook, so it must not be force-hidden anywhere, and the
        # token layer must style it as a real control.
        self.assertNotIn("source-health-wrap{display:none", html)
        self.assertIn("source-health-toggle", html)
        self.assertIn("#source-health-toggle[aria-expanded=\"true\"]", html)
        self.assertIn('id="source-registry-list"', html)
        self.assertIn('id="collection-run-sources"', html)
        self.assertIn('id="resource-results"', html)
        self.assertIn('id="page-research"', html)
        self.assertIn('id="research-btn"', html)
        self.assertIn('id="research-run-btn"', html)
        self.assertIn('id="settings-github-token"', html)
        # Iteration 2: the resource page slot is live ("unified") instead of the
        # Iteration-1 "pending" wiring stub; the unified ResourceSet renderer and
        # search entry point are part of the new IA contract. The legacy skill /
        # github renderers must keep existing for legacy slots only.
        self.assertIn('data-resource-discovery-slot="unified"', html)
        self.assertIn('function renderResourceSet(resourceSet)', html)
        self.assertIn('function discoverResources(preset)', html)
        self.assertIn('id="settings-open-model"', html)
        self.assertIn('function setAppPage(page)', html)
        self.assertIn('function renderResourceResults(skills)', html)
        self.assertIn('function renderResourceGithubResults(repos)', html)
        self.assertIn('function loadSourceRegistry()', html)
        self.assertIn('function loadCollectionRuns()', html)
        self.assertIn('/api/sources', html)
        self.assertIn('/api/sources/enabled', html)
        self.assertIn('/api/sources/fetch-interval', html)
        self.assertIn('/api/collection-runs?limit=20', html)
        self.assertIn('/api/research', html)
        # The editorial/community filter is the `scope` contract now, and the
        # retired `lane` name appears nowhere in the client.
        self.assertIn('state.newsScope', html)
        self.assertIn('id="news-scope-select"', html)
        client = html.split("<script>", 1)[1]
        for retired in ("state.lane", "setLane(", "laneStatus", "data-lane", "body.lane-", "lane !== "):
            self.assertNotIn(retired, client)
        # 3.2.2 removed the definition-only JS functions; no consumer ever
        # called them, so their names must not survive in the client.
        for dead in (
            "loadResearchDossier",
            "endpointLabel",
            "renderSettingsModels",
            "stepImagePicker",
            "stepGithubGallery",
            "stepSkillsGallery",
            "drawGithubCover",
            "drawGithubPoster",
            "github-canvas",
        ):
            self.assertNotIn(dead, client)
        for legacy_route in ('/api/status', '/api/hotspots?hours=', '/api/collect'):
            self.assertIn(legacy_route, html)

    def test_publishing_center_has_draft_list_and_actions(self) -> None:
        html = self._index()
        self.assertIn('id="publishing-draft-list"', html)
        self.assertIn('id="publishing-refresh-btn"', html)
        self.assertIn('/api/drafts?limit=20', html)
        self.assertIn('function loadPublishingDrafts()', html)
        self.assertIn('function renderPublishingDrafts(drafts)', html)
        self.assertIn('function openDraftForPublishing(draftId, action)', html)
        self.assertIn('PUBLISHING_STATUS_LABELS', html)
        self.assertIn('"待审批"', html)
        self.assertIn('"已审批"', html)
        self.assertIn('id="schedule-at"', html)

    def test_skill_surfaces_follow_the_iteration_two_layout(self) -> None:
        """Lane-era skill search blocks stay retired, while both poster studios
        are reachable inside the creation page."""
        html = self._index()
        # The source block and the degraded note are visible studio parts now,
        # not hidden write targets: a user must be able to see where the
        # collected skills came from and which source did not answer.
        self.assertIn('id="skills-sources"', html)
        self.assertIn('id="skills-source-chips"', html)
        self.assertIn('id="skills-degraded-note"', html)
        # The 1.4.x stage chips and gap note stay retired, and so does the
        # hidden collected-skill list the studio used to write into.
        for retired in ("skill-source-bar", "skill-source-chips", "skill-degraded-note",
                        "skill-stage-chips", "skill-gap-note", "skill-list"):
            self.assertNotIn(f'id="{retired}"', html)
        style = re.sub(r"/\*.*?\*/", "", html.split("</style>", 1)[0], flags=re.S)
        # The retired lane-era hidden blocks stay retired in CSS too.
        self.assertNotIn("#skill-search-row", style)
        self.assertNotIn("#skill-degraded-note", style)
        self.assertIn("#skills-degraded-note[hidden]{display:none;}", style)
        # No lane-scoped rule may survive anywhere in the sheet.
        self.assertNotIn("lane", style)
        # Iteration 2: the weekly 3:4 studio and the skill poster studio are
        # always available in the creation page instead of lane-gated.
        self.assertRegex(style, r"#github-studio,#skills-studio\{display:flex")
        self.assertIn("#github-render-controls{display:flex;", style)
        self.assertRegex(style, r"\.studio-heading\{")
        self.assertIn('id="github-fetch-btn"', html)
        self.assertIn('id="skills-fetch-btn"', html)

    def test_github_cover_css_is_three_by_four(self) -> None:
        html = self._index()
        self.assertRegex(html, r"\.gh-cover\{[^}]*aspect-ratio:3/4")
        self.assertRegex(html, r"\.gh-poster\{[^}]*aspect-ratio:3/4")
        self.assertRegex(html, r"\.xhs-paper\{[^}]*aspect-ratio:9/16")
        self.assertRegex(html, r"\.gh-tile\{[^}]*aspect-ratio:4/3")
        self.assertIn(".gh-tile.gh-cover{aspect-ratio:3/4;}", html)
        self.assertIn(".gh-tile.gh-poster{aspect-ratio:3/4;}", html)
        self.assertIn("object-fit:contain", html)
        source = (
            Path(__file__).resolve().parents[1] / "src" / "autoposting" / "llm.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("IMAGE_ASPECT_RATIO", source)
        self.assertNotIn("generate_image", source)
        overview = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "image_prompts"
            / "page-schemas"
            / "ranking-overview.md"
        ).read_text(encoding="utf-8")
        self.assertIn("画布 3:4", overview)
        self.assertNotIn("画布 9:16", overview)
        # D5: the model-image prompt files are gone; only the HTML visual system remains.
        prompts_dir = Path(__file__).resolve().parents[1] / "docs" / "image_prompts"
        for gone in ("STYLE.md", "cover.md", "poster.md", "skill_cover.md"):
            self.assertFalse((prompts_dir / gone).exists(), f"{gone} must be deleted")

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
        select_js = html.split("function selectHotspot(item, button)")[1].split(
            "function clearHotspotSelection("
        )[0]
        self.assertNotIn("resetArticle()", select_js)
        self.assertIn(
            "state.platform = platform;\n"
            '  document.body.classList.toggle("platform-xhs", platform === "xiaohongshu");\n'
            "  resetArticle();",
            html,
        )

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
        self.assertIn('button.dataset.originalLabel = result.manual_required ? "已生成发布包" : "已到草稿";', html)
        self.assertIn("已写入公众号草稿箱", html)
        self.assertIn("if (completed) { button.disabled = true; }", html)

    def test_first_wechat_configuration_resumes_pending_publish(self) -> None:
        html = self._index()
        self.assertIn("pendingPublish: false", html)
        self.assertIn("openSettings(true)", html)
        self.assertIn("var resumePublish = state.pendingPublish", html)
        self.assertIn("state.pendingPublish = false", html)
        self.assertIn("if (resumePublish) {", html)
        self.assertIn("publish();", html)

    def test_hotspot_and_article_render_fields(self) -> None:
        html = self._index()
        for field in ("item_id", "title", "summary", "observed_at", "source_count", "title_zh", "summary_zh"):
            self.assertIn(field, html)
        for element in ("article-title", "article-body", "article-sources", "draft_id"):
            self.assertIn(element, html)

    def test_news_discovery_wording_and_weekly_studio(self) -> None:
        html = self._index()
        # Iteration 2 retired the 热点候选 / 观点候选 wording and lanes entirely.
        self.assertNotIn("热点候选", html)
        self.assertNotIn("观点候选", html)
        self.assertNotIn('data-lane="viewpoints"', html)
        self.assertIn("资讯探索", html)
        self.assertIn("按综合分、时效与独立来源聚合资讯事件", html)
        self.assertIn("研究已选事件", html)
        self.assertNotIn("function setLane(", html)
        self.assertIn("function setNewsScope(", html)
        self.assertIn("function applyNewsScope(", html)
        # The weekly 3:4 capability stays, now as a creation-page studio with a
        # fixed heading instead of a lane-conditional one.
        self.assertIn("GitHub 周榜出图工作台", html)
        self.assertNotIn("GitHub AI 项目周榜", html)
        self.assertNotIn("GitHub AI项目周榜", html)
        self.assertNotIn("body.lane-", html)
        self.assertIn('id="github-studio"', html)
        self.assertIn('id="github-fetch-btn"', html)
        self.assertIn("榜单模型", html)
        self.assertIn("用于榜单", html)
        # The cover title now lives only in the render plan (image_gen.py),
        # asserted by test_web.py:206 and test_visual.py:794; the retired
        # canvas-draw path that hard-coded it in the client was removed in 3.2.2.
        self.assertNotIn("本周爆火ai工具", html)
        self.assertIn("本周值得关注的github ai项目前10", html)
        self.assertIn("exportGithubImages", html)
        self.assertIn("github-gallery", html)
        self.assertIn("github-select-all", html)
        self.assertIn("github-gallery-counter", html)
        self.assertIn("aspect-ratio:3/4", html)
        self.assertIn("/api/github-weekly", html)
        # D8: the image-model configuration surface is torn out, not hidden.
        self.assertNotIn("出图模型", html)
        self.assertNotIn("用于出图", html)
        self.assertNotIn("endpoint-use-image", html)
        self.assertNotIn("image_provider", html)
        self.assertNotIn("image_llm", html)
        self.assertNotIn("请选 qwen-image / wan", html)
        # D8: no image-model aspect-ratio copy survives; the 9:16 that remains is
        # the Xiaohongshu paper card, not an image model.
        self.assertNotIn("9:16", html)
        self.assertIn("aspect-ratio:9/16", html)
        self.assertIn("本周新建", html)
        self.assertIn("/api/leaderboard/model", html)
        self.assertIn("抓取周榜", html)
        self.assertIn("生成图片", html)
        self.assertIn("浅色", html)
        self.assertIn("暗色", html)
        self.assertIn("gh-color-main", html)
        self.assertIn("gh-color-secondary", html)
        self.assertIn("gh-color-accent", html)
        self.assertIn("/api/github-weekly/render", html)
        self.assertIn("renderGithubWeeklyImages", html)
        self.assertIn("/api/github-weekly/image", html)
        self.assertIn("waitApi(\"github_render\"", html)
        self.assertIn('id="wait-banner"', html)
        self.assertNotIn("position:fixed; top:4.6rem; right:1.4rem", html)
        self.assertIn("#wait-jobs{display:flex; flex-direction:column; gap:.55rem; pointer-events:none;}", html)
        self.assertIn(".wait-stop{\n  pointer-events:auto;", html)
        self.assertLess(html.index('id="wait-banner"'), html.index("<main>"))
        self.assertGreater(html.index('id="wait-banner"'), html.index("</header>"))
        self.assertIn("function waitStart(", html)
        self.assertIn("function waitProgress(", html)
        self.assertIn("function waitDone(", html)
        self.assertIn("function waitStop(", html)
        self.assertIn("waitJobs", html)
        self.assertIn("AbortController", html)
        self.assertIn("wait-stop", html)
        self.assertIn("已停止", html)
        self.assertIn("WAIT_HINTS", html)
        self.assertIn('waitStart("github_render"', html)
        self.assertIn('waitStart("collect"', html)
        self.assertIn('waitStart("generate"', html)
        self.assertIn('waitStart("github_fetch"', html)
        self.assertIn('waitStart("translate"', html)
        self.assertNotIn('waitStart("chat"', html)
        self.assertIn('waitStart("original"', html)
        self.assertIn('waitStart("publish"', html)
        self.assertIn("is-running", html)
        self.assertIn("is-queued", html)
        self.assertIn("已全部生成", html)
        self.assertIn("已用", html)
        self.assertIn("每张大约", html)
        self.assertIn("通常", html)
        self.assertNotIn("return generateMissingGithubImages();", html)
        self.assertIn("function proxiedImage(url)", html)
        self.assertIn('url.indexOf("/api/generated/") === 0', html)

    def test_bilingual_card_contract(self) -> None:
        html = self._index()
        self.assertIn("原文", html)
        self.assertIn("中文（AI翻译，非官方表述）", html)
        self.assertIn("未翻译（未配置翻译模型）", html)

    def test_index_html_ignores_query_string(self) -> None:
        request = urllib.request.Request(self.base_url + "/?reload=1", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("text/html", response.headers["Content-Type"])
            html = response.read().decode("utf-8")
        self.assertIn("AutoPosting", html)
        self.assertIn("GitHub 周榜", html)
        self.assertIn("GitHub 周榜出图工作台", html)
        # The retired lane-conditional heading must not come back.
        self.assertNotIn("GitHub AI 项目周榜", html)

    def test_version_badge(self) -> None:
        html = self._index()
        self.assertIn("3.2.2", html)
        self.assertIn("没有已选配图时才生成标题大字报", html)
        self.assertIn('card.kind === "cover" && !card.image_url', html)
        self.assertNotIn('"700 72px "', html)
        self.assertIn('700 72px "Songti SC"', html)

    def test_generated_weekly_image_is_not_browser_cached(self) -> None:
        folder = self.root / "packages" / "github-weekly"
        folder.mkdir(parents=True)
        (folder / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        request = urllib.request.Request(self.base_url + "/api/generated/github-weekly/cover.png")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(200, response.status)
            cache = response.headers.get("Cache-Control") or ""
            self.assertIn("no-store", cache)
            self.assertNotIn("max-age=300", cache)

    def test_github_gallery_cache_busts_image_urls(self) -> None:
        html = self._index()
        self.assertIn("function githubFreshUrl", html)
        self.assertIn("githubFreshUrl(image.url)", html)
        self.assertRegex(html, r"\.gh-tile\{[^}]*aspect-ratio:4/3")
        self.assertIn('image.kind === "cover" ? " gh-cover"', html)
        self.assertIn('image.kind === "poster" ? " gh-poster"', html)
        self.assertIn('openGithubLightbox(index)', html)
        self.assertIn('openSkillsLightbox(index)', html)

    def test_patch_042_style_selector_and_original_reader_contract(self) -> None:
        html = self._index()
        self.assertIn('id="prompt-style-select"', html)
        self.assertIn('<option value="AI专家" selected>AI专家</option>', html)
        self.assertIn('<option value="经验分享">经验分享</option>', html)
        self.assertIn('<option value="klinkenborg_style">klinkenborg_style</option>', html)
        self.assertNotIn("1 消息", html)
        self.assertNotIn('option value="8"', html)
        self.assertNotIn("新闻特写", html)
        self.assertIn('id="xhs-cards"', html)
        self.assertIn("xhs-cards", html)
        self.assertIn("grid-template-columns:repeat(3", html)
        self.assertIn("aspect-ratio:9/16", html)
        self.assertIn("canvas.height = 1920", html)
        self.assertIn('prompt_style: $("prompt-style-select").value', html)
        self.assertIn('id="open-original-btn" disabled', html)
        self.assertLess(html.index("时间优先"), html.index("打开原文"))
        self.assertIn('id="original-dialog"', html)
        self.assertIn('waitApi("original", "/api/original"', html)
        self.assertIn('$("original-content").textContent = data.content', html)
        self.assertIn('link.rel = "noopener noreferrer"', html)
        self.assertIn("function clearHotspotSelection(", html)
        self.assertIn('button.classList.toggle("selected", selected)', html)
        self.assertIn('busy($("open-original-btn"), false)', html)

    def test_patch_043_translation_model_and_original_translation_contract(self) -> None:
        html = self._index()
        self.assertIn('name="translation_model"', html)
        self.assertIn('id="endpoint-use-translation"', html)
        self.assertIn('waitApi("assign", paths[purpose], payload)', html)
        self.assertIn('translation: {model: "", base_url: "", configured: false, enabled: false}', html)
        self.assertIn('state.translation.model || state.llm.model', html)
        self.assertIn('hotspotGen: 0', html)
        self.assertIn('if (gen !== state.hotspotGen) { return null; }', html)
        self.assertIn('id="original-translate"', html)
        self.assertIn('id="original-translation-wrap"', html)
        self.assertIn('waitApi("original_tr", "/api/original/translate"', html)
        self.assertLess(html.index('id="original-content"'), html.index('id="original-translation"'))
        self.assertIn('.model-role-row', html)
        self.assertIn('flex:0 0 36px', html)
        self.assertIn('white-space:nowrap; border-color:transparent; background:transparent', html)
        self.assertIn('.original-head{display:flex; width:100%; min-width:0;', html)
        self.assertIn('.original-head-actions{display:flex; flex:0 0 auto; flex-wrap:nowrap', html)
        self.assertIn('aria-label="关闭" title="关闭">×</button>', html)

    def test_patch_020_chat_ui_and_setting_removed(self) -> None:
        html = self._index()
        for element_id in (
            "chat-input",
            "chat-model",
            "chat-send-btn",
            "chat-output",
        ):
            self.assertNotIn(f'id="{element_id}"', html)
        self.assertNotIn('name="chat_model"', html)
        self.assertNotIn('id="endpoint-use-chat"', html)
        self.assertNotIn('waitApi("chat", "/api/chat"', html)
        self.assertNotIn("state.chat", html)
        self.assertNotIn("function sendChat()", html)
        self.assertIn(".article-scroll[hidden]{display:none;}", html)
        self.assertIn("@media (max-width: 360px)", html)

    def test_patch_041_compact_model_labels_without_chat_picker(self) -> None:
        html = self._index()
        self.assertNotIn('<textarea id="chat-input" rows="3"', html)
        self.assertIn('var option = el("option", "", model);', html)
        self.assertNotIn('model + " · " + endpoint.base_url', html)
        self.assertIn('if (purpose === "article") { payload.effort = "none"; }', html)
        self.assertNotIn("function fitChatInput()", html)
        self.assertNotIn("min-height:72px; max-height:40vh", html)

    def test_patch_021_chat_output_removed(self) -> None:
        html = self._index()
        self.assertIn("function fitArticleBody", html)
        self.assertIn("min-height:calc(100dvh - 8.1rem)", html)
        self.assertNotIn("body.chat-expanded", html)
        self.assertNotIn(".chat-section", html)

    def test_favorites_open_redraws_focus_dock_and_refresh_keeps_favorite_focus(self) -> None:
        html = self._index()
        self.assertIn("var favorite = state.favoriteIds && state.favoriteIds[selectedId];", html)
        favorites_js = html.split("function toggleFavoritesDrawer(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("renderPickedDock();", favorites_js)

    def test_runtime_index_javascript_parses_with_node(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        html = self._index()
        script = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            [node, "--check"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_late_generation_cannot_overwrite_a_newer_request(self) -> None:
        """The per-request generation guards survive; the lane-era article
        session cache does not, because nothing restores across pages."""
        html = self._index()
        self.assertNotIn("articleSessions", html)
        self.assertNotIn("articleLastKeys", html)
        for retired in ("articleSessionKey", "currentArticleSessionKey",
                        "storeGeneratedArticle", "articleTargetIds", "articleContextKey"):
            self.assertNotIn(f"function {retired}(", html)
        self.assertIn("requestGen !== state.articleRequestGen", html)
        self.assertIn("collectGen: 0", html)
        self.assertIn("var requestGen = ++state.collectGen", html)
        self.assertIn("requestGen !== state.collectGen", html)

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
        self.assertIn('waitApi("save", "/api/style"', html)
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
        self.assertIn('waitApi("models", "/api/models"', html)
        self.assertIn("获取可用模型", html)
        self.assertIn("添加自定义提供方", html)
        self.assertIn("一键配置全部入口", html)
        self.assertIn("name=\"article_provider\"", html)
        self.assertIn(".dialog-content{display:flex; flex-direction:column; gap:.8rem; min-height:0; overflow-y:auto", html)

    def test_patch_022_model_management_contract(self) -> None:
        html = self._index()
        self.assertNotIn('<select name="chat_model"', html)
        self.assertIn('id="endpoint-connect"', html)
        self.assertIn('id="endpoint-use-article"', html)
        self.assertNotIn('id="endpoint-use-chat"', html)
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
        self.assertIn("function persistArticle(", html)
        self.assertIn("state.articleDirty ? persistArticle(", html)
        self.assertIn('api("/api/articles/edit"', html)
        self.assertNotIn('status("publish-status", "请先保存修改"', html)
        self.assertIn(".article-field-head{display:flex; align-items:center;", html)
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
        self.assertIn('waitApi("schedule", "/api/schedule"', html)
        self.assertIn("获取可用模型", html)
        self.assertIn("用于文章", html)
        self.assertIn("persist: true", html)
        self.assertIn("一键配置全部入口", html)

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
        self.assertIn('type="checkbox"', html)
        self.assertIn("image-check", html)
        self.assertIn("cover-check", html)
        self.assertIn("body-check", html)
        self.assertNotIn("设为封面", html)
        self.assertNotIn("加入正文", html)
        self.assertIn("上移", html)
        self.assertIn("下移", html)
        self.assertIn("默认只勾选封面", html)
        self.assertIn('id="image-rescan"', html)
        self.assertIn('waitApi("rescan", "/api/articles/images/rescan"', html)
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
        # Iteration 2: community posts are folded into the unified news radar,
        # so the retired S4 wording is gone instead of hidden.
        self.assertNotIn("社区帖过阈会升温", html)
        self.assertNotIn("某某称", html)
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
        self.assertLess(html.index('id="article-model"'), html.index('id="publish-status"'))
        self.assertLess(html.index('id="article-scroll"'), html.index('id="publish-status"'))
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
        self.assertIn("AI 信息生产工作台", html)
        picker = html[html.index("#image-picker{") : html.index("#image-picker[hidden]")]
        self.assertIn("overflow:hidden", picker)
        scroller = html[html.index("#image-picker-list{") : html.index(".image-carousel-nav{")]
        self.assertIn("overflow:hidden", scroller)
        self.assertNotIn("overflow-y:auto", scroller)
        self.assertIn('class="image-picker-head"', html)
        list_rule = html[html.index("#hotspot-list{") : html.index("#hotspot-list li")]
        self.assertIn("overflow-y:auto", list_rule)
        self.assertIn("height:var(--hotspot-list-h)", list_rule)
        self.assertNotIn("emoji", html.lower())

    def test_patch_142_image_carousels(self) -> None:
        html = self._index()
        picker = html[html.index('id="image-picker"') : html.index('id="xhs-studio"')]
        self.assertIn('id="image-picker-counter"', picker)
        github = html[html.index('id="github-studio"') : html.index('id="skills-studio"')]
        self.assertIn('id="github-gallery-counter"', github)
        skills = html[html.index('id="skills-studio"') : html.index('id="article-view"')]
        self.assertIn('id="skills-gallery-counter"', skills)
        self.assertIn("function clampCarouselIndex", html)
        self.assertIn("function paintCarouselNav", html)
        self.assertIn("imagePickerIndex", html)
        self.assertIn("githubGalleryIndex", html)
        self.assertIn("skillsGalleryIndex", html)
        gallery_css = html[html.index(".gh-gallery{") : html.index(".gh-tile{")]
        self.assertIn("display:flex", gallery_css)
        self.assertNotIn("grid-template-columns:repeat(3", gallery_css)
        scroller = html[html.index("#image-picker-list{") : html.index(".image-card{")]
        self.assertNotIn("overflow-y:auto", scroller)
        github_fn = html[
            html.index("function renderGithubGallery()") : html.index("function toggleGithubImage(")
        ]
        self.assertIn("githubGalleryIndex", github_fn)
        self.assertIn("clampCarouselIndex", github_fn)
        skills_fn = html[
            html.index("function renderSkillsGallery()") : html.index("function toggleSkillImage(")
        ]
        self.assertIn("skillsGalleryIndex", skills_fn)
        picker_fn = html[
            html.index("function renderImagePicker(") : html.index("function proxiedImage(")
        ]
        self.assertIn("imagePickerIndex", picker_fn)
        render_fn = html[
            html.index("function renderGithubWeeklyImages()") : html.index("function copyGithubXhs()")
        ]
        self.assertIn("state.githubGalleryIndex = index", render_fn)

    def test_patch_144_lightbox_side_hits_keep_preview_buttons(self) -> None:
        html = self._index()
        lightbox = html[html.index('id="image-lightbox"') : html.index("<script>")]
        self.assertIn('id="lightbox-prev"', lightbox)
        self.assertIn('id="lightbox-next"', lightbox)
        self.assertIn("上一张", lightbox)
        self.assertIn("下一张", lightbox)
        github = html[html.index('id="github-studio"') : html.index('id="skills-studio"')]
        self.assertNotIn('id="github-gallery-prev"', github)
        self.assertNotIn('id="github-gallery-next"', github)
        self.assertNotIn("上一张", github)
        skills = html[html.index('id="skills-studio"') : html.index('id="article-view"')]
        self.assertNotIn('id="skills-gallery-prev"', skills)
        self.assertNotIn('id="skills-gallery-next"', skills)
        self.assertNotIn("上一张", skills)
        picker = html[html.index('id="image-picker"') : html.index('id="xhs-studio"')]
        self.assertNotIn('id="image-picker-prev"', picker)
        self.assertNotIn('id="image-picker-next"', picker)
        self.assertIn("function hitLightboxSide", html)
        self.assertIn("function onLightboxImageClick", html)
        self.assertIn('event.key === "Escape"', html)
        self.assertNotIn('$("lightbox-image").addEventListener("click", closeImageLightbox)', html)

    def test_patch_144_skill_source_chips_toggle_and_pasted_links(self) -> None:
        html = self._index()
        self.assertIn("function collectedSkills", html)
        self.assertIn("function toggleSkillsBySource", html)
        self.assertIn("pack.manual_skills", html)
        chips_fn = html[
            html.index("function renderSkillSourceChips(") : html.index("function skillIdentity(")
        ]
        self.assertIn("toggleSkillsBySource", chips_fn)
        self.assertIn("label + \" \" + counts[label]", chips_fn)
        self.assertIn('button.title = "点击全选或取消来自" + label + "的 skill"', chips_fn)
        # Selecting by source must land on the same collector the gallery and
        # the payload use, so a chip toggle really changes what gets generated.
        toggle_fn = html[html.index("function toggleSkillsBySource(") : html.index("function renderSkillPackage(")]
        self.assertIn("collectedSkills(pack)", toggle_fn)
        self.assertIn("skill.selected = allOn ? false : true;", toggle_fn)
        self.assertIn("renderSkillPackage(pack);", toggle_fn)
        self.assertIn("collectedSkills", html[html.index("function selectedSkills(") : html.index("function toggleSkillsBySource(")])
        pack_fn = html[html.index("function renderSkillPackage(") : html.index("function queuedSkillImages(")]
        self.assertIn("collectedSkills", pack_fn)
        self.assertIn("selected_ids: selectedSkills(pack).map(skillIdentity)", html)

    def test_patch_144_poster_template_picker(self) -> None:
        html = self._index()
        self.assertIn('id="poster-template-dock"', html)
        self.assertIn("模版一", html)
        self.assertIn("模版二", html)
        # It used to be revealed only by a lane class; both poster studios are
        # in the creation page now, so it is a plain visible control there.
        self.assertNotIn("lane-github", html)
        self.assertIn("posterTemplate", html)
        self.assertIn("poster_template", html)
        self.assertRegex(html, r"#poster-template-dock\{[^}]*margin:")
        self.assertNotIn("#poster-template-dock{display:none", html)
        creation = html[html.index('id="page-creation"') : html.index('id="page-publishing"')]
        self.assertIn('id="poster-template-dock"', creation)
        self.assertLess(creation.index('id="poster-template-dock"'), creation.index('id="github-studio"'))
        self.assertNotIn('id="poster-template-dock"', html[: html.index('id="page-creation"')])
        render_fn = html[
            html.index("function renderGithubWeeklyImages()") : html.index("function copyGithubXhs()")
        ]
        self.assertIn("poster_template", render_fn)
        skill_fn = html[
            html.index("function renderSkillImages()") : html.index("function exportSkillImages(")
        ]
        self.assertIn("poster_template", skill_fn)

    def test_patch_143_skill_selection_surface(self) -> None:
        """The collected-skill list became the studio gallery: selection is a
        per-image check in the gallery plus a per-source toggle in the chips."""
        html = self._index()
        self.assertLess(html.index('id="skills-source-chips"'), html.index('id="skills-gallery"'))
        self.assertIn("function skillPageUrl", html)
        self.assertIn("function selectedSkills", html)
        self.assertIn("function skillIdentity", html)
        self.assertIn("function toggleSkillImage", html)
        self.assertIn("function selectAllSkillImages", html)
        # The retired hidden list and its per-row checkbox must not come back.
        self.assertNotIn("function renderSkillList", html)
        self.assertNotIn("function toggleSkillIncluded", html)
        self.assertNotIn('id="skill-list"', html)
        gallery_fn = html[html.index("function renderSkillsGallery(") : html.index("function toggleSkillImage(")]
        self.assertIn('check.className = "gh-check"', gallery_fn)
        self.assertIn("toggleSkillImage(image.id)", gallery_fn)
        self.assertIn("skillPageUrl", html[html.index("function renderResourceResults(") : html.index("function renderResourceGithubResults(")])
        queued = html[html.index("function queuedSkillImages(") : html.index("function renderSkillsGallery(")]
        self.assertIn("selectedSkills", queued)
        render_fn = html[html.index("function renderSkillImages()") : html.index("function exportSkillImages(")]
        self.assertIn("selected_ids", render_fn)
        self.assertIn("selectedSkills", render_fn)
        self.assertIn("超过条目上限", render_fn)
        pack_fn = html[html.index("function renderSkillPackage(") : html.index("function queuedSkillImages(")]
        # The package render must paint the gallery and the source chips, not a
        # retired hidden list.
        self.assertIn("renderSkillsGallery()", pack_fn)
        self.assertIn("renderSkillSourceChips(", pack_fn)
        self.assertNotIn("renderSkillList", pack_fn)
        self.assertIn("linkifySkillArticle", html)
        self.assertIn('.replace(/\\n/g, "<br>")', html)
        self.assertIn("replacePlainToken", html)
        self.assertIn('lastIndexOf("<a ")', html)

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
        self.assertNotIn('name="app_secret" value', html)
        # The skill paste box and the token box carry user-facing hint text, so
        # the bare word `placeholder` is no longer a usable proxy for "an example
        # secret leaked into the markup". Assert the intent directly: no
        # credential input is declared with a value attribute.
        self.assertNotRegex(html, r'id="skill-github-token"[^>]*value=')
        self.assertNotRegex(html, r'name="app_secret"[^>]*value=')
        self.assertNotRegex(html, r'name="x_bearer_token"[^>]*value=')

    def test_favorites_button_drawer_multiselect_and_reflow_contract(self) -> None:
        html = self._index()
        header = html[html.index("<header>") : html.index("</header>")]
        self.assertRegex(
            header,
            r'<div class="header-actions">\s*'
            r'<button type="button" id="favorites-btn" aria-expanded="false" '
            r'aria-controls="favorites-drawer">收藏</button>\s*'
            r'<button type="button" id="config-btn">设置</button>',
        )
        self.assertIn('id="favorites-drawer"', html)
        self.assertIn("is-picked", html)
        self.assertIn("body.favorites-open", html)
        self.assertRegex(
            html,
            r"body\.favorites-open[^{]*\{[^}]*(margin-right|padding-right|grid-template)",
        )
        self.assertIn("state.pickedIds", html)
        self.assertIn("ht-title", html)
        generate_js = html.split("function generate()")[1].split("function persistArticle(")[0]
        self.assertIn("item_ids", generate_js)
        self.assertIn("pickedIdList()", generate_js)

    def test_hotspot_selected_css_is_quiet_focus_not_pick_gold(self) -> None:
        html = self._index()
        selected_start = html.index(".hotspot-item.selected{")
        selected_before = html.index(".hotspot-item.selected::before{")
        selected = html[selected_start:selected_before]
        picked_start = html.index(".fav-item.is-picked, .hotspot-item.is-picked{")
        picked = html[picked_start : html.index("}", picked_start) + 1]
        self.assertIn("box-shadow:inset", picked)
        self.assertIn("rgba(207, 169, 107", picked)
        before_block = html[selected_before : html.index("}", selected_before) + 1]
        self.assertIn("width:3px", before_block)
        self.assertNotIn("accent-glow", selected)
        self.assertNotIn("rgba(207, 169, 107", selected)
        self.assertNotIn("box-shadow:inset", selected)
        self.assertNotIn("linear-gradient", selected)

    def test_select_hotspot_reclick_keeps_selection_visible(self) -> None:
        html = self._index()
        select_js = html.split("function selectHotspot(item, button)")[1].split(
            "function clearHotspotSelection("
        )[0]
        self.assertNotIn("clearHotspotSelection()", select_js)
        self.assertIn("state.item.item_id === item.item_id", select_js)
        self.assertIn('classList.toggle("selected", active)', select_js)
        self.assertIn('setAttribute("aria-pressed", String(active))', select_js)
        self.assertIn('typeof renderPickedDock === "function"', select_js)
        self.assertIn("renderPickedDock()", select_js)
        clear_js = html.split("function clearHotspotSelection(")[1].split(
            "function pickedIdList()"
        )[0]
        self.assertIn("state.item = null", clear_js)
        self.assertIn("classList.remove", clear_js)
        self.assertIn('"selected"', clear_js)
        self.assertIn('typeof renderPickedDock === "function"', clear_js)
        # D2: clicking the title must equal clicking the card (enter 焦点),
        # so the hotspot card's title carries no click hijack of its own.
        hotspots_js = html.split("function renderHotspots(")[1].split("\nfunction ")[0]
        self.assertNotIn('title.addEventListener("click"', hotspots_js)
        self.assertIn(
            'button.addEventListener("click", function () { selectHotspot(item, button); });',
            hotspots_js,
        )
        self.assertNotIn("resetArticle()", select_js)
        # ...and the explicit multi-select checkbox lives on the card instead.
        self.assertIn("pickToggle(item)", hotspots_js)
        self.assertIn("function pickToggle(item)", html)
        self.assertIn('togglePicked(item.item_id);', html)
        self.assertNotIn("resetArticle()", select_js)

    def test_render_picked_dock_shows_focus_and_picks_together(self) -> None:
        """A2: 焦点 and 已选 N are two independent groups and must coexist."""
        html = self._index()
        dock_js = html.split("function renderPickedDock(")[1].split("\nfunction ")[0]
        self.assertIn('el("span", "picked-dock-label", "焦点")', dock_js)
        self.assertIn('el("span", "picked-dock-label", "已选 " + ids.length)', dock_js)
        self.assertIn('el("span", "picked-dock-chip-label", state.item.title || "")', dock_js)
        # The old early-return that hid the focus group whenever picks existed is gone.
        self.assertNotIn("dock.appendChild(chip);\n    });\n    return;\n  }\n  if (state.newsFocus)", dock_js)
        # Only the focus chip is inert; picked chips must stay clickable.
        self.assertIn("picked-dock-chip is-static", dock_js)
        static_css = html.split(".picked-dock-chip.is-static{")[1].split("}")[0]
        self.assertIn("pointer-events:none", static_css)
        self.assertNotIn("#picked-dock.is-focus .picked-dock-chip{", html)

    def test_hotspot_card_has_explicit_pick_checkbox(self) -> None:
        """A2: 已选 is driven by a visible checkbox, not by a hidden title gesture."""
        html = self._index()
        pick_js = html.split("function pickToggle(item)")[1].split("\nfunction ")[0]
        self.assertIn('el("button", "ht-pick"', pick_js)
        self.assertIn('setAttribute("role", "checkbox")', pick_js)
        self.assertIn("aria-checked", pick_js)
        self.assertIn('setAttribute("data-item-id", item.item_id)', pick_js)
        self.assertIn("event.stopPropagation()", pick_js)
        self.assertIn("togglePicked(item.item_id)", pick_js)
        self.assertIn(".ht-pick{", html)
        # The pick control must not reuse the focus styling.
        pick_css = html.split(".ht-pick{")[1].split("}")[0]
        self.assertNotIn("box-shadow:inset", pick_css)

    def test_generate_clears_focus_only_after_a_successful_response(self) -> None:
        """A1: 成功返回后清空焦点；失败分支保留焦点上下文。"""
        html = self._index()
        helper = html.split("function clearFocusAfterGenerate()")[1].split("\nfunction ")[0]
        self.assertIn("state.item = null", helper)
        self.assertIn("renderPickedDock()", helper)
        # Must not disturb the article panel, the draft or the multi-select set.
        self.assertNotIn("resetArticle()", helper)
        self.assertNotIn("state.draft", helper)
        self.assertNotIn("state.pickedIds", helper)
        self.assertNotIn("storeGeneratedArticle", helper)
        # The retired 焦点 grouping and its hook clearing must not come back.
        self.assertNotIn("newsFocus", helper)
        self.assertNotIn("clearHooks", helper)

        generate_js = html.split("function generate()")[1].split("\nfunction persistArticle(")[0]
        self.assertIn('id="article-paste"', html)
        self.assertIn("payload.pasted_text", generate_js)
        success = generate_js.split('waitApi("generate", "/api/articles", payload).then(')[1]
        success_body = success.split("}).catch(")[0]
        failure = generate_js.split("}).catch(function (error) {")[1]
        failure_body = failure.split("}).then(")[0]
        self.assertIn("clearFocusAfterGenerate();", success_body)
        self.assertNotIn("clearFocusAfterGenerate()", failure_body)
        # Payload assembly happens first; clearing the focus comes after it.
        self.assertLess(
            generate_js.index("payload.item_ids = picked;"),
            generate_js.index("clearFocusAfterGenerate();"),
        )
        self.assertLess(
            generate_js.index("payload.item_id = state.item.item_id;"),
            generate_js.index("clearFocusAfterGenerate();"),
        )

    def test_generate_sends_only_the_article_context_it_has(self) -> None:
        """The payload carries a dossier id XOR an item/item_ids selection."""
        html = self._index()
        generate_js = html.split("function generate()")[1].split("\nfunction persistArticle(")[0]
        self.assertIn("payload.dossier_id = dossierId;", generate_js)
        self.assertIn("payload.item_ids = picked;", generate_js)
        self.assertIn("payload.item_id = state.item.item_id;", generate_js)
        # The retired payload fields must never be assembled again.
        for retired in ("chosen_hooks", "related_items"):
            self.assertNotIn(retired, generate_js)
        # A late response for a superseded context must not overwrite the panel.
        success = generate_js.split('waitApi("generate", "/api/articles", payload).then(')[1]
        success_body = success.split("}).catch(")[0]
        self.assertIn("requestGen !== state.articleRequestGen", success_body)

    def test_generate_still_sends_item_ids_from_picked_id_list_when_picks_exist(self) -> None:
        html = self._index()
        generate_js = html.split("function generate()")[1].split("function persistArticle(")[0]
        # A dossier article sends neither item ids nor an item: the dossier is the
        # whole selection. Without one, the picked list wins over the single item.
        self.assertIn("var picked = dossierId ? [] : pickedIdList();", generate_js)
        self.assertIn("if (picked.length >= 1)", generate_js)
        self.assertIn("payload.item_ids = picked;", generate_js)
        self.assertIn("payload.item_id = state.item.item_id;", generate_js)
        self.assertLess(
            generate_js.index("if (picked.length >= 1)"),
            generate_js.index("payload.item_id = state.item.item_id;"),
        )

    def test_picked_dock_markup_is_last_child_of_hotspots_panel(self) -> None:
        html = self._index()
        panel = html[html.index('id="hotspots-panel"') : html.index('id="article-panel"')]
        self.assertIn('id="source-health-wrap"', panel)
        self.assertNotIn('id="picked-dock"', panel)
        self.assertIn('id="picked-dock"', html)
        self.assertRegex(html, r'<aside id="picked-dock" hidden')
        self.assertGreater(html.index('id="picked-dock"'), html.index("</main>"))
        self.assertEqual(html.count('id="picked-dock"'), 1)

    def test_picked_dock_css_is_lane_free_and_still_positioned(self) -> None:
        html = self._index()
        # The lane-scoped hiding rules retired with the lanes; the dock's own
        # positioning contract is what still matters.
        style = re.sub(r"/\*.*?\*/", "", html.split("</style>", 1)[0], flags=re.S)
        self.assertNotIn("lane", style)
        dock_css = html.split("#picked-dock{")[1].split("}")[0]
        self.assertIn("position:fixed", dock_css)
        self.assertIn("bottom:", dock_css)
        self.assertIn("left:", dock_css)
        self.assertIn("bottom:1.5rem", dock_css)
        self.assertIn("left:1.5rem", dock_css)
        self.assertNotIn("right:", dock_css)
        self.assertIn("z-index:45", dock_css)
        self.assertIn("flex-direction:column", dock_css)
        self.assertIn("max-width:18rem", dock_css)
        self.assertIn("max-height:40vh", dock_css)
        self.assertIn("overflow-y:auto", dock_css)
        self.assertIn("pointer-events:auto", dock_css)
        self.assertIn("border:", dock_css)
        self.assertIn("border-radius:", dock_css)
        self.assertIn("padding:", dock_css)
        self.assertNotIn("position:absolute", dock_css)
        self.assertNotIn("body.favorites-open #picked-dock{", html)
        chip_css = html.split(".picked-dock-chip{")[1].split("}")[0]
        self.assertIn("pointer-events:auto", chip_css)
        self.assertIn("text-align:left", chip_css)
        self.assertIn("justify-content:flex-start", chip_css)
        self.assertIn("display:flex", chip_css)
        self.assertIn("font-size:.82rem", chip_css)
        self.assertIn("line-height:1.5", chip_css)
        self.assertIn("padding:.75rem .8rem", chip_css)
        self.assertIn("min-height:3.4rem", chip_css)
        label_css = html.split(".picked-dock-chip-label{")[1].split("}")[0]
        self.assertIn("overflow:hidden", label_css)
        self.assertIn("white-space:normal", label_css)
        self.assertIn("-webkit-line-clamp:2", label_css)

    def test_render_picked_dock_wraps_titles_in_clamped_inner_labels(self) -> None:
        html = self._index()
        dock_js = html.split("function renderPickedDock(")[1].split("\nfunction ")[0]
        self.assertIn('el("button", "picked-dock-chip")', dock_js)
        self.assertIn('el("span", "picked-dock-chip-label", title)', dock_js)
        self.assertIn('el("span", "picked-dock-chip-label", state.item.title || "")', dock_js)

    def test_article_editor_keeps_adaptive_height_and_scroll_anchor(self) -> None:
        html = self._index()
        body_css = html.split("#article-body{", 1)[1].split("}", 1)[0]
        self.assertIn("height:auto", body_css)
        self.assertIn("overflow-anchor:none", body_css)
        fit_js = html.split("function fitArticleBody()", 1)[1].split("function renderArticle(", 1)[0]
        self.assertIn("window.scrollY", fit_js)
        self.assertIn("pageY", fit_js)
        self.assertIn("stableY", fit_js)
        self.assertIn("window.scrollTo(0, stableY)", fit_js)
        self.assertIn('if (id === "article-body") { fitArticleBody(); }', html)

    def test_render_picked_dock_function_and_toggle_picked_call(self) -> None:
        html = self._index()
        self.assertIn("function renderPickedDock(", html)
        toggle_js = html.split("function togglePicked(")[1].split("\nfunction ")[0]
        self.assertIn("renderPickedDock()", toggle_js)
        hotspots_js = html.split("function renderHotspots(")[1].split("\nfunction ")[0]
        self.assertIn("renderPickedDock()", hotspots_js)
        favorites_js = html.split("function renderFavorites(")[1].split("\nfunction ")[0]
        self.assertIn("renderPickedDock()", favorites_js)

    def test_picked_dock_copy_selected_count_and_focus_fallback(self) -> None:
        html = self._index()
        dock_js = html.split("function renderPickedDock(")[1].split("\nfunction ")[0]
        self.assertIn("已选", dock_js)
        self.assertIn("焦点", dock_js)
        self.assertIn("pickedIdList()", dock_js)
        self.assertIn("togglePicked(", dock_js)
        self.assertIn("state.favoriteIds", dock_js)
        self.assertIn("state.rankings", dock_js)
        self.assertIn("state.item", dock_js)
        self.assertIn(".hidden", dock_js)

    def test_picked_dock_renders_more_than_four_selected_items(self) -> None:
        html = self._index()
        dock_js = html.split("function renderPickedDock(")[1].split("\nfunction ")[0]
        self.assertIn("ids.forEach", dock_js)
        self.assertNotIn("slice(0, 4)", dock_js)

    def test_render_favorites_includes_open_original_control(self) -> None:
        html = self._index()
        favorites_js = html.split("function renderFavorites(")[1].split("\nfunction ")[0]
        self.assertIn("打开原文", favorites_js)
        self.assertIn('el("button"', favorites_js)
        self.assertIn("openOriginal(item)", favorites_js)
        open_at = favorites_js.index("openOriginal(item)")
        open_handler = favorites_js[favorites_js.rfind("addEventListener", 0, open_at) : open_at]
        self.assertIn("event.stopPropagation()", open_handler)
        self.assertNotIn("togglePicked", open_handler)

    def test_open_original_uses_item_id_from_argument(self) -> None:
        html = self._index()
        self.assertIn("function openOriginal(item)", html)
        self.assertIn("function closeOriginal(", html)
        open_js = html.split("function openOriginal(item)")[1].split("\nfunction ")[0]
        self.assertIn("item || state.item", open_js)
        self.assertIn("var itemId = item.item_id", open_js)
        self.assertIn('waitApi("original", "/api/original", {item_id: itemId})', open_js)
        self.assertIn("item.title_zh || item.title", open_js)
        self.assertNotIn("state.item.item_id !== itemId", open_js)
        self.assertNotIn("state.item.title", open_js)
        self.assertRegex(
            html,
            r'\$\("open-original-btn"\)\.addEventListener\("click", function \(\) \{ openOriginal\(\); \}\);',
        )

    def test_favorites_use_the_same_explicit_checkbox_as_hotspot_cards(self) -> None:
        """A2, extended to the drawer that feeds the same 已选 set.

        The drawer used to hide the multi-select gesture on the title. It now
        carries the same visible checkbox as a hotspot card, so no surface owns a
        secret gesture.
        """
        html = self._index()
        favorites_js = html.split("function renderFavorites(")[1].split("\nfunction ")[0]
        self.assertNotIn('title.addEventListener("click"', favorites_js)
        self.assertIn("pickToggle(item)", favorites_js)
        self.assertIn('el("span", "ht-title"', favorites_js)
        # The checkbox state must be synced on this surface too.
        sync_js = html.split("function syncPickedHighlights(")[1].split("\nfunction ")[0]
        self.assertIn("#favorites-list .ht-pick", sync_js)
        # Its own controls must survive the change.
        self.assertIn("openOriginal", favorites_js)
        self.assertIn("starButton(item)", favorites_js)


if __name__ == "__main__":
    unittest.main()


class ResourceWebBridgeTests(unittest.TestCase):
    """The resource web leg goes through the retained engine's single bridge."""

    def test_the_web_leg_uses_the_same_adapter_as_research(self) -> None:
        import autoposting.research.adapters as adapters

        calls: list[tuple[str, int]] = []

        def fake_search(self, query, *, adapters, limit):  # noqa: A002 - protocol name
            calls.append((query, limit))
            return adapters_module.SearchResponse(
                query=query,
                results=({"title": "Guide", "url": "https://docs.example/guide"},),
                adapters=adapters,
            )

        adapters_module = adapters
        original = adapters.RelatedNewsSearchAdapter.search
        adapters.RelatedNewsSearchAdapter.search = fake_search
        try:
            app = WebApp.__new__(WebApp)
            app.skill_web_search = None
            app.workbench = types.SimpleNamespace(page_fetcher=None)
            payload = app._resource_web_adapter("ppt agent", limit=4)
        finally:
            adapters.RelatedNewsSearchAdapter.search = original

        self.assertEqual([("ppt agent", 4)], calls)
        self.assertEqual([{"title": "Guide", "url": "https://docs.example/guide", "source": "web", "kind": "website"}], payload["results"])

    def test_a_degraded_bridge_response_does_not_raise(self) -> None:
        import autoposting.background as background
        import autoposting.research.adapters as adapters

        def failing_searcher(*_args, **_kwargs):
            raise background.RelatedSearchError(
                "budget_timeout", detail="https://user:secret@host/path"
            )

        degraded = adapters.RelatedNewsSearchAdapter(searcher=failing_searcher).search(
            "ppt agent", adapters=("related_search",), limit=2
        )
        self.assertTrue(degraded.degraded)

        original = adapters.RelatedNewsSearchAdapter.search
        adapters.RelatedNewsSearchAdapter.search = lambda *args, **kwargs: degraded
        try:
            app = WebApp.__new__(WebApp)
            app.skill_web_search = None
            app.workbench = types.SimpleNamespace(page_fetcher=None)
            payload = app._resource_web_adapter("ppt agent", limit=2)
        finally:
            adapters.RelatedNewsSearchAdapter.search = original

        self.assertTrue(payload["degraded"])
        self.assertEqual("budget_timeout", payload["cause_code"])
        self.assertNotIn("secret", json.dumps(payload))
