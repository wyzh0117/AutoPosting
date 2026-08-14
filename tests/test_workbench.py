import json
import tempfile
import threading
import types
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autoposting.content import FactValidationError
from autoposting.db import Database
from autoposting.llm import LLMError, LLMService
from autoposting.models import FeedItem, Source
from autoposting.publishing import PublicationError
from autoposting.workbench import HotspotService, WeChatPublisher, Workbench


class FakeModel:
    """Prompt-aware fake LLM: echoes translations, or returns a valid article."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_with = fail_with

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(argv)
        if self.fail_with is not None:
            raise self.fail_with
        prompt = argv[2]
        if "翻译引擎" in prompt:
            payload = json.loads(prompt.rsplit("数据：\n", 1)[1])
            return types.SimpleNamespace(
                stdout=json.dumps(
                    {
                        "translations": [
                            {
                                "item_id": item["item_id"],
                                "title_zh": f"中文翻译：{item['title']}",
                                "summary_zh": f"中文摘要：{item['summary']}",
                            }
                            for item in payload
                        ]
                    },
                    ensure_ascii=False,
                ),
                stderr="",
                returncode=0,
            )
        if "候选切口JSON" in prompt:
            return types.SimpleNamespace(
                stdout='{"candidates":[{"phrase":"协和医学院4+4试点班","kind":"program"}]}',
                stderr="",
                returncode=0,
            )
        marker = "body 必须原样包含每个来源 ID："
        source_ids = prompt.split(marker, 1)[1].splitlines()[0].strip() if marker in prompt else ""
        body = (
            f"导语：今日要点。\n核心信息：官方消息。\n来源与未知项：{source_ids}\n"
            "AIGC 声明：本文由 AI 辅助生成。"
        )
        return types.SimpleNamespace(
            stdout=json.dumps({"title": "模型一正式发布", "body": body}, ensure_ascii=False),
            stderr="",
            returncode=0,
        )


def _pub_date(minutes_ago: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return value.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _feed(entries: list[tuple[str, str, str, int]]) -> bytes:
    items = "".join(
        f"<item><guid>{guid}</guid><title>{title}</title><link>{url}</link>"
        f"<description>{title} 内容摘要</description><pubDate>{_pub_date(minutes_ago)}</pubDate></item>"
        for guid, title, url, minutes_ago in entries
    )
    return (
        "<rss version='2.0'><channel><title>Fixture</title>"
        + items
        + "</channel></rss>"
    ).encode("utf-8")


EMPTY_FEED = _feed([])


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        self.sources = (
            Source("lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15),
            Source("media_feed", "AI Media", "rss", "https://media.example/feed.xml", "S3", ("models",), True, 15),
            Source("rumor_board", "Rumor Board", "rss", "https://rumor.example/feed.xml", "S4", ("models",), True, 15),
        )
        for source in self.sources:
            self.db.upsert_source(source)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_collect_forces_collection_and_ranks_recent_multi_source_first(self) -> None:
        fetcher = {
            "https://lab.example/feed.xml": _feed([("a1", "Model One news", "https://lab.example/a1", 10)]),
            "https://media.example/feed.xml": _feed(
                [
                    ("a1-dup", "Model One news", "https://media.example/a1", 10),
                    ("b2", "Model Two news", "https://media.example/b2", 40),
                ]
            ),
            "https://rumor.example/feed.xml": EMPTY_FEED,
        }
        service = HotspotService(self.db, self.sources, fetcher=fetcher.get)

        payload = service.collect(24)
        candidates = payload["candidates"]

        self.assertEqual(2, len(candidates))
        first, second = candidates
        self.assertEqual("Model One news", first["title"])
        self.assertEqual({"lab_blog", "media_feed"}, set(first["source_ids"]))
        self.assertEqual(2, len(first["source_urls"]))
        self.assertEqual("Model Two news", second["title"])
        self.assertEqual(1, len(second["source_ids"]))
        self.assertLess(second["observed_at"], first["observed_at"])
        self.assertEqual(payload["rankings"]["time"], candidates)
        self.assertIn("heat", payload["rankings"])
        self.assertTrue(payload["sources"])
        self.assertGreaterEqual(payload["collection"]["inserted"], 1)
        for candidate in candidates:
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
                set(candidate),
            )
            self.assertTrue(candidate["summary"])
        self.assertEqual(2, self.db.count_items())

    def test_heat_and_time_rankings_diverge(self) -> None:
        now = datetime.now(timezone.utc)
        lab, media, rumor = self.sources
        self.db.insert_item(
            lab,
            FeedItem("fresh", "https://lab.example/fresh", "Just now", "Just now 内容", now.isoformat(), {}),
        )
        self.db.insert_item(
            lab,
            FeedItem("hot", "https://lab.example/hot", "Widely seen", "Widely seen 内容", (now - timedelta(minutes=40)).isoformat(), {}),
        )
        self.db.insert_item(
            media,
            FeedItem("hot-m", "https://media.example/hot", "Widely seen", "Widely seen 内容", (now - timedelta(minutes=40)).isoformat(), {}),
        )
        self.db.insert_item(
            rumor,
            FeedItem("hot-r", "https://rumor.example/hot", "Widely seen", "Widely seen 内容", (now - timedelta(minutes=40)).isoformat(), {}),
        )
        service = HotspotService(self.db, self.sources, fetcher=lambda _: EMPTY_FEED)

        payload = service.collect(24)
        time_titles = [item["title"] for item in payload["rankings"]["time"]]
        heat_titles = [item["title"] for item in payload["rankings"]["heat"]]

        self.assertEqual("Just now", time_titles[0])
        self.assertEqual("Widely seen", heat_titles[0])
        self.assertGreaterEqual(payload["rankings"]["heat"][0]["source_count"], 2)

    def test_source_health_and_single_source_force_refresh(self) -> None:
        fetched = []

        def fetcher(url: str) -> bytes:
            fetched.append(url)
            if url == "https://lab.example/feed.xml":
                return _feed([("only-lab", "Lab only", "https://lab.example/only", 5)])
            raise RuntimeError("media down")

        service = HotspotService(self.db, self.sources[:2], fetcher=fetcher)
        health = {row["source_id"]: row for row in service.source_health()}
        self.assertEqual("never", health["lab_blog"]["status"])
        payload = service.collect(24, force=True, source_id="lab_blog")
        self.assertEqual(["https://lab.example/feed.xml"], fetched)
        self.assertEqual(1, payload["collection"]["inserted"])
        self.assertEqual("lab_blog", payload["collection"]["source_id"])
        health = {row["source_id"]: row for row in payload["sources"]}
        self.assertEqual("ok", health["lab_blog"]["status"])
        self.assertEqual("never", health["media_feed"]["status"])
        service.collect(24, force=True, source_id="media_feed")
        health = {row["source_id"]: row for row in service.source_health()}
        self.assertEqual("error", health["media_feed"]["status"])
        self.assertIn("media down", health["media_feed"]["last_error"])
        with self.assertRaises(ValueError):
            service.collect(24, source_id="missing")

    def test_collect_rejects_out_of_range_hours_and_defaults_to_four(self) -> None:
        service = HotspotService(self.db, self.sources, fetcher=lambda _: EMPTY_FEED)
        with self.assertRaises(ValueError):
            service.collect(0)
        with self.assertRaises(ValueError):
            service.collect(721)
        self.assertEqual([], service.collect()["candidates"])

    def test_candidates_are_capped_at_ten(self) -> None:
        lab = self.sources[0]
        now = datetime.now(timezone.utc)
        for index in range(12):
            item = FeedItem(
                external_id=f"cap-{index}",
                canonical_url=f"https://lab.example/cap-{index}",
                title=f"Candidate {index}",
                content_text=f"Candidate {index} 内容",
                published_at=(now - timedelta(seconds=index)).isoformat(),
                raw_data={},
            )
            self.db.insert_item(lab, item)
        service = HotspotService(self.db, self.sources, fetcher=lambda _: EMPTY_FEED)

        candidates = service.collect(24)["candidates"]

        self.assertEqual(10, len(candidates))
        self.assertEqual([f"Candidate {index}" for index in range(10)], [c["title"] for c in candidates])

    def test_generate_article_builds_traceable_idempotent_draft(self) -> None:
        lab, media = self.sources[0], self.sources[1]
        self.db.insert_item(
            lab,
            FeedItem("a1", "https://lab.example/a1", "Model One news", "Model One news 内容", None, {}),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        self.db.insert_item(
            media,
            FeedItem("a1-dup", "https://media.example/a1", "Model One news", "Model One news 内容", None, {}),
        )
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))

        first = workbench.generate_article(item_id, "wechat")
        second = workbench.generate_article(item_id, "wechat")
        xiaohongshu = workbench.generate_article(item_id, "xiaohongshu")

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(2, self.db.count_drafts())
        self.assertEqual("DRAFTED", first["status"])
        self.assertEqual("wechat", first["platform"])
        self.assertEqual("xiaohongshu", xiaohongshu["platform"])
        self.assertIn("body", first)
        self.assertIn("AIGC", first["body"])
        self.assertIn("lab_blog", first["body"])
        self.assertIn("media_feed", first["body"])
        self.assertIn("【信息来源 2 条 · 原文 2 篇】", first["body"])
        self.assertIn("原文链接", first["body"])
        self.assertIn("https://lab.example/a1", first["body"])
        self.assertIn("https://media.example/a1", first["body"])
        self.assertEqual(["lab_blog", "media_feed"], list(first["source_ids"]))
        event = self.db.get_event(first["event_id"])
        self.assertEqual("DRAFTED", event["status"])
        self.assertIn("lab_blog", event["fact_list_json"])
        self.assertIn("media_feed", event["fact_list_json"])
        draft = self.db.get_draft(first["draft_id"])
        self.assertEqual({"lab_blog", "media_feed"}, set(json.loads(draft["source_ids_json"])))
        self.assertEqual("openai-compatible/unconfigured:none:article-v3", draft["generator"])
        self.assertEqual("article-v3", draft["processing_version"])
        self.assertEqual("HOTSPOT_ITEM", event["trigger_type"])
        self.assertEqual(2, len(model.calls))
        self.assertIn("前因与脉络", model.calls[0][2])
        self.assertIn("background", model.calls[0][2])
        self.assertEqual([], (first.get("assets") or {}).get("candidates", first.get("assets") or []))

    def test_generate_article_attaches_source_images_and_credits(self) -> None:
        lab = self.sources[0]
        self.db.insert_item(
            lab,
            FeedItem(
                "img-1",
                "https://lab.example/img-1",
                "Model One news",
                "Model One news 内容",
                None,
                {"image_urls": ["https://lab.example/a.jpg", "https://lab.example/b.jpg", "https://lab.example/c.jpg"]},
            ),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        pngs = {
            "https://lab.example/a.jpg": b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (900).to_bytes(4, "big") + (1200).to_bytes(4, "big"),
            "https://lab.example/b.jpg": b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (750).to_bytes(4, "big") + (1000).to_bytes(4, "big"),
            "https://lab.example/c.jpg": b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (800).to_bytes(4, "big") + (800).to_bytes(4, "big"),
        }
        workbench = Workbench(
            self.db,
            llm_service=LLMService(self.db, runner=FakeModel()),
            image_fetcher=pngs.__getitem__,
        )

        result = workbench.generate_article(item_id, "wechat")

        self.assertEqual(3, len(result["assets"]["candidates"]))
        self.assertEqual("article-assets-v2", result["assets"]["schema"])
        self.assertTrue(result["assets"]["cover_id"])
        self.assertEqual(3, len(result["assets"]["body_ids"]))
        self.assertIn("配图（来自原文，非 AI 生成）", result["body"])
        self.assertIn("封面：", result["body"])
        self.assertIn("https://lab.example/a.jpg", result["body"])
        self.assertIn("来源 lab_blog", result["body"])

    def test_generate_article_fetches_page_opengraph_image(self) -> None:
        lab = self.sources[0]
        self.db.insert_item(
            lab,
            FeedItem(
                "og-1",
                "https://news.example/story",
                "Model One news",
                "Model One news 内容",
                None,
                {},
            ),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        html = (
            b'<html><head><meta property="og:image" '
            b'content="https://cdn.example/opengraph-image?id=1"></head></html>'
        )
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1200).to_bytes(4, "big") + (630).to_bytes(4, "big")
        workbench = Workbench(
            self.db,
            llm_service=LLMService(self.db, runner=FakeModel()),
            page_fetcher=lambda url: html if "story" in url else b"",
            image_fetcher=lambda url: png,
        )
        result = workbench.generate_article(item_id, "wechat")
        self.assertEqual(1, len(result["assets"]["candidates"]))
        self.assertIn("opengraph-image", result["assets"]["candidates"][0]["url"])
        self.assertIn("封面：", result["body"])

    def test_s4_only_item_can_generate_discussion_article(self) -> None:
        rumor = self.sources[2]
        self.db.insert_item(
            rumor,
            FeedItem("r1", "https://rumor.example/r1", "Unverified rumor", "Unverified rumor 内容", None, {}),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=FakeModel()))

        result = workbench.generate_article(item_id, "wechat")

        self.assertIn("【信息来源 1 条 · 原文 1 篇】", result["body"])
        self.assertIn("https://rumor.example/r1", result["body"])
        self.assertIn("讨论提示", result["body"])
        self.assertIn("某某称", result["body"])
        event = self.db.connect().execute("SELECT status FROM events LIMIT 1").fetchone()
        self.assertEqual("DRAFTED", event["status"])

    def test_generate_article_rejects_unknown_platform_and_item(self) -> None:
        workbench = Workbench(self.db)
        with self.assertRaises(ValueError):
            workbench.generate_article("missing-item", "douyin")
        with self.assertRaises(KeyError):
            workbench.generate_article("missing-item", "wechat")

    HOOK_BODY = (
        "本科是北大地质学，2020 年通过协和医学院“4+4 试点班”转入临床医学。"
    )

    def _one_item(self, title: str, body: str) -> str:
        self.db.insert_item(
            self.sources[0],
            FeedItem("hook-1", "https://lab.example/hook-1", title, body, None, {}),
        )
        return str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])

    def test_find_hooks_uses_injected_searcher(self) -> None:
        item_id = self._one_item("中国医生用 GPT-5.6 破解数学题", self.HOOK_BODY)

        def searcher(query: str) -> list[dict[str, str]]:
            if "试点班" in query or "4+4" in query:
                return [
                    {
                        "title": "协和4+4试点班争议",
                        "url": "https://news.example/4plus4",
                        "snippet": "舆论质疑。",
                    }
                ]
            return []

        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=FakeModel()))
        payload = workbench.find_hooks(item_id, searcher=searcher)
        self.assertTrue(payload["ok"])
        self.assertTrue(any("4+4" in card["phrase"] for card in payload["hooks"]))

    def test_generate_article_without_hooks_omits_chosen_hooks_payload(self) -> None:
        item_id = self._one_item("Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        workbench.generate_article(item_id, "wechat")
        self.assertNotIn('"chosen_hooks"', model.calls[0][2])

    def test_generate_article_with_hooks_puts_them_in_prompt_order(self) -> None:
        item_id = self._one_item("Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        workbench.generate_article(
            item_id,
            "wechat",
            chosen_hooks=[
                {"phrase": "协和4+4试点班", "why": "公开检索出现争议"},
                {"phrase": "第二切口"},
            ],
        )
        prompt = model.calls[-1][2]
        self.assertIn('"chosen_hooks"', prompt)
        self.assertLess(prompt.index("协和4+4试点班"), prompt.index("第二切口"))


class WorkbenchLlmTests(unittest.TestCase):
    def setUp(self) -> None:
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_window_hides_retail_gadget_titles(self) -> None:
        now = datetime.now(timezone.utc)
        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "keep",
                "https://lab.example/keep",
                "DeepSeek Harness public preview",
                "Agent framework.",
                now.isoformat(),
                {},
            ),
        )
        self.db.insert_item(
            self.sources[1],
            FeedItem(
                "ad",
                "https://media.example/ad",
                "OPPO Enco Clip2 耳夹耳机新配色开售，限时优惠价 809.1 元",
                "文里写了 AI 降噪。",
                now.isoformat(),
                {},
            ),
        )
        service = HotspotService(self.db, self.sources, fetcher=lambda _: EMPTY_FEED)
        titles = [item["title"] for item in service.list_window(24, translate=False)["candidates"]]
        self.assertIn("DeepSeek Harness public preview", titles)
        self.assertNotIn("OPPO Enco Clip2 耳夹耳机新配色开售，限时优惠价 809.1 元", titles)

    def test_collect_translates_missing_english_candidates_in_one_batch(self) -> None:
        fetcher = {
            "https://lab.example/feed.xml": _feed([("a1", "Model One news", "https://lab.example/a1", 10)]),
            "https://media.example/feed.xml": _feed(
                [("a1-dup", "Model One news", "https://media.example/a1", 10), ("b2", "Model Two news", "https://media.example/b2", 40)]
            ),
        }
        model = FakeModel()
        service = HotspotService(
            self.db, self.sources, fetcher=fetcher.get, llm_service=LLMService(self.db, runner=model)
        )

        collected = service.collect(24)["candidates"]
        self.assertEqual(0, len(model.calls))
        self.assertEqual(2, len(collected))
        self.assertEqual("unavailable", collected[0]["translation_status"])

        candidates = service.list_window(24, translate=True)["candidates"]

        self.assertEqual(1, len(model.calls))
        self.assertEqual(2, len(candidates))
        first, second = candidates
        self.assertEqual("Model One news", first["title"])
        self.assertEqual("中文翻译：Model One news", first["title_zh"])
        self.assertEqual("translated", first["translation_status"])
        self.assertEqual("", first["translation_error"])
        self.assertEqual("Model Two news", second["title"])
        self.assertEqual("中文翻译：Model Two news", second["title_zh"])
        self.assertEqual("translated", second["translation_status"])

    def test_collect_cjk_item_copied_without_runner_call(self) -> None:
        self.db.insert_item(
            self.sources[0],
            FeedItem("cn1", "https://lab.example/cn1", "中文新闻标题", "中文摘要内容", None, {}),
        )
        model = FakeModel()
        service = HotspotService(
            self.db, self.sources, fetcher=lambda _: EMPTY_FEED, llm_service=LLMService(self.db, runner=model)
        )

        candidates = service.list_window(24, translate=True)["candidates"]

        self.assertEqual(0, len(model.calls))
        self.assertEqual(1, len(candidates))
        self.assertEqual("original", candidates[0]["translation_status"])
        self.assertEqual("中文新闻标题", candidates[0]["title_zh"])
        self.assertEqual("中文新闻标题", candidates[0]["title"])

    def test_collect_llm_failure_degrades_to_original(self) -> None:
        fetcher = {
            "https://lab.example/feed.xml": _feed([("a1", "Model One news", "https://lab.example/a1", 10)]),
            "https://media.example/feed.xml": _feed([("a1-dup", "Model One news", "https://media.example/a1", 10)]),
        }
        model = FakeModel(fail_with=TimeoutError("hung"))
        service = HotspotService(
            self.db, self.sources, fetcher=fetcher.get, llm_service=LLMService(self.db, runner=model)
        )

        collected = service.collect(24)["candidates"]
        self.assertEqual("unavailable", collected[0]["translation_status"])
        candidates = service.list_window(24, translate=True)["candidates"]

        self.assertEqual(1, len(candidates))
        self.assertEqual("Model One news", candidates[0]["title"])
        self.assertEqual("failed", candidates[0]["translation_status"])
        self.assertEqual("translation_unavailable", candidates[0]["translation_error"])
        self.assertEqual("", candidates[0]["title_zh"])

    def test_generate_article_llm_unavailable_raises_without_draft(self) -> None:
        self.db.insert_item(
            self.sources[0],
            FeedItem("a1", "https://lab.example/a1", "Model One news", "Model One news 内容", None, {}),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        model = FakeModel(fail_with=TimeoutError("hung"))
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))

        with self.assertRaises(LLMError) as raised:
            workbench.generate_article(item_id, "wechat")

        self.assertEqual("article_generation_failed", str(raised.exception))
        self.assertEqual(0, self.db.count_drafts())
        self.assertEqual("VERIFIED", self.db.get_event(
            self.db.connect().execute("SELECT event_id FROM events LIMIT 1").fetchone()["event_id"]
        )["status"])

    def test_generate_article_s4_gate_runs_before_llm(self) -> None:
        rumor = Source("rumor_board", "Rumor Board", "rss", "https://rumor.example/feed.xml", "S4", ("models",), True, 15)
        self.db.upsert_source(rumor)
        self.db.insert_item(
            rumor,
            FeedItem("r1", "https://rumor.example/r1", "Unverified rumor", "Unverified rumor 内容", None, {}),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))

        result = workbench.generate_article(item_id, "wechat")

        self.assertGreater(len(model.calls), 0)
        self.assertIn("讨论提示", result["body"])


class WechatStubHandler(BaseHTTPRequestHandler):
    fail_errcode: int | None = None
    requests: list[tuple[str, str, str | None]] = []

    def _send(self, payload: dict[str, object], code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        WechatStubHandler.requests.append(("GET", self.path, None))
        if WechatStubHandler.fail_errcode is not None:
            self._send({"errcode": WechatStubHandler.fail_errcode, "errmsg": "stubbed failure"})
            return
        self._send({"access_token": "stub-token-123", "expires_in": 7200})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/cgi-bin/media/uploadimg"):
            WechatStubHandler.requests.append(("POST", self.path, raw[:40]))
            self._send({"url": "https://mmbiz.qpic.cn/stub-uploaded"})
            return
        if self.path.startswith("/cgi-bin/media/upload"):
            WechatStubHandler.requests.append(("POST", self.path, raw[:40]))
            self._send({"media_id": "stub-thumb-1", "type": "thumb"})
            return
        body = raw.decode("utf-8") if raw else None
        WechatStubHandler.requests.append(("POST", self.path, body))
        if self.path.startswith("/cgi-bin/draft/add"):
            self._send({"media_id": "stub-media-1"})
        elif self.path.startswith("/cgi-bin/freepublish/submit"):
            self._send({"publish_id": "stub-publish-1"})
        else:
            self._send({"errcode": 404, "errmsg": "not found"})

    def log_message(self, *args: object) -> None:
        pass


class WeChatPublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        WechatStubHandler.fail_errcode = None
        WechatStubHandler.requests = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), WechatStubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        WechatStubHandler.requests = []
        WechatStubHandler.fail_errcode = None

    def _draft(self) -> dict[str, object]:
        return {"title": "Model One 发布", "body": "第一行\nAIGC 声明：本文由自动化工具辅助生成。"}

    def test_publish_runs_token_draft_submit_flow(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id", "s3cr3t-value", "thumb-1", "测试作者", base_url=self.base_url
        )

        result = publisher.publish(self._draft())

        self.assertEqual("SUBMITTED", result["status"])
        self.assertEqual("stub-publish-1", result["reference"])
        method, path, _ = WechatStubHandler.requests[0]
        self.assertEqual("GET", method)
        self.assertIn("/cgi-bin/token", path)
        self.assertIn("appid=wx_app_id", path)
        self.assertIn("secret=s3cr3t-value", path)
        method, path, body = WechatStubHandler.requests[1]
        self.assertEqual("POST", method)
        self.assertIn("/cgi-bin/draft/add?access_token=stub-token-123", path)
        payload = json.loads(body)
        article = payload["articles"][0]
        self.assertEqual("thumb-1", article["thumb_media_id"])
        self.assertEqual("测试作者", article["author"])
        self.assertEqual("Model One 发布", article["title"])
        self.assertIn("<p>第一行</p>", article["content"])
        method, path, body = WechatStubHandler.requests[2]
        self.assertEqual("POST", method)
        self.assertIn("/cgi-bin/freepublish/submit", path)
        self.assertEqual({"media_id": "stub-media-1"}, json.loads(body))

    def test_errcode_maps_to_secret_free_exception(self) -> None:
        WechatStubHandler.fail_errcode = 40013
        publisher = WeChatPublisher("wx_app_id", "s3cr3t-value", "thumb-1", "", base_url=self.base_url)

        with self.assertRaisesRegex(PublicationError, "invalid_appid"):
            publisher.publish(self._draft())

    def test_unknown_errcode_is_reported_without_secret(self) -> None:
        WechatStubHandler.fail_errcode = 99999
        publisher = WeChatPublisher("wx_app_id", "s3cr3t-value", "thumb-1", "", base_url=self.base_url)

        with self.assertRaisesRegex(PublicationError, "wechat_api_error:99999"):
            publisher.publish(self._draft())
        try:
            publisher.publish(self._draft())
        except PublicationError as exc:
            self.assertNotIn("s3cr3t-value", str(exc))
            self.assertNotIn("stub-token-123", str(exc))

    def test_network_error_does_not_expose_secret_url(self) -> None:
        def failing_opener(request):
            raise urllib.error.URLError(request.full_url)

        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            opener=failing_opener,
        )

        with self.assertRaises(PublicationError) as raised:
            publisher.publish(self._draft())

        self.assertEqual("wechat_api_unavailable", str(raised.exception))
        self.assertNotIn("s3cr3t-value", str(raised.exception))

    def test_publish_uploads_source_images_and_embeds_three_by_four_figures(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            image_fetcher=lambda _url: b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
            base_url=self.base_url,
        )
        draft = {
            "title": "配图稿",
            "body": "正文\nAIGC",
            "assets": [
                {
                    "url": "https://lab.example/hero.jpg",
                    "source_id": "lab_blog",
                    "width": 900,
                    "height": 1200,
                }
            ],
        }

        publisher.publish(draft)

        paths = [item[1] for item in WechatStubHandler.requests]
        self.assertTrue(any("/cgi-bin/media/uploadimg" in path for path in paths))
        self.assertTrue(any("/cgi-bin/media/upload?" in path and "type=thumb" in path for path in paths))
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("stub-thumb-1", article["thumb_media_id"])
        content = article["content"]
        self.assertIn("https://mmbiz.qpic.cn/stub-uploaded", content)
        self.assertIn("aspect-ratio:3/4", content)
        self.assertIn("来源：lab_blog", content)
        self.assertIn("https://lab.example/hero.jpg", content)


if __name__ == "__main__":
    unittest.main()
