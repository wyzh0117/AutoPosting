from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

from .paths import tokens_path

PALETTE_KEYS = ("main", "secondary", "accent")

# The geometric weekly templates predate the generic visual token set. Keep
# their established colors as the picker defaults while still routing every
# visible template color through the same palette.
WEEKLY_PALETTE_DEFAULTS = {
    "dark": {"main": "#06155A", "secondary": "#101F63", "accent": "#F3F0E8"},
    "light": {"main": "#F3EFE4", "secondary": "#FFFCF6", "accent": "#5C4FD6"},
}


def default_weekly_palette(theme: str = "dark") -> dict[str, str]:
    name = "light" if str(theme).strip().lower() == "light" else "dark"
    return dict(WEEKLY_PALETTE_DEFAULTS[name])


@lru_cache(maxsize=1)
def load_tokens() -> dict[str, Any]:
    path = tokens_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"tokens must be an object: {path}")
    return payload


def token(path: str) -> Any:
    current: Any = load_tokens()
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def parse_palette(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    palette: dict[str, str] = {}
    for key in PALETTE_KEYS:
        normalized = normalize_hex(str(raw.get(key) or ""))
        if normalized:
            palette[key] = normalized
    return palette


def resolve_tokens(theme: str = "dark", palette: dict[str, str] | None = None) -> dict[str, Any]:
    tokens = copy.deepcopy(load_tokens())
    name = "light" if str(theme).strip().lower() == "light" else "dark"
    themed = tokens.get("themes") if isinstance(tokens.get("themes"), dict) else {}
    overlay = themed.get(name) if isinstance(themed, dict) else None
    if isinstance(overlay, dict) and isinstance(overlay.get("color"), dict):
        tokens["color"] = copy.deepcopy(overlay["color"])
    apply_palette(tokens, palette or {}, name)
    tokens["meta"] = dict(tokens.get("meta") or {})
    tokens["meta"]["theme"] = name
    return tokens


def apply_palette(tokens: dict[str, Any], palette: dict[str, str], theme: str) -> None:
    color = tokens.setdefault("color", {})
    main = normalize_hex(palette.get("main", ""))
    secondary = normalize_hex(palette.get("secondary", ""))
    accent = normalize_hex(palette.get("accent", ""))
    if main:
        color["background"] = main
    if secondary:
        color["surface"] = secondary
        color["surfaceRaised"] = mix_hex(secondary, main or secondary, 0.22)
        color["border"] = mix_hex(secondary, accent or secondary, 0.18)
        color["progressTrack"] = mix_hex(secondary, main or secondary, 0.35)
    if accent:
        color["primary"] = accent
        color["progressFill"] = accent
        color["primaryMuted"] = rgba(accent, 0.16)
        color["glow"] = rgba(accent, 0.20)
        color["backgroundGrid"] = rgba(accent, 0.05)
        color["wash"] = rgba(accent, 0.08)
        color["quote"] = mix_hex(accent, str(color.get("text", {}).get("primary") or "#F3F0E8"), 0.35)
        color["text"] = dict(color.get("text") or {})
        color["text"]["onPrimary"] = contrast_ink(accent)
    background = str(color.get("background") or "#0B0D12")
    ink = contrast_ink(background)
    muted = mix_hex(ink, background, 0.42)
    color.setdefault("text", {})
    if theme == "light" or luminance(background) >= 0.45:
        color["text"]["primary"] = color["text"].get("primary") if not main else ink
        if main:
            color["text"]["primary"] = ink
            color["text"]["secondary"] = mix_hex(ink, background, 0.32)
            color["text"]["muted"] = mix_hex(ink, background, 0.5)
        color["rank"] = color["text"]["primary"]
        color["rankMuted"] = color["text"].get("muted") or muted
    elif main:
        color["text"]["primary"] = ink
        color["text"]["secondary"] = mix_hex(ink, background, 0.28)
        color["text"]["muted"] = mix_hex(ink, background, 0.48)
        color["rank"] = ink
        color["rankMuted"] = color["text"]["muted"]


def apply_type_scale(tokens: dict[str, Any], multiplier: float) -> dict[str, Any]:
    scaled = copy.deepcopy(tokens)
    factor = float(multiplier)
    typography = scaled.get("typography") if isinstance(scaled.get("typography"), dict) else {}
    for spec in typography.values():
        if isinstance(spec, dict) and isinstance(spec.get("size"), (int, float)):
            spec["size"] = round(spec["size"] * factor)
    sizes = scaled.get("fontSize") if isinstance(scaled.get("fontSize"), dict) else {}
    for key, value in list(sizes.items()):
        if isinstance(value, (int, float)):
            sizes[key] = round(value * factor)
    return scaled


def flatten_tokens(node: Any = None, prefix: str = "") -> dict[str, str]:
    if node is None:
        node = load_tokens()
    flat: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"meta", "themes"}:
                continue
            child = f"{prefix}-{key}" if prefix else key
            if isinstance(value, dict) and any(isinstance(item, (dict, list)) for item in value.values()):
                flat.update(flatten_tokens(value, child))
            elif isinstance(value, dict) and all(
                not isinstance(item, (dict, list)) for item in value.values()
            ):
                for inner_key, inner_value in value.items():
                    flat[f"{child}-{inner_key}"] = _css_value(inner_value)
            else:
                flat[child] = _css_value(value)
    return flat


def normalize_hex(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        return ""
    try:
        int(text, 16)
    except ValueError:
        return ""
    return f"#{text.upper()}"


def mix_hex(left: str, right: str, amount: float) -> str:
    first = _rgb(normalize_hex(left) or "#000000")
    second = _rgb(normalize_hex(right) or "#000000")
    t = min(1.0, max(0.0, amount))
    return _hex(tuple(int(a + (b - a) * t) for a, b in zip(first, second)))


def rgba(color: str, alpha: float) -> str:
    red, green, blue = _rgb(normalize_hex(color) or "#000000")
    return f"rgba({red}, {green}, {blue}, {min(1.0, max(0.0, alpha)):.3f})"


def contrast_ink(background: str) -> str:
    return "#1A1C22" if luminance(background) >= 0.45 else "#F3F0E8"


def luminance(color: str) -> float:
    red, green, blue = _rgb(normalize_hex(color) or "#000000")
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255


def _rgb(color: str) -> tuple[int, int, int]:
    text = color[1:]
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _hex(parts: tuple[int, ...]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, part)) for part in parts))


def _css_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
