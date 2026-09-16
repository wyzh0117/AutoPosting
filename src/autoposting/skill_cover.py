"""Skill-finder cover using the shared deep-blue geometric language."""

from __future__ import annotations

import html
import tempfile
from pathlib import Path
from typing import Callable, Iterable

COVER_WIDTH = 1080
COVER_HEIGHT = 1440
ScreenshotFn = Callable[..., Path]


def skill_cover_html(
    title: str,
    theme: str = "",
    count: int = 0,
    stages: Iterable[str] | None = None,
) -> str:
    heading = html.escape((title or "").strip())
    theme_text = html.escape((theme or "").strip())
    stage_list = [html.escape(str(item).strip()) for item in (stages or []) if str(item).strip()]
    stage_markup = "".join(f"<li>{item}</li>" for item in stage_list)
    size = _title_size((title or "").strip())
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{heading}</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:{COVER_WIDTH}px;height:{COVER_HEIGHT}px;overflow:hidden;background:#06155a;color:#fff;font-family:Inter,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}}
.frame{{position:relative;width:{COVER_WIDTH}px;height:{COVER_HEIGHT}px;overflow:hidden;background:#06155a;padding:0 6%}}
.top-band{{position:absolute;left:27%;top:0;width:73%;height:8%;background:#101f63}}
.top-outline{{position:absolute;left:48%;top:0;width:47%;height:20%;border:1px solid rgba(112,128,185,.76)}}
.main-diagonal{{position:absolute;inset:0;background:#101f63;clip-path:polygon(100% 35%,100% 100%,28% 100%)}}
.corner-cut{{position:absolute;inset:0;background:#06155a;clip-path:polygon(100% 79%,100% 100%,91.5% 100%)}}
.lower-outline{{position:absolute;inset:0;pointer-events:none}}
.lower-outline::before{{content:"";position:absolute;left:46%;top:99%;width:58%;height:1px;background:rgba(119,135,193,.92);transform-origin:left center;transform:rotate(-31.5deg)}}
.lower-outline::after{{content:"";position:absolute;left:46%;top:99%;width:1px;height:12px;background:rgba(119,135,193,.92)}}
.corner-line{{position:absolute;right:0;bottom:0;width:5%;height:1px;background:rgba(119,135,193,.92);transform-origin:right center;transform:rotate(-59deg)}}
.edge-frame{{position:absolute;inset:0;border:1px solid rgba(112,128,185,.46);pointer-events:none}}
.copy{{position:absolute;z-index:1;left:6%;right:6%;top:22%;bottom:8%;display:flex;flex-direction:column;justify-content:space-between}}
h1{{margin:0;font-size:{size}px;line-height:1.08;letter-spacing:-1.3px;font-weight:300;word-break:break-word;max-width:90%}}
.sub{{margin:24px 0 0;font-size:28px;letter-spacing:.04em;font-weight:300}}
ul{{margin:0;padding:0;list-style:none;display:grid;grid-template-columns:1fr 1fr;gap:12px 28px;font-size:24px;letter-spacing:.02em}}
</style>
</head>
<body>
<div class="frame">
  <div class="top-band"></div>
  <div class="top-outline"></div>
  <div class="main-diagonal"></div>
  <div class="corner-cut"></div>
  <div class="lower-outline"></div>
  <div class="corner-line"></div>
  <div class="edge-frame"></div>
  <div class="copy">
    <div>
      <h1>{heading}</h1>
      <p class="sub">{theme_text}{" · " if theme_text and count else ""}{int(count) if count else ""}</p>
    </div>
    <ul>{stage_markup}</ul>
  </div>
</div>
</body>
</html>
"""


def render_skill_cover(
    title: str,
    theme: str = "",
    count: int = 0,
    stages: Iterable[str] | None = None,
    *,
    screenshot_fn: ScreenshotFn | None = None,
    tmp_dir: Path | None = None,
) -> bytes:
    from .visual.screenshot import screenshot_html

    shot = screenshot_fn or screenshot_html
    markup = skill_cover_html(title, theme, count, stages)
    if tmp_dir is None:
        with tempfile.TemporaryDirectory(prefix="autoposting-skill-cover-") as raw:
            return _write_and_shot(Path(raw), markup, shot)
    return _write_and_shot(Path(tmp_dir), markup, shot)


def _title_size(title: str) -> int:
    count = max(len(title), 1)
    if count <= 8:
        return 96
    if count <= 16:
        return 72
    if count <= 24:
        return 58
    return 46


def _write_and_shot(folder: Path, markup: str, shot: ScreenshotFn) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    html_path = folder / "skill-cover.html"
    png_path = folder / "skill-cover.png"
    html_path.write_text(markup, encoding="utf-8")
    shot(html_path, png_path, width=COVER_WIDTH, height=COVER_HEIGHT)
    payload = png_path.read_bytes()
    if not payload:
        raise RuntimeError("skill_cover_empty")
    return payload
