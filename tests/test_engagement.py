import unittest

from autoposting.engagement import (
    Engagement,
    effective_tier,
    extract_hn_id,
    frame_article_body,
    is_promoted,
)
from autoposting.ingestion import normalize_x_handle


class EngagementTests(unittest.TestCase):
    def test_hn_threshold_promotes_s4_to_s3(self) -> None:
        cold = Engagement(score=10, comments=4)
        hot = Engagement(score=80, comments=12)
        self.assertFalse(is_promoted("hn", cold))
        self.assertTrue(is_promoted("hn", hot))
        self.assertEqual("S4", effective_tier("S4", cold, "hn"))
        self.assertEqual("S3", effective_tier("S4", hot, "hn"))
        self.assertEqual("S1", effective_tier("S1", cold, "hn"))

    def test_x_threshold_uses_likes_and_reposts(self) -> None:
        self.assertFalse(is_promoted("x", Engagement(likes=20, reposts=3, replies=2)))
        self.assertTrue(is_promoted("x", Engagement(likes=80, reposts=60, replies=10)))
        self.assertTrue(is_promoted("x", Engagement(likes=180, reposts=10, replies=20)))

    def test_frame_article_puts_counts_first_and_links_last(self) -> None:
        body = frame_article_body(
            "导语。\nAIGC 声明。",
            [
                {
                    "source_id": "hn_frontpage",
                    "name": "Hacker News",
                    "source_url": "https://news.ycombinator.com/item?id=1",
                }
            ],
            discussion=True,
        )
        self.assertTrue(body.startswith("【信息来源 1 条 · 原文 1 篇】"))
        self.assertIn("原文链接", body)
        self.assertIn("https://news.ycombinator.com/item?id=1", body)
        self.assertIn("讨论提示", body)
        self.assertGreater(body.find("原文链接"), body.find("导语"))

    def test_extract_hn_id_and_x_handle(self) -> None:
        self.assertEqual("421", extract_hn_id("https://news.ycombinator.com/item?id=421"))
        self.assertEqual("sama", normalize_x_handle("@sama"))
        self.assertEqual("karpathy", normalize_x_handle("https://x.com/karpathy"))


if __name__ == "__main__":
    unittest.main()
