from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMAGE_RE = re.compile(r"<img[^>]+src=['\"]([^'\"]+)['\"]", re.I)
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.M)
BADGE_HINT = (
    "shields.io",
    "badge",
    "travis-ci",
    "circleci",
    "codecov",
    "coveralls",
    "github.com/workflows",
    "dependencies",
    "snyk.io",
    ".svg",
)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif")
MAX_BEATS = 4
FetchBytes = Callable[[str], bytes]


def snake_rows(items: list[Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for index in range(0, len(items), 2):
        chunk = list(items[index : index + 2])
        if (index // 2) % 2 == 1:
            chunk.reverse()
        rows.append(chunk)
    return rows


def find_readme_logo(readme: str, *, owner: str = "", repo: str = "") -> str:
    from .marks import is_author_avatar, looks_like_logo

    for beat in extract_story_beats(readme, owner=owner, repo=repo):
        url = str(beat.get("image") or "")
        caption = str(beat.get("caption") or "")
        if url and looks_like_logo(url, caption) and not is_author_avatar(url):
            return url
    return ""


def extract_story_beats(readme: str, *, owner: str = "", repo: str = "") -> list[dict[str, str]]:
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/" if owner and repo else ""
    beats: list[dict[str, str]] = []
    seen: set[str] = set()
    for alt, raw_src in MD_IMAGE_RE.findall(readme or ""):
        url = _resolve(raw_src, base)
        if not url or url in seen or _is_badge(url):
            continue
        seen.add(url)
        beats.append({"caption": _caption(alt, readme, url), "image": url})
        if len(beats) >= MAX_BEATS:
            return beats
    for raw_src in HTML_IMAGE_RE.findall(readme or ""):
        url = _resolve(raw_src, base)
        if not url or url in seen or _is_badge(url):
            continue
        seen.add(url)
        beats.append({"caption": _nearby_heading(readme, raw_src), "image": url})
        if len(beats) >= MAX_BEATS:
            break
    return beats


def load_readme(owner: str, repo: str, fetcher: FetchBytes | None) -> str:
    if fetcher is None or not owner or not repo:
        return ""
    for url in (
        f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md",
        f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md",
    ):
        try:
            payload = fetcher(url)
        except (OSError, TimeoutError, ValueError):
            continue
        if not payload:
            continue
        text = payload.decode("utf-8", errors="replace").strip()
        if text and "<html" not in text[:80].lower():
            return text[:8000]
    return ""


def _resolve(src: str, base: str) -> str:
    value = (src or "").strip().strip("<>")
    if not value or value.startswith("#"):
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if not base:
        return ""
    return urljoin(base, value.lstrip("/"))


def _is_badge(url: str) -> bool:
    lowered = url.lower()
    if any(hint in lowered for hint in BADGE_HINT):
        return True
    path = urlparse(url).path.lower()
    if path.endswith(IMAGE_EXT):
        return False
    return "githubusercontent.com" not in lowered


def _caption(alt: str, readme: str, url: str) -> str:
    if alt.strip() and alt.strip().lower() not in {"screenshot", "image", "img", "demo"}:
        return " ".join(alt.split())[:42]
    return _nearby_heading(readme, url) or alt.strip() or "功能步骤"


def _nearby_heading(readme: str, needle: str) -> str:
    token = needle.split("/")[-1][:40]
    index = (readme or "").find(token) if token else -1
    if index < 0:
        index = (readme or "").find(needle)
    block = (readme or "")[: index if index > 0 else 200]
    headings = HEADING_RE.findall(block)
    if headings:
        return headings[-1][1].strip()[:42]
    return ""
