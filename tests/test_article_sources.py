import json
import tempfile
import types
import unittest
from pathlib import Path

from autoposting.db import Database
from autoposting.engagement import frame_article_body
from autoposting.llm import LLMService
from autoposting.models import FeedItem, Source
from autoposting.workbench import Workbench


class _InlineArticleModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> object:
        self.calls.append(argv)
        return types.SimpleNamespace(
            stdout=json.dumps(
                {
                    "title": "模型一正式发布",
                    "body": (
                        "官方实验室发布了模型一，lab_blog。\n"
                        "媒体随后解释了影响，media_feed。\n"
                        "AIGC 声明：本文由 AI 辅助生成。\n"
                        "来源索引：lab_blog, media_feed"
                    ),
                },
                ensure_ascii=False,
            ),
            stderr="",
            returncode=0,
        )


class ArticleSourceLinkTests(unittest.TestCase):
    def test_inline_frame_links_first_reference_and_removes_source_tail(self) -> None:
        body = (
            "官方实验室发布了模型一，lab_blog。\n"
            "媒体随后解释了影响，media_feed。\n"
            "来源索引：lab_blog, media_feed\n"
            "AIGC 声明：本文由 AI 辅助生成。"
        )
        framed = frame_article_body(
            body,
            [
                {
                    "source_id": "lab_blog",
                    "source_url": "https://lab.example/article",
                },
                {
                    "source_id": "media_feed",
                    "source_url": "https://media.example/article",
                },
            ],
            inline_sources=True,
        )

        self.assertIn("[官方实验室发布了模型一](https://lab.example/article)。", framed)
        self.assertIn("[媒体随后解释了影响](https://media.example/article)。", framed)
        self.assertNotIn("来源索引", framed)
        self.assertNotIn("原文链接", framed)
        self.assertNotIn("信息来源", framed)

    def test_existing_markdown_link_is_preserved(self) -> None:
        body = "官方原文[链接](https://official.example/article)已说明，lab_blog。\nAIGC"
        framed = frame_article_body(
            body,
            [{"source_id": "lab_blog", "source_url": "https://lab.example/article"}],
            inline_sources=True,
        )

        self.assertIn("[链接](https://official.example/article)", framed)
        self.assertIn("[已说明](https://lab.example/article)", framed)

    def test_source_word_in_normal_prose_is_not_deleted(self) -> None:
        framed = frame_article_body(
            "来源：lab_blog 的发布说明显示模型已上线。\nAIGC",
            [{"source_id": "lab_blog", "source_url": "https://lab.example/article"}],
            inline_sources=True,
        )

        self.assertIn(
            "[的发布说明显示模型已上线](https://lab.example/article)", framed
        )

    def test_invalid_source_url_is_not_emitted_as_a_link(self) -> None:
        framed = frame_article_body(
            "正文事实，lab_blog。\nAIGC",
            [{"source_id": "lab_blog", "source_url": "javascript:alert(1)"}],
            inline_sources=True,
        )

        self.assertNotIn("javascript:", framed)
        self.assertNotIn("[", framed)

    def test_source_labels_are_not_link_anchors_and_url_tail_is_removed(self) -> None:
        framed = frame_article_body(
            "原文：lab_blog\n"
            "图片来源：lab_blog · https://lab.example/image.jpg\n"
            "原文链接：https://lab.example/article\n"
            "AIGC",
            [{"source_id": "lab_blog", "source_url": "https://lab.example/article"}],
            inline_sources=True,
        )

        self.assertNotIn("[原文]", framed)
        self.assertNotIn("图片来源", framed)
        self.assertNotIn("原文链接", framed)
        self.assertNotIn("https://lab.example/image.jpg", framed)

    def test_duplicate_source_id_does_not_guess_a_second_article(self) -> None:
        framed = frame_article_body(
            "第一篇事实，lab_blog。第二篇事实，lab_blog。\nAIGC",
            [
                {"source_id": "lab_blog", "source_url": "https://lab.example/one"},
                {"source_id": "lab_blog", "source_url": "https://lab.example/two"},
            ],
            inline_sources=True,
        )

        self.assertIn("[第一篇事实](https://lab.example/one)", framed)
        self.assertIn("lab_blog", framed)
        self.assertNotIn("https://lab.example/two", framed)

    def test_duplicate_source_id_uses_unique_citation_markers_when_present(self) -> None:
        framed = frame_article_body(
            "第一篇事实，lab_blog。第二篇事实，lab_blog#2。\nAIGC",
            [
                {
                    "source_id": "lab_blog",
                    "citation_id": "lab_blog",
                    "source_url": "https://lab.example/one",
                },
                {
                    "source_id": "lab_blog",
                    "citation_id": "lab_blog#2",
                    "source_url": "https://lab.example/two",
                },
            ],
            inline_sources=True,
        )

        self.assertIn("[第一篇事实](https://lab.example/one)", framed)
        self.assertIn("[第二篇事实](https://lab.example/two)", framed)
        self.assertNotIn("lab_blog", framed)

    def test_workbench_generates_links_at_source_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            source = Source(
                "lab_blog",
                "AI Lab",
                "rss",
                "https://lab.example/feed.xml",
                "S1",
                ("models",),
                True,
                15,
            )
            second = Source(
                "media_feed",
                "AI Media",
                "rss",
                "https://media.example/feed.xml",
                "S3",
                ("models",),
                True,
                15,
            )
            db.upsert_source(source)
            db.upsert_source(second)
            db.insert_item(
                source,
                FeedItem(
                    "model-one",
                    "https://lab.example/article",
                    "Model One",
                    "Model One 内容正文",
                    None,
                    {},
                ),
            )
            item_id = str(
                db.connect()
                .execute("SELECT item_id FROM items LIMIT 1")
                .fetchone()["item_id"]
            )
            # Add the second source to the same hotspot through a duplicate title.
            db.insert_item(
                second,
                FeedItem(
                    "model-one-media",
                    "https://media.example/article",
                    "Model One",
                    "Model One 媒体正文",
                    None,
                    {},
                ),
            )
            item_ids = [
                str(row["item_id"])
                for row in db.connect().execute("SELECT item_id FROM items").fetchall()
            ]
            second_item_id = next(value for value in item_ids if value != item_id)
            model = _InlineArticleModel()
            result = Workbench(db, llm_service=LLMService(db, runner=model)).generate_article(
                item_id, "wechat", item_ids=[item_id, second_item_id]
            )

            self.assertIn("[官方实验室发布了模型一](https://lab.example/article)", result["body"])
            self.assertIn("[媒体随后解释了影响](https://media.example/article)", result["body"])
            self.assertNotIn("来源索引", result["body"])
            self.assertNotIn("原文链接", result["body"])
            self.assertNotIn("【信息来源", result["body"])
            self.assertIn('"citation_id": "lab_blog"', model.calls[0][2])
            self.assertIn('"citation_id": "media_feed"', model.calls[0][2])


if __name__ == "__main__":
    unittest.main()
