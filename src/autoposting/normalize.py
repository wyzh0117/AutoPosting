from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower() or "https"
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit((scheme, hostname, path, urlencode(sorted(query)), ""))


def content_hash(title: str, content_text: str) -> str:
    normalized = f"{normalize_text(title).casefold()}\n{normalize_text(content_text).casefold()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def contains_source_id(text: str, source_id: str) -> bool:
    boundary = r"A-Za-z0-9_.-"
    return re.search(
        rf"(?<![{boundary}]){re.escape(source_id)}(?![{boundary}])", text
    ) is not None