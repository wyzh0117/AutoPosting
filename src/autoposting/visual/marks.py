"""Themed SVG marks with a tagged local catalog. Never use GitHub author avatars."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .icons import svg_id

AVATAR_RE = re.compile(
    r"(avatars\.githubusercontent\.com|gravatar\.com|github\.com/[^/]+\.png(?:\?|$))",
    re.I,
)
LOGO_HINT = re.compile(r"(logo|icon|mark|brand|wordmark|avatar@?2x)", re.I)

# Only attach tags that actually describe the drawing. No generic "ai" / "开源".
TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "agent": ("agent", "agents", "multi-agent", "多智能体", "团队", "角色", "协作"),
    "cli": ("cli", "terminal", "command", "终端", "命令行"),
    "mcp": ("mcp", "plugin", "插件"),
    "rag": ("rag", "retrieval", "检索", "知识库"),
    "model": ("model", "llm", "gpt", "模型"),
    "dataset": ("dataset", "corpus", "数据集", "语料"),
    "webui": ("webui", "ui", "dashboard", "网页", "界面"),
    "image": ("image", "vision", "绘图", "图像", "图片"),
    "chat": ("chat", "对话", "聊天"),
    "workflow": ("workflow", "pipeline", "工作流", "自动化"),
    "code": ("sdk", "framework", "框架", "sdk"),
}

FAMILY_ORDER = (
    "agent",
    "mcp",
    "rag",
    "cli",
    "image",
    "chat",
    "workflow",
    "webui",
    "dataset",
    "model",
    "code",
)


def is_author_avatar(url: str) -> bool:
    text = (url or "").strip()
    if not text:
        return False
    return AVATAR_RE.search(text) is not None


def looks_like_logo(url: str, alt: str = "") -> bool:
    name = url.rsplit("/", 1)[-1]
    return bool(LOGO_HINT.search(name) or LOGO_HINT.search(alt or ""))


def content_tokens(kind: str = "", topics: Iterable[str] | None = None, description: str = "", name: str = "") -> set[str]:
    blob = " ".join(
        [kind or "", name or "", description or "", " ".join(str(item) for item in (topics or []))]
    ).lower()
    found: set[str] = set()
    for tag, synonyms in TAG_SYNONYMS.items():
        if any(token in blob for token in synonyms) or tag in blob:
            found.add(tag)
    return found


def resolve_mark(
    *,
    name: str,
    kind: str = "",
    topics: Iterable[str] | None = None,
    description: str = "",
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    tokens = content_tokens(kind, topics, description, name)
    family = next((item for item in FAMILY_ORDER if item in tokens), "generic")
    tags = _tags_for_family(family, tokens)
    catalog_path = Path(catalog_dir) if catalog_dir else None
    existing = _load_catalog(catalog_path)
    match = _best_match(existing, tokens)
    if match is not None:
        return match
    svg = themed_svg(name or family, family, seed=name or family)
    mark_id = _unique_id(family, existing)
    record = {"id": mark_id, "tags": tags, "svg": svg, "file": f"{mark_id}.svg"}
    if catalog_path is not None:
        _save_mark(catalog_path, record)
    return record


def family_from_kind(kind: str = "", name: str = "", description: str = "") -> str:
    tokens = content_tokens(kind, None, description, name)
    return next((item for item in FAMILY_ORDER if item in tokens), "generic")


def themed_svg(name: str, family: str = "generic", seed: str = "") -> str:
    resolved = family if family in _FAMILY_DRAWINGS else family_from_kind(family, name)
    uid = svg_id("mark", seed or name or resolved)
    inner = _FAMILY_DRAWINGS.get(resolved, _draw_spark)(uid)
    return _frame(uid, inner)


def _tags_for_family(family: str, tokens: set[str]) -> list[str]:
    if family == "generic":
        return []
    tags = [family]
    extras = [tag for tag in sorted(tokens) if tag != family][:2]
    tags.extend(extras)
    return tags


def _best_match(catalog: list[dict[str, Any]], tokens: set[str]) -> dict[str, Any] | None:
    if not tokens:
        return None
    scored: list[tuple[int, dict[str, Any]]] = []
    for mark in catalog:
        tags = {str(tag) for tag in (mark.get("tags") or []) if tag}
        overlap = tags & tokens
        if not overlap:
            continue
        extra = tags - tokens
        score = len(overlap) * 10 - len(extra) * 4
        if score > 0:
            scored.append((score, mark))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _load_catalog(catalog_dir: Path | None) -> list[dict[str, Any]]:
    if catalog_dir is None:
        return []
    index = catalog_dir / "catalog.json"
    if not index.is_file():
        return []
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    marks = payload.get("marks") if isinstance(payload, dict) else payload
    if not isinstance(marks, list):
        return []
    loaded: list[dict[str, Any]] = []
    for item in marks:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        row = dict(item)
        if not row.get("svg"):
            file_name = str(row.get("file") or f"{row['id']}.svg")
            path = catalog_dir / file_name
            if path.is_file():
                row["svg"] = path.read_text(encoding="utf-8")
        if row.get("svg"):
            loaded.append(row)
    return loaded


def _save_mark(catalog_dir: Path, record: dict[str, Any]) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    svg_path = catalog_dir / str(record["file"])
    svg_path.write_text(str(record["svg"]), encoding="utf-8")
    existing = _load_catalog(catalog_dir)
    by_id = {str(item.get("id")): item for item in existing}
    by_id[str(record["id"])] = {
        "id": record["id"],
        "tags": list(record.get("tags") or []),
        "file": record["file"],
        "svg": record["svg"],
    }
    payload = {
        "marks": [
            {"id": item["id"], "tags": item.get("tags") or [], "file": item.get("file")}
            for item in by_id.values()
        ]
    }
    (catalog_dir / "catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _unique_id(family: str, existing: list[dict[str, Any]]) -> str:
    used = {str(item.get("id")) for item in existing}
    index = 1
    while True:
        candidate = f"{family}-{index:02d}"
        if candidate not in used:
            return candidate
        index += 1


def _frame(uid: str, inner: str) -> str:
    return f"""<svg class="c-logo-3d-svg" viewBox="0 0 128 128" aria-hidden="true">
  <defs>
    <linearGradient id="{uid}-face" x1="18%" y1="8%" x2="88%" y2="96%">
      <stop offset="0%" stop-color="currentColor" stop-opacity="0.95"/>
      <stop offset="55%" stop-color="currentColor" stop-opacity="0.72"/>
      <stop offset="100%" stop-color="currentColor" stop-opacity="0.42"/>
    </linearGradient>
  </defs>
  <rect x="8" y="8" width="112" height="112" rx="32" fill="url(#{uid}-face)"/>
  {inner}
</svg>"""


def _draw_team(uid: str) -> str:
    return (
        '<circle cx="44" cy="50" r="10" fill="#F3F0E8"/>'
        '<circle cx="84" cy="50" r="10" fill="#F3F0E8"/>'
        '<circle cx="64" cy="78" r="10" fill="#F3F0E8"/>'
        '<path d="M28 96c2-12 10-18 16-18s14 6 16 18M68 96c2-12 10-18 16-18s14 6 16 18" '
        'fill="none" stroke="#F3F0E8" stroke-width="5" stroke-linecap="round"/>'
    )


def _draw_terminal(uid: str) -> str:
    return (
        '<rect x="30" y="38" width="68" height="52" rx="10" fill="none" stroke="#F3F0E8" stroke-width="5"/>'
        '<path d="M40 56l12 8-12 8M56 74h22" fill="none" stroke="#F3F0E8" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    )


def _draw_plug(uid: str) -> str:
    return (
        '<rect x="48" y="34" width="32" height="22" rx="6" fill="#F3F0E8"/>'
        '<path d="M56 34V24M72 34V24M64 56v36" fill="none" stroke="#F3F0E8" stroke-width="5" '
        'stroke-linecap="round"/>'
        '<circle cx="64" cy="98" r="7" fill="#F3F0E8"/>'
    )


def _draw_search(uid: str) -> str:
    return (
        '<circle cx="56" cy="56" r="18" fill="none" stroke="#F3F0E8" stroke-width="6"/>'
        '<path d="M70 70l18 18" fill="none" stroke="#F3F0E8" stroke-width="6" stroke-linecap="round"/>'
    )


def _draw_layers(uid: str) -> str:
    return (
        '<path d="M64 32l28 14-28 14L36 46z" fill="none" stroke="#F3F0E8" stroke-width="5"/>'
        '<path d="M36 64l28 14 28-14M36 80l28 14 28-14" fill="none" stroke="#F3F0E8" '
        'stroke-width="5" stroke-linecap="round"/>'
    )


def _draw_stack(uid: str) -> str:
    return (
        '<rect x="36" y="36" width="56" height="16" rx="5" fill="#F3F0E8"/>'
        '<rect x="36" y="56" width="56" height="16" rx="5" fill="#F3F0E8" opacity="0.75"/>'
        '<rect x="36" y="76" width="56" height="16" rx="5" fill="#F3F0E8" opacity="0.5"/>'
    )


def _draw_window(uid: str) -> str:
    return (
        '<rect x="30" y="36" width="68" height="56" rx="10" fill="none" stroke="#F3F0E8" stroke-width="5"/>'
        '<path d="M30 52h68M42 44h4M52 44h4" fill="none" stroke="#F3F0E8" stroke-width="5" '
        'stroke-linecap="round"/>'
    )


def _draw_frame(uid: str) -> str:
    return (
        '<rect x="34" y="36" width="60" height="56" rx="10" fill="none" stroke="#F3F0E8" stroke-width="5"/>'
        '<circle cx="50" cy="54" r="6" fill="#F3F0E8"/>'
        '<path d="M40 84l16-18 12 12 8-8 18 14" fill="none" stroke="#F3F0E8" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    )


def _draw_bubbles(uid: str) -> str:
    return (
        '<rect x="30" y="36" width="48" height="32" rx="12" fill="#F3F0E8"/>'
        '<rect x="50" y="64" width="48" height="28" rx="12" fill="#F3F0E8" opacity="0.7"/>'
    )


def _draw_nodes(uid: str) -> str:
    return (
        '<circle cx="40" cy="64" r="9" fill="#F3F0E8"/>'
        '<circle cx="64" cy="44" r="9" fill="#F3F0E8"/>'
        '<circle cx="88" cy="64" r="9" fill="#F3F0E8"/>'
        '<circle cx="64" cy="88" r="9" fill="#F3F0E8"/>'
        '<path d="M47 58l10-10M75 50l8 8M72 80l-8-8M56 80l-8-8" fill="none" stroke="#F3F0E8" '
        'stroke-width="4" stroke-linecap="round"/>'
    )


def _draw_brackets(uid: str) -> str:
    return (
        '<path d="M48 36h-10v56h10M80 36h10v56H80" fill="none" stroke="#F3F0E8" stroke-width="6" '
        'stroke-linecap="round"/>'
        '<path d="M56 64h16" fill="none" stroke="#F3F0E8" stroke-width="6" stroke-linecap="round"/>'
    )


def _draw_spark(uid: str) -> str:
    return (
        '<path d="M64 28l8 24h26l-22 16 8 26-20-14-20 14 8-26-22-16h26z" fill="#F3F0E8"/>'
    )


_FAMILY_DRAWINGS = {
    "agent": _draw_team,
    "cli": _draw_terminal,
    "mcp": _draw_plug,
    "rag": _draw_search,
    "model": _draw_layers,
    "dataset": _draw_stack,
    "webui": _draw_window,
    "image": _draw_frame,
    "chat": _draw_bubbles,
    "workflow": _draw_nodes,
    "code": _draw_brackets,
    "generic": _draw_spark,
}
