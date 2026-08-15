import struct
import unittest

from autoposting.images import (
    IMAGE_SECTION_TITLE,
    append_image_credits,
    apply_selection,
    build_asset_bundle,
    collect_article_images,
    extract_image_urls_from_feed_fields,
    extract_image_urls_from_html,
    is_chrome_image_url,
    is_usable_content_image,
    normalize_assets,
    parse_image_size,
    select_article_images,
    selected_body_images,
    should_fetch_page_images,
    suggest_selection,
)


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


class ImageHelperTests(unittest.TestCase):
    def test_extracts_rss_enclosure_and_html_images(self) -> None:
        urls = extract_image_urls_from_feed_fields(
            page_url="https://news.example/story",
            content_html='<p>hi</p><img src="/photos/a.jpg"><img src="https://cdn.example/pixel.gif?1x1=1">',
            enclosure_url="https://cdn.example/hero.jpg",
            enclosure_type="image/jpeg",
            media_urls=["https://cdn.example/media-3x4.jpg"],
        )
        self.assertEqual(
            [
                "https://cdn.example/hero.jpg",
                "https://cdn.example/media-3x4.jpg",
                "https://news.example/photos/a.jpg",
            ],
            urls,
        )

    def test_extracts_open_graph_image_from_article_page(self) -> None:
        html = """<html><head>
        <meta property="og:image" content="https://news.example/og.jpg">
        <meta name="twitter:image" content="https://news.example/tw.jpg">
        </head><body><img src="https://news.example/body.png"></body></html>"""
        urls = extract_image_urls_from_html(html, "https://news.example/story")
        self.assertEqual(
            [
                "https://news.example/og.jpg",
                "https://news.example/tw.jpg",
                "https://news.example/body.png",
            ],
            urls,
        )

    def test_keeps_opengraph_image_without_file_extension(self) -> None:
        html = (
            '<meta property="og:image" '
            'content="https://aihot.example/items/abc/opengraph-image-1az256?hash=1">'
        )
        urls = extract_image_urls_from_html(html, "https://aihot.example/items/abc")
        self.assertEqual(
            ["https://aihot.example/items/abc/opengraph-image-1az256?hash=1"],
            urls,
        )

    def test_parse_png_size_and_prefer_three_by_four(self) -> None:
        self.assertEqual((900, 1200), parse_image_size(_png(900, 1200)))
        selected = select_article_images(
            [
                {"url": "wide", "width": 1600, "height": 900},
                {"url": "portrait", "width": 900, "height": 1200},
                {"url": "square", "width": 800, "height": 800},
                {"url": "unknown"},
                {"url": "another", "width": 750, "height": 1000},
            ]
        )
        self.assertEqual(4, len(selected))
        self.assertEqual("portrait", selected[0]["url"])
        self.assertEqual("another", selected[1]["url"])
        self.assertNotIn("wide", [item["url"] for item in selected[:2]])

    def test_drops_avatars_and_header_chrome(self) -> None:
        html = """<html><head>
        <meta property="og:image" content="https://pbs.twimg.com/profile_images/1/normal.jpg">
        <meta name="twitter:image" content="https://cdn.example/hero.jpg">
        </head><body>
        <header><img class="avatar" src="https://cdn.example/authors/jane.jpg" width="64" height="64"></header>
        <article>
          <img src="https://cdn.example/story-hero.jpg">
          <img class="author-avatar" src="https://pbs.twimg.com/profile_images/2/bigger.jpg">
        </article>
        <aside><img src="https://cdn.example/related-thumb.jpg" width="80" height="80"></aside>
        </body></html>"""
        urls = extract_image_urls_from_html(html, "https://news.example/story")
        self.assertEqual(["https://cdn.example/hero.jpg", "https://cdn.example/story-hero.jpg"], urls)
        self.assertTrue(is_chrome_image_url("https://pbs.twimg.com/profile_images/1/normal.jpg"))
        self.assertFalse(should_fetch_page_images("https://x.com/bcherny/status/1"))
        self.assertTrue(should_fetch_page_images("https://www.ithome.com/0/123.htm"))
        self.assertFalse(
            is_usable_content_image(
                {"url": "https://cdn.example/face.jpg", "width": 128, "height": 128}
            )
        )

    def test_skips_x_status_page_and_keeps_tweet_media(self) -> None:
        item = {
            "canonical_url": "https://x.com/bcherny/status/99",
            "content_text": "New model drop",
            "raw_json": (
                '{"image_urls":["https://pbs.twimg.com/media/HPoY9F3aMAASBZV.jpg",'
                '"https://pbs.twimg.com/profile_images/1/normal.jpg"]}'
            ),
        }

        def page_fetcher(url: str) -> bytes:
            raise AssertionError(f"must not scrape {url}")

        images = collect_article_images(
            item,
            [{"source_id": "x_bcherny", "source_url": "https://x.com/bcherny/status/99"}],
            page_fetcher=page_fetcher,
            probe_fetcher=lambda url: _png(1200, 800),
        )
        self.assertEqual(["https://pbs.twimg.com/media/HPoY9F3aMAASBZV.jpg"], [row["url"] for row in images])

    def test_collects_three_to_four_images_and_credits_body(self) -> None:
        item = {
            "canonical_url": "https://lab.example/a1",
            "content_text": "Model One news 内容",
            "raw_json": "{}",
        }
        probes = {
            "https://lab.example/1.jpg": _png(900, 1200),
            "https://lab.example/2.jpg": _png(800, 800),
            "https://lab.example/3.jpg": _png(750, 1000),
            "https://lab.example/4.jpg": _png(600, 800),
            "https://lab.example/5.jpg": _png(2000, 800),
        }

        def page_fetcher(url: str) -> bytes:
            self.assertEqual("https://lab.example/a1", url)
            return (
                b"<html><body>"
                + b"".join(
                    f'<img src="https://lab.example/{index}.jpg">'.encode()
                    for index in range(1, 6)
                )
                + b"</body></html>"
            )

        images = collect_article_images(
            item,
            [{"source_id": "lab_blog", "source_url": "https://lab.example/a1"}],
            page_fetcher=page_fetcher,
            probe_fetcher=probes.__getitem__,
        )
        self.assertEqual(5, len(images))
        self.assertEqual("https://lab.example/1.jpg", images[0]["url"])
        self.assertTrue(all(image["source_id"] == "lab_blog" for image in images))
        bundle = build_asset_bundle(images)
        self.assertEqual(5, len(bundle["candidates"]))
        cover_id, body_ids = suggest_selection(bundle["candidates"])
        self.assertEqual(images[0]["id"], cover_id)
        self.assertEqual(3, len(body_ids))
        self.assertNotIn(cover_id, body_ids)
        bundle = apply_selection(bundle, cover_id, body_ids)
        body = append_image_credits("正文 lab_blog AIGC", selected_body_images(bundle), None)
        self.assertIn(IMAGE_SECTION_TITLE, body)
        self.assertIn("正文：", body)
        self.assertIn("https://lab.example/4.jpg", body)
        self.assertNotIn("https://lab.example/5.jpg", body)
        few = build_asset_bundle(images[:2])
        self.assertEqual(2, len(few["body_ids"]))
        legacy = normalize_assets([{"url": "https://lab.example/1.jpg", "source_id": "lab_blog"}])
        self.assertEqual("article-assets-v2", legacy["schema"])
        self.assertEqual(1, len(legacy["body_ids"]))
