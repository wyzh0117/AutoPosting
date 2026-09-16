"""Discussion heat, dynamic source grade, and article source framing.

S4 starts as a watch signal: forums and frontline posts can become an article.
When public engagement crosses a threshold, the *item* is treated as S3
attention, not as an official confirmation.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlsplit

TIER_RANK = {"S1": 4, "S2": 3, "S3": 2, "S4": 1}
HN_SCORE_THRESHOLD = 50
HN_COMMENT_THRESHOLD = 30
X_TOTAL_THRESHOLD = 200
X_REPOST_THRESHOLD = 50
REDDIT_SCORE_THRESHOLD = 100
REDDIT_COMMENT_THRESHOLD = 50
GENERIC_WEIGHT_THRESHOLD = 100

HEADER_PATTERN = re.compile(r"^【信息来源\s+\d+\s+条\s*·\s*原文\s+\d+\s+篇】\s*")
FOOTER_MARK = "\n原文链接\n"
DISCUSSION_NOTE = (
    "讨论提示：本文整理的是社区或一线讨论，不是官方证实。"
    "文中按「某某称」转述，供关注「大家在聊什么」。"
)
PROMOTED_NOTE = (
    "讨论提示：该话题讨论热度已越过关注阈值，仍非官方声明。"
    "文中按「某某称」转述。"
)
HN_ITEM_RE = re.compile(r"(?:news\.ycombinator\.com/item\?id=|hn_id=)(\d+)")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_HTML_LINK_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_RAW_URL_RE = re.compile(r"https?://[^\s)\]}>，。；、]+", re.IGNORECASE)
_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:来源|信息源|信息来源|原文|原文链接|图片来源|配图来源|参考来源|事实说明)(?:与未知项|索引)?\s*[:：].*$",
    re.IGNORECASE,
)


def _safe_source_url(value: object) -> str:
    """Return only ordinary public link syntax supplied by a source record."""
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or any(char.isspace() for char in url)
    ):
        return ""
    return url


def _plain_text_spans(text: str) -> list[tuple[int, int]]:
    """Identify existing links so source IDs inside their markup are untouched."""
    spans = [match.span() for match in _MARKDOWN_LINK_RE.finditer(text)]
    spans.extend(match.span() for match in _HTML_LINK_RE.finditer(text))
    spans.extend(match.span() for match in _RAW_URL_RE.finditer(text))
    return sorted(spans)


def _replace_first_source_id(text: str, source_id: str, url: str) -> str:
    if not source_id or not url:
        return text
    protected = _plain_text_spans(text)
    pattern = re.compile(
        rf"(?<![\w.-]){re.escape(source_id)}(?![\w.-])"
    )
    for match in pattern.finditer(text):
        start, end = match.span()
        if any(left <= start < right for left, right in protected):
            continue
        sentence_start = max(
            text.rfind(boundary, 0, start) for boundary in "。！？；\n"
        ) + 1
        anchor_start = sentence_start
        prior_links = [
            right
            for left, right in protected
            if sentence_start <= left < start and right <= start
        ]
        if prior_links:
            anchor_start = max(anchor_start, max(prior_links))
        while anchor_start < start and text[anchor_start].isspace():
            anchor_start += 1
        anchor_end = start
        while anchor_end > anchor_start and text[anchor_end - 1] in "，,：:、;； \t":
            anchor_end -= 1
        if anchor_end <= anchor_start:
            continue
        anchor = text[anchor_start:anchor_end]
        if anchor in {
            "来源", "信息源", "信息来源", "原文", "原文链接",
            "图片来源", "配图来源", "参考来源", "事实说明",
        }:
            sentence_end = len(text)
            for boundary in "。！？；\n":
                position = text.find(boundary, end)
                if position >= 0:
                    sentence_end = min(sentence_end, position)
            follow_start = end
            while follow_start < sentence_end and text[follow_start] in "，,：:、;； \t":
                follow_start += 1
            follow_end = sentence_end
            while follow_end > follow_start and text[follow_end - 1] in "，,：:、;； \t":
                follow_end -= 1
            if follow_end <= follow_start:
                continue
            return (
                text[:sentence_start]
                + f"[{text[follow_start:follow_end]}]({url})"
                + text[follow_end:]
            )
        if any(
            left < anchor_end and right > anchor_start for left, right in protected
        ):
            continue
        return text[:anchor_start] + f"[{anchor}]({url})" + text[end:]
    return text


def _inline_source_links(text: str, records: list[tuple[str, str]]) -> str:
    for source_id, url in sorted(records, key=lambda item: len(item[0]), reverse=True):
        text = _replace_first_source_id(text, source_id, url)
    return text


def source_citation_id(source_id: str, occurrence: int) -> str:
    return source_id if occurrence == 1 else f"{source_id}#{occurrence}"


def _source_only_line(line: str, source_ids: tuple[str, ...]) -> bool:
    match = _SOURCE_LINE_RE.match(line)
    if match is None:
        return False
    _prefix, tail = line.split("：", 1) if "：" in line else line.split(":", 1)
    residual = tail
    for source_id in source_ids:
        residual = re.sub(re.escape(source_id), "", residual)
    residual = _MARKDOWN_LINK_RE.sub("", residual)
    residual = _HTML_LINK_RE.sub("", residual)
    residual = _RAW_URL_RE.sub("", residual)
    residual = re.sub(r"[\s,，、。；！？|:：()（）\[\]<>·•—–-]+", "", residual)
    if not residual:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]+", tail.strip()))


def _remove_source_tail(text: str, source_ids: tuple[str, ...]) -> str:
    footer_at = text.rfind(FOOTER_MARK)
    if footer_at >= 0:
        text = text[:footer_at].rstrip()
    lines = [
        line for line in text.splitlines() if not _source_only_line(line, source_ids)
    ]
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class Engagement:
    score: int = 0
    comments: int = 0
    likes: int = 0
    reposts: int = 0
    replies: int = 0

    @property
    def weight(self) -> int:
        return (
            self.score
            + self.comments * 2
            + self.likes
            + self.reposts * 3
            + self.replies * 2
        )

    @property
    def total(self) -> int:
        return self.score + self.comments + self.likes + self.reposts + self.replies

    def as_public(self) -> dict[str, int]:
        return asdict(self)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def engagement_from_raw(raw: object) -> Engagement:
    payload = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return Engagement(
        score=_as_int(payload.get("score") or payload.get("engagement_score")),
        comments=_as_int(
            payload.get("comments")
            or payload.get("descendants")
            or payload.get("engagement_comments")
        ),
        likes=_as_int(payload.get("likes") or payload.get("like_count") or payload.get("engagement_likes")),
        reposts=_as_int(
            payload.get("reposts") or payload.get("retweet_count") or payload.get("engagement_reposts")
        ),
        replies=_as_int(
            payload.get("replies") or payload.get("reply_count") or payload.get("engagement_replies")
        ),
    )


def engagement_kind(source_id: str, url: str = "", source_type: str = "") -> str:
    blob = f"{source_id} {url} {source_type}".lower()
    if source_type == "x" or "x.com/" in blob or "twitter.com/" in blob:
        return "x"
    if "news.ycombinator.com" in blob or source_id == "hn_frontpage":
        return "hn"
    if source_type == "reddit" or "reddit.com" in blob:
        return "reddit"
    return "generic"


def is_promoted(kind: str, engagement: Engagement) -> bool:
    if kind == "hn":
        return engagement.score >= HN_SCORE_THRESHOLD or engagement.comments >= HN_COMMENT_THRESHOLD
    if kind == "x":
        return (
            engagement.likes + engagement.reposts + engagement.replies >= X_TOTAL_THRESHOLD
            or engagement.reposts >= X_REPOST_THRESHOLD
        )
    if kind == "reddit":
        return (
            engagement.score >= REDDIT_SCORE_THRESHOLD
            or engagement.comments >= REDDIT_COMMENT_THRESHOLD
        )
    return engagement.weight >= GENERIC_WEIGHT_THRESHOLD


def better_tier(left: str, right: str) -> str:
    return left if TIER_RANK.get(left, 0) >= TIER_RANK.get(right, 0) else right


def effective_tier(base_tier: str, engagement: Engagement, kind: str) -> str:
    if base_tier != "S4":
        return base_tier
    if is_promoted(kind, engagement):
        return "S3"
    return "S4"


def extract_hn_id(*values: object) -> str:
    for value in values:
        text = str(value or "")
        match = HN_ITEM_RE.search(text)
        if match:
            return match.group(1)
        parts = urlsplit(text)
        if "news.ycombinator.com" in parts.netloc:
            item_id = (parse_qs(parts.query).get("id") or [""])[0]
            if item_id.isdigit():
                return item_id
    return ""


def frame_article_body(
    body: str,
    records: list[dict[str, str]],
    *,
    discussion: bool = False,
    promoted: bool = False,
    inline_sources: bool = False,
) -> str:
    seen_sources: list[str] = []
    links: list[tuple[str, str, str, str]] = []
    seen_urls: set[str] = set()
    source_occurrences: dict[str, int] = {}
    for record in records:
        source_id = str(record.get("source_id") or "").strip()
        if source_id and source_id not in seen_sources:
            seen_sources.append(source_id)
        url = _safe_source_url(record.get("source_url") or record.get("url"))
        name = str(record.get("name") or source_id or "来源").strip()
        if url and url not in seen_urls:
            seen_urls.add(url)
            source_occurrences[source_id] = source_occurrences.get(source_id, 0) + 1
            links.append(
                (name, source_id, url, str(record.get("citation_id") or "").strip()
                 or source_citation_id(source_id, source_occurrences[source_id]))
            )
    text = HEADER_PATTERN.sub("", (body or "").strip()).strip()
    if inline_sources:
        source_links: list[tuple[str, str]] = []
        seen_link_source_ids: set[str] = set()
        for _name, source_id, url, citation_id in links:
            if citation_id and citation_id not in seen_link_source_ids:
                seen_link_source_ids.add(citation_id)
                source_links.append((citation_id, url))
        source_markers = tuple(
            marker
            for _name, source_id, _url, citation_id in links
            for marker in (source_id, citation_id)
            if marker
        )
        text = _remove_source_tail(text, source_markers)
        text = _inline_source_links(
            text,
            source_links,
        )
        if discussion:
            text = "\n".join(
                [text, "", PROMOTED_NOTE if promoted else DISCUSSION_NOTE]
            ).strip()
        return text

    source_count = len(seen_sources)
    article_count = len(links) or source_count
    header = f"【信息来源 {source_count} 条 · 原文 {article_count} 篇】"
    footer_at = text.rfind(FOOTER_MARK)
    if footer_at >= 0:
        text = text[:footer_at].rstrip()
    lines = [header, "", text, "", "原文链接"]
    if links:
        for name, source_id, url, _citation_id_value in links:
            label = f"{name}（{source_id}）" if source_id else name
            lines.append(f"- {label}：{url}")
    else:
        lines.append("- 暂无可用原文链接")
    if discussion:
        lines.extend(["", PROMOTED_NOTE if promoted else DISCUSSION_NOTE])
    return "\n".join(lines)
