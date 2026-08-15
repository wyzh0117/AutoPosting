import tempfile
import unittest
from pathlib import Path

from autoposting.db import Database
from autoposting.ingestion import (
    AggregateCollector,
    RSSCollector,
    SitemapNewsCollector,
    XCollector,
    collect_sitemap_news,
    is_retail_or_gadget_noise,
    item_matches_filter,
    news_slug_published_at,
    parse_aggregate_page,
    parse_feed,
    parse_official_news_page,
    parse_sitemap_locs,
    should_hide_candidate,
    tweet_image_urls,
    x_user_tweets_url,
)
from autoposting.models import FeedItem, Source


RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example AI Lab</title>
    <item>
      <guid>release-1</guid>
      <title>Model One released</title>
      <link>https://example.com/news/model-one?utm_source=rss</link>
      <description><![CDATA[<p>Model One is available today.</p><img src="https://example.com/news/hero.jpg">]]></description>
      <enclosure url="https://example.com/news/cover.jpg" type="image/jpeg"/>
      <pubDate>Sat, 08 Aug 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

AIHOT_PAGE = b"""<!doctype html><html><body>
<div class="timeline-item"><article class="timeline-card"><div data-item-id="news-1">
<span class="timeline-source">OpenAI Developers</span>
<a class="timeline-title" href="/items/news-1">A very long AI news title that remains intact</a>
<p class="timeline-summary">A compact summary from the aggregate page.</p>
<div class="timeline-time">08:15</div>
</div></article></div>
</body></html>"""


DEEPSEEK_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://api-docs.deepseek.com/guides/tool_calls</loc></url>
  <url><loc>https://api-docs.deepseek.com/news/news1226</loc></url>
  <url><loc>https://api-docs.deepseek.com/news/news260813</loc></url>
  <url><loc>https://api-docs.deepseek.com/updates</loc></url>
</urlset>
"""

DEEPSEEK_NEWS_PAGE = b"""<!doctype html><html><head>
<title>DeepSeek-V4-Pro GA Release | DeepSeek API Docs</title>
<meta name="description" content="The GA release of DeepSeek-V4-Pro is live on App, Web, and API.">
</head><body><article><p>Official successor details.</p></article></body></html>"""


class IngestionTests(unittest.TestCase):
    def test_deepseek_official_sitemap_becomes_dated_feed_items(self) -> None:
        self.assertEqual(
            "2026-08-13T00:00:00+00:00",
            news_slug_published_at("https://api-docs.deepseek.com/news/news260813"),
        )
        self.assertEqual(
            "2024-12-26T00:00:00+00:00",
            news_slug_published_at("https://api-docs.deepseek.com/news/news1226"),
        )
        locs = parse_sitemap_locs(DEEPSEEK_SITEMAP)
        self.assertEqual(4, len(locs))
        parsed = parse_official_news_page(
            DEEPSEEK_NEWS_PAGE, "https://api-docs.deepseek.com/news/news260813"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual("DeepSeek-V4-Pro GA Release", parsed.title)
        self.assertEqual("https://api-docs.deepseek.com/news/news260813", parsed.canonical_url)
        self.assertIn("GA release", parsed.content_text)

        pages = {
            "https://api-docs.deepseek.com/sitemap.xml": DEEPSEEK_SITEMAP,
            "https://api-docs.deepseek.com/news/news260813": DEEPSEEK_NEWS_PAGE,
            "https://api-docs.deepseek.com/news/news1226": (
                b"<html><head><title>Introducing DeepSeek-V3 | DeepSeek API Docs</title></head>"
                b"<body><p>V3 is open-source.</p></body></html>"
            ),
        }
        items = collect_sitemap_news(pages.__getitem__, "https://api-docs.deepseek.com/sitemap.xml")
        self.assertEqual(
            ["DeepSeek-V4-Pro GA Release", "Introducing DeepSeek-V3"],
            [item.title for item in items],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "deepseek_official",
                "DeepSeek Official",
                "sitemap",
                "https://api-docs.deepseek.com/sitemap.xml",
                "S1",
                ("models",),
                True,
                15,
            )
            db.upsert_source(source)
            result = SitemapNewsCollector(db, fetcher=pages.__getitem__).collect(source)
            self.assertEqual(2, result.inserted)
            self.assertEqual(2, db.count_items())

    def test_parse_aihot_style_aggregate_page(self) -> None:
        items = parse_aggregate_page(AIHOT_PAGE, "https://aihot.virxact.com/")

        self.assertEqual(1, len(items))
        self.assertEqual("news-1", items[0].external_id)
        self.assertEqual("A very long AI news title that remains intact", items[0].title)
        self.assertEqual("https://aihot.virxact.com/items/news-1", items[0].canonical_url)
        self.assertIn("OpenAI Developers", items[0].content_text)

    def test_aggregate_collector_inserts_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "aihot", "AIHOT", "web", "https://aihot.virxact.com/",
                "S2", ("models", "products"), True, 15,
            )
            db.upsert_source(source)

            result = AggregateCollector(db, fetcher=lambda _: AIHOT_PAGE).collect(source)

            self.assertEqual(1, result.inserted)
            self.assertEqual(1, db.count_items())

    def test_single_invalid_url_skips_only_that_entry(self) -> None:
        feed = RSS_FEED.replace(
            b"</channel>",
            b"""<item>
      <guid>release-bad</guid>
      <title>Broken link item</title>
      <link>https://example.com:99999/news</link>
      <description>This entry has an unparseable port.</description>
    </item>
    <item>
      <guid>release-2</guid>
      <title>Model Two released</title>
      <link>https://example.com/news/model-two</link>
      <description>Model Two is available today.</description>
      <pubDate>Sat, 08 Aug 2026 09:00:00 GMT</pubDate>
    </item>
  </channel>""",
        )
        items = parse_feed(feed)

        self.assertEqual(2, len(items))
        self.assertEqual({"Model One released", "Model Two released"}, {item.title for item in items})
        self.assertTrue(all(item.canonical_url for item in items))

    def test_collecting_same_feed_twice_creates_one_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                source_id="lab_blog",
                name="Example AI Lab",
                type="rss",
                url_or_handle="https://example.com/feed.xml",
                tier="S1",
                topics=("models",),
                enabled=True,
                fetch_interval_minutes=15,
            )
            db.upsert_source(source)
            collector = RSSCollector(db, fetcher=lambda _: RSS_FEED)

            first = collector.collect(source)
            second = collector.collect(source)

            self.assertEqual(1, first.inserted)
            self.assertEqual(0, second.inserted)
            self.assertEqual(1, second.duplicates)
            self.assertEqual(1, db.count_items())
            stored = parse_feed(RSS_FEED)[0]
            self.assertIn("https://example.com/news/cover.jpg", stored.raw_data["image_urls"])
            self.assertIn("https://example.com/news/hero.jpg", stored.raw_data["image_urls"])

    def test_content_duplicate_keeps_all_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            first_source = Source(
                "lab_blog", "Example AI Lab", "rss", "https://example.com/feed.xml",
                "S1", ("models",), True, 15,
            )
            second_source = Source(
                "media_feed", "Example Media", "rss", "https://media.example/feed.xml",
                "S3", ("models",), True, 15,
            )
            db.upsert_source(first_source)
            db.upsert_source(second_source)
            second_feed = RSS_FEED.replace(
                b"release-1", b"media-release-1"
            ).replace(
                b"https://example.com/news/model-one?utm_source=rss",
                b"https://media.example/model-one",
            )

            RSSCollector(db, fetcher=lambda _: RSS_FEED).collect(first_source)
            result = RSSCollector(db, fetcher=lambda _: second_feed).collect(second_source)

            self.assertEqual(0, result.inserted)
            self.assertEqual(1, db.count_items())
            self.assertEqual({"lab_blog", "media_feed"}, db.get_item_source_ids())

    def test_ai_content_filter_keeps_model_news_and_drops_phone_news(self) -> None:
        keep = FeedItem(
            "1", "https://example.com/a", "OpenAI releases GPT-5", "A new model.", None, {}
        )
        drop = FeedItem(
            "2", "https://example.com/b", "New phone available today", "Camera and battery.", None, {}
        )
        self.assertTrue(item_matches_filter(keep, "ai"))
        self.assertFalse(item_matches_filter(drop, "ai"))
        self.assertTrue(item_matches_filter(drop, ""))

    def test_ai_filter_ignores_body_only_ai_and_retail_gadgets(self) -> None:
        ad = FeedItem(
            "a",
            "https://www.ithome.com/ad",
            "OPPO Enco Clip2 耳夹耳机新配色开售，限时优惠价 809.1 元",
            "IT之家消息，支持 AI 降噪。",
            None,
            {},
        )
        car = FeedItem(
            "c",
            "https://www.ithome.com/car",
            "5.68 万元起，五菱缤果 Pro 上市 80 天累计销量突破 5 万台",
            "车机也有 AI 语音。",
            None,
            {},
        )
        digest = FeedItem(
            "d",
            "https://www.ithome.com/digest",
            "IT早报 0814：长鑫超腾讯成中国市值最高上市公司",
            "其中提到 DeepSeek 和 OpenAI。",
            None,
            {},
        )
        camera = FeedItem(
            "cam",
            "https://www.ithome.com/cam",
            "大疆 Osmo 360 II 全景相机发布：AI 超级夜景 2.0，3299 元起",
            "支持 AI 一键成片。",
            None,
            {},
        )
        harness = FeedItem(
            "h",
            "https://www.ithome.com/h",
            "对标 Claude Cowork：DeepSeek Harness 公测",
            "开源代码智能体。",
            None,
            {},
        )
        body_only = FeedItem(
            "b",
            "https://www.ithome.com/b",
            "谷歌硬件副总裁参观工厂",
            "文末出现 AI 两个字母。",
            None,
            {},
        )
        self.assertTrue(is_retail_or_gadget_noise(ad.title))
        self.assertFalse(item_matches_filter(ad, "ai"))
        self.assertFalse(item_matches_filter(car, "ai"))
        self.assertFalse(item_matches_filter(digest, "ai"))
        self.assertFalse(item_matches_filter(camera, "ai"))
        self.assertTrue(item_matches_filter(harness, "ai"))
        self.assertFalse(item_matches_filter(body_only, "ai"))
        self.assertTrue(
            should_hide_candidate("谷歌 Gboard 输入法转录 Rambler 功能将支持学习用户口语词汇", ("ithome",))
        )
        self.assertFalse(should_hide_candidate("DeepSeek Harness public preview", ("hn_frontpage",)))

    def test_ai_filter_applied_during_rss_collect(self) -> None:
        mixed = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Mixed Feed</title>
    <item>
      <guid>ai-1</guid>
      <title>OpenAI releases GPT-5</title>
      <link>https://example.com/news/gpt</link>
      <description>A new model.</description>
    </item>
    <item>
      <guid>phone-1</guid>
      <title>New phone available today</title>
      <link>https://example.com/news/phone</link>
      <description>Camera and battery.</description>
    </item>
  </channel>
</rss>
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "ithome", "IT Home", "rss", "https://example.com/rss",
                "S3", ("models",), True, 15, False, "ai",
            )
            db.upsert_source(source)
            result = RSSCollector(db, fetcher=lambda _: mixed).collect(source)
            self.assertEqual(1, result.inserted)
            self.assertEqual(1, db.count_items())

    def test_hn_comments_link_is_kept_and_score_is_merged(self) -> None:
        feed = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>AI model discussion</title>
    <link>https://example.com/story</link>
    <comments>https://news.ycombinator.com/item?id=99</comments>
    <guid>https://news.ycombinator.com/item?id=99</guid>
  </item>
</channel></rss>"""
        items = parse_feed(feed)
        self.assertEqual("99", items[0].raw_data["hn_id"])

        def fetcher(url: str) -> bytes:
            if "firebaseio" in url:
                return b'{"score": 77, "descendants": 41}'
            return feed

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "hn_frontpage", "HN", "rss", "https://news.ycombinator.com/rss",
                "S4", ("models",), True, 15,
            )
            db.upsert_source(source)
            RSSCollector(db, fetcher=fetcher).collect(source)
            stored = db.get_item(db.connect().execute("SELECT item_id FROM items").fetchone()["item_id"])
            raw = __import__("json").loads(str(stored["raw_json"]))
            self.assertEqual("77", raw["score"])
            self.assertEqual("41", raw["comments"])

    def test_x_collector_reads_official_timeline(self) -> None:
        def fetcher(url: str) -> bytes:
            if "users/by/username/sama" in url:
                return b'{"data": {"id": "1", "username": "sama"}}'
            if "users/1/tweets" in url:
                return (
                    b'{"data": [{"id": "99", "text": "New model drop", '
                    b'"created_at": "2026-08-13T00:00:00.000Z", '
                    b'"public_metrics": {"like_count": 300, "retweet_count": 80, "reply_count": 20}}]}'
                )
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source("x_sama", "X Sam", "x", "@sama", "S4", ("models",), True, 15)
            db.upsert_source(source)
            result = XCollector(db, fetcher=fetcher, bearer_token="token").collect(source)
            self.assertEqual(1, result.inserted)
            item = db.get_item(db.connect().execute("SELECT item_id FROM items").fetchone()["item_id"])
            self.assertIn("x.com/sama/status/99", str(item["canonical_url"]))

    def test_x_collector_skips_retweets_and_keeps_tweet_media(self) -> None:
        seen: list[str] = []

        def fetcher(url: str) -> bytes:
            seen.append(url)
            if "users/by/username/OpenAI" in url:
                return b'{"data": {"id": "2", "username": "OpenAI"}}'
            if "users/2/tweets" in url:
                return (
                    b'{"data":['
                    b'{"id":"10","text":"RT old news","referenced_tweets":[{"type":"retweeted","id":"1"}]},'
                    b'{"id":"11","text":"Ship day","attachments":{"media_keys":["m1"]},'
                    b'"created_at":"2026-08-15T00:00:00.000Z",'
                    b'"public_metrics":{"like_count":9,"retweet_count":1,"reply_count":0}}'
                    b'],"includes":{"media":[{"media_key":"m1","type":"photo",'
                    b'"url":"https://pbs.twimg.com/media/hero.jpg"}]}}'
                )
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source("x_openai", "X OpenAI", "x", "@OpenAI", "S1", ("models",), True, 15, True)
            db.upsert_source(source)
            result = XCollector(db, fetcher=fetcher, bearer_token="token").collect(source)
            self.assertEqual(1, result.inserted)
            item = db.get_item(db.connect().execute("SELECT item_id FROM items").fetchone()["item_id"])
            raw = __import__("json").loads(str(item["raw_json"]))
            self.assertEqual(["https://pbs.twimg.com/media/hero.jpg"], raw["image_urls"])
            self.assertIn("exclude=replies", seen[1])
            self.assertIn("attachments.media_keys", seen[1])
            self.assertIn("pbs.twimg.com/media", tweet_image_urls(
                {"attachments": {"media_keys": ["m1"]}},
                {"m1": {"url": "https://pbs.twimg.com/media/hero.jpg"}},
            )[0])
            self.assertIn("exclude=replies", x_user_tweets_url("2"))


if __name__ == "__main__":
    unittest.main()