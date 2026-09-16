from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .pages import render_html
from .schema import infer_kind, load_example, validate_content
from .screenshot import screenshot_html
from .tokens import apply_type_scale, load_tokens, resolve_tokens

TYPE_SCALES = (
    ("scale-a-current", 1.0, "当前字号"),
    ("scale-b-medium", 1.18, "中号 3:4"),
    ("scale-c-large", 1.32, "大号 3:4"),
    ("scale-d-xlarge", 1.48, "特大号 3:4"),
)

PNG_WIDTH = 1080
PNG_HEIGHT = 1440


def render_page(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    screenshot: bool = True,
    tokens: dict[str, Any] | None = None,
    file_stem: str | None = None,
    screenshot_fn: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    kind = infer_kind(payload)
    validate_content(kind, payload)
    resolved = tokens or load_tokens()
    html = render_html(payload, resolved)
    page = str(payload["page"])
    stem = file_stem or page
    canvas = resolved.get("canvas") if isinstance(resolved.get("canvas"), dict) else load_tokens()["canvas"]
    result: dict[str, Any] = {
        "page": page,
        "kind": kind,
        "html_text": html,
        "html": "",
        "png": "",
        "width": int(canvas["width"]),
        "height": int(canvas["height"]),
    }
    if out_dir is None:
        return result
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{stem}.html"
    html_path.write_text(html, encoding="utf-8")
    result["html"] = str(html_path)
    if screenshot:
        shot = screenshot_fn or screenshot_html
        png_path = shot(
            html_path,
            out_dir / f"{stem}.png",
            width=int(canvas["width"]),
            height=int(canvas["height"]),
        )
        result["png"] = str(png_path)
    return result


def render_examples(out_dir: Path, *, screenshot: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind in ("ranking", "project"):
        payload = load_example(kind)
        target = out_dir / str(payload["page"])
        rows.append(render_page(payload, out_dir=target, screenshot=screenshot))
    return rows


def render_type_scale_examples(out_dir: Path, *, screenshot: bool = True) -> list[dict[str, Any]]:
    payload = load_example("project")
    rows: list[dict[str, Any]] = []
    for name, multiplier, _label in TYPE_SCALES:
        tokens = apply_type_scale(resolve_tokens("dark"), multiplier)
        rows.append(
            render_page(
                payload,
                out_dir=out_dir / name,
                screenshot=screenshot,
                tokens=tokens,
                file_stem="project-detail",
            )
        )
        rows[-1]["scale"] = name
        rows[-1]["multiplier"] = multiplier
    return rows
