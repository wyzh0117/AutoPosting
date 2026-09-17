"""Format-only poster copy checks. Writing rules live in the prompt files."""

from __future__ import annotations

from .llm import LLMError

COPY_FIELDS = ("summary", "does", "how", "audience", "scene")
ENRICHMENT_CODE = "llm_enrichment_failed"


def enrichment_error(detail: str, *, cause_code: str | None = None) -> LLMError:
    return LLMError(ENRICHMENT_CODE, f"LLM_ENRICHMENT_FAILED: {detail}", cause_code=cause_code)


def validate_copy_items(
    repos: list[dict[str, object]], items: object
) -> dict[str, dict[str, object]]:
    """Every input repo must have a matching item with non-empty copy fields."""
    if not isinstance(items, list):
        raise enrichment_error('missing field "items"')
    by_name: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise enrichment_error("items entry is not an object")
        full_name = str(item.get("full_name") or "").strip()
        if not full_name:
            raise enrichment_error('missing field "full_name"')
        by_name[full_name] = item
    for repo in repos:
        full_name = str(repo.get("full_name") or "").strip()
        incoming = by_name.get(full_name)
        if incoming is None:
            raise enrichment_error(f'missing item for "{full_name}"')
        for field in COPY_FIELDS:
            value = incoming.get(field)
            if not isinstance(value, str) or not value.strip():
                raise enrichment_error(f'missing field "{field}" for "{full_name}"')
    return by_name


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
