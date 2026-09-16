"""Collect original news images and let the author pick cover vs body shots.

Images come from the source item (RSS enclosure/media/HTML, then the article
page). Nothing here generates pictures. Ranking prefers a 3:4 portrait ratio.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import struct
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

IMAGE_LIMIT = 4
IMAGE_CANDIDATE_CAP = 16
IMAGE_TARGET_RATIO = 3 / 4
IMAGE_MIN_EDGE = 200
IMAGE_SECTION_TITLE = "配图（来自原文，非 AI 生成）"
ASSETS_SCHEMA = "article-assets-v2"
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")
_SKIP_HINTS = (
    "1x1",
    "pixel",
    "spacer",
    "tracker",
    "favicon",
    "logo-small",
    "apple-touch-icon",
    "profile_images",
    "profile_banners",
    "default_profile",
    "avatar",
    "gravatar",
    "identicon",
    "twemoji",
    "/emoji/",
    "userpic",
    "author-avatar",
    "author_avatar",
    "site-logo",
    "header-logo",
)
_AVATAR_ATTR_RE = re.compile(
    r"avatar|gravatar|byline|profile[-_]?img|user[-_]?(?:pic|image|avatar)|"
    r"author[-_]?(?:img|image|photo|avatar|pic)|headshot|twemoji",
    re.I,
)
_CONTENT_CLASS_RE = re.compile(
    r"article|post-content|entry-content|article-body|post-body|content-body|"
    r"rich-text|article__content|post__content",
    re.I,
)
_CHROME_CLASS_RE = re.compile(
    r"\b(header|footer|navbar|sidebar|byline|author-box|comments?|share-bar|"
    r"related|recommend|breadcrumb)\b",
    re.I,
)
_CHROME_TAGS = frozenset({"header", "footer", "nav", "aside", "svg"})
_CONTENT_TAGS = frozenset({"article", "main", "figure"})
_X_HOSTS = frozenset(
    {"x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}
)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ImageError(RuntimeError):
    pass


class _ImageUrlParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []
        self.trusted: list[str] = []
        self.loose: list[str] = []
        self._stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        blob = f"{values.get('class', '')} {values.get('id', '')} {values.get('alt', '')}"
        self._stack.append((tag, blob))
        if tag == "img":
            raw = ""
            for key in ("src", "data-src", "data-original"):
                if values.get(key):
                    raw = values[key]
                    break
            if raw:
                url = urljoin(self.base_url, raw)
                if not is_chrome_image(url, values, self._stack):
                    if _in_content_region(self._stack):
                        self.urls.append(url)
                    else:
                        self.loose.append(url)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            if name in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                content = values.get("content")
                if content and content.startswith(("http://", "https://")):
                    url = urljoin(self.base_url, content)
                    if not is_chrome_image_url(url):
                        self.trusted.append(url)
        if tag == "link" and values.get("rel", "").lower() == "image_src" and values.get("href"):
            url = urljoin(self.base_url, values["href"])
            if not is_chrome_image_url(url):
                self.trusted.append(url)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def is_chrome_image_url(url: str) -> bool:
    if not url:
        return True
    lowered = url.lower()
    return any(hint in lowered for hint in _SKIP_HINTS)


def _declared_size(values: dict[str, str]) -> tuple[int, int] | None:
    raw_w = re.sub(r"[^\d]", "", values.get("width") or "")
    raw_h = re.sub(r"[^\d]", "", values.get("height") or "")
    try:
        width = int(raw_w) if raw_w else 0
        height = int(raw_h) if raw_h else 0
    except ValueError:
        return None
    if width or height:
        return width, height
    return None


def is_chrome_image(
    url: str,
    values: dict[str, str] | None = None,
    stack: list[tuple[str, str]] | None = None,
) -> bool:
    if is_chrome_image_url(url):
        return True
    values = values or {}
    blob = f"{values.get('class', '')} {values.get('id', '')} {values.get('alt', '')}"
    if _AVATAR_ATTR_RE.search(blob):
        return True
    size = _declared_size(values)
    if size and 0 < min(edge for edge in size if edge > 0) <= 96:
        return True
    for tag, ancestor in stack or []:
        if tag in _CHROME_TAGS or _CHROME_CLASS_RE.search(ancestor):
            if size and max(size) >= 400:
                continue
            if _AVATAR_ATTR_RE.search(ancestor) or tag in {"header", "nav", "aside", "footer"}:
                return True
    return False


def _in_content_region(stack: list[tuple[str, str]]) -> bool:
    tags = {tag for tag, _ in stack}
    if tags & _CONTENT_TAGS:
        return True
    if any(_CONTENT_CLASS_RE.search(blob) for _, blob in stack):
        return True
    if tags & _CHROME_TAGS:
        return False
    if any(_CHROME_CLASS_RE.search(blob) for _, blob in stack):
        return False
    return True


def should_fetch_page_images(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host not in _X_HOSTS


def is_usable_content_image(item: dict[str, object]) -> bool:
    if is_chrome_image_url(str(item.get("url") or "")):
        return False
    width = item.get("width")
    height = item.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        if min(width, height) < IMAGE_MIN_EDGE:
            return False
        if max(width, height) <= 256 and abs(width - height) <= 24:
            return False
    return True


def _looks_like_image_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    if is_chrome_image_url(url):
        return False
    path = urlsplit(url).path.lower()
    if any(path.endswith(ext) for ext in _IMAGE_EXT):
        return True
    return "image" in url.lower() or "img" in path or "photo" in url.lower() or "media" in url.lower()


def extract_image_urls_from_html(html: str, base_url: str) -> list[str]:
    parser = _ImageUrlParser(base_url)
    try:
        parser.feed(html)
    except Exception:
        return []
    trusted = [url for url in parser.trusted if url.startswith(("http://", "https://"))]
    extras = [url for url in parser.urls if _looks_like_image_url(url)]
    if trusted or extras:
        return list(dict.fromkeys(trusted + extras))
    loose = [url for url in parser.loose if _looks_like_image_url(url)]
    return list(dict.fromkeys(loose))


def extract_image_urls_from_feed_fields(
    *,
    page_url: str,
    content_html: str = "",
    enclosure_url: str = "",
    enclosure_type: str = "",
    media_urls: list[str] | None = None,
) -> list[str]:
    found: list[str] = []
    if enclosure_url and (
        enclosure_type.startswith("image/") or _looks_like_image_url(enclosure_url)
    ):
        found.append(urljoin(page_url, enclosure_url))
    for url in media_urls or []:
        if _looks_like_image_url(url):
            found.append(urljoin(page_url, url))
    found.extend(extract_image_urls_from_html(content_html, page_url))
    return list(dict.fromkeys(found))


def extract_image_urls_from_item(item: dict[str, object]) -> list[str]:
    raw: dict[str, object] = {}
    raw_json = item.get("raw_json")
    if isinstance(raw_json, str) and raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError:
            raw = {}
    page_url = str(item.get("canonical_url") or raw.get("link") or "")
    stored = raw.get("image_urls")
    urls: list[str] = []
    if isinstance(stored, list):
        urls.extend(str(url) for url in stored if isinstance(url, str))
    urls.extend(
        extract_image_urls_from_feed_fields(
            page_url=page_url,
            content_html=str(item.get("content_text") or ""),
        )
    )
    return [url for url in dict.fromkeys(urls) if _looks_like_image_url(url) and not is_chrome_image_url(url)]


def parse_image_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        if width > 0 and height > 0:
            return int(width), int(height)
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3}:
                height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                if width > 0 and height > 0:
                    return int(width), int(height)
                return None
            if marker in {0xD8, 0x01}:
                index += 2
                continue
            if marker == 0xD9 or marker == 0xDA:
                break
            if index + 4 > len(data):
                break
            length = struct.unpack(">H", data[index + 2 : index + 4])[0]
            index += 2 + length
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        if width > 0 and height > 0:
            return int(width), int(height)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
            if width and height:
                return width, height
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return width, height
        if chunk == b"VP8X" and len(data) >= 30:
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return width, height
    return None


def ratio_distance(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        return 1.0
    return abs((width / height) - IMAGE_TARGET_RATIO)


def default_probe_fetcher(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Range": "bytes=0-65535",
        },
    )
    with urlopen(request, timeout=12) as response:
        return response.read(65536)


def _validate_public_page_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("page_url_invalid")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {
        result[4][0]
        for result in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("page_url_not_public")


class _PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_page_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_page_fetcher(url: str) -> bytes:
    _validate_public_page_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        },
    )
    with build_opener(_PublicRedirectHandler()).open(request, timeout=15) as response:
        return response.read(1_500_000)


def select_article_images(
    candidates: list[dict[str, object]],
    limit: int = IMAGE_LIMIT,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    scored: list[tuple[float, int, dict[str, object]]] = []
    unscored: list[tuple[int, dict[str, object]]] = []
    for index, item in enumerate(candidates):
        width = item.get("width")
        height = item.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            scored.append((ratio_distance(width, height), index, item))
        else:
            unscored.append((index, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    chosen = [item for _, _, item in scored[:limit]]
    for _, item in unscored:
        if len(chosen) >= limit:
            break
        chosen.append(item)
    return chosen[:limit]


def probe_candidates(
    urls: list[str],
    *,
    source_id: str = "",
    source_url: str = "",
    fetcher: Callable[[str], bytes] | None = None,
) -> list[dict[str, object]]:
    probe = fetcher or default_probe_fetcher
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        item: dict[str, object] = {
            "id": asset_id(url),
            "url": url,
            "source_id": source_id,
            "source_url": source_url or url,
        }
        try:
            payload = probe(url)
            size = parse_image_size(payload) if isinstance(payload, (bytes, bytearray)) else None
            if size:
                item["width"], item["height"] = size
        except Exception:
            pass
        results.append(item)
    return results


def collect_article_images(
    item: dict[str, object],
    source_pairs: list[dict[str, object]],
    *,
    page_fetcher: Callable[[str], bytes] | None = None,
    probe_fetcher: Callable[[str], bytes] | None = None,
) -> list[dict[str, object]]:
    urls = extract_image_urls_from_item(item)
    page_url = str(item.get("canonical_url") or "")
    if (
        page_url
        and page_fetcher is not None
        and should_fetch_page_images(page_url)
        and len(urls) < IMAGE_CANDIDATE_CAP
    ):
        try:
            payload = page_fetcher(page_url)
            html = payload.decode("utf-8", errors="replace")
            urls.extend(extract_image_urls_from_html(html, page_url))
        except Exception:
            pass
    urls = list(
        dict.fromkeys(
            url
            for url in urls
            if _looks_like_image_url(url) and not is_chrome_image_url(url)
        )
    )[:IMAGE_CANDIDATE_CAP]
    source_id = ""
    source_url = page_url
    if source_pairs:
        source_id = str(source_pairs[0].get("source_id") or "")
        source_url = str(source_pairs[0].get("source_url") or page_url)
    probed = [
        row
        for row in probe_candidates(
            urls,
            source_id=source_id,
            source_url=source_url,
            fetcher=probe_fetcher,
        )
        if is_usable_content_image(row)
    ]
    ranked = select_article_images(probed, limit=max(len(probed), 1))
    return ranked[:IMAGE_CANDIDATE_CAP]


def asset_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _credit_line(image: dict[str, object]) -> str:
    width = image.get("width")
    height = image.get("height")
    ratio_note = ""
    if isinstance(width, int) and isinstance(height, int) and height:
        ratio_note = (
            f"（{width}×{height}，约 3:4）"
            if ratio_distance(width, height) <= 0.08
            else f"（{width}×{height}）"
        )
    source = str(image.get("source_id") or "原文")
    url = str(image.get("url") or "")
    return f"来源 {source} · {url}{ratio_note}"


def format_image_credits(
    images: list[dict[str, object]],
    cover: dict[str, object] | None = None,
) -> str:
    if not images and cover is None:
        return ""
    lines = [IMAGE_SECTION_TITLE]
    if cover is not None:
        lines.append("封面：" + _credit_line(cover))
    if images:
        lines.append("正文：")
        for index, image in enumerate(images, start=1):
            lines.append(f"{index}. {_credit_line(image)}")
    return "\n".join(lines)


def strip_image_credits(body: str) -> str:
    if IMAGE_SECTION_TITLE not in body:
        return body.rstrip()
    before, _, after = body.partition(IMAGE_SECTION_TITLE)
    kept: list[str] = []
    skipping = True
    for line in after.lstrip("\n").splitlines():
        if skipping and (
            not line.strip()
            or line.startswith("封面：")
            or line.startswith("正文：")
            or line.startswith("来源 ")
            or re.match(r"\d+\. ", line)
        ):
            continue
        skipping = False
        kept.append(line)
    text = before.rstrip()
    if kept:
        text = text + "\n" + "\n".join(kept)
    return text.rstrip()


def append_image_credits(
    body: str,
    images: list[dict[str, object]],
    cover: dict[str, object] | None = None,
) -> str:
    # Source links belong on the image itself when the draft is rendered.
    # Keep this compatibility helper from reintroducing a source block at the end.
    return strip_image_credits(body)


def candidate_entry(image: dict[str, object]) -> dict[str, object]:
    url = str(image.get("url") or "")
    entry: dict[str, object] = {
        "id": str(image.get("id") or asset_id(url)),
        "url": url,
        "source_id": str(image.get("source_id") or ""),
        "source_url": str(image.get("source_url") or url),
    }
    if isinstance(image.get("width"), int):
        entry["width"] = image["width"]
    if isinstance(image.get("height"), int):
        entry["height"] = image["height"]
    return entry


def empty_asset_bundle() -> dict[str, object]:
    return {
        "schema": ASSETS_SCHEMA,
        "candidates": [],
        "cover_id": "",
        "body_ids": [],
    }


def suggest_selection(candidates: list[dict[str, object]]) -> tuple[str, list[str]]:
    ranked = select_article_images(candidates, limit=max(len(candidates), 1))
    if not ranked:
        return "", []
    return str(ranked[0]["id"]), []


def build_asset_bundle(
    images: list[dict[str, object]], item_id: str = ""
) -> dict[str, object]:
    candidates = [candidate_entry(image) for image in images if image.get("url")]
    cover_id, body_ids = suggest_selection(candidates)
    payload: dict[str, object] = {
        "schema": ASSETS_SCHEMA,
        "candidates": candidates,
        "cover_id": cover_id,
        "body_ids": body_ids,
    }
    if item_id:
        payload["item_id"] = item_id
    return payload


def normalize_assets(raw: object) -> dict[str, object]:
    parsed: object = raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return empty_asset_bundle()
    if isinstance(parsed, dict) and parsed.get("schema") == ASSETS_SCHEMA:
        candidates = [
            candidate_entry(item)
            for item in parsed.get("candidates", [])
            if isinstance(item, dict)
        ]
        known = {str(item["id"]) for item in candidates}
        cover_id = str(parsed.get("cover_id") or "")
        if cover_id not in known:
            cover_id = ""
        body_ids = [
            str(item_id)
            for item_id in parsed.get("body_ids", [])
            if str(item_id) in known
        ]
        payload = {
            "schema": ASSETS_SCHEMA,
            "candidates": candidates,
            "cover_id": cover_id,
            "body_ids": body_ids,
        }
        if parsed.get("item_id"):
            payload["item_id"] = str(parsed["item_id"])
        _copy_manuscript(parsed, payload)
        return payload
    if isinstance(parsed, list):
        return build_asset_bundle([item for item in parsed if isinstance(item, dict)])
    return empty_asset_bundle()


def apply_selection(
    bundle: dict[str, object], cover_id: str, body_ids: list[str]
) -> dict[str, object]:
    normalized = normalize_assets(bundle)
    known = {str(item["id"]) for item in normalized["candidates"]}
    chosen_cover = cover_id if cover_id in known else ""
    chosen_body: list[str] = []
    for item_id in body_ids:
        if item_id in known and item_id not in chosen_body:
            chosen_body.append(item_id)
    payload = {
        "schema": ASSETS_SCHEMA,
        "candidates": normalized["candidates"],
        "cover_id": chosen_cover,
        "body_ids": chosen_body,
    }
    if normalized.get("item_id"):
        payload["item_id"] = normalized["item_id"]
    _copy_manuscript(normalized, payload)
    return payload


def _copy_manuscript(source: dict[str, object], dest: dict[str, object]) -> None:
    manuscript = source.get("manuscript")
    if isinstance(manuscript, str) and manuscript.strip():
        dest["manuscript"] = manuscript


def _by_id(bundle: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item["id"]): item for item in bundle.get("candidates", []) if isinstance(item, dict)}


def cover_image(bundle: dict[str, object]) -> dict[str, object] | None:
    return _by_id(bundle).get(str(bundle.get("cover_id") or ""))


def selected_body_images(bundle: dict[str, object]) -> list[dict[str, object]]:
    lookup = _by_id(bundle)
    return [lookup[item_id] for item_id in bundle.get("body_ids", []) if item_id in lookup]


def assets_to_jsonable(images: list[dict[str, object]]) -> dict[str, object]:
    return build_asset_bundle(images)


def guess_image_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


_SRC_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)


def figure_html(image: dict[str, object], published_url: str | None = None) -> str:
    src = published_url or str(image.get("url") or "")
    original = str(image.get("source_url") or image.get("url") or "")
    href = _safe_external_url(original)
    image_html = (
        f'<img src="{_escape_attr(src)}" alt="" '
        'style="display:block;width:100%;max-width:100%;aspect-ratio:3/4;object-fit:cover;"/>'
    )
    if href:
        image_html = f'<a href="{_escape_attr(href)}">{image_html}</a>'
    return (
        '<p class="autoposting-figure" style="margin:12px 0 16px;">'
        f"{image_html}</p>"
    )


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _safe_external_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(char.isspace() for char in value)
    ):
        return ""
    return value
