"""Parse pasted skill links (GitHub and skills.sh) into candidate records.

Pure text parsing: standard library only, no network and no file I/O. The Skill
Finder paste box hands the blob to :func:`parse_skill_links`; another module
then fetches `SKILL.md` for each candidate.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

GITHUB_SOURCE = "GitHub"
SKILLS_SH_SOURCE = "skills.sh"
GITHUB_HOST = "github.com"
RAW_HOST = "raw.githubusercontent.com"
SKILLS_SH_HOST = "skills.sh"

# Every candidate carries exactly these keys.
CANDIDATE_KEYS: tuple[str, ...] = (
    "source",
    "source_id",
    "full_name",
    "name",
    "url",
    "skill_path",
    "raw_url",
)

# A first GitHub/skills.sh segment in this set is site navigation, not an owner.
RESERVED_SEGMENTS: frozenset[str] = frozenset(
    {
        "about",
        "apps",
        "collections",
        "explore",
        "features",
        "login",
        "marketplace",
        "notifications",
        "orgs",
        "pricing",
        "search",
        "settings",
        "sponsors",
        "topics",
        "trending",
    }
)

_HOST_KINDS: dict[str, str] = {
    GITHUB_HOST: "github",
    RAW_HOST: "raw",
    SKILLS_SH_HOST: "skills_sh",
}
_TREE_BLOB: tuple[str, ...] = ("tree", "blob")
_SKILL_FILE = "skill.md"

# Paste noise: `-`/`*`/`1.` list markers, markdown wrappers, sentence punctuation.
# `_MARKER_RE` has no `^`: `_strip_noise` matches it at a moving cursor instead.
_MARKER_RE = re.compile(r"(?:[-\*•]+|\d{1,3}[.)、:：]|[\(\[]+)")
_MARKDOWN_LINK_RE = re.compile(r"^\[[^\]]*\]\(([^()\s]+)\)$")
_SPLIT_RE = re.compile(r"[\s,，]+")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# GitHub owners never contain a dot; repos may (and may end in `.git`).
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LEAD_NOISE = "([<\"'“‘`「『"
_TRAIL_NOISE = ".,;:!?)]}>）］】」』\"'”’。，；：、*`"
_LEAD_CHARS = frozenset(_LEAD_NOISE + " \t")
_TRAIL_CHARS = frozenset(_TRAIL_NOISE + " \t")


def _strip_noise(token: str) -> str:
    """Peel list markers, wrapping punctuation, and markdown link syntax."""
    text = token.strip()
    markdown = _MARKDOWN_LINK_RE.match(text)
    if markdown is not None:
        text = markdown.group(1).strip()
    # Two cursors advance inward instead of re-slicing: every character is
    # visited once, so a pathological paste cannot go quadratic.
    start, end, moved = 0, len(text), True
    while moved and start < end:
        moved = False
        marker = _MARKER_RE.match(text, start)
        if marker is not None:
            start = marker.end()
            moved = True
        while start < end and text[start] in _LEAD_CHARS:
            start += 1
            moved = True
        while end > start and text[end - 1] in _TRAIL_CHARS:
            end -= 1
            moved = True
    return text[start:end]


def _split_tokens(text: str) -> list[tuple[str, str]]:
    """Split on whitespace/commas into `(as-pasted, cleaned)` pairs with a body."""
    tokens: list[tuple[str, str]] = []
    for raw in _SPLIT_RE.split(text):
        cleaned = _strip_noise(raw)
        if cleaned:
            tokens.append((raw, cleaned))
    return tokens


def _segments(path: str) -> list[str]:
    """Path segments with empty pieces (leading/trailing slashes) dropped."""
    return [part for part in path.split("/") if part]


def _host_only(netloc: str) -> str:
    """Host of a netloc: no userinfo, no port, no `www.`, lowercased."""
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].strip().strip(".")
    return host.lower().removeprefix("www.")


def _repo_name(segment: str) -> str:
    """Validated repo name with a trailing `.git` removed (`""` when invalid)."""
    name = segment[:-4] if segment.lower().endswith(".git") else segment
    if name in {".", ".."} or not _REPO_RE.match(name):
        return ""
    return name


def _path_parts(parts: list[str]) -> list[str] | None:
    """In-repo path segments, or None when a literal/percent-encoded dot segment appears."""
    for part in parts:
        lowered = part.lower()
        if lowered in {".", "..", "%2e", "%2e%2e", ".%2e", "%2e."}:
            return None
    return parts


def _repo_candidate(owner: str, repo: str) -> dict[str, object]:
    """Repo-root GitHub candidate; callers may add path/raw hints."""
    return {
        "source": GITHUB_SOURCE,
        "source_id": f"{owner}/{repo}",
        "full_name": f"{owner}/{repo}",
        "name": repo,
        "url": f"https://{GITHUB_HOST}/{owner}/{repo}",
        "skill_path": "",
        "raw_url": "",
    }


def _github_path_candidate(
    owner: str, repo: str, ref: str, path: list[str], kind: str
) -> dict[str, object]:
    """Candidate for a `/tree/` or `/blob/` link that carries an in-repo path."""
    joined = "/".join(path)
    candidate = _repo_candidate(owner, repo)
    if path[-1].lower() != _SKILL_FILE:
        # Not a SKILL.md path: mirror the `/tree/` rule, i.e. the pasted path is
        # the skill directory and SKILL.md sits one level below it.
        candidate["skill_path"] = f"{joined}/SKILL.md"
        candidate["name"] = path[-1]
        return candidate
    candidate["skill_path"] = joined
    if kind == "blob":
        candidate["name"] = path[-2] if len(path) > 1 else repo
        candidate["raw_url"] = f"https://{RAW_HOST}/{owner}/{repo}/{ref}/{joined}"
    else:
        # `/tree/<ref>/<path>`: the literal last segment names the candidate.
        candidate["name"] = path[-1]
    return candidate


def _github_candidate(segments: list[str]) -> tuple[dict[str, object] | None, str]:
    """GitHub link: repo root, `/tree/<ref>/<path...>`, or `/blob/<ref>/<path...>`."""
    if not segments:
        return None, "not_a_link"
    owner = segments[0]
    if owner.lower() in RESERVED_SEGMENTS:
        return None, "reserved_owner"
    if len(segments) < 2:
        return None, "not_a_link"
    repo = _repo_name(segments[1])
    if not _OWNER_RE.match(owner) or not repo:
        return None, "not_a_link"
    root = _repo_candidate(owner, repo)
    if len(segments) == 2:
        return root, ""
    kind = segments[2].lower()
    if kind not in _TREE_BLOB:
        return None, "not_a_link"
    # `/tree` and `/tree/<ref>` with no path still describe the repo itself.
    if len(segments) < 5:
        return root, ""
    path = _path_parts(segments[4:])
    if path is None:
        return None, "not_a_link"
    return _github_path_candidate(owner, repo, segments[3], path, kind), ""


def _raw_candidate(segments: list[str]) -> tuple[dict[str, object] | None, str]:
    """`raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>` -> file candidate."""
    if len(segments) < 4:
        return None, "not_a_link"
    owner = segments[0]
    if owner.lower() in RESERVED_SEGMENTS:
        return None, "reserved_owner"
    repo = _repo_name(segments[1])
    if not _OWNER_RE.match(owner) or not repo:
        return None, "not_a_link"
    path = _path_parts(segments[3:])
    if path is None:
        return None, "not_a_link"
    joined = "/".join(path)
    candidate = _repo_candidate(owner, repo)
    candidate["skill_path"] = joined
    # Rebuilt canonically (https, lowercased host, no query) so the fetcher
    # always gets a usable URL; for a plain paste it is byte-identical.
    candidate["raw_url"] = f"https://{RAW_HOST}/{owner}/{repo}/{segments[2]}/{joined}"
    if path[-1].lower() == _SKILL_FILE:
        candidate["name"] = path[-2] if len(path) > 1 else repo
    else:
        stem = path[-1].rsplit(".", 1)[0] if "." in path[-1] else path[-1]
        candidate["name"] = stem or path[-1]
    return candidate, ""


def _skills_sh_candidate(segments: list[str]) -> tuple[dict[str, object] | None, str]:
    """`skills.sh/<owner>/<repo>[/<skill>]` -> skills.sh candidate."""
    if not segments:
        return None, "not_a_link"
    if segments[0].lower() in RESERVED_SEGMENTS:
        return None, "reserved_owner"
    if len(segments) < 2:
        return None, "not_a_link"
    owner = segments[0]
    repo = _repo_name(segments[1])
    if not _OWNER_RE.match(owner) or not repo:
        return None, "not_a_link"
    full_name = f"{owner}/{repo}"
    if len(segments) < 3:
        source_id = name = full_name
    else:
        skill = segments[2]
        if not _SEGMENT_RE.match(skill) or skill in {".", ".."}:
            return None, "not_a_link"
        source_id = f"{full_name}/{skill}"
        name = skill
    return {
        "source": SKILLS_SH_SOURCE,
        "source_id": source_id,
        "full_name": full_name,
        "name": name,
        "url": f"https://{SKILLS_SH_HOST}/{source_id}",
        "skill_path": "",
        "raw_url": "",
    }, ""


def _from_host(kind: str, segments: list[str]) -> tuple[dict[str, object] | None, str]:
    """Dispatch a hosted link to the parser that owns its host."""
    if kind == "github":
        return _github_candidate(segments)
    if kind == "raw":
        return _raw_candidate(segments)
    return _skills_sh_candidate(segments)


def _hostless(segments: list[str]) -> tuple[dict[str, object] | None, str]:
    """No scheme: a `host/...` link, a bare `owner/repo`, or garbage."""
    if not segments:
        return None, "not_a_link"
    head = segments[0].lower().removeprefix("www.")
    kind = _HOST_KINDS.get(head)
    if kind is not None:
        return _from_host(kind, segments[1:])
    if "." in segments[0] and len(segments) > 1:
        # A dotted first segment reads as a host only when a path follows it;
        # a lone dotted word ("README.md") is text, not a link.
        return None, "unsupported_host"
    # Bare shorthand is only trusted as `owner/repo`: a third segment is never
    # guessed into a skill path.
    if len(segments) != 2:
        return None, "not_a_link"
    return _github_candidate(segments)


def _parse_token(token: str) -> tuple[dict[str, object] | None, str]:
    """Parse one cleaned token into `(candidate, "")` or `(None, reason)`."""
    if _SCHEME_RE.match(token):
        try:
            parts = urlsplit(token)
        except ValueError:
            # Malformed netloc (bad IPv6 brackets, NFKC-hostile characters).
            return None, "not_a_link"
        if parts.scheme.lower() not in {"http", "https"}:
            return None, "unsupported_host"
        host = _host_only(parts.netloc)
        segments = _segments(parts.path)
        if not host:
            # `https:/github.com/a/b`: recover the host from the path.
            return _hostless(segments)
        kind = _HOST_KINDS.get(host)
        if kind is None:
            return None, "unsupported_host"
        return _from_host(kind, segments)
    if token.startswith("//"):
        return _parse_token("https:" + token)
    return _hostless(_segments(token))


def _dedupe_key(candidate: dict[str, object]) -> str:
    """Identity of one pasted skill, including its slot so monorepo siblings survive."""
    path = str(candidate.get("skill_path") or "").lower()
    slot = path or str(candidate.get("name") or "").lower()
    return f"{candidate['source']}:{candidate['source_id']}:{slot}".lower()


def parse_skill_links(text: str) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Split pasted text into (candidates, rejects).

    Candidates keep paste order and are deduped case-insensitively on
    `source` + `source_id` + skill slot; every later repeat becomes a
    `duplicate` reject. A reject echoes the token as pasted, so the paste box
    can show the user exactly what it could not use. Text with nothing
    parseable yields empty lists, and no input ever raises.
    """
    candidates: list[dict[str, object]] = []
    rejects: list[dict[str, str]] = []
    if not isinstance(text, str):
        return candidates, rejects
    seen: set[str] = set()
    for raw, token in _split_tokens(text):
        candidate, reason = _parse_token(token)
        if candidate is None:
            if reason:
                rejects.append({"input": raw, "reason": reason})
            continue
        key = _dedupe_key(candidate)
        if key in seen:
            rejects.append({"input": raw, "reason": "duplicate"})
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates, rejects


def looks_like_skill_links(text: str) -> bool:
    """True when the text contains at least one parseable candidate. Never raises."""
    try:
        return bool(parse_skill_links(text)[0])
    except Exception:  # pragma: no cover - a paste box must never break a request
        return False
