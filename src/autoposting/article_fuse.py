from __future__ import annotations


def fuse_hotspot_items(items: list[dict]) -> dict:
    """Concatenate hotspot candidates into one synthetic article payload."""
    if not items:
        raise ValueError("items required")
    titles: list[str] = []
    sections: list[str] = []
    source_ids: list[str] = []
    seen_sources: set[str] = set()
    item_ids: list[str] = []
    for index, item in enumerate(items, start=1):
        title = str(item.get("title") or "").strip()
        body = str(item.get("content_text") or item.get("summary") or "").strip()
        item_id = str(item.get("item_id") or "").strip()
        if item_id:
            item_ids.append(item_id)
        if title:
            titles.append(title)
        heading = f"来源 {index}"
        if title:
            heading = f"{heading}：{title}"
        section = heading if not body else f"{heading}\n{body}"
        sections.append(section)
        raw_ids = item.get("source_ids")
        if raw_ids is None and item.get("source_id"):
            raw_ids = [item.get("source_id")]
        if isinstance(raw_ids, (list, tuple)):
            for source_id in raw_ids:
                value = str(source_id).strip()
                if value and value not in seen_sources:
                    seen_sources.add(value)
                    source_ids.append(value)
    fused_body = "\n\n".join(sections)
    return {
        "title": " / ".join(titles) if titles else "融合选题",
        "content_text": fused_body,
        "summary": fused_body,
        "source_ids": source_ids,
        "item_ids": item_ids,
    }
