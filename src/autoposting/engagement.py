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


def _following_clause(
    text: str,
    start: int,
    end: int,
    sentence_end: int,
    markers: tuple[str, ...] = (),
) -> tuple[int, int]:
    """Byte range of the clause after a citation token, without its separators.

    The clause stops at the next citation token: a link label must never swallow
    a *different* source's machine id, which would print it to the reader.
    """
    follow_start = end
    while follow_start < sentence_end and text[follow_start] in "，,：:、;； \t":
        follow_start += 1
    follow_end = sentence_end
    for marker in markers:
        if not marker:
            continue
        found = text.find(marker, follow_start, follow_end)
        while found >= 0:
            follow_end = min(follow_end, found)
            found = text.find(marker, found + 1, follow_end + len(marker))
    while follow_end > follow_start and text[follow_end - 1] in "，,：:、;； \t":
        follow_end -= 1
    return follow_start, follow_end


def _replace_first_source_id(
    text: str, source_id: str, url: str, markers: tuple[str, ...] = ()
) -> str:
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
        # A label must never swallow another source's token: cut the anchor after
        # the last marker inside it, or fall through to the following clause.
        for marker in markers:
            if not marker:
                continue
            position = text.rfind(marker, anchor_start, anchor_end)
            if position >= 0:
                anchor_start = position + len(marker)
        while anchor_start < anchor_end and text[anchor_start] in "，,：:、;； \t":
            anchor_start += 1
        while anchor_end > anchor_start and text[anchor_end - 1] in "，,：:、;； \t":
            anchor_end -= 1
        if anchor_end <= anchor_start:
            # A sentence-initial token has no clause to wrap in front of it, so
            # link the clause that follows instead.  Dropping the token would
            # leave the article with no attribution for that source and the
            # draft with nothing to validate an edit against.
            sentence_end = len(text)
            for boundary in "。！？；\n":
                position = text.find(boundary, end)
                if position >= 0:
                    sentence_end = min(sentence_end, position)
            follow_start, follow_end = _following_clause(
                text, start, end, sentence_end, markers
            )
            if follow_end > follow_start:
                return (
                    text[:start]
                    + f"[{text[follow_start:follow_end]}]({url})"
                    + text[follow_end:]
                )
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
            follow_start, follow_end = _following_clause(
                text, start, end, sentence_end, markers
            )
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
    markers = tuple(
        sorted({source_id for source_id, _url in records if source_id}, key=len, reverse=True)
    )
    for source_id, url in sorted(records, key=lambda item: len(item[0]), reverse=True):
        text = _replace_first_source_id(text, source_id, url, markers)
    return _strip_leftover_source_markers(text, records)


def _strip_leftover_source_markers(
    text: str, records: list[tuple[str, str]]
) -> str:
    """Remove source ids that survived inlining.

    A source id is a machine token (``openai_news``), never prose: the model is
    told to reference each source, and :func:`_replace_first_source_id` turns the
    first reference into a link.  A second mention — or one whose clause cannot
    be linked — would otherwise be printed verbatim to the reader, reading as a
    non-sequitur inside a sentence.  Drop the token; keep every link already made.
    """
    markers = sorted(
        {
            marker
            for source_id, _url in records
            if source_id
            for marker in (source_id, f"{source_id}#")
        },
        key=len,
        reverse=True,
    )
    if not markers:
        return text
    protected = _plain_text_spans(text)
    pattern = re.compile(
        r"(?<![\w.-])(?:"
        + "|".join(re.escape(marker) for marker in markers)
        + r")\d*(?![\w.-])"
    )
    spans = [
        (start, end)
        for start, end in (match.span() for match in pattern.finditer(text))
        if not any(left <= start < right for left, right in protected)
    ]
    terminal = "。；！？,.;!?)）"
    leading = "，,、；;：:"
    for start, end in reversed(spans):
        # The token sits inside a sentence, so a bare cut leaves a hole
        # ("不再提供 。").  Take the leading separator with it.
        if start > 0 and text[start - 1] == " ":
            if (
                end >= len(text)
                or text[end] in terminal
                or text[end] == " "
                or text[end] in leading
            ):
                # Also avoids a double space ("Luna  的") and a stranded
                # "两个新模型 ：" once the token between them is gone.
                start -= 1
        elif start > 0 and text[start - 1] in "，,、；;：:":
            if end >= len(text) or text[end] in terminal:
                # "第二篇事实，lab_blog。" must not become "第二篇事实，。".
                start -= 1
        text = text[:start] + text[end:]
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


def _has_prose(text: str) -> bool:
    """True when the framed body still reads as an article.

    A body that is nothing but the AIGC disclosure (its source labels having been
    removed) is not worth a reference section.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "AIGC" in stripped and len(stripped) <= 40:
            continue
        return True
    return False


def _append_unlinked_sources(
    text: str, links: list[tuple[str, str, str, str]], candidates: str
) -> str:
    """Keep every source reachable when its citation never became a link.

    A model sometimes lists its sources on one line ("hn_frontpage openai_news")
    instead of naming them in the prose.  That line is not readable, so it is
    dropped — but dropping it must not leave the article with no attribution at
    all (and the draft with no source an edit could be validated against).  Those
    sources get a reference list, the same shape the non-inline frame uses.

    Only a source that really was cited this way is listed: a source whose label
    block was removed is gone by design, and a second url for an already-linked
    source is not an attribution the article ever made.
    """
    if not _has_prose(text):
        return text
    missing: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for name, source_id, url, _citation_id in links:
        if not url or source_id in seen:
            continue
        if url in text:
            # Already attributed in the prose; a second url for the same source
            # is not a citation the article ever made.
            seen.add(source_id)
            continue
        if not re.search(rf"(?<![\w.-]){re.escape(source_id)}(?![\w.-])", candidates):
            continue
        seen.add(source_id)
        missing.append((name, source_id, url))
    if not missing:
        return text
    lines = [text.rstrip(), "", "原文链接"]
    for name, source_id, url in missing:
        label = f"{name}（{source_id}）" if source_id else name
        lines.append(f"- {label}：{url}")
    return "\n".join(lines).strip()


def frame_article_body(
    body: str,
    records: list[dict[str, str]],
    *,
    discussion: bool = False,
    promoted: bool = False,
    inline_sources: bool = False,
    extra_source_ids: tuple[str, ...] = (),
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
        # Every source that will be reported keeps its machine id out of the
        # prose, including one with no url to link: the id is not a word.
        covered = {link_id.split("#", 1)[0] for link_id, _url in source_links}
        source_links.extend(
            (source_id, "") for source_id in seen_sources if source_id not in covered
        )
        # A model can cite a source that is not part of this article's records.
        # The caller knows the environment's source ids, so they still leave the
        # prose instead of being printed as a machine token.
        source_links.extend(
            (str(source_id).strip(), "")
            for source_id in extra_source_ids
            if str(source_id).strip() and str(source_id).strip() not in covered
        )
        source_markers = tuple(
            marker
            for _name, source_id, _url, citation_id in links
            for marker in (source_id, citation_id)
            if marker
        )
        # A source cited on its own label line ("来源：openai_news") is removed as
        # unreadable, exactly like a bare id line; it must still become
        # reachable, so the citation check runs before the tail removal.
        cited_candidates = text
        text = _remove_source_tail(text, source_markers)
        text = _inline_source_links(
            text,
            source_links,
        )
        text = _append_unlinked_sources(text, links, cited_candidates)
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
