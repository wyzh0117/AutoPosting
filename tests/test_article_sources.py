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
        self.assertNotIn("https://lab.example/two", framed)
        # The repeat still must not be guessed at ... but a bare machine id is
        # not prose either: it is dropped, and the source panel carries the
        # second source record.  What matters is that the text stays readable.
        self.assertNotIn("lab_blog", framed)
        self.assertNotIn("，。", framed)

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


class LeftoverSourceMarkerTests(unittest.TestCase):
    """A source id is a machine token; it must never reach the reader as prose."""

    RECORDS = [
        {"source_id": "openai_news", "source_url": "https://openai.com/index/x", "name": "OpenAI"},
        {"source_id": "reddit_openai", "source_url": "https://www.reddit.com/r/OpenAI/comments/1/", "name": "r/OpenAI"},
    ]

    def test_second_mention_is_dropped_but_the_link_survives(self) -> None:
        framed = frame_article_body(
            "此前定位最低的 Terra 型号不再提供 reddit_openai。\n"
            "社区在讨论 Luna reddit_openai 的性价比。",
            self.RECORDS,
            inline_sources=True,
        )
        self.assertNotIn("reddit_openai", framed)
        self.assertEqual(1, framed.count("](https://www.reddit.com/r/OpenAI/comments/1/)"))
        # The cut must not leave a hole or a double space behind.
        self.assertNotIn("提供 。", framed)
        self.assertNotIn("  ", framed)

    def test_a_clause_that_cannot_be_linked_still_loses_its_token(self) -> None:
        framed = frame_article_body(
            "开头 openai_news 就提到它。", self.RECORDS, inline_sources=True
        )
        self.assertNotIn("openai_news", framed)
        self.assertIn("](https://openai.com/index/x)", framed)

    def test_occurrence_suffixed_ids_are_dropped_too(self) -> None:
        framed = frame_article_body(
            "社区在讨论 Luna reddit_openai 的性价比，也有人对比 reddit_openai#2 的旧帖。",
            self.RECORDS,
            inline_sources=True,
        )
        self.assertNotIn("reddit_openai", framed)
        self.assertEqual(1, framed.count("]("))

    def test_a_link_is_never_stripped(self) -> None:
        framed = frame_article_body(
            "官方发布了 openai_news 的新模型。", self.RECORDS, inline_sources=True
        )
        self.assertIn("[官方发布了](https://openai.com/index/x) 的新模型。", framed)

    def test_without_inline_sources_the_ids_are_left_alone(self) -> None:
        # The manual/discussion paths write a source tail instead of inline
        # links, so the token stays part of that tail.
        framed = frame_article_body(
            "正文提到 openai_news 一次。", self.RECORDS
        )
        self.assertIn("openai_news", framed)

if __name__ == "__main__":
    unittest.main()


class SentenceInitialCitationTests(unittest.TestCase):
    """A citation at the start of a sentence still becomes a link."""

    def test_a_sentence_initial_token_links_the_clause_after_it(self) -> None:
        body = "hn_frontpage 页面显示，新模型把输入价格降到每百万 token 2 美元。"
        framed = frame_article_body(
            body,
            [{"source_id": "hn_frontpage", "name": "Hacker News", "source_url": "https://hn.example/1"}],
            inline_sources=True,
        )
        self.assertNotIn("hn_frontpage", framed)
        self.assertIn("[页面显示，新模型把输入价格降到每百万 token 2 美元](https://hn.example/1)", framed)

    def test_a_label_word_still_links_the_clause_after_it(self) -> None:
        body = "来源：openai_news 官方说明把价格下调了 50%。"
        framed = frame_article_body(
            body,
            [{"source_id": "openai_news", "name": "OpenAI News", "source_url": "https://openai.example/1"}],
            inline_sources=True,
        )
        self.assertNotIn("openai_news", framed)
        self.assertIn("(https://openai.example/1)", framed)

    def test_a_link_label_never_swallows_another_source_token(self) -> None:
        # Both ids share one url, so only the first gets a link record; the other
        # must leave the prose instead of being printed inside the label.
        body = "两个新模型 openai_news：Sol 和 Luna hn_frontpage。"
        framed = frame_article_body(
            body,
            [
                {"source_id": "hn_frontpage", "name": "Hacker News", "source_url": "https://openai.example/1"},
                {"source_id": "openai_news", "name": "OpenAI News", "source_url": "https://openai.example/1"},
            ],
            inline_sources=True,
        )
        for source_id in ("hn_frontpage", "openai_news"):
            self.assertNotIn(source_id, framed)
        self.assertNotIn(" ：", framed)
        self.assertIn("](https://openai.example/1)", framed)

    def test_an_unknown_source_id_still_leaves_the_prose(self) -> None:
        # The model occasionally cites a source that is not in this article's
        # records; the caller passes the environment's ids so it is cleaned too.
        body = "社区讨论热烈 reddit_openai，但这次发布是官方口径 openai_news。"
        framed = frame_article_body(
            body,
            [{"source_id": "openai_news", "name": "OpenAI News", "source_url": "https://openai.example/1"}],
            inline_sources=True,
            extra_source_ids=("reddit_openai",),
        )
        self.assertNotIn("reddit_openai", framed)
        self.assertNotIn("openai_news", framed)
        self.assertIn("](https://openai.example/1)", framed)

    def test_a_source_line_that_cannot_be_inlined_becomes_a_reference_list(self) -> None:
        # The model sometimes lists its sources on one line instead of naming
        # them in the prose.  That line is dropped, so the sources must still be
        # reachable — and the draft must still carry a url an edit can validate.
        body = "hn_frontpage openai_news\n\n文章正文讨论了价格下调与后续影响。"
        framed = frame_article_body(
            body,
            [
                {"source_id": "hn_frontpage", "name": "Hacker News", "source_url": "https://hn.example/1"},
                {"source_id": "openai_news", "name": "OpenAI News", "source_url": "https://openai.example/1"},
            ],
            inline_sources=True,
        )
        self.assertNotIn("hn_frontpage ", framed.split("原文链接")[0])
        self.assertIn("原文链接", framed)
        self.assertIn("- Hacker News（hn_frontpage）：https://hn.example/1", framed)
        self.assertIn("- OpenAI News（openai_news）：https://openai.example/1", framed)

    def test_an_inlined_source_does_not_add_a_reference_list(self) -> None:
        body = "社区讨论很热烈 reddit_openai，官方随后更新了价格页。"
        framed = frame_article_body(
            body,
            [
                {"source_id": "reddit_openai", "name": "Reddit r/OpenAI", "source_url": "https://reddit.example/1"},
            ],
            inline_sources=True,
        )
        self.assertNotIn("原文链接", framed)
        self.assertIn("](https://reddit.example/1)", framed)
        self.assertNotIn("reddit_openai", framed)

    def test_a_source_without_a_url_adds_nothing_to_the_reference_list(self) -> None:
        body = "正文只提到了一次 lab_blog。"
        framed = frame_article_body(
            body,
            [{"source_id": "lab_blog", "name": "Lab Blog", "source_url": ""}],
            inline_sources=True,
        )
        self.assertNotIn("原文链接", framed)
        self.assertNotIn("lab_blog", framed)

    def test_a_label_line_citation_still_keeps_the_source_reachable(self) -> None:
        # A model may cite its source on its own label line instead of in prose.
        # That line is unreadable and is removed, but the article must not lose
        # its attribution — otherwise the draft can never be saved again.
        framed = frame_article_body(
            "实验室发布了模型一。\n来源：openai_news\nAIGC 声明：本文由 AI 辅助生成。",
            [{"source_id": "openai_news", "name": "OpenAI News", "source_url": "https://openai.example/1"}],
            inline_sources=True,
        )
        self.assertNotIn("来源：openai_news", framed)
        self.assertIn("原文链接", framed)
        self.assertIn("https://openai.example/1", framed)

    def test_a_label_line_removes_a_decoy_url_but_keeps_the_source_url(self) -> None:
        framed = frame_article_body(
            "正文事实。\n图片来源：lab_blog · https://lab.example/image.jpg\n来源：lab_blog\nAIGC",
            [{"source_id": "lab_blog", "name": "Lab Blog", "source_url": "https://lab.example/article"}],
            inline_sources=True,
        )
        self.assertNotIn("https://lab.example/image.jpg", framed)
        self.assertIn("https://lab.example/article", framed)
