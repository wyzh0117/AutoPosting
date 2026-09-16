"""Pull GitHub repos linked from a viewpoint post into article material."""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import urlparse

Fetcher = Callable[[str], bytes]

GITHUB_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})/"
    r"([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_SKIP_OWNERS = {
    "sponsors",
    "settings",
    "orgs",
    "marketplace",
    "topics",
    "search",
    "features",
    "pricing",
    "about",
    "login",
    "apps",
    "collections",
    "explore",
    "notifications",
    "new",
    "organizations",
}
_SKIP_REPOS = {"releases", "issues", "pulls", "actions", "wiki", "security", "pulse"}
README_LIMIT = 2500
WORK_CAP = 3


def extract_github_repos(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in GITHUB_REPO_RE.finditer(text or ""):
        owner = match.group(1)
        repo = match.group(2).rstrip(".")
        if owner.lower() in _SKIP_OWNERS or repo.lower() in _SKIP_REPOS:
            continue
        key = (owner, repo)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
        if len(found) >= WORK_CAP:
            break
    return found


def fetch_linked_works(text: str, fetcher: Fetcher | None = None) -> list[dict[str, str]]:
    if fetcher is None:
        return []
    works: list[dict[str, str]] = []
    for owner, repo in extract_github_repos(text):
        readme = _fetch_readme(owner, repo, fetcher)
        works.append(
            {
                "full_name": f"{owner}/{repo}",
                "url": f"https://github.com/{owner}/{repo}",
                "readme": readme,
            }
        )
    return works


def _fetch_readme(owner: str, repo: str, fetcher: Fetcher) -> str:
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
            return text[:README_LIMIT]
    return ""


def looks_like_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
