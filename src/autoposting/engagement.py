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
    if "reddit.com" in blob:
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
) -> str:
    seen_sources: list[str] = []
    links: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for record in records:
        source_id = str(record.get("source_id") or "").strip()
        if source_id and source_id not in seen_sources:
            seen_sources.append(source_id)
        url = str(record.get("source_url") or record.get("url") or "").strip()
        name = str(record.get("name") or source_id or "来源").strip()
        if url and url not in seen_urls:
            seen_urls.add(url)
            links.append((name, source_id, url))
    source_count = len(seen_sources)
    article_count = len(links) or source_count
    header = f"【信息来源 {source_count} 条 · 原文 {article_count} 篇】"
    text = HEADER_PATTERN.sub("", (body or "").strip()).strip()
    footer_at = text.rfind(FOOTER_MARK)
    if footer_at >= 0:
        text = text[:footer_at].rstrip()
    lines = [header, "", text, "", "原文链接"]
    if links:
        for name, source_id, url in links:
            label = f"{name}（{source_id}）" if source_id else name
            lines.append(f"- {label}：{url}")
    else:
        lines.append("- 暂无可用原文链接")
    if discussion:
        lines.extend(["", PROMOTED_NOTE if promoted else DISCUSSION_NOTE])
    return "\n".join(lines)
