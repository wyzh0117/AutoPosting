import unittest

from autoposting.xhs_cards import (
    XHS_CARD_HEIGHT,
    XHS_CARD_WIDTH,
    XHS_CAPTION_FULL_LIMIT,
    XHS_MAX_CARDS,
    XHS_PLACEHOLDER_ID,
    build_xhs_cards,
    fill_xhs_caption,
    first_paragraph,
    paginate_text,
    visible_len,
)


class XiaohongshuCardTests(unittest.TestCase):
    def test_card_size_is_9_by_16(self) -> None:
        self.assertEqual(1080, XHS_CARD_WIDTH)
        self.assertEqual(1920, XHS_CARD_HEIGHT)
        self.assertEqual(18, XHS_MAX_CARDS)

    def test_cover_uses_image_when_present(self) -> None:
        cards = build_xhs_cards(
            "模型一发布",
            "第一段说明发生了什么。\n\n第二段补充背景。\n\n第三段收尾。",
            cover_url="https://lab.example/cover.jpg",
        )
        self.assertEqual(1080, cards["width"])
        self.assertEqual(1920, cards["height"])
        self.assertEqual(XHS_PLACEHOLDER_ID, cards["handle"])
        cover = cards["cards"][0]
        self.assertEqual("cover", cover["kind"])
        self.assertEqual("模型一发布", cover["title"])
        self.assertEqual("https://lab.example/cover.jpg", cover["image_url"])
        self.assertEqual("", cover.get("lede", ""))
        self.assertEqual(1080, cover["width"])
        self.assertEqual(1920, cover["height"])
        self.assertEqual(XHS_PLACEHOLDER_ID, cover["handle"])
        self.assertEqual("", cover["avatar"])
        rest = "\n".join(page["text"] for page in cards["cards"][1:])
        self.assertIn("第一段说明发生了什么。", rest)
        self.assertIn("第二段补充背景。", rest)

    def test_cover_falls_back_to_title_and_first_paragraph(self) -> None:
        cards = build_xhs_cards("模型一发布", "开场这一句。\n\n后文继续讲细节。")
        cover = cards["cards"][0]
        self.assertEqual("cover", cover["kind"])
        self.assertEqual("模型一发布", cover["title"])
        self.assertEqual("开场这一句。", cover["lede"])
        self.assertEqual("", cover.get("image_url", ""))
        self.assertEqual("后文继续讲细节。", cards["cards"][1]["text"])

    def test_account_name_becomes_handle_placeholder_avatar_stays_empty(self) -> None:
        cards = build_xhs_cards("标题", "一段正文。", handle="实验室笔记")
        self.assertEqual("实验室笔记", cards["handle"])
        self.assertEqual("实验室笔记", cards["cards"][0]["handle"])
        self.assertEqual("", cards["cards"][0]["avatar"])

    def test_long_manuscript_is_paginated_and_capped(self) -> None:
        pages = paginate_text("。".join(f"第{i}句" for i in range(80)) + "。", limit=40)
        self.assertGreater(len(pages), 2)
        self.assertTrue(all(len(page) <= 40 for page in pages))
        manuscript = "\n\n".join(f"段落{index}：" + ("详情" * 40) for index in range(30))
        cards = build_xhs_cards("长稿", manuscript)
        self.assertLessEqual(len(cards["cards"]), XHS_MAX_CARDS)
        self.assertEqual("page", cards["cards"][1]["kind"])

    def test_first_paragraph_stops_at_blank_line(self) -> None:
        self.assertEqual("开场。", first_paragraph("开场。\n\n后文。"))
        self.assertEqual("单段", first_paragraph("单段"))
        self.assertEqual("", first_paragraph("  \n "))


class XiaohongshuCaptionFallbackTests(unittest.TestCase):
    def test_short_article_without_caption_quotes_full_body(self) -> None:
        body = "官方确认模型一已经上线。lab_blog AIGC"
        caption = fill_xhs_caption(body, ("lab_blog",), caption=None)
        self.assertLessEqual(visible_len(body), XHS_CAPTION_FULL_LIMIT)
        self.assertIn("官方确认模型一已经上线", caption)
        self.assertIn("lab_blog", caption)
        self.assertIn("AIGC", caption)

    def test_long_article_without_caption_uses_highlights(self) -> None:
        body = "开场把事情说清楚。" + ("详情补充。" * 120) + "\n\n第二段讲影响。\n\n第三段讲未知项。lab_blog AIGC"
        self.assertGreater(visible_len(body), XHS_CAPTION_FULL_LIMIT)
        caption = fill_xhs_caption(body, ("lab_blog",), caption=None)
        self.assertLess(visible_len(caption), visible_len(body))
        self.assertIn("开场把事情说清楚", caption)
        self.assertRegex(caption, r"(?m)^-\s+\S")
        self.assertIn("lab_blog", caption)
        self.assertIn("AIGC", caption)

    def test_long_article_keeps_valid_model_caption(self) -> None:
        body = "长稿" * 280 + " lab_blog AIGC"
        given = "这件事刚被公开。\n- 官方发了模型一\n来源 lab_blog\nAIGC"
        caption = fill_xhs_caption(body, ("lab_blog",), caption=given)
        self.assertEqual(given, caption)

    def test_short_article_prefers_full_quote_over_model_caption(self) -> None:
        body = "短讯已经发生。lab_blog AIGC"
        given = "这件事刚被公开。\n- 官方发了模型一\n来源 lab_blog\nAIGC"
        caption = fill_xhs_caption(body, ("lab_blog",), caption=given)
        self.assertIn("短讯已经发生", caption)
        self.assertNotIn("这件事刚被公开", caption)
