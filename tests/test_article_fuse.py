import json
import unittest

from autoposting.article_fuse import fuse_hotspot_items
from autoposting.article_prompt import build_article_prompt


class ArticleFuseTests(unittest.TestCase):
    def test_fuse_two_items_into_one_article_payload(self) -> None:
        fused = fuse_hotspot_items(
            [
                {
                    "item_id": "a",
                    "title": "Alpha launch",
                    "content_text": "Alpha body from lab",
                    "source_ids": ["lab_blog"],
                },
                {
                    "item_id": "b",
                    "title": "Beta debate",
                    "summary": "Beta body from media",
                    "source_ids": ["media_feed", "lab_blog"],
                },
            ]
        )

        self.assertIsInstance(fused["title"], str)
        self.assertTrue(fused["title"])
        body = str(fused.get("content_text") or fused.get("summary") or "")
        self.assertIn("Alpha launch", body)
        self.assertIn("Alpha body from lab", body)
        self.assertIn("Beta debate", body)
        self.assertIn("Beta body from media", body)
        self.assertEqual(["lab_blog", "media_feed"], list(fused["source_ids"]))

        prompt = build_article_prompt(
            "wechat",
            tuple(fused["source_ids"]),
            {
                "title": fused["title"],
                "summary": body,
            },
        )
        payload, _ = json.JSONDecoder().raw_decode(prompt.rsplit("数据：\n", 1)[1].strip())
        self.assertEqual(fused["title"], payload["title"])
        self.assertIn("Alpha body from lab", payload["summary"])
        self.assertIn("Beta body from media", payload["summary"])
        self.assertNotIn("item_ids", payload)
