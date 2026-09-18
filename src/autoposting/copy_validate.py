"""Format-only poster copy checks. Writing rules live in the prompt files."""

from __future__ import annotations

from .llm import LLMError

COPY_FIELDS = ("summary", "does", "how", "audience", "scene")
ENRICHMENT_CODE = "llm_enrichment_failed"


def enrichment_error(detail: str, *, cause_code: str | None = None) -> LLMError:
    return LLMError(ENRICHMENT_CODE, f"LLM_ENRICHMENT_FAILED: {detail}", cause_code=cause_code)


def _norm_key(value: object) -> str:
    return str(value or "").strip().lower()


def _short_name(full_name: str, name: str = "") -> str:
    if name.strip():
        return name.strip()
    parts = [part for part in full_name.split("/") if part]
    return parts[-1] if parts else ""


def validate_copy_items(
    repos: list[dict[str, object]], items: object
) -> dict[str, dict[str, object]]:
    """Every input repo must have a matching item with non-empty copy fields.

    ``full_name`` is preferred. A unique short skill name also matches, so a
    model that only returns ``video-inpainting`` can still fill
    ``owner/repo/video-inpainting``.
    """
    if not isinstance(items, list):
        raise enrichment_error('missing field "items"')
    by_full: dict[str, dict[str, object]] = {}
    by_short: dict[str, list[dict[str, object]]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise enrichment_error("items entry is not an object")
        full_name = str(item.get("full_name") or "").strip()
        name = str(item.get("name") or "").strip()
        if not full_name and not name:
            raise enrichment_error('missing field "full_name"')
        if full_name:
            by_full[_norm_key(full_name)] = item
        short = _short_name(full_name, name)
        if short:
            by_short.setdefault(_norm_key(short), []).append(item)
    input_shorts: dict[str, int] = {}
    for repo in repos:
        short = _norm_key(
            _short_name(str(repo.get("full_name") or ""), str(repo.get("name") or ""))
        )
        if short:
            input_shorts[short] = input_shorts.get(short, 0) + 1
    matched: dict[str, dict[str, object]] = {}
    for repo in repos:
        full_name = str(repo.get("full_name") or "").strip()
        name = str(repo.get("name") or "").strip()
        incoming = by_full.get(_norm_key(full_name))
        if incoming is None:
            short = _norm_key(_short_name(full_name, name))
            candidates = by_short.get(short) or []
            if len(candidates) == 1 and input_shorts.get(short, 0) == 1:
                incoming = candidates[0]
        if incoming is None:
            raise enrichment_error(f'missing item for "{full_name or name}"')
        label = full_name or name
        for field in COPY_FIELDS:
            value = incoming.get(field)
            if not isinstance(value, str) or not value.strip():
                raise enrichment_error(f'missing field "{field}" for "{label}"')
        matched[full_name or name] = incoming
    return matched


def require_enriched_copy(repos: list[dict[str, object]]) -> None:
    """Snapshot already merged from the LLM must still carry every copy field."""
    if not repos:
        raise enrichment_error("enrichment not completed")
    for repo in repos:
        if not bool(repo.get("enriched")):
            raise enrichment_error("enrichment not completed")
        full_name = str(repo.get("full_name") or "")
        for field in COPY_FIELDS:
            value = repo.get(field)
            if not isinstance(value, str) or not value.strip():
                raise enrichment_error(f'missing field "{field}" for "{full_name}"')
