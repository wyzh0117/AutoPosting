from __future__ import annotations

import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .background import _tokens

CONTROVERSY_MARKERS = ("争议", "舆论", "风波", "质疑", "热议", "丑闻")
PROGRAM_MARKERS = ("试点班", "医学院", "计划", "通道")
EMPTY_HOOKS_MESSAGE = "未发现可用切口"
MAX_SEARCH = 8
MAX_CARDS = 8
SEARCH_WORKERS = 4
SEARCH_TIMEOUT_SECONDS = 8
FIND_DEADLINE_SECONDS = 25
_HOOK_ID_QUOTES = str.maketrans("", "", "\"'“”‘’「」")
_KIND_ORDER = {"program": 0, "org": 1, "person": 2, "other": 3}
_ALLOWED_KINDS = frozenset(_KIND_ORDER)


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


def _controversy_hit_count(blob: str) -> int:
    total = 0
    for marker in CONTROVERSY_MARKERS:
        start = 0
        while True:
            index = blob.find(marker, start)
            if index < 0:
                break
            # "无风波" / "无争议" is a denial, not a hit.
            if not (index > 0 and blob[index - 1] == "无"):
                total += 1
            start = index + len(marker)
    return total


def result_has_controversy(result: dict[str, str]) -> bool:
    return _controversy_hit_count(result_blob(result)) > 0


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


def merge_candidates(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    index_by_id: dict[str, int] = {}
    for group in groups:
        if not group:
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            phrase = item.get("phrase")
            if not isinstance(phrase, str) or not phrase.strip():
                continue
            phrase = phrase.strip()
            hook_id = normalize_hook_id(phrase)
            if not hook_id:
                continue
            kind = item.get("kind") if item.get("kind") in _ALLOWED_KINDS else "other"
            incoming = {"phrase": phrase, "kind": kind}
            existing_index = index_by_id.get(hook_id)
            if existing_index is None:
                index_by_id[hook_id] = len(merged)
                merged.append(incoming)
                continue
            if len(phrase) > len(merged[existing_index]["phrase"]):
                merged[existing_index] = incoming
    return merged


def _overlaps_title(phrase: str, title_tokens: set[str]) -> bool:
    return bool(_tokens(phrase) & title_tokens)


def _queue_sort_key(item: dict[str, str]) -> tuple[int, int]:
    kind_rank = _KIND_ORDER.get(item.get("kind", ""), 3)
    return (kind_rank, -len(item.get("phrase") or ""))


def select_search_queue(
    candidates: list[dict[str, str]], title: str, limit: int = MAX_SEARCH
) -> list[dict[str, str]]:
    title_tokens = _tokens(title)
    title_hits: list[dict[str, str]] = []
    others: list[dict[str, str]] = []
    for item in candidates:
        phrase = item.get("phrase") if isinstance(item, dict) else None
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        if _overlaps_title(phrase, title_tokens):
            title_hits.append(item)
        else:
            others.append(item)
    others.sort(key=_queue_sort_key)
    title_hits.sort(key=_queue_sort_key)
    return (others + title_hits)[:limit]


def _controversy_marker_count(results: list[dict[str, str]]) -> int:
    return sum(_controversy_hit_count(result_blob(result)) for result in results)


def build_hook_card(phrase: str, kind: str, results: list[dict[str, str]]) -> dict[str, object]:
    first_title = ""
    if results:
        first_title = str(results[0].get("title") or "")
    cleaned = sanitize_chosen_hooks(
        [
            {
                "phrase": phrase,
                "kind": kind,
                "why": f"公开检索出现：{first_title}",
                "results": results,
            }
        ]
    )
    return cleaned[0] if cleaned else {
        "hook_id": normalize_hook_id(phrase),
        "phrase": phrase,
        "kind": kind if kind in _ALLOWED_KINDS else "other",
        "why": f"公开检索出现：{first_title}",
        "results": [],
    }


def find_hooks(
    title: str,
    excerpt: str,
    searcher,
    extra_candidates: list[dict[str, str]] | None = None,
) -> list[dict[str, object]]:
    merged = merge_candidates(extract_rule_phrases(excerpt), extra_candidates or [])
    queue = select_search_queue(merged, title, limit=MAX_SEARCH)
    if not queue:
        return []

    deadline = time.monotonic() + FIND_DEADLINE_SECONDS
    scored: list[tuple[int, int, dict[str, object]]] = []
    pool = ThreadPoolExecutor(max_workers=SEARCH_WORKERS)
    try:
        future_map = {
            pool.submit(searcher, f"{item['phrase']} 争议"): item for item in queue
        }
        pending = set(future_map)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=min(SEARCH_TIMEOUT_SECONDS, remaining),
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                item = future_map[future]
                try:
                    raw = future.result(timeout=0)
                except (OSError, TimeoutError, ValueError):
                    raw = []
                if not isinstance(raw, list):
                    raw = []
                filtered = filter_results(
                    item["phrase"],
                    [row for row in raw if isinstance(row, dict)],
                )
                if not filtered:
                    continue
                card = build_hook_card(
                    item["phrase"], item.get("kind", "other"), filtered
                )
                scored.append((len(filtered), _controversy_marker_count(filtered), card))
        for leftover in pending:
            leftover.cancel()
    finally:
        # Do not join hung workers; unfinished queries are dropped.
        pool.shutdown(wait=False, cancel_futures=True)

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:MAX_CARDS]]
