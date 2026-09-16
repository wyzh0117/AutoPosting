from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .tokens import load_tokens

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "msedge",
)


class ScreenshotError(RuntimeError):
    pass


def screenshot_html(
    html_path: Path,
    png_path: Path,
    width: int | None = None,
    height: int | None = None,
) -> Path:
    tokens = load_tokens()
    width = int(width or tokens["canvas"]["width"])
    height = int(height or tokens["canvas"]["height"])
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if _try_playwright(html_path, png_path, width, height):
        return png_path
    chrome = _find_chrome()
    if chrome is None:
        raise ScreenshotError("no Playwright and no Chrome/Chromium for screenshot")
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--window-size={width},{height}",
        f"--screenshot={png_path.resolve()}",
        "--virtual-time-budget=4000",
        html_path.resolve().as_uri(),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    if completed.returncode != 0 or not png_path.is_file():
        detail = (completed.stderr or completed.stdout or "").strip()[:400]
        raise ScreenshotError(detail or f"chrome screenshot failed: {chrome}")
    return png_path


def _try_playwright(html_path: Path, png_path: Path, width: int, height: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
        )
        page.goto(html_path.resolve().as_uri(), wait_until="load")
        page.screenshot(path=str(png_path), clip={"x": 0, "y": 0, "width": width, "height": height})
        browser.close()
    return png_path.is_file()


def _find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None
