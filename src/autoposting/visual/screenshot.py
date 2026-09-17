from __future__ import annotations

import shutil
import subprocess
import time
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

# One page at a time; a rendered PNG normally lands in 2–6s. The ceiling only
# exists so a wedged Chrome cannot hang the render loop forever.
CHROME_TIMEOUT_SECONDS = 45
# A retry costs a couple of seconds and clears the common transient failures
# (a leftover singleton lock, a half-written profile, a busy font service).
CHROME_ATTEMPTS = 2
RETRY_PAUSE_SECONDS = 0.7

# Chrome's own log lines are not actionable for the person clicking 生成图片.
# These are the failures seen in the field, each with what to actually do.
CHROME_FAILURE_HINTS: tuple[tuple[str, str], ...] = (
    (
        "Failed to create a unique user data directory for headless",
        "Chrome 建不出自己的临时用户目录。当前运行服务的进程不允许写系统临时目录"
        "（常见于把服务放在受限沙箱/受限启动项里跑）。请用 AutoPosting服务控制.command 重新启动服务后再点「生成图片」。",
    ),
    (
        "Failed to create a socket directory",
        "Chrome 建不出进程锁目录，同样是系统临时目录不可写。请用 AutoPosting服务控制.command 重新启动服务后再试。",
    ),
    (
        "Failed to create a ProcessSingleton",
        "Chrome 无法在该用户目录上建立单例锁，通常是有另一个 Chrome 实例占着同一个临时目录，稍后重试或重启服务即可。",
    ),
)


class ScreenshotError(RuntimeError):
    pass


def png_ready(png_path: Path) -> bool:
    """A usable screenshot exists on disk (non-empty file)."""
    try:
        return png_path.is_file() and png_path.stat().st_size > 0
    except OSError:
        return False


def explain_chrome_failure(detail: str) -> str:
    """Turn a raw Chrome log tail into something the user can act on."""
    text = str(detail or "")
    for marker, message in CHROME_FAILURE_HINTS:
        if marker in text:
            return message
    cleaned = " ".join(text.split())
    return f"Chrome 没能完成截图：{cleaned[:240]}" if cleaned else "Chrome 没能完成截图"


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
        raise ScreenshotError(
            "没找到 Chrome/Chromium，无法把 HTML 截成图片。装一个 Google Chrome（或 Chromium / Edge）后重试。"
        )
    failures: list[str] = []
    for attempt in range(CHROME_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE_SECONDS)
        try:
            _chrome_screenshot(chrome, html_path, png_path, width, height)
        except ScreenshotError as exc:
            failures.append(str(exc))
            continue
        if png_ready(png_path):
            return png_path
        failures.append("Chrome 退出时没有写出 PNG")
    # Report the first failure: the retry usually repeats it, and the first one is
    # the one that names the real cause.
    raise ScreenshotError(failures[0] if failures else "Chrome 没能完成截图")


def _chrome_screenshot(
    chrome: str,
    html_path: Path,
    png_path: Path,
    width: int,
    height: int,
) -> None:
    """One Chrome run. Raises ScreenshotError when it fails to leave a PNG."""
    if png_ready(png_path):
        # Never let a stale file from an earlier render pass as this one's result.
        try:
            png_path.unlink()
        except OSError:
            pass
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
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=CHROME_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A brand-new Chrome profile keeps the browser process alive for a while
        # after the screenshot is on disk (background services, first-run tasks).
        # The PNG is the deliverable, so accept a finished file instead of
        # throwing a usable render away.
        if png_ready(png_path):
            return
        raise ScreenshotError(
            f"Chrome 截图超时（{CHROME_TIMEOUT_SECONDS}s 内没有写出 PNG）"
        ) from exc
    if completed.returncode != 0 or not png_ready(png_path):
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ScreenshotError(explain_chrome_failure(detail))


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
