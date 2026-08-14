from __future__ import annotations

import re

from .background import _tokens

CONTROVERSY_MARKERS = ("争议", "舆论", "风波", "质疑", "热议", "丑闻")
PROGRAM_MARKERS = ("试点班", "医学院", "计划", "通道")
EMPTY_HOOKS_MESSAGE = "未发现可用切口"
_HOOK_ID_QUOTES = str.maketrans("", "", "\"'“”‘’「」")


def normalize_hook_id(phrase: str) -> str:
    text = " ".join((phrase or "").translate(_HOOK_ID_QUOTES).split()).casefold()
    return re.sub(r"\s+", "", text)


def compact_phrase(phrase: str) -> str:
    return normalize_hook_id(phrase)


def extract_rule_phrases(excerpt: str) -> list[dict[str, str]]:
    text = excerpt or ""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(phrase: str, kind: str) -> None:
        # Drop whitespace so「4+4 试点班」stays one unit (not a "4+4 试" fragment).
        cleaned = re.sub(r"\s+", "", phrase or "")
        hook_id = normalize_hook_id(cleaned)
        if len(cleaned) < 2 or hook_id in seen:
            return
        seen.add(hook_id)
        found.append({"phrase": cleaned, "kind": kind})

    # Allow spaces/quotes so「协和医学院“4+4 试点班”」stays one program phrase.
    _program_prefix = r"[\u4e00-\u9fffA-Za-z0-9+＋\-—\s\"'“”‘’「」]{0,16}"
    for marker in PROGRAM_MARKERS:
        for match in re.finditer(_program_prefix + re.escape(marker), text):
            add(match.group(0), "program")
    for token in _tokens(text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) < 4:
            continue
        kind = "other"
        if any(mark in token for mark in PROGRAM_MARKERS):
            kind = "program"
        elif token.endswith("大学") or token.endswith("学院") or token.endswith("医院"):
            kind = "org"
        add(token, kind)
    return found


def result_blob(result: dict[str, str]) -> str:
    return f"{result.get('title', '')} {result.get('snippet', '')}"


def result_matches_phrase(result: dict[str, str], phrase: str) -> bool:
    blob = result_blob(result)
    compact_blob = compact_phrase(blob)
    compact = compact_phrase(phrase)
    if phrase in blob or (compact and compact in compact_blob):
        return True
    # Design: also hit compact fragments (e.g. 4+4) after quote/space strip.
    fragments = re.findall(r"[0-9]+[+＋][0-9]+|[\u4e00-\u9fff]{2,}", phrase or "")
    return any(compact_phrase(frag) in compact_blob for frag in fragments if compact_phrase(frag))


def result_has_controversy(result: dict[str, str]) -> bool:
    blob = result_blob(result)
    return any(marker in blob for marker in CONTROVERSY_MARKERS)


def filter_results(phrase: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for result in results:
        if result_matches_phrase(result, phrase) and result_has_controversy(result):
            kept.append(result)
    return kept


def sanitize_chosen_hooks(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        phrase = item.get("phrase")
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        phrase = phrase.strip()
        hook_id = item.get("hook_id")
        if not isinstance(hook_id, str) or not hook_id.strip():
            hook_id = normalize_hook_id(phrase)
        results = item.get("results")
        if not isinstance(results, list):
            results = []
        why = item.get("why") if isinstance(item.get("why"), str) else ""
        kind = item.get("kind") if item.get("kind") in {"org", "program", "person", "other"} else "other"
        cleaned.append(
            {
                "hook_id": hook_id,
                "phrase": phrase,
                "kind": kind,
                "why": why,
                "results": [
                    {
                        "title": str(row.get("title") or ""),
                        "url": str(row.get("url") or ""),
                        "snippet": str(row.get("snippet") or ""),
                    }
                    for row in results
                    if isinstance(row, dict)
                ][:3],
            }
        )
    return cleaned
