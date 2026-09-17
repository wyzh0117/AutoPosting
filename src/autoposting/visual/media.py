from __future__ import annotations

import base64
import http.client
import urllib.error
import urllib.request


def fetch_bytes(url: str, timeout: float = 8) -> bytes:
    """Best-effort GET. An unusable URL is a `ValueError`, never a raw HTTPException.

    `http.client.InvalidURL` (a path segment with a control character, say) is an
    `HTTPException`, so it slipped past callers that catch `OSError/ValueError`
    and turned one bad skill name into a failed render.
    """
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raise ValueError("url_invalid")
    request = urllib.request.Request(
        raw,
        headers={"User-Agent": "AutoPosting-visual/1.0.7"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(400_000)
    except http.client.InvalidURL as exc:
        raise ValueError("url_invalid") from exc


def fetch_data_uri(url: str, timeout: float = 6) -> str:
    raw = (url or "").strip()
    if not raw or raw.startswith("data:"):
        return raw
    request = urllib.request.Request(
        raw,
        headers={"User-Agent": "AutoPosting-visual/1.0.7"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(220_000)
            content_type = response.headers.get_content_type() or "image/png"
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException):
        return ""
    if not payload:
        return ""
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{encoded}"
