"""Fallback cover when an article has no selected cover image."""

from __future__ import annotations

import html
import tempfile
from pathlib import Path
from typing import Callable

POSTER_WIDTH = 1080
POSTER_HEIGHT = 1440
ScreenshotFn = Callable[..., Path]


def poster_html(title: str) -> str:
    text = html.escape((title or "").strip() or "未命名")
    size = _font_size((title or "").strip() or "未命名")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{text}</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:{POSTER_WIDTH}px;height:{POSTER_HEIGHT}px;overflow:hidden;background:#06155a;color:#fff;font-family:Inter,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}}
.frame{{position:relative;width:{POSTER_WIDTH}px;height:{POSTER_HEIGHT}px;overflow:hidden;background:#06155a}}
.top-band{{position:absolute;left:28%;top:0;width:72%;height:12%;background:#101f63}}
.top-outline{{position:absolute;left:48%;top:0;width:48%;height:23%;border:1px solid rgba(112,128,185,.76)}}
.main-diagonal{{position:absolute;inset:0;background:#101f63;clip-path:polygon(100% 35%,100% 100%,28% 100%)}}
.corner-cut{{position:absolute;inset:0;background:#06155a;clip-path:polygon(100% 79%,100% 100%,91.5% 100%)}}
.lower-outline{{position:absolute;inset:0;pointer-events:none}}
.lower-outline::before{{content:"";position:absolute;left:46%;top:99%;width:58%;height:1px;background:rgba(119,135,193,.92);transform-origin:left center;transform:rotate(-31.5deg)}}
.lower-outline::after{{content:"";position:absolute;left:46%;top:99%;width:1px;height:12px;background:rgba(119,135,193,.92)}}
.corner-line{{position:absolute;right:0;bottom:0;width:5%;height:1px;background:rgba(119,135,193,.92);transform-origin:right center;transform:rotate(-59deg)}}
.edge-frame{{position:absolute;inset:0;border-left:1px solid rgba(112,128,185,.46);border-bottom:1px solid rgba(112,128,185,.46);pointer-events:none}}
.title{{position:absolute;left:5%;top:42%;margin:0;color:#fff;font-size:{size}px;line-height:1.2;font-weight:300;letter-spacing:-1.3px;white-space:pre-line;word-break:break-word;max-width:88%}}
</style>
</head>
<body>
<main class="frame">
  <div class="top-band"></div>
  <div class="top-outline"></div>
  <div class="main-diagonal"></div>
  <div class="corner-cut"></div>
  <div class="lower-outline"></div>
  <div class="corner-line"></div>
  <div class="edge-frame"></div>
  <h1 class="title">{text}</h1>
</main>
</body>
</html>
"""


def render_title_poster(
    title: str,
    *,
    screenshot_fn: ScreenshotFn | None = None,
    tmp_dir: Path | None = None,
) -> bytes:
    from .visual.screenshot import screenshot_html

    shot = screenshot_fn or screenshot_html
    markup = poster_html(title)
    if tmp_dir is None:
        with tempfile.TemporaryDirectory(prefix="autoposting-poster-") as raw:
            return _write_and_shot(Path(raw), markup, shot)
    return _write_and_shot(Path(tmp_dir), markup, shot)


def _font_size(title: str) -> int:
    count = max(len(title), 1)
    if count <= 8:
        return 120
    if count <= 16:
        return 88
    if count <= 24:
        return 68
    return 52


def _write_and_shot(folder: Path, markup: str, shot: ScreenshotFn) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    html_path = folder / "title-poster.html"
    png_path = folder / "title-poster.png"
    html_path.write_text(markup, encoding="utf-8")
    shot(html_path, png_path, width=POSTER_WIDTH, height=POSTER_HEIGHT)
    payload = png_path.read_bytes()
    if not payload:
        raise RuntimeError("title_poster_empty")
    return payload
