import json
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from autoposting.background import derive_related_query
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
        citation_lines = "\n".join(
            f"官方发布了最新进展，{source_id.strip()}。"
            for source_id in source_ids.split(",")
            if source_id.strip()
        )
        body = (
            f"导语：今日要点。\n核心信息：官方消息。\n{citation_lines}\n"
            "AIGC 声明：本文由 AI 辅助生成。"
        )
        payload = {"title": "模型一正式发布", "body": body}
        if "caption" in prompt:
            payload["caption"] = (
                f"这件事刚被公开确认。\n- 官方发布了最新进展\n- 细节仍以原文为准\n"
                f"来源：{source_ids}\nAIGC 声明：本文由 AI 辅助生成。"
            )
        return types.SimpleNamespace(
            stdout=json.dumps(payload, ensure_ascii=False),
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
        self.assertNotIn("lab_blog", first["body"])
        self.assertNotIn("media_feed", first["body"])
        self.assertNotIn("【信息来源", first["body"])
        self.assertNotIn("原文链接", first["body"])
        self.assertIn("https://lab.example/a1", first["body"])
        self.assertIn("https://media.example/a1", first["body"])
        self.assertEqual(["lab_blog", "media_feed"], list(first["source_ids"]))
        event = self.db.get_event(first["event_id"])
        self.assertEqual("DRAFTED", event["status"])
        self.assertIn("lab_blog", event["fact_list_json"])
        self.assertIn("media_feed", event["fact_list_json"])
        draft = self.db.get_draft(first["draft_id"])
        self.assertEqual({"lab_blog", "media_feed"}, set(json.loads(draft["source_ids_json"])))
        self.assertEqual("openai-compatible/unconfigured:none:article-v12", draft["generator"])
        self.assertEqual("article-v12", draft["processing_version"])
        self.assertEqual("HOTSPOT_ITEM", event["trigger_type"])
        self.assertEqual(2, len(model.calls))
        self.assertEqual("AI专家", first["prompt_style"])
        self.assertNotIn("caption", first)
        self.assertIn("导语：今日要点", xiaohongshu["body"])
        self.assertEqual(xiaohongshu["body"], xiaohongshu["caption"])
        self.assertIn("导语：今日要点", xiaohongshu["manuscript"])
        self.assertEqual("cover", xiaohongshu["cards"][0]["kind"])
        self.assertEqual(1080, xiaohongshu["cards"][0]["width"])
        self.assertEqual(1920, xiaohongshu["cards"][0]["height"])
        self.assertIn("一般人容易看漏的角度", model.calls[0][2])
        self.assertIn("background", model.calls[0][2])
        self.assertEqual([], (first.get("assets") or {}).get("candidates", first.get("assets") or []))

    def test_generate_article_attaches_source_images_without_source_credits(self) -> None:
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
        self.assertEqual([], result["assets"]["body_ids"])
        self.assertNotIn("配图（来自原文，非 AI 生成）", result["body"])
        self.assertNotIn("https://lab.example/a.jpg", result["body"])
        self.assertNotIn("来源 lab_blog", result["body"])

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
        self.assertNotIn("配图（来自原文，非 AI 生成）", result["body"])

    def test_s4_only_item_can_generate_discussion_article(self) -> None:
        rumor = self.sources[2]
        self.db.insert_item(
            rumor,
            FeedItem("r1", "https://rumor.example/r1", "Unverified rumor", "Unverified rumor 内容", None, {}),
        )
        item_id = self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"]
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=FakeModel()))

        result = workbench.generate_article(item_id, "wechat")

        self.assertIn("[官方发布了最新进展](https://rumor.example/r1)", result["body"])
        self.assertNotIn("【信息来源", result["body"])
        self.assertNotIn("原文链接", result["body"])
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

    def _named_item(self, external_id: str, title: str, body: str) -> str:
        self.db.insert_item(
            self.sources[0],
            FeedItem(external_id, f"https://lab.example/{external_id}", title, body, None, {}),
        )
        return str(
            self.db.connect()
            .execute("SELECT item_id FROM items WHERE title = ?", (title,))
            .fetchone()["item_id"]
        )

    def test_generate_article_one_item_ids_matches_item_id_path(self) -> None:
        item_id = self._named_item("one-path", "Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        first = workbench.generate_article(item_id, "wechat")
        second = workbench.generate_article(item_id, "wechat", item_ids=[item_id])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(first["source_ids"], second["source_ids"])
        self.assertEqual(first["body"], second["body"])

    def test_generate_article_includes_pasted_page_text_in_prompt(self) -> None:
        item_id = self._named_item("paste-1", "Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        workbench.generate_article(
            item_id,
            "wechat",
            pasted_text="这是用户从内容页粘贴进来的完整正文，必须进入生成提示。",
        )
        prompt = model.calls[-1][2]
        self.assertIn("这是用户从内容页粘贴进来的完整正文，必须进入生成提示。", prompt)
        self.assertIn("pasted_page", prompt)

    def test_generate_article_item_ids_fuses_two_items_into_one_prompt(self) -> None:
        first_id = self._named_item("fuse-a", "Alpha launch", "Alpha body from lab")
        second_id = self._named_item("fuse-b", "Beta debate", "Beta body from media")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        result = workbench.generate_article(
            first_id,
            "wechat",
            item_ids=[second_id, first_id],
        )
        prompt = model.calls[-1][2]
        payload, _ = json.JSONDecoder().raw_decode(prompt.rsplit("数据：\n", 1)[1].strip())
        self.assertIn("Alpha launch", payload["summary"])
        self.assertIn("Alpha body from lab", payload["summary"])
        self.assertIn("Beta debate", payload["summary"])
        self.assertIn("Beta body from media", payload["summary"])
        self.assertIsInstance(payload["title"], str)
        self.assertTrue(payload["title"])
        self.assertIn("lab_blog", result["source_ids"])
        self.assertEqual(1, len(model.calls))


    def _related_draft(self, body: str, draft_id: str = "draft-related-search") -> str:
        """Insert one event/draft pair directly; related search only needs the body."""
        now = datetime.now(timezone.utc).isoformat()
        event_id = f"event-{draft_id}"
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO events (
                    event_id, topic, name, trigger_type, window_start, triggered_at,
                    current_count, independent_sources, baseline_mean, baseline_stddev,
                    statistical_threshold, evidence_json, rule_version, fact_list_json,
                    status, created_at
                ) VALUES (?, 'related', 'related', 'HOTSPOT_ITEM', ?, ?, 1, 1, 0, 0, 0, '{}',
                          'v1', '{}', 'DRAFTED', ?)
                """,
                (event_id, now, now, now),
            )
            connection.execute(
                """
                INSERT INTO drafts (
                    draft_id, event_id, platform, title, body, tags_json, assets_json,
                    source_ids_json, generator, processing_version, status, validation_json,
                    content_hash, created_at, updated_at
                ) VALUES (?, ?, 'wechat', ?, ?, '[]', '[]', '[]', 'test', 'draft-v1',
                          'DRAFTED', '{}', 'hash', ?, ?)
                """,
                (draft_id, event_id, "相关检索草稿", body, now, now),
            )
        return draft_id

    def test_related_search_returns_derived_query_and_items(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        html = (
            '<a class="result__a" href="https://news.example/chip">英伟达 AI 芯片新进展</a>'
            '<div class="result__snippet">英伟达新一代芯片报道</div>'
        ).encode("utf-8")

        def fetcher(url: str) -> bytes:
            return html if "duckduckgo.com/html" in url else b""

        result = Workbench(self.db, page_fetcher=fetcher).related_search(
            draft_id, selection, 0, len(selection)
        )

        self.assertTrue(result["ok"])
        self.assertEqual("ok", result["search_status"])
        self.assertEqual(derive_related_query(selection).text, result["query"])
        self.assertEqual("https://news.example/chip", result["items"][0]["url"])
        self.assertEqual("", result["cause_code"])

    def test_related_search_returns_more_than_one_google_hit(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        rss = (
            "<rss><channel>"
            "<item><title>英伟达芯片一</title><link>https://news.a.example/one</link><description>一</description></item>"
            "<item><title>英伟达芯片二</title><link>https://news.b.example/two</link><description>二</description></item>"
            "</channel></rss>"
        ).encode("utf-8")

        def fetcher(url: str) -> bytes:
            return rss if "news.google.com/rss" in url else b""

        result = Workbench(self.db, page_fetcher=fetcher).related_search(
            draft_id, selection, 0, len(selection)
        )
        self.assertEqual(2, len(result["items"]))
        self.assertEqual("https://news.a.example/one", result["items"][0]["url"])
        self.assertEqual("https://news.b.example/two", result["items"][1]["url"])
        self.assertFalse(result["query_degraded"])
        self.assertEqual(len(selection), result["query_source_chars"])

    def test_related_search_empty_result_is_success_not_failure(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        empty_feed = b"<rss><channel><title>no items</title></channel></rss>"

        result = Workbench(self.db, page_fetcher=lambda _url: empty_feed).related_search(
            draft_id, selection, 0, len(selection)
        )

        self.assertTrue(result["ok"])
        self.assertEqual("empty", result["search_status"])
        self.assertEqual("no_qualified_result", result["cause_code"])
        self.assertEqual([], result["items"])

    def test_related_search_maps_graded_failures_to_cause_codes(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")

        def unreachable(_url: str) -> bytes:
            raise OSError("dns lookup failed")

        def unparsable(_url: str) -> bytes:
            return b'{"error": "rate limited"}'

        for cause, fetcher, expected_reason in (
            ("transport_unavailable", unreachable, "网络不可达"),
            ("parse_error", unparsable, "解析失败"),
        ):
            with self.subTest(cause):
                workbench = Workbench(self.db, page_fetcher=fetcher)
                with self.assertLogs("autoposting.workbench", level="WARNING") as logs:
                    with self.assertRaises(LLMError) as raised:
                        workbench.related_search(draft_id, selection, 0, len(selection))
                # The top-level API code must never change.
                self.assertEqual("related_search_failed", str(raised.exception))
                self.assertEqual(cause, raised.exception.cause_code)
                self.assertIn(expected_reason, raised.exception.reason)
                self.assertIn(cause, "\n".join(logs.output))

    def test_related_search_timeout_is_graded_and_logged(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")

        def slow(_url: str) -> bytes:
            time.sleep(0.2)
            return b"<rss><channel /></rss>"

        workbench = Workbench(self.db, page_fetcher=slow)
        with patch("autoposting.background._RELATED_SEARCH_BUDGET_SECONDS", 0.05):
            with self.assertLogs("autoposting.workbench", level="WARNING"):
                with self.assertRaises(LLMError) as raised:
                    workbench.related_search(draft_id, selection, 0, len(selection))

        self.assertEqual("related_search_failed", str(raised.exception))
        self.assertEqual("timeout", raised.exception.cause_code)
        self.assertIn("检索超时", raised.exception.reason)

    def test_related_search_reason_does_not_leak_upstream_text(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        secret = "secret upstream failure"

        def fetcher(_url: str) -> bytes:
            raise RuntimeError(secret)

        workbench = Workbench(self.db, page_fetcher=fetcher)
        with self.assertLogs("autoposting.workbench", level="WARNING"):
            with self.assertRaises(LLMError) as raised:
                workbench.related_search(draft_id, selection, 0, len(selection))

        self.assertNotIn(secret, raised.exception.reason)
        self.assertIn("网络不可达", raised.exception.reason)

    def test_related_search_failure_log_never_carries_credentials(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        secret = "SUPERSECRETVALUE"
        # The verifier's reproduced upstream text, credential and all.
        upstream = (
            "ddg: ConnectionRefusedError: SECRET-UPSTREAM-BODY-"
            f"http://internal.example/token={secret}"
        )

        def fetcher(_url: str) -> bytes:
            raise ConnectionRefusedError(upstream)

        workbench = Workbench(self.db, page_fetcher=fetcher)
        with self.assertLogs("autoposting.workbench", level="WARNING") as logs:
            with self.assertRaises(LLMError) as raised:
                workbench.related_search(draft_id, selection, 0, len(selection))

        logged = "\n".join(logs.output)
        self.assertIn("related search failed", logged)
        self.assertNotIn(secret, logged)
        self.assertNotIn("SECRET-UPSTREAM-BODY", logged)
        self.assertIn("token=***", logged)
        self.assertIn("internal.example", logged)
        # The HTTP-facing error stays the graded Chinese reason.
        self.assertEqual("related_search_failed", str(raised.exception))
        self.assertEqual("transport_unavailable", raised.exception.cause_code)
        self.assertNotIn(secret, raised.exception.reason)
        self.assertNotIn("SECRET-UPSTREAM-BODY", raised.exception.reason)

    def test_related_search_crash_log_never_carries_credentials(self) -> None:
        selection = "英伟达发布新一代AI芯片"
        draft_id = self._related_draft(f"{selection}，性能大幅提升。")
        secret = "SUPERSECRETVALUE"

        workbench = Workbench(self.db, page_fetcher=lambda _url: b"")
        with patch(
            "autoposting.workbench.search_related_news",
            side_effect=RuntimeError(
                f"upstream said https://api.example/v1?api_key={secret}"
            ),
        ):
            with self.assertLogs("autoposting.workbench", level="WARNING") as logs:
                with self.assertRaises(LLMError) as raised:
                    workbench.related_search(draft_id, selection, 0, len(selection))

        logged = "\n".join(logs.output)
        self.assertIn("related search crashed", logged)
        self.assertNotIn(secret, logged)
        self.assertIn("api_key=***", logged)
        self.assertEqual("related_search_failed", str(raised.exception))
        self.assertNotIn(secret, raised.exception.reason)

    def test_related_search_reports_degraded_long_selection_query(self) -> None:
        paragraph = (
            "英伟达在大会上发布了新一代AI芯片，这款芯片采用了全新的架构设计，"
            "在推理性能上比上一代产品提升了两倍以上。分析师认为，随着数据中心对算力的需求持续增长，"
            "这款芯片预计将推动公司下一季度的营收增长。与此同时，竞争对手也在加快自研芯片的节奏，"
            "整个行业的竞争格局正在发生变化，供应链厂商同样受到关注。业内普遍预计，"
            "未来几个季度相关产品的出货量还会继续攀升，下游客户也在调整采购计划，"
            "多家机构上调了行业增速预期，但同时也提醒投资者关注供需波动带来的风险。"
        )
        draft_id = self._related_draft(paragraph)
        rss = (
            "<rss><channel><item>"
            "<title>英伟达发布新一代AI芯片</title>"
            "<link>https://news.example/ai-chip</link>"
            "<description>英伟达 AI 芯片报道</description>"
            "</item></channel></rss>"
        ).encode("utf-8")

        result = Workbench(self.db, page_fetcher=lambda _url: rss).related_search(
            draft_id, paragraph, 0, len(paragraph)
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["query_degraded"])
        self.assertEqual("degraded_leading_clause", result["query_strategy"])
        self.assertEqual(len(paragraph), result["query_source_chars"])
        self.assertLess(len(result["query"]), len(paragraph))
        self.assertIn("英伟达", result["query"])
        self.assertEqual("https://news.example/ai-chip", result["items"][0]["url"])


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
        if "material/add_material" in self.path:
            WechatStubHandler.requests.append(("POST", self.path, raw[:40]))
            self._send({"media_id": "stub-permanent-1", "url": "https://mmbiz.qpic.cn/stub-permanent"})
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

    def test_publish_stops_at_draft_add_without_freepublish(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            "测试作者",
            base_url=self.base_url,
            poster_renderer=lambda _title: b"",
        )

        result = publisher.publish(self._draft())

        self.assertEqual("DRAFT_SAVED", result["status"])
        self.assertEqual("stub-media-1", result["reference"])
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
        self.assertFalse(
            any("/cgi-bin/freepublish/submit" in item[1] for item in WechatStubHandler.requests)
        )

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

    def _asset_draft(
        self,
        *,
        cover_id: str = "c1",
        body_ids: list[str] | None = None,
        candidates: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        rows = candidates or [
            {
                "id": "c1",
                "url": "https://lab.example/hero.jpg",
                "source_id": "lab_blog",
                "width": 900,
                "height": 1200,
            },
            {
                "id": "b1",
                "url": "https://lab.example/body.jpg",
                "source_id": "lab_blog",
                "width": 750,
                "height": 1000,
            },
        ]
        return {
            "title": "配图稿",
            "body": "正文\nAIGC",
            "assets": {
                "schema": "article-assets-v2",
                "candidates": rows,
                "cover_id": cover_id,
                "body_ids": list(body_ids if body_ids is not None else ["b1"]),
            },
        }

    def test_publish_uploads_source_images_and_embeds_three_by_four_figures(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            image_fetcher=lambda _url: b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,
            base_url=self.base_url,
            poster_renderer=lambda _title: b"POSTER-SHOULD-NOT-RUN",
        )
        draft = self._asset_draft()

        result = publisher.publish(draft)

        self.assertEqual("DRAFT_SAVED", result["status"])
        paths = [item[1] for item in WechatStubHandler.requests]
        self.assertTrue(any("/cgi-bin/media/uploadimg" in path for path in paths))
        self.assertTrue(any("material/add_material" in path and "type=image" in path for path in paths))
        self.assertFalse(any("/cgi-bin/media/upload?" in path and "type=thumb" in path for path in paths))
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("stub-permanent-1", article["thumb_media_id"])
        content = article["content"]
        self.assertIn("https://mmbiz.qpic.cn/stub-uploaded", content)
        self.assertIn("aspect-ratio:3/4", content)
        self.assertIn('<a href="https://lab.example/body.jpg">', content)
        self.assertNotIn("来源：lab_blog", content)
        self.assertNotIn("https://lab.example/hero.jpg", content)

    def test_publish_digest_uses_best_article_paragraph_and_strips_image_credits(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            base_url=self.base_url,
            poster_renderer=lambda _title: b"",
        )
        draft = {
            "title": "配图稿",
            "body": (
                "导语：这是一段足够精彩的摘要内容，用于展示文章核心进展和读者最关心的事实。\n"
                "来源 lab_blog · https://lab.example/article\n"
                "AIGC 声明：本文由自动化工具辅助生成。\n\n"
                "配图（来自原文，非 AI 生成）\n"
                "封面：来源 lab_blog · https://lab.example/hero.jpg"
            ),
        }

        publisher.publish(draft)

        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual(
            "导语：这是一段足够精彩的摘要内容，用于展示文章核心进展和读者最关心的事实。",
            article["digest"],
        )
        self.assertLessEqual(len(article["digest"]), 120)
        self.assertNotIn("来自原文", article["content"])

    def test_publish_preserves_safe_markdown_and_html_links(self) -> None:
        publisher = WeChatPublisher("wx_app_id", "s3cr3t-value", "thumb-1")

        content = publisher._filled_content(
            {
                "title": "链接稿",
                "body": (
                    "官方公告[详细说明](https://official.example/article)，"
                    '<a href="https://existing.example/page">已有链接</a>。'
                ),
            },
            "stub-token",
        )

        self.assertIn(
            '<a href="https://official.example/article">详细说明</a>', content
        )
        self.assertIn(
            '<a href="https://existing.example/page">已有链接</a>', content
        )
        self.assertNotIn("&lt;a", content)

    def test_publish_uses_selected_cover_and_skips_title_poster(self) -> None:
        posters: list[str] = []
        fetched: list[str] = []

        def fetch(url: str) -> bytes:
            fetched.append(url)
            return b"\xff\xd8" + b"\x00" * 16

        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "",
            image_fetcher=fetch,
            base_url=self.base_url,
            poster_renderer=lambda title: posters.append(str(title)) or b"POSTER",
        )

        result = publisher.publish(self._asset_draft(body_ids=[]))

        self.assertEqual("DRAFT_SAVED", result["status"])
        self.assertEqual(["https://lab.example/hero.jpg"], fetched)
        self.assertEqual([], posters)
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("stub-permanent-1", article["thumb_media_id"])

    def test_publish_uses_selected_body_image_when_cover_missing_not_poster(self) -> None:
        posters: list[str] = []
        fetched: list[str] = []

        def fetch(url: str) -> bytes:
            fetched.append(url)
            return b"\xff\xd8" + b"\x00" * 16

        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "",
            image_fetcher=fetch,
            base_url=self.base_url,
            poster_renderer=lambda title: posters.append(str(title)) or b"POSTER",
        )

        result = publisher.publish(self._asset_draft(cover_id="", body_ids=["b1"]))

        self.assertEqual("DRAFT_SAVED", result["status"])
        self.assertEqual(["https://lab.example/body.jpg"], list(dict.fromkeys(fetched)))
        self.assertEqual([], posters)

    def test_publish_does_not_replace_selected_cover_with_poster_if_fetch_fails(self) -> None:
        posters: list[str] = []

        def fetch(_url: str) -> bytes:
            raise OSError("hotlink blocked")

        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            image_fetcher=fetch,
            base_url=self.base_url,
            poster_renderer=lambda title: posters.append(str(title)) or b"POSTER",
        )

        result = publisher.publish(self._asset_draft(body_ids=[]))

        self.assertEqual("DRAFT_SAVED", result["status"])
        self.assertEqual([], posters)
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("thumb-1", article["thumb_media_id"])

    def test_publish_without_cover_uploads_title_poster(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "",
            base_url=self.base_url,
            poster_renderer=lambda title: b"\x89PNG\r\n\x1a\n" + title.encode("utf-8"),
        )

        result = publisher.publish(self._draft())

        self.assertEqual("DRAFT_SAVED", result["status"])
        self.assertTrue(
            any("material/add_material" in item[1] for item in WechatStubHandler.requests)
        )
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("stub-permanent-1", article["thumb_media_id"])

    def test_publish_uses_fallback_thumb_when_poster_fails(self) -> None:
        def boom(_title: str) -> bytes:
            raise RuntimeError("no chrome")

        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "thumb-1",
            base_url=self.base_url,
            poster_renderer=boom,
        )

        result = publisher.publish(self._draft())

        self.assertEqual("DRAFT_SAVED", result["status"])
        add_body = next(item[2] for item in WechatStubHandler.requests if "/cgi-bin/draft/add" in item[1])
        article = json.loads(add_body)["articles"][0]
        self.assertEqual("thumb-1", article["thumb_media_id"])

    def test_publish_without_cover_or_poster_or_fallback_fails(self) -> None:
        publisher = WeChatPublisher(
            "wx_app_id",
            "s3cr3t-value",
            "",
            base_url=self.base_url,
            poster_renderer=lambda _title: b"",
        )

        with self.assertRaisesRegex(PublicationError, "wechat_cover_required"):
            publisher.publish(self._draft())


if __name__ == "__main__":
    unittest.main()
