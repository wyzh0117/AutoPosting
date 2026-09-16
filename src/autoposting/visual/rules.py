from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from .paths import rules_path


def load_rules() -> dict[str, Any]:
    return json.loads(rules_path().read_text(encoding="utf-8"))


def format_compact_number(value: float | int) -> str:
    number = float(value)
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{text}m"
    if number >= 1000:
        text = f"{number / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    if number == int(number):
        return str(int(number))
    return str(number)


def apply_ranking_rules(payload: dict[str, Any]) -> dict[str, Any]:
    rules = load_rules()
    data = deepcopy(payload)
    items = list(data.get("items") or [])[: int(rules["maxItems"])]
    peak = max((_metric_value(item) for item in items), default=0) or 1
    clipped: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row["description"] = _clip(str(row.get("description") or ""), int(rules["descriptionMaxChars"]))
        row["name"] = _clip(str(row.get("name") or ""), 28)
        metric = dict(row.get("metric") or {})
        value = float(metric.get("value") or 0)
        metric["display"] = str(metric.get("display") or format_compact_number(value))
        row["metric"] = metric
        row["progress"] = max(0.08, min(1.0, value / peak))
        clipped.append(row)
    data["items"] = clipped
    data["title"] = _clip(str(data.get("title") or ""), 24)
    data["subtitle"] = _clip(str(data.get("subtitle") or ""), 24)
    data["tags"] = [str(tag) for tag in (data.get("tags") or [])][: int(rules["maxTags"])]
    data["density"] = "compact" if len(clipped) >= 8 else "comfortable"
    return data


def apply_project_rules(payload: dict[str, Any]) -> dict[str, Any]:
    rules = load_rules()
    data = deepcopy(payload)
    data["name"] = _clip(str(data.get("name") or ""), 28)
    data["tagline"] = _clip(str(data.get("tagline") or ""), int(rules["taglineMaxChars"]))
    data["keyMessage"] = _clip(str(data.get("keyMessage") or ""), int(rules["keyMessageMaxChars"]))
    data["features"] = [str(item).strip() for item in (data.get("features") or []) if str(item).strip()][
        : int(rules["maxFeatures"])
    ]
    data["how"] = [str(item).strip() for item in (data.get("how") or []) if str(item).strip()][
        : int(rules.get("maxHow") or rules["maxWorkflow"])
    ]
    data["audience"] = [str(item).strip() for item in (data.get("audience") or []) if str(item).strip()][
        : int(rules["maxAudience"])
    ]
    data["scene"] = [str(item).strip() for item in (data.get("scene") or []) if str(item).strip()][
        : int(rules.get("maxScene") or rules["maxAudience"])
    ]
    metrics = []
    for metric in data.get("metrics") or []:
        row = dict(metric)
        value = float(row.get("value") or 0)
        row["display"] = str(row.get("display") or format_compact_number(value))
        metrics.append(row)
    data["metrics"] = metrics
    url = str(data.get("url") or "")
    data["url"] = url
    data["urlDisplay"] = _short_url(url, int(rules["urlMaxChars"]))
    story = []
    for beat in data.get("story") or []:
        if not isinstance(beat, dict):
            continue
        caption = _clip(str(beat.get("caption") or ""), 42)
        image = str(beat.get("image") or "")
        if caption or image:
            story.append({"caption": caption, "image": image})
    data["story"] = story[:6]
    return data


def _metric_value(item: dict[str, Any]) -> float:
    metric = item.get("metric") if isinstance(item.get("metric"), dict) else {}
    try:
        return float(metric.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _clip(text: str, limit: int) -> str:
    value = " ".join((text or "").split())
    if limit < 1 or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _short_url(url: str, limit: int) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    compact = f"{host}{path}".removeprefix("www.")
    return _clip(compact, limit)
