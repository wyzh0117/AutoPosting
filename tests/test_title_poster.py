import unittest
from pathlib import Path

from autoposting.title_poster import POSTER_HEIGHT, POSTER_WIDTH, poster_html, render_title_poster


class TitlePosterTests(unittest.TestCase):
    def test_poster_html_uses_escaped_title_and_three_by_four_canvas(self) -> None:
        html = poster_html("A <b>Title</b> 发布")
        self.assertIn("A &lt;b&gt;Title&lt;/b&gt; 发布", html)
        self.assertNotIn("<b>Title</b>", html)
        self.assertIn(f"{POSTER_WIDTH}px", html)
        self.assertIn(f"{POSTER_HEIGHT}px", html)
        self.assertIn("#06155a", html)
        self.assertIn("clip-path", html)
        self.assertIn("main-diagonal", html)
        self.assertIn("#fff", html)
        self.assertNotIn("#8b1a1a", html)
        self.assertEqual(1080, POSTER_WIDTH)
        self.assertEqual(1440, POSTER_HEIGHT)

    def test_render_title_poster_returns_injected_png_bytes(self) -> None:
        def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
            self.assertTrue(html_path.is_file())
            self.assertIn("标题大字报", html_path.read_text(encoding="utf-8"))
            self.assertEqual(POSTER_WIDTH, width)
            self.assertEqual(POSTER_HEIGHT, height)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            return png_path

        payload = render_title_poster("标题大字报", screenshot_fn=fake_shot)
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertGreater(len(payload), 8)


if __name__ == "__main__":
    unittest.main()
