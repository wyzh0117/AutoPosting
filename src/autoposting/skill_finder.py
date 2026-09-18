"""Coverage-first skill search: decompose, fetch, gate, package, write overview."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urlencode, urlsplit

from .github_weekly import SEARCH_URL, default_github_get, infer_kind
from .llm import HTTP_USER_AGENT, LLMError, _has_cjk, parse_model_json

_LOG = logging.getLogger(__name__)

# Skill-domain capability categories, tried BEFORE the GitHub-weekly KIND_RULES
# so a skill is classified by what it does rather than by where it was found.
# The old code wrote ``kind = source``, which collapsed every skill into one
# category label. kind is a capability category, never the crawl source.
SKILL_KIND_RULES: tuple[tuple[str, str], ...] = (
    # Compound intents first: a "video thumbnail" skill is an image tool.
    ("封面", "图像"), ("缩略图", "图像"), ("thumbnail", "图像"), ("配图", "图像"),
    ("脚本", "写作"), ("分镜", "写作"), ("storyboard", "写作"), ("script", "写作"),
    ("复盘", "数据"), ("数据", "数据"), ("analytics", "数据"),
    ("剪辑", "视频"), ("视频", "视频"), ("video", "视频"), ("剪映", "视频"),
    ("字幕", "视频"), ("subtitle", "视频"),
    ("图像", "图像"), ("图片", "图像"), ("image", "图像"), ("海报", "图像"),
    ("写作", "写作"), ("文案", "写作"), ("writing", "写作"), ("博客", "写作"), ("blog", "写作"),
    ("数据", "数据"), ("表格", "数据"), ("excel", "数据"), ("data", "数据"),
    ("测试", "测试"), ("test", "测试"),
    ("部署", "部署"), ("deploy", "部署"), ("发布", "部署"),
    ("演示", "演示"), ("ppt", "演示"), ("slide", "演示"),
    ("设计", "设计"), ("design", "设计"),
    ("翻译", "翻译"), ("translat", "翻译"),
    ("选题", "检索"), ("热榜", "检索"), ("榜单", "检索"), ("趋势", "检索"), ("trend", "检索"),
    ("检索", "检索"), ("搜索", "检索"), ("search", "检索"),
    ("爬虫", "采集"), ("采集", "采集"), ("crawl", "采集"), ("scrap", "采集"),
    ("音频", "音频"), ("播客", "音频"), ("audio", "音频"), ("voice", "音频"),
)

Getter = Callable[[str], object]
Searcher = Callable[[str], list[dict[str, str]]]
Completer = Callable[[str], str]
OnEvent = Callable[[str, dict[str, object]], None]
CancelCheck = Callable[[], bool]

# The production chain a skill package is filed into when nothing else supplies
# one: a paste-only package, or a keyword search. Pasted skills used to land in a
# single 手动补充 bucket, and the poster copy literally read「接手手动补充这一环」,
# so the boards were unusable. These stages are matched by `_assign_stage` against
# the skill's own name / description / SKILL.md, so a poster names the real step
# it belongs to (图像与设计, 视频与剪辑, …) instead of the bucket it arrived in.
WORKFLOW_STAGES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("topic", "选题与检索", ("选题", "趋势", "热榜", "榜单", "检索", "搜索", "search", "trend", "research", "idea")),
    ("collect", "采集与整理", ("爬虫", "采集", "抓取", "归档", "crawl", "scrap", "extract", "parse", "ingest")),
    ("write", "写作与文案", ("写作", "文案", "脚本", "分镜", "writing", "script", "storyboard", "blog", "copy", "prompt")),
    ("image", "图像与设计", ("图像", "图片", "封面", "配图", "海报", "设计", "image", "thumbnail", "poster", "design", "logo")),
    ("video", "视频与剪辑", ("视频", "剪辑", "字幕", "转码", "video", "subtitle", "ffmpeg", "clip")),
    ("audio", "音频与配音", ("音频", "配音", "播客", "语音", "audio", "voice", "tts", "podcast", "speech")),
    ("data", "数据与复盘", ("数据", "表格", "复盘", "报表", "analytics", "data", "excel", "report", "metric")),
    ("ship", "发布与自动化", ("发布", "部署", "自动化", "定时", "deploy", "publish", "automat", "workflow", "bot", "cron")),
)

# Every place the skill finder scrapes on its own. The UI reports the enabled
# count in the skills lane and expands into this list on click, so a user can see
# what "自动抓取" actually means without reading the source.
def crawl_sources(*, github_token: bool = False) -> list[dict[str, str]]:
    """The configured auto-scrape sources, in fetch order."""
    return [
        {
            "id": "github",
            "name": "GitHub 代码检索",
            "url": "https://api.github.com/search/repositories",
            "enabled": "1",
            "detail": (
                "按关键词搜仓库；已配 Token，每小时 5000 次"
                if github_token
                else "按关键词搜仓库；未配 Token 时每小时 60 次，容易限流"
            ),
        },
        {
            "id": "skills.sh",
            "name": "skills.sh",
            "url": SKILLS_SH_SEARCH_URL,
            "enabled": "1",
            "detail": "skills.sh 站内搜索；无需登录，返回安装量",
        },
        {
            "id": "known_roots",
            "name": "GitHub 官方 skills 仓库",
            "url": KNOWN_ROOTS[0],
            "enabled": "1",
            "detail": "anthropics/skills 的目录清单，作为固定兜底来源",
        },
        {
            "id": "web_search",
            "name": "网页检索",
            "url": "https://html.duckduckgo.com/html/",
            "enabled": "1",
            "detail": "DuckDuckGo 网页结果（失败时退回 lite / JSON / 维基）",
        },
    ]


SKILL_CAP = 16
POSTER_RENDER_CAP = 10
FRESH_DAYS = 180
MIN_STARS = 10
# One query now costs up to two search calls (GitHub + skills.sh) instead of one,
# and every stage keeps a fair slice of what is left, so the budget doubles with
# the source count. FETCH_DEADLINE_SECONDS stays the wall-clock guard.
MAX_FETCHES = 160
FETCH_DEADLINE_SECONDS = 240
SKILLS_SH_SOURCE = "skills.sh"
SKILLS_SH_SEARCH_URL = "https://skills.sh/api/search"
# skills.sh reports installs, not stars. Its public /api/search JSON is free and
# unauthenticated (the official docs price no API at all; a third-party
# `mastra-ai/skills-api` exists precisely because of that), ~1-3s and ~16KB per
# response, and it answers Chinese queries. GET /api/skill/<id> returns 401, so
# only the search endpoint is used. A skills.sh entry has no stars, so its
# popularity signal is the install count: 100 installs is the floor where an
# entry has clearly been installed by real users (the same "not a random fork"
# intent as MIN_STARS=10) without cutting the long tail of useful skills.
SKILLS_SH_MIN_INSTALLS = 100
# /api/search returns up to 100 rows. They arrive install-sorted and only a
# handful can clear the gate inside one stage's slice, so keep the head of the
# list: it caps the SKILL.md fetches each query can trigger.
SKILLS_SH_PER_QUERY = 10
# cap_skills ranks by popularity across both sources; treat 100 installs as
# roughly one star so a widely installed skills.sh entry is not automatically
# outranked by any starred repo.
SKILLS_SH_INSTALLS_PER_STAR = 100
GAP_REASON = "这一环没有找到合格 skill"
# Minimum fair slice per stage: both search sources plus one SKILL.md verification.
# The slice is still clamped by the global budget, so it cannot starve tail stages.
MIN_STAGE_FETCHES = 4
# api.github.com is quota-metered per resource: unauthenticated callers get 60
# core requests/hour and 10 search requests/minute. One finder run wants dozens
# of `contents` calls, so the quota runs out and every later call answers 403
# (the old `github:RuntimeError`). Once a resource is spent, skip it for a while
# and probe raw.githubusercontent.com instead — a CDN with no such quota — so the
# remaining budget goes to sources that still answer.
_GITHUB_RESOURCE_BACKOFF = {"search": 60.0, "core": 900.0}
# Never spend the last few calls: keep one in reserve so a single 403 cannot turn
# into a whole-stage rejection.
_GITHUB_QUOTA_FLOOR = 2
# A 404 means the path we probed does not exist — the ordinary outcome of walking
# SKILL_PATHS — so it must not be reported as "this source did not answer".
_NON_DEGRADING_KINDS = {"not_found"}
_MAX_DEGRADED_REASONS = 8
_MAX_LEDGER = 60
_MAX_REJECTIONS_EXPOSED = 20
# Keyword-first pipeline (1.4.0): the user's own words are the query, so there is
# no 理解需求 / 还原生产链 / 分解工作流 step to report any more.
PIPELINE_STEPS: tuple[tuple[str, str, str], ...] = (
    ("keywords", "整理关键词", "正在把输入切成检索关键词"),
    ("search", "抓取来源", "正在按关键词去各来源抓取 skill"),
    ("group", "按工作流归组", "正在把抓到的 skill 归到工作流各环节"),
    ("compose", "汇总打包", "正在汇总成端到端工作流"),
)
PIPELINE_BY_STEP = {
    step: (index, label, detail)
    for index, (step, label, detail) in enumerate(PIPELINE_STEPS, start=1)
}
CACHE_TTL_SECONDS = 15 * 60
_MISSING = object()
_cache_lock = threading.Lock()
_github_cache: dict[str, tuple[float, object]] = {}
_LAST_SNAPSHOT: dict[str, dict[str, object]] = {}
GITHUB_PER_PAGE = 5
GITHUB_REPO_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.I,
)
# skills.sh links look like https://skills.sh/<owner>/<repo>/<skill> or
# https://skills.sh/trending; both are preserved instead of being discarded.
SKILLS_SH_LINK_RE = re.compile(
    r"(?:^|//|\.)skills\.sh/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?(?:/([A-Za-z0-9_.-]+))?",
    re.I,
)
SKIP_OWNERS = {
    "topics",
    "search",
    "orgs",
    "settings",
    "features",
    "about",
    "pricing",
    "explore",
    "login",
    "apps",
    "sponsors",
}
_SKILL_FILE_MARKERS = ("SKILL.md", ".agents/skills", ".claude/skills", ".cursor/skills")
# Every layout docs/skill_finder/COVERAGE.md promises. `{name}` is the repo / skill
# directory name. fetch_skill_md walks these cheapest-first and only probes nested
# layouts whose top directory the repo root listing actually advertises.
SKILL_PATHS: tuple[str, ...] = (
    "SKILL.md",
    "{name}/SKILL.md",
    ".agents/skills/{name}/SKILL.md",
    ".claude/skills/{name}/SKILL.md",
    "skills/{name}/SKILL.md",
    ".cursor/skills/{name}/SKILL.md",
)
KNOWN_ROOTS = (
    "https://api.github.com/repos/anthropics/skills/contents",
)


def _github_search_terms(terms: str) -> str:
    """GitHub query template — keeps the domain filter and the SKILL.md hint."""
    return f"{terms} SKILL.md site:github.com"


def _skills_sh_search_terms(terms: str) -> str:
    """skills.sh query template — its own in-site search, with no domain filter."""
    return _canonical_query(terms)


SEARCH_TEMPLATES: dict[str, Callable[[str], str]] = {
    "github": _github_search_terms,
    SKILLS_SH_SOURCE: _skills_sh_search_terms,
}


class _RateLimited(RuntimeError):
    """GitHub 403/429/timeout — not evidence that a repo lacks a skill."""


def reset_skill_finder_state() -> None:
    with _cache_lock:
        _github_cache.clear()
        _LAST_SNAPSHOT.clear()


def _canonical_query(value: object) -> str:
    """Normalize only layout whitespace; case can be meaningful in searches."""
    return " ".join(str(value or "").split())


def _cache_get(key: str) -> object:
    with _cache_lock:
        row = _github_cache.get(key)
        if row is None:
            return _MISSING
        stamped, value = row
        if time.monotonic() - stamped > CACHE_TTL_SECONDS:
            _github_cache.pop(key, None)
            return _MISSING
        return deepcopy(value)


def _cache_put(key: str, value: object) -> None:
    with _cache_lock:
        _github_cache[key] = (time.monotonic(), deepcopy(value))


_RATE_LIMIT_MARKERS = (
    "403",
    "429",
    "rate limit",
    "rate-limit",
    "rate_limited",
    "rate limited",
    "ratelimit",
    "too many requests",
    "timeout",
    "timed out",
)
_SENTINEL_FIELDS = {"status", "status_code", "code", "error", "message"}


def _is_rate_limit_payload(value: object) -> bool:
    """Recognize structured upstream failure sentinels without scanning content."""
    if isinstance(value, dict):
        for key, marker in value.items():
            if str(key).lower() not in _SENTINEL_FIELDS:
                continue
            if isinstance(marker, (dict, list, tuple)):
                if _is_rate_limit_payload(marker):
                    return True
                continue
            if isinstance(marker, int) and marker in {403, 429}:
                return True
            text = str(marker or "").lower()
            if any(token in text for token in _RATE_LIMIT_MARKERS):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_is_rate_limit_payload(item) for item in value)
    return False


def _is_rate_limit(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, _RateLimited)):
            return True
        code = getattr(current, "code", None)
        if code in {403, 429}:
            return True
        text = str(current).lower()
        if any(
            token in text
            for token in ("403", "429", "timeout", "timed out", "rate limit", "ratelimit")
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def docs_root() -> Path:
    here = Path(__file__).resolve()
    packaged = here.parents[2] / "docs"
    if packaged.is_dir():
        return packaged
    return Path.cwd() / "docs"


def load_finder_prompt(name: str) -> str:
    path = docs_root() / "skill_finder" / name
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise FileNotFoundError(f"skill_prompt_empty:{name}")
    return text


def _source_of_url(url: str) -> str:
    """Which search source an HTTP call belongs to (used for degrade reasons)."""
    host = (urlsplit(url).hostname or "").lower()
    if host == SKILLS_SH_SOURCE or host.endswith("." + SKILLS_SH_SOURCE):
        return SKILLS_SH_SOURCE
    if host == "api.github.com" or host.endswith("githubusercontent.com"):
        return "github"
    return "http"


def _github_resource(url: str) -> str:
    """Which GitHub quota an api.github.com URL draws from."""
    return "search" if urlsplit(url).path.startswith("/search/") else "core"


def _degrade_kind(exc: BaseException, url: str) -> str:
    """A short, legible reason token for the UI instead of the exception class.

    `github_weekly.GitHubFetchError` carries its own `kind`; anything injected
    (tests, adapters) falls back to the old classification so the vocabulary
    stays stable: `rate_limited`, `timeout`, `unreachable`, or the class name.
    """
    kind = str(getattr(exc, "kind", "") or "").strip()
    if kind:
        return kind
    if isinstance(exc, TimeoutError):
        return "timeout"
    if _is_rate_limit(exc):
        return "rate_limited"
    return type(exc).__name__


def _github_quota_state() -> dict[str, object] | None:
    """Last `X-RateLimit-*` snapshot from `github_weekly`, when it has one."""
    try:
        from .github_weekly import github_rate_state
    except ImportError:  # pragma: no cover - only while the getter is being built
        return None
    try:
        state = github_rate_state()
    except Exception:  # pragma: no cover - a probe must never break a run
        return None
    return state if isinstance(state, dict) else None


def _fair_share(budget: "_FetchBudget", stages_left: int) -> int:
    """An even slice of what is left, so wide maps do not starve tail stages."""
    left = max(0, budget.max_fetches - budget.calls)
    if left <= 0:
        return 0
    stages = max(1, int(stages_left))
    even = left // stages
    # Keep one call in reserve for every later stage, so a generous per-stage
    # floor can never starve the tail of a wide map (12 stages / 24 calls must
    # still give each stage at least one attempt).
    affordable = max(1, left - max(0, stages - 1))
    floor = min(MIN_STAGE_FETCHES, affordable)
    return min(max(floor, even), left)


def _fair_time_share(budget: "_FetchBudget", stages_left: int) -> float:
    """The wall-clock half of the same fair slice: every stage gets real time."""
    left = max(0.0, budget.deadline - time.monotonic())
    return left / max(1, int(stages_left))


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_fresh(hit: dict[str, object], now: datetime | None = None) -> bool:
    pushed = _parse_time(str(hit.get("pushed_at") or ""))
    moment = now or datetime.now(timezone.utc)
    return pushed is not None and moment - pushed <= timedelta(days=FRESH_DAYS)


def quality_reason(hit: dict[str, object], now: datetime | None = None) -> tuple[bool, str]:
    """Why a candidate clears (or misses) the COVERAGE.md quality gate.

    The layout clause is shared: a locatable SKILL.md (or an equivalent skill
    description file) is mandatory. The popularity clause is per source — GitHub
    keeps stars/freshness, skills.sh is judged on its install count.
    """
    # Evidence of a locatable skill file: its content, or a URL that itself names
    # one. `skill_path` alone is deliberately NOT evidence — a path we never
    # fetched must not be able to satisfy the gate on its own.
    declared_in_url = any(
        marker in str(hit.get("url") or "") for marker in _SKILL_FILE_MARKERS
    )
    has_file = (
        bool(str(hit.get("skill_md") or "").strip())
        or declared_in_url
        or bool(hit.get("skill_verified"))
    )
    if not has_file:
        return False, "no_skill_md"
    if hit.get("registry"):
        return True, "registry"
    if str(hit.get("source") or "") == SKILLS_SH_SOURCE:
        installs = _as_int(hit.get("installs"))
        if installs >= SKILLS_SH_MIN_INSTALLS:
            return True, f"installs>={SKILLS_SH_MIN_INSTALLS}"
        # A brand-new skills.sh entry can still be fresh on GitHub, and a repo
        # that already earned MIN_STARS is popular by the GitHub rule too.
        if _as_int(hit.get("stars")) >= MIN_STARS:
            return True, f"stars>={MIN_STARS}"
        if _is_fresh(hit, now):
            return True, "fresh"
        return False, f"installs<{SKILLS_SH_MIN_INSTALLS}"
    if _as_int(hit.get("stars")) >= MIN_STARS:
        return True, f"stars>={MIN_STARS}"
    if _is_fresh(hit, now):
        return True, "fresh"
    return False, "stars_and_stale"


def passes_quality(hit: dict[str, object], now: datetime | None = None) -> bool:
    return quality_reason(hit, now=now)[0]


class _StageQuota(RuntimeError):
    """The current stage spent its fair share of the budget — not a failure.

    Kept distinct from the global `skill_search_budget` exhaustion so a wide
    coverage map keeps its tail stages searched instead of starving them.
    """


class _FetchBudget:
    def __init__(
        self,
        getter: Getter,
        searcher: Searcher,
        *,
        max_fetches: int,
        deadline: float,
    ) -> None:
        self._getter = getter
        self._searcher = searcher
        self.max_fetches = max_fetches
        self.deadline = deadline
        self.calls = 0
        self.degraded = False
        self.reasons: list[str] = []
        self.stage_quota_skips = 0
        self.stage_calls: list[dict[str, object]] = []
        self.fetch_calls = 0
        self.search_calls = 0
        self._started = time.monotonic()
        self._quota_limit: int | None = None
        self._quota_deadline: float | None = None
        self._stage_id = ""
        self._stage_name = ""
        self._stage_start_calls = 0
        self._github_blocked_until: dict[str, float] = {}

    def mark_degraded(self, reason: str) -> None:
        self.degraded = True
        if reason and reason not in self.reasons and len(self.reasons) < _MAX_DEGRADED_REASONS:
            self.reasons.append(reason)

    def _block_github(self, url: str, reset_at: float | None) -> None:
        """Stop spending a GitHub quota that is already answered with 403.

        The wait is the reported reset when it is sooner than the conservative
        per-resource cap, so a search quota (resets every minute) comes back
        inside one run while an exhausted core quota stays parked.
        """
        if _source_of_url(url) != "github" or "api.github.com" not in url:
            return
        resource = _github_resource(url)
        wait = _GITHUB_RESOURCE_BACKOFF.get(resource, 300.0)
        if isinstance(reset_at, (int, float)) and reset_at > 0:
            remaining = float(reset_at) - time.time()
            wait = max(5.0, min(wait, remaining))
        self._github_blocked_until[resource] = time.monotonic() + wait

    def github_api_ok(self, url: str) -> bool:
        """False when api.github.com should not be called for `url` right now."""
        if _source_of_url(url) != "github" or "api.github.com" not in url:
            return True
        resource = _github_resource(url)
        if time.monotonic() < self._github_blocked_until.get(resource, 0.0):
            return False
        state = _github_quota_state()
        if not isinstance(state, dict):
            return True
        if str(state.get("resource") or "") != resource:
            # The snapshot belongs to the other quota; only a real 403 blocks this one.
            return True
        reset = state.get("reset")
        if isinstance(reset, (int, float)) and reset > 0 and reset < time.time():
            # The window this snapshot describes has already rolled over.
            return True
        remaining = state.get("remaining")
        if isinstance(remaining, int) and remaining <= _GITHUB_QUOTA_FLOOR:
            # Do not wait for the 403: park the resource while it still answers.
            self._block_github(url, state.get("reset") if isinstance(state.get("reset"), (int, float)) else None)
            self.mark_degraded("github:rate_limited")
            return False
        return True

    def remaining(self) -> bool:
        return self.calls < self.max_fetches and time.monotonic() < self.deadline

    def start_stage(
        self,
        stage_id: str,
        stage_name: str,
        share: int,
        *,
        seconds: float | None = None,
    ) -> None:
        """Reserve one stage's fair slice of whatever budget is left (calls + time)."""
        self._stage_id = stage_id
        self._stage_name = stage_name
        self._stage_start_calls = self.calls
        self._quota_limit = min(self.max_fetches, self.calls + max(0, int(share)))
        self._quota_deadline = (
            self.deadline if seconds is None else min(self.deadline, time.monotonic() + max(0.0, seconds))
        )

    def end_stage(self) -> int:
        used = self.calls - self._stage_start_calls
        self.stage_calls.append(
            {"stage_id": self._stage_id, "name": self._stage_name, "calls": used}
        )
        self._quota_limit = None
        self._quota_deadline = None
        self._stage_id = ""
        self._stage_name = ""
        return used

    def over_quota(self) -> bool:
        if self._quota_limit is not None and self.calls >= self._quota_limit:
            return True
        return self._quota_deadline is not None and time.monotonic() >= self._quota_deadline

    def report(self) -> dict[str, object]:
        return {
            "max_fetches": self.max_fetches,
            "calls": self.calls,
            "fetch_calls": self.fetch_calls,
            "search_calls": self.search_calls,
            "stage_quota_skips": self.stage_quota_skips,
            "elapsed_ms": int(max(0.0, (time.monotonic() - self._started) * 1000)),
            "degraded": self.degraded,
            "degraded_reasons": list(self.reasons),
            "stages": [dict(row) for row in self.stage_calls],
        }

    def get(self, url: str) -> object:
        cached = _cache_get("G:" + url)
        if cached is not _MISSING:
            return cached
        if self.over_quota():
            self.stage_quota_skips += 1
            raise _StageQuota(f"skill_stage_quota:{url}")
        if not self.remaining():
            self.mark_degraded("budget_exhausted")
            raise RuntimeError("skill_search_budget")
        self.calls += 1
        self.fetch_calls += 1
        try:
            result = self._getter(url)
        except Exception as exc:
            kind = _degrade_kind(exc, url)
            if kind not in _NON_DEGRADING_KINDS:
                self.mark_degraded(f"{_source_of_url(url)}:{kind}")
            if kind == "rate_limited":
                reset_at = getattr(exc, "reset_at", None)
                self._block_github(
                    url, reset_at if isinstance(reset_at, (int, float)) else None
                )
            raise
        # An empty response is not evidence that this endpoint has no data:
        # transient upstream failures and rate-limit fallbacks are commonly
        # represented as an empty list/object by injected getters.
        if _is_rate_limit_payload(result):
            kind = "rate_limited"
            self.mark_degraded(f"{_source_of_url(url)}:{kind}")
            self._block_github(url, None)
        elif _has_cacheable_content(result):
            _cache_put("G:" + url, result)
        return result

    def search(self, query: str) -> list[dict[str, str]]:
        normalized = _canonical_query(query)
        cached = _cache_get("S:" + normalized)
        if cached is not _MISSING:
            return list(cached) if isinstance(cached, list) else []
        if self.over_quota():
            self.stage_quota_skips += 1
            return []
        if not self.remaining():
            self.mark_degraded("budget_exhausted")
            return []
        self.calls += 1
        self.search_calls += 1
        try:
            result = self._searcher(normalized)
        except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
            self.mark_degraded(f"web_search:{type(exc).__name__}")
            return []
        if _is_rate_limit_payload(result):
            self.mark_degraded("web_search:rate_limit")
            return []
        if not isinstance(result, list):
            self.mark_degraded("web_search:invalid_payload")
            return []
        if result:
            _cache_put("S:" + normalized, result)
        return result


def _has_cacheable_content(value: object) -> bool:
    if _is_rate_limit_payload(value):
        return False
    if value is None:
        return False
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return bool(items)
        message = str(value.get("message") or "").lower()
        if any(token in message for token in ("rate limit", "timeout", "timed out")):
            return False
        return bool(value)
    if isinstance(value, (list, tuple, set, str, bytes)):
        return bool(value)
    return True


def _reusable_snapshot(snapshot: object, query: str) -> dict[str, object] | None:
    if not isinstance(snapshot, dict):
        return None
    if _canonical_query(snapshot.get("query")) != query:
        return None
    if str(snapshot.get("phase") or "") not in {"skills_ready", "done"}:
        return None
    if snapshot.get("error") or snapshot.get("error_reason") or snapshot.get("degraded"):
        return None
    if not isinstance(snapshot.get("skills"), list) or not snapshot["skills"]:
        return None
    return deepcopy(snapshot)


def _last_snapshot_for(query: str) -> dict[str, object] | None:
    with _cache_lock:
        snapshot = _LAST_SNAPSHOT.get(query)
        return deepcopy(snapshot) if snapshot is not None else None


def _save_last_snapshot(query: str, snapshot: dict[str, object]) -> None:
    with _cache_lock:
        _LAST_SNAPSHOT[query] = deepcopy(snapshot)


def find_skills(
    query: str,
    *,
    complete: Completer | None,
    github_get: Getter | None = None,
    web_search: Searcher | None = None,
    now: datetime | None = None,
    output_root: Path | None = None,
    max_fetches: int = MAX_FETCHES,
    fetch_deadline_seconds: float = FETCH_DEADLINE_SECONDS,
    on_event: OnEvent | None = None,
    previous: dict[str, object] | None = None,
    cancel: CancelCheck | None = None,
    coverage: dict[str, object] | None = None,
) -> dict[str, object]:
    """Scrape skills for one query.

    The plan is the user's own keywords (:func:`keyword_coverage`) — no model is
    involved in deciding *what* to search, and ``complete`` is only used to write
    the intro piece afterwards, so an unconfigured model no longer blocks a
    search. ``coverage`` lets a caller supply an explicit plan (an edited
    workflow, or a caller that already holds one) instead of the keyword split.
    """
    text = _canonical_query(query)
    if not text or len(text) > 200:
        raise ValueError("query must be 1-200 characters")
    moment = now or datetime.now(timezone.utc)
    # The pasted set is user state: it is carried through every snapshot this run
    # writes, so a search can never erase a slot that it did not re-collect.
    stored_manual = [
        row
        for row in ((previous or {}).get("manual_skills") or [])
        if isinstance(row, dict)
    ]

    def aborted() -> bool:
        return bool(cancel and cancel())

    def emit(kind: str, payload: dict[str, object]) -> None:
        if on_event is None:
            return
        on_event(kind, dict(payload))

    started_dt = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    started_at = started_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    clock0 = time.monotonic()
    last_elapsed = -1
    goal = text
    chain: list[dict[str, object]] = []
    stages: list[dict[str, object]] = []
    collected: list[dict[str, object]] = []
    seen: set[str] = set()
    title = text
    theme = text
    prior = _reusable_snapshot(previous, text) if previous is not None else _last_snapshot_for(text)
    root = Path(output_root) if output_root is not None else Path.cwd() / "output" / "skills"

    def elapsed_ms() -> int:
        nonlocal last_elapsed
        ms = int(max(0.0, (time.monotonic() - clock0) * 1000))
        if ms <= last_elapsed:
            ms = last_elapsed + 1
        last_elapsed = ms
        return ms

    def current_progress(step: str, detail: str = "") -> dict[str, object]:
        index, label, default_detail = PIPELINE_BY_STEP[step]
        return {
            "step": step,
            "label": label,
            "detail": detail or default_detail,
            "step_index": index,
            "step_total": len(PIPELINE_STEPS),
            "started_at": started_at,
            "elapsed_ms": elapsed_ms(),
        }

    def snapshot(
        phase: str,
        article: dict[str, str] | None = None,
        package_path: str = "",
        detail: str = "",
    ) -> dict[str, object]:
        skills = [
            _public_skill(skill)
            for skill in cap_skills(_dedupe_by_skill_name(collected), stages)
        ]
        filled = _filled_stages(stages, skills)
        visible = _visible_stages(stages, skills)
        use_filled = phase in {"group", "compose", "skills_ready", "error"}
        progress_step = phase if phase in PIPELINE_BY_STEP else ("compose" if phase == "skills_ready" else "search")
        return {
            "query": text,
            "goal": goal,
            "chain": list(chain),
            "title": title,
            "theme": theme,
            # Empty stages stay visible with `gap: true` so the frontend can show
            # where coverage is missing instead of silently dropping a row.
            "stages": visible if use_filled else [
                {
                    "id": stage["id"],
                    "name": stage["name"],
                    "queries": list(stage.get("queries") or []),
                    "required": bool(stage.get("required", True)),
                    "gap": bool(stage.get("gap")),
                }
                for stage in stages
            ],
            "skills": skills,
            "groups": _skill_groups(filled, skills),
            "article": article or {"title": "", "body": ""},
            "images": [],
            "package_path": package_path,
            "phase": phase,
            "progress": current_progress(progress_step, detail),
        }

    def emit_progress(step: str, detail: str = "") -> dict[str, object]:
        body = snapshot(step, detail=detail)
        emit("progress", body)
        return body

    def emit_new_skills(before: int, kind: str = "skill") -> None:
        for skill in collected[before:]:
            public = _public_skill(skill)
            module = str(public.get("stage_name") or "")
            keyword = module
            body = snapshot("search", detail=f"正在检索：{module} · {keyword}" if module else "")
            body["skill"] = public
            emit(kind, body)

    emit_progress("keywords")
    # No model in the loop: the user's keywords are the plan. That keeps a search
    # working with nothing configured and stops one click from spending a request
    # (and ~10s) re-writing what the user already said.
    plan = coverage if isinstance(coverage, dict) else keyword_coverage(text)
    goal = str(plan.get("goal") or text)
    chain = list(plan.get("chain") or [])
    title = str(plan.get("title") or text)
    theme = str(plan.get("theme") or text)
    stages = [dict(stage) for stage in (plan.get("stages") or []) if isinstance(stage, dict)]
    if not stages:
        raise ValueError("query must contain at least one keyword")
    coverage = {
        "goal": goal,
        "title": title,
        "theme": theme,
        "chain": chain,
        "stages": stages,
    }
    budget = _FetchBudget(
        github_get or _default_get,
        web_search or _default_web_search,
        max_fetches=max(1, int(max_fetches)),
        deadline=time.monotonic() + max(1.0, float(fetch_deadline_seconds)),
    )
    getter = budget.get
    searcher = budget.search
    ledger: list[dict[str, object]] = []

    def run_stage(stage: dict[str, object], stages_left: int, *, second_pass: bool) -> None:
        """Search one stage inside its fair slice, recording the consumption."""
        budget.start_stage(
            str(stage["id"]),
            str(stage["name"]),
            _fair_share(budget, stages_left),
            seconds=_fair_time_share(budget, stages_left),
        )
        try:
            _search_stage(
                stage,
                getter,
                searcher,
                collected,
                seen,
                moment,
                second_pass=second_pass,
                on_query=on_query_for(stage),
                on_degrade=budget.mark_degraded,
                ledger=ledger,
                api_ok=budget.github_api_ok,
            )
        finally:
            stage["fetches"] = int(stage.get("fetches") or 0) + budget.end_stage()

    def on_query_for(stage: dict[str, object]) -> Callable[[str, str], None]:
        module_name = str(stage["name"])
        return lambda _name, terms: emit_progress("search", f"正在检索：{module_name} · {terms}")

    try:
        # The package accumulates: this search adds to what is already there, so
        # 生成图片 aggregates one set instead of only the newest keyword's hits.
        reuse_source = previous if isinstance(previous, dict) else prior
        reused = _reuse_previous(reuse_source, stages, collected, seen, moment)
        manual_added = _reinject_manual(
            (reuse_source or {}).get("manual_skills"), stages, collected, seen
        )
        if reused or manual_added:
            emit("stage", snapshot("search"))
            emit_new_skills(0)
        if not aborted():
            before_roots = len(collected)
            _collect_known_roots(
                getter, collected, seen, moment, ledger=ledger, api_ok=budget.github_api_ok
            )
            emit_new_skills(before_roots)
        # The searchable lanes are fixed up front: the workflow fallback rows are
        # appended later purely to file results into, and must never be searched.
        searchable = [stage for stage in stages if _is_searchable(stage)]
        for index, stage in enumerate(searchable):
            if aborted():
                break
            module_name = str(stage["name"])
            queries = [str(item) for item in (stage.get("queries") or []) if str(item).strip()] or [module_name]
            emit_progress("search", f"正在检索：{module_name} · {queries[0]}")
            before = len(collected)
            run_stage(stage, len(searchable) - index, second_pass=False)
            if _stage_skills(collected, str(stage["id"])):
                emit("stage", snapshot("search"))
            emit_new_skills(before)
        _assign_pending(collected, stages)
        for index, stage in enumerate(searchable):
            if aborted():
                break
            if stage.get("required") and not _stage_skills(collected, str(stage["id"])):
                module_name = str(stage["name"])
                retry = f"{module_name} claude skill OR agent skill"
                emit_progress("search", f"正在检索：{module_name} · {retry}")
                before = len(collected)
                run_stage(stage, len(searchable) - index, second_pass=True)
                emit_new_skills(before)
            # A stage with no qualified skill is a visible gap (B4), required or not.
            stage["gap"] = not _stage_skills(collected, str(stage["id"]))
        # Anything the keywords left in no lane at all still belongs to a real
        # production step, not to a nameless bucket: file it into the workflow.
        _workflow_stage_rows(stages)
        _assign_pending(collected, stages)
    except RuntimeError as exc:
        if not any(
            token in str(exc) for token in ("skill_search_budget", "skill_stage_quota")
        ):
            raise
    capped = cap_skills(_dedupe_by_skill_name(collected), stages)
    kept_ids = {id(row) for row in capped}
    # The cap is a real limit and pasted skills are pinned, so a big package can
    # only make room by dropping a searched skill. That must be visible: a silent
    # disappearance after a search reads as a bug.
    capped_dropped = [
        str(row.get("name") or row.get("source_id") or "")
        for row in collected
        if id(row) not in kept_ids and not row.get("manual")
    ]
    skills = [skill for skill in capped if not skill.get("gap")]
    filled = _filled_stages(stages, skills)
    coverage = dict(coverage)
    coverage["goal"] = goal
    coverage["chain"] = chain
    # The article prompt only ever sees stages that really have a skill.
    coverage["stages"] = filled
    coverage["groups"] = _skill_groups(filled, skills)
    emit_progress("group")
    if aborted():
        payload = snapshot("error")
        payload["phase"] = "error"
        return payload
    emit_progress("compose")
    article: dict[str, str] = {}
    if complete is not None:
        try:
            article = write_article(text, coverage, skills, complete)
        except LLMError:
            # The model is optional now: a keyword scrape must still deliver a
            # package, so the mechanical intro stands in for the written one.
            article = {}
    if not article:
        # Keyword search and paste both work with no model at all, so the
        # mechanical intro always lands — including the "found nothing" case,
        # whose body must not be empty or the browser would keep polling a
        # finished search forever.
        article = _fallback_article(skills, title=title, goal=goal)
    payload = snapshot("skills_ready", article=article)
    # Pasted skills are user state and survive every search, including the slots
    # the search itself did not re-collect.
    payload["manual_skills"] = stored_manual
    payload["degraded"] = bool(budget.degraded)
    payload["degraded_reasons"] = list(budget.reasons)
    payload["budget"] = budget.report()
    payload["rejected"] = _ledger_rows(ledger)
    payload["skill_cap"] = SKILL_CAP
    if capped_dropped:
        payload["capped_dropped"] = capped_dropped
    payload["package_path"] = str(write_package(root, payload, skills))
    emit("article", payload)
    emit("skills_ready", payload)
    if skills and not budget.degraded:
        _save_last_snapshot(text, payload)
    return payload


# One space/comma-separated keyword becomes one search lane. Six is enough to
# cover "视频 剪辑 字幕" style input without turning one click into a crawl of
# everything, and the cap is what keeps the fair-slice budget workable.
KEYWORD_LIMIT = 6
_KEYWORD_SPLIT_RE = re.compile(r"[\s,，、;；/|+]+")


def extract_keywords(query: str, limit: int = KEYWORD_LIMIT) -> list[str]:
    """Split the user's input into in-order, de-duplicated keywords.

    The finder no longer asks a model to解构 the input: the user's own words are
    the query. Anything separated by whitespace, a comma, or a slash is one
    keyword; a single long phrase stays one keyword instead of being chopped.
    """
    text = _canonical_query(query)
    if not text:
        return []
    tokens = [token for token in _KEYWORD_SPLIT_RE.split(text) if token]
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens or [text]:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(token)
        if len(keywords) >= max(1, int(limit)):
            break
    return keywords


def _keyword_stage_id(keyword: str) -> str:
    """Stable, ASCII, collision-free stage id for one keyword.

    Content-derived on purpose: the same keyword always maps to the same lane, and
    two different keywords never share one, so a skill carried over from an older
    search is not silently relabelled into the newest keyword's stage.
    """
    digest = hashlib.sha1(_canonical_query(keyword).lower().encode("utf-8")).hexdigest()
    return f"kw-{digest[:10]}"


def keyword_coverage(query: str) -> dict[str, object]:
    """Coverage built from the user's keywords — no model, no decomposition.

    Each keyword is one stage, so the coverage order is the order the user typed,
    and the same struct the rest of the pipeline already understands (stages with
    `queries`) carries straight into `_search_stage`.
    """
    text = _canonical_query(query)
    keywords = extract_keywords(text)
    stages: list[dict[str, object]] = [
        {
            # The id is derived from the keyword, never from its position: a
            # positional `kw1` collides with the *next* search's first keyword, and
            # a carried-over skill then gets relabelled as if it belonged to that
            # new keyword's lane.
            "id": _keyword_stage_id(keyword),
            "name": keyword,
            "queries": [keyword],
            # A keyword the user typed is a wish, not a required production step:
            # finding nothing for it must not trigger the second-pass retry loop
            # for every other keyword too.
            "required": False,
            "gap": False,
        }
        for keyword in keywords
    ]
    chain = [
        {"id": f"c{index}", "name": str(stage["name"]), "role": ""}
        for index, stage in enumerate(stages, start=1)
    ]
    return {
        "goal": text,
        "title": text,
        "theme": text,
        "chain": chain,
        "stages": stages,
        "keywords": keywords,
    }


def workflow_stages() -> list[dict[str, object]]:
    """A fresh copy of the fallback production chain (ids are stable).

    ``offline`` marks a row the search loops must not query: these rows exist to
    file results into, and searching for the bare word 图像 would burn the fetch
    budget on noise.
    """
    return [
        {
            "id": stage_id,
            "name": name,
            "queries": list(tokens),
            "required": False,
            "offline": True,
            "gap": False,
        }
        for stage_id, name, tokens in WORKFLOW_STAGES
    ]


def _is_searchable(stage: dict[str, object]) -> bool:
    """True when the search loops may query this stage.

    The 通用工具 bucket (id ``manual``) and the workflow fallback rows are buckets
    to file skills into, not search targets; the search loops skip every stage that
    is not searchable.
    """
    return not stage.get("manual") and not stage.get("offline")


def _workflow_stage_rows(stages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Append the missing workflow stages after whatever coverage already exists.

    Existing stages keep their order and their ids: a real coverage map is what
    "原本的工作流" means for this package. The workflow rows only fill the gaps a
    paste-only or keyword-only package would otherwise leave empty.
    """
    known = {str(stage.get("id") or "") for stage in stages}
    for row in workflow_stages():
        if str(row["id"]) not in known:
            stages.append(row)
            known.add(str(row["id"]))
    return stages


def decompose_coverage(query: str, complete: Completer) -> dict[str, object]:
    """Legacy AI coverage planner — kept for archived packages and callers.

    The finder itself no longer calls this: 1.4.0 searches the user's own
    keywords (see :func:`keyword_coverage`), so a search works with no model
    configured and never spends a request re-writing the input.
    """
    query = _canonical_query(query)
    cache_key = "P:" + query
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        # find_skills annotates stages with transient gap fields, so never
        # return the mutable cached plan itself.
        return deepcopy(cached)
    prompt = (
        load_finder_prompt("COVERAGE.md")
        + "\n\n用户需求：\n"
        + query.strip()
        + "\n\n只输出 JSON 对象："
        '{"goal":"...","title":"...","theme":"...","chain":[{"id":"c1","name":"...","role":"..."}],'
        '"stages":[{"id":"...","name":"...","queries":["..."],"required":true}]}'
        "\n阶段必须 6 到 12 个。生产链从左到右、由浅入深。"
    )
    parsed = parse_model_json(complete(prompt))
    if not isinstance(parsed, dict):
        raise LLMError("skill_search_failed", "覆盖地图不是对象", cause_code="llm_invalid_output")
    raw_stages = parsed.get("stages")
    if not isinstance(raw_stages, list):
        raise LLMError("skill_search_failed", "覆盖地图缺少阶段", cause_code="llm_invalid_output")
    stages: list[dict[str, object]] = []
    for index, item in enumerate(raw_stages):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        stage_id = str(item.get("id") or f"stage-{index + 1}").strip() or f"stage-{index + 1}"
        queries = [
            str(entry).strip()
            for entry in (item.get("queries") or [])
            if str(entry).strip()
        ]
        if not queries:
            queries = [name]
        stages.append(
            {
                "id": stage_id,
                "name": name,
                "queries": queries,
                "required": bool(item.get("required", True)),
                "gap": False,
            }
        )
    if len(stages) < 6 or len(stages) > 12:
        raise LLMError("skill_search_failed", "覆盖阶段数量无效", cause_code="llm_invalid_output")
    chain: list[dict[str, object]] = []
    raw_chain = parsed.get("chain")
    if isinstance(raw_chain, list):
        for index, item in enumerate(raw_chain):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            chain.append(
                {
                    "id": str(item.get("id") or f"c{index + 1}").strip() or f"c{index + 1}",
                    "name": name,
                    "role": str(item.get("role") or "").strip(),
                }
            )
    if not chain:
        chain = [
            {"id": f"c{index}", "name": str(stage["name"]), "role": ""}
            for index, stage in enumerate(stages, start=1)
        ]
    result = {
        "goal": str(parsed.get("goal") or query).strip() or query,
        "title": str(parsed.get("title") or query).strip() or query,
        "theme": str(parsed.get("theme") or query).strip() or query,
        "chain": chain,
        "stages": stages,
    }
    _cache_put(cache_key, deepcopy(result))
    return result


def write_article(
    query: str,
    coverage: dict[str, object],
    skills: list[dict[str, object]],
    complete: Completer,
) -> dict[str, str]:
    public_skills = [_public_skill(skill) for skill in skills]
    stage_rows = [
        item
        for item in (coverage.get("stages") or [])
        if isinstance(item, dict)
    ]
    package = {
        "query": query,
        "goal": coverage.get("goal"),
        "chain": coverage.get("chain"),
        "title": coverage.get("title"),
        "theme": coverage.get("theme"),
        "stages": stage_rows,
        "groups": coverage.get("groups") or _skill_groups(stage_rows, public_skills),
        "skills": public_skills,
    }
    prompt = (
        load_finder_prompt("PACKAGE_ARTICLE.md")
        + "\n\n打包结果 JSON：\n"
        + json.dumps(package, ensure_ascii=False)
        + "\n\n只输出 JSON：{\"title\":\"...\",\"body\":\"...\"}"
    )
    try:
        parsed = parse_model_json(complete(prompt))
    except LLMError as exc:
        raise LLMError("skill_search_failed", exc.reason, cause_code=exc.code) from exc
    title = ""
    body = ""
    if isinstance(parsed, dict):
        title = str(parsed.get("title") or coverage.get("title") or query).strip()
        body = str(parsed.get("body") or "").strip()
    missing: list[str] = []
    for skill in skills:
        source_id = str(skill.get("source_id") or "")
        name = str(skill.get("name") or "")
        if source_id and source_id not in body:
            missing.append(f"{name} `{source_id}`".strip())
        elif name and name not in body:
            missing.append(name)
    if missing:
        body = (body + "\n\n收录来源：\n" + "\n".join(missing)).strip()
    if "AIGC" not in body:
        body += "\n\nAIGC"
    return {"title": title or str(coverage.get("title") or query), "body": body}


def _popularity(skill: dict[str, object]) -> int:
    """Comparable popularity across sources: installs for skills.sh, stars for GitHub.

    skills.sh has no stars, so its entries would always lose a star-only sort;
    SKILLS_SH_INSTALLS_PER_STAR is the documented, deliberately crude exchange
    rate that keeps a genuinely installed skill in the capped selection.
    """
    if str(skill.get("source") or "") == SKILLS_SH_SOURCE:
        return max(1, _as_int(skill.get("installs")) // SKILLS_SH_INSTALLS_PER_STAR)
    return _as_int(skill.get("stars"))


def aggregate_skills(snapshot: dict[str, object] | None) -> list[dict[str, object]]:
    """Every skill the package holds — searched or pasted — in workflow order.

    生成图片 renders the package, not the last API response: a skill that was
    pasted after a search, or one the current coverage map has no stage for, must
    still get its own poster. Rows are deduped, filed into the package's own
    workflow (a real coverage map first, then :data:`WORKFLOW_STAGES`), and sorted
    so the posters read as one pipeline.
    """
    base = snapshot if isinstance(snapshot, dict) else {}
    stored_stages = [dict(row) for row in (base.get("stages") or []) if isinstance(row, dict)]
    for stage in stored_stages:
        # A package stored before 1.4.0 still carries the old copy-facing name on
        # the bucket stage; the posters must not print it.
        if str(stage.get("id") or "") == MANUAL_STAGE_ID:
            stage["name"] = MANUAL_STAGE_NAME
    stages = list(stored_stages)
    _workflow_stage_rows(stages)
    by_stage = {str(stage.get("id") or ""): str(stage.get("name") or "") for stage in stages}

    manual_rows = [
        row
        for row in (base.get("manual_skills") or [])
        if isinstance(row, dict) and not row.get("gap")
    ]
    public_rows = [
        row
        for row in (base.get("skills") or [])
        if isinstance(row, dict) and not row.get("gap")
    ]
    # A pasted skill is the newer intent for its repo, so it wins the repo slot;
    # the searched twin of the same repo does not get a second poster.
    claimed = {_skill_key(row) for row in manual_rows if _skill_key(row)}

    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in public_rows:
        key = _skill_key(row)
        if not key or key in claimed or key in seen:
            continue
        seen.add(key)
        rows.append(dict(row))
    # Pasted rows dedupe per *slot* (repo + skill path), never per repo: two
    # skills living in one monorepo are two skills and keep two posters.
    seen_slots: set[str] = set()
    for row in manual_rows:
        slot = _manual_key(row)
        if not slot or slot in seen_slots:
            continue
        seen_slots.add(slot)
        rows.append(_public_skill(row))

    order = {str(stage.get("id") or ""): index for index, stage in enumerate(stages)}
    name_to_id = {
        str(stage.get("name") or ""): str(stage.get("id") or "") for stage in stages
    }
    for row in rows:
        stage_id = str(row.get("stage_id") or "")
        # The package's own stages win: a row stored with only a stage *name* still
        # belongs to that step of this package's workflow.
        if not stage_id:
            stage_id = name_to_id.get(str(row.get("stage_name") or ""), "")
        # A row sitting in no stage, or in the catch-all bucket, gets a real
        # production step whenever its own material names one.
        if stage_id in ("", MANUAL_STAGE_ID) or stage_id not in order:
            stage_id = _stage_for_manual(row, stages)
            order = {str(stage.get("id") or ""): index for index, stage in enumerate(stages)}
            name_to_id = {
                str(stage.get("name") or ""): str(stage.get("id") or "") for stage in stages
            }
        row["stage_id"] = stage_id
        row["stage_name"] = by_stage.get(stage_id) or str(row.get("stage_name") or "")
    # Rebuild the name map once: `_stage_for_manual` may have appended stages.
    by_stage = {str(stage.get("id") or ""): str(stage.get("name") or "") for stage in stages}
    for row in rows:
        row["stage_name"] = by_stage.get(str(row.get("stage_id") or "")) or str(row.get("stage_name") or "")
    rows.sort(key=lambda row: order.get(str(row.get("stage_id") or ""), len(order)))
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def skill_identity(skill: dict[str, object]) -> str:
    """Stable id for the collected-skill checkbox: repo plus skill slot."""
    return _manual_key(skill)


def apply_skill_selection(
    skills: list[dict[str, object]],
    selected_ids: list[object] | None,
) -> list[dict[str, object]]:
    """Skills that should enter 生成图片.

    ``selected_ids is None`` keeps every row unless it was stored
    ``selected: False``. An explicit list — including empty — is the
    current checkbox set and wins over stored flags.
    """
    rows = [row for row in skills if isinstance(row, dict)]
    if selected_ids is None:
        return [dict(row) for row in rows if row.get("selected") is not False]
    wanted = {str(item).strip().lower() for item in selected_ids if str(item).strip()}
    picked: list[dict[str, object]] = []
    for row in rows:
        keys = {
            skill_identity(row),
            str(row.get("name") or "").lower(),
            str(row.get("source_id") or "").lower(),
            str(row.get("full_name") or "").lower(),
        }
        if keys & wanted:
            item = dict(row)
            item["selected"] = True
            picked.append(item)
    return picked


def _dedupe_by_skill_name(skills: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep one searched skill per name. Manual rows are never collapsed."""
    kept: list[dict[str, object]] = []
    index_by_name: dict[str, int] = {}
    for skill in skills:
        if skill.get("gap"):
            kept.append(skill)
            continue
        name = str(skill.get("name") or "").strip().lower()
        if not name or skill.get("manual"):
            kept.append(skill)
            continue
        previous = index_by_name.get(name)
        if previous is None:
            index_by_name[name] = len(kept)
            kept.append(skill)
            continue
        current = kept[previous]
        if current.get("manual"):
            continue
        if _popularity(skill) > _popularity(current):
            kept[previous] = skill
    return kept


def cap_skills(
    skills: list[dict[str, object]],
    stages: list[dict[str, object]],
    limit: int = SKILL_CAP,
) -> list[dict[str, object]]:
    if len(skills) <= limit:
        return list(skills)
    # A pasted skill is user state, not a search result: it must never be the
    # entry a popularity sort drops.
    pinned = [skill for skill in skills if skill.get("manual")]
    rest = [skill for skill in skills if not skill.get("manual")]
    by_stage: dict[str, list[dict[str, object]]] = {}
    for skill in rest:
        by_stage.setdefault(str(skill.get("stage_id") or ""), []).append(skill)
    kept: list[dict[str, object]] = list(pinned[:limit])
    extras: list[dict[str, object]] = []
    for stage in stages:
        group = list(by_stage.get(str(stage["id"]), []))
        if not group:
            continue
        group.sort(key=_popularity, reverse=True)
        kept.append(group[0])
        extras.extend(group[1:])
    extras.sort(key=_popularity, reverse=True)
    kept.extend(extras[: max(limit - len(kept), 0)])
    order = {str(stage["id"]): index for index, stage in enumerate(stages)}
    kept.sort(
        key=lambda item: (
            order.get(str(item.get("stage_id") or ""), 99),
            -_popularity(item),
        )
    )
    return kept


def _stage_skill_counts(skills: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for skill in skills:
        if skill.get("gap"):
            continue
        sid = str(skill.get("stage_id") or "")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def _filled_stages(
    stages: list[dict[str, object]],
    skills: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Stages that really hold a skill — the only ones written into the article."""
    counts = _stage_skill_counts(skills)
    filled: list[dict[str, object]] = []
    for stage in stages:
        if stage.get("gap"):
            continue
        sid = str(stage.get("id") or "")
        if counts.get(sid, 0) < 1:
            continue
        filled.append(
            {
                "id": stage["id"],
                "name": stage["name"],
                "queries": list(stage.get("queries") or []),
                "required": bool(stage.get("required", True)),
            }
        )
    return filled


def _visible_stages(
    stages: list[dict[str, object]],
    skills: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Every stage in coverage order, empty ones marked `gap` instead of dropped."""
    counts = _stage_skill_counts(skills)
    rows: list[dict[str, object]] = []
    for stage in stages:
        sid = str(stage.get("id") or "")
        filled = counts.get(sid, 0) >= 1
        row: dict[str, object] = {
            "id": stage["id"],
            "name": stage["name"],
            "queries": list(stage.get("queries") or []),
            "required": bool(stage.get("required", True)),
            "gap": not filled,
        }
        if not filled:
            row["gap_reason"] = str(stage.get("gap_reason") or GAP_REASON)
        for key in ("fetches", "searched_terms"):
            if key in stage:
                row[key] = stage[key]
        rows.append(row)
    return rows


def _seen_keys(skill: dict[str, object], *, include_repo: bool = True) -> list[str]:
    """The dedupe keys one skill occupies in a search's `seen` set.

    Three levels, and only the ones that apply:

    * ``repo:<owner/repo>`` — a GitHub repo is one entry whatever found it, so the
      same project cannot be collected twice from two sources;
    * ``id:<source_id>`` — a skills.sh id is ``<owner>/<repo>/<skill>``, so one
      repo may legitimately host several skills;
    * ``slot:<source_id>|<skill path or name>`` — the monorepo slot, which is what
      keeps two skills from the same repo apart.

    A bare skill *name* is deliberately not a key: two different owners can ship a
    skill with the same name, and deduping on the name silently dropped one of
    them. ``include_repo=False`` is for a pasted row: the user pasted a specific
    slot, so a sibling from the same repo must not evict it.
    """
    source_id = str(skill.get("source_id") or skill.get("full_name") or "").strip().lower()
    slot = str(skill.get("skill_path") or skill.get("name") or "").strip().lower()
    parts = [part for part in source_id.split("/") if part]
    # Only a repo-shaped id occupies the repo slot. A skills.sh id is
    # `<owner>/<repo>/<skill>`, and one repo may legitimately host many skills, so
    # it must never claim the whole repo.
    repo = ""
    if len(parts) == 2:
        repo = "/".join(parts)
    elif len(parts) < 2:
        repo = str(skill.get("full_name") or skill.get("repo") or "").strip().lower()
    keys: list[str] = []
    if repo and include_repo:
        keys.append(f"repo:{repo}")
    if source_id:
        keys.append(f"id:{source_id}")
        keys.append(f"slot:{source_id}|{slot}")
    return keys


def _matches_seen(skill: dict[str, object], seen: set[str]) -> bool:
    """True when this skill (or one of its slots) was already collected.

    A pasted row matches on its own slot only — ``<source_id>|<skill path>`` — so
    a sibling skill from the same monorepo neither evicts it nor is evicted by it.
    A searched row matches on repo, id and slot, which is what keeps one GitHub
    repo to one entry.
    """
    if skill.get("manual"):
        source_id = str(skill.get("source_id") or skill.get("full_name") or "").strip().lower()
        slot = str(skill.get("skill_path") or skill.get("name") or "").strip().lower()
        if not source_id:
            return False
        return f"slot:{source_id}|{slot}" in seen
    return any(key in seen for key in _seen_keys(skill))


def _mark_seen(skill: dict[str, object], seen: set[str]) -> None:
    seen.update(_seen_keys(skill))


def _reuse_previous(
    previous: dict[str, object] | None,
    stages: list[dict[str, object]],
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
) -> int:
    """Carry the package's existing skills into this search.

    A keyword search adds to the package instead of replacing it: 生成图片 is the
    step that aggregates, so losing an earlier keyword's skills would make the
    cover summarise a set the user never assembled. A row whose old stage no
    longer exists is re-filed into the new workflow rather than dropped, and a
    pasted row (user state) skips the quality gate it already bypassed on paste.
    """
    if not isinstance(previous, dict):
        return 0
    stage_by_id = {str(stage["id"]): stage for stage in stages}
    stage_by_name = {str(stage.get("name") or ""): stage for stage in stages}
    added = 0
    for skill in previous.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        source_id = str(skill.get("source_id") or skill.get("full_name") or "").strip()
        is_skills_sh = str(skill.get("source") or "") == SKILLS_SH_SOURCE
        if not source_id or ("/" not in source_id and not is_skills_sh):
            continue
        if _matches_seen(skill, seen):
            continue
        hit = dict(skill)
        stage = stage_by_id.get(str(skill.get("stage_id") or "")) or stage_by_name.get(
            str(skill.get("stage_name") or "")
        )
        if stage is None:
            # Re-file instead of dropping: the coverage map changed, the skill did
            # not stop existing.
            stage_id = _stage_for_manual(hit, stages)
            stage_by_id = {str(row.get("id") or ""): row for row in stages}
            stage_by_name = {str(row.get("name") or ""): row for row in stages}
            stage = stage_by_id.get(stage_id)
        if stage is None:
            continue
        hit["source_id"] = source_id
        hit["full_name"] = source_id
        hit["name"] = str(hit.get("name") or source_id.rsplit("/", 1)[-1])
        hit["stage_id"] = str(stage["id"])
        hit["stage_name"] = str(stage.get("name") or "")
        # This skill cleared the gate in a previous run, so its verification is
        # carried over explicitly. Do NOT fabricate a `skill_path` to satisfy the
        # gate: a path we never fetched must not count as evidence.
        hit["skill_verified"] = True
        hit["gap"] = False
        if not hit.get("manual") and not passes_quality(hit, now=now):
            continue
        _mark_seen(hit, seen)
        collected.append(hit)
        added += 1
    return added


def write_package(
    output_root: Path,
    payload: dict[str, object],
    skills: list[dict[str, object]],
) -> Path:
    digest = hashlib.sha256(str(payload.get("query") or "").encode("utf-8")).hexdigest()[:12]
    folder = Path(output_root) / digest
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "images").mkdir(exist_ok=True)
    skills_dir = folder / "skills"
    skills_dir.mkdir(exist_ok=True)
    for skill in skills:
        name = _safe_name(str(skill.get("source_id") or skill.get("name") or "skill")) + ".md"
        (skills_dir / name).write_text(str(skill.get("skill_md") or ""), encoding="utf-8")
    article = payload.get("article") if isinstance(payload.get("article"), dict) else {}
    (folder / "article.md").write_text(
        f"# {article.get('title') or payload.get('title')}\n\n{article.get('body') or ''}\n",
        encoding="utf-8",
    )
    public = dict(payload)
    public["package_path"] = str(folder)
    (folder / "index.json").write_text(
        json.dumps(public, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return folder


def _skill_kind(record: dict[str, object]) -> str:
    # Deliberately excludes the coverage stage: "剪辑" is a pipeline position,
    # not evidence of what the skill does, and including it collapsed every
    # stage of a wide map into one category.
    blob = " ".join(
        [
            str(record.get("name") or ""),
            str(record.get("description") or ""),
            str(record.get("skill_excerpt") or ""),
        ]
    )
    lowered = blob.lower()
    for token, label in SKILL_KIND_RULES:
        if token in lowered:
            return label
    return infer_kind("", _as_str_list(record.get("topics")), blob)


def _skill_url(skill: dict[str, object], source_id: str) -> str:
    explicit = str(skill.get("url") or skill.get("html_url") or "").strip()
    if explicit.startswith(("http://", "https://")):
        return explicit
    if str(skill.get("source") or "") == SKILLS_SH_SOURCE:
        return f"https://{SKILLS_SH_SOURCE}/{source_id}"
    if "/" in source_id:
        return f"https://github.com/{source_id}"
    return f"https://{SKILLS_SH_SOURCE}/"


def skills_as_repos(skills: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """Turn collected skills into fact records for the poster pipeline.

    ``kind`` is the capability category (never the source). Copy fields stay
    empty until the LLM enrich step fills them.
    """
    from .github_weekly import format_stars

    repos: list[dict[str, object]] = []
    for index, skill in enumerate(skills, start=1):
        name = str(skill.get("name") or f"skill-{index}")
        stage = str(skill.get("stage_name") or skill.get("stage") or "")
        description = str(skill.get("description") or skill.get("does") or "")
        excerpt = str(skill.get("skill_excerpt") or "")
        installs = int(skill.get("installs") or skill.get("stars") or 0)
        source_id = str(skill.get("source_id") or name)
        record: dict[str, object] = {
            "rank": index,
            "name": name,
            "full_name": source_id,
            "owner": source_id.split("/", 1)[0],
            # The poster schema requires a URL; a candidate whose source never
            # supplied one must still render instead of aborting the package.
            "url": _skill_url(skill, source_id),
            "stars": installs,
            "stars_label": format_stars(installs),
            "installs": installs,
            "description": description,
            "skill_excerpt": excerpt,
            "stage_name": stage,
            "source": str(skill.get("source") or ""),
            "topics": _as_str_list(skill.get("topics")),
            "icon_url": str(skill.get("icon_url") or ""),
            "summary": "",
            "does": "",
            "how": "",
            "audience": "",
            "scene": "",
            "enriched": False,
        }
        record["kind"] = _skill_kind(record)
        repos.append(record)
    return repos


def _skill_groups(
    stages: list[dict[str, object]],
    skills: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_stage: dict[str, list[str]] = {}
    for skill in skills:
        if skill.get("gap"):
            continue
        sid = str(skill.get("stage_id") or "")
        source_id = str(skill.get("source_id") or skill.get("full_name") or "")
        if not sid or not source_id:
            continue
        bucket = by_stage.setdefault(sid, [])
        if source_id not in bucket:
            bucket.append(source_id)
    groups: list[dict[str, object]] = []
    for stage in stages:
        sid = str(stage.get("id") or "")
        ids = by_stage.get(sid) or []
        if not ids:
            continue
        groups.append({"stage_id": sid, "skill_ids": ids})
    return groups


def _record_rejection(
    ledger: list[dict[str, object]] | None,
    hit: dict[str, object],
    reason: str,
) -> None:
    """Keep why a candidate was dropped, so coverage gaps are explainable (B4)."""
    if ledger is None or len(ledger) >= _MAX_LEDGER:
        return
    ledger.append(
        {
            "source": str(hit.get("source") or ""),
            "source_id": str(hit.get("source_id") or hit.get("full_name") or ""),
            "name": str(hit.get("name") or ""),
            "reason": reason,
        }
    )


def _ledger_rows(ledger: list[dict[str, object]]) -> list[dict[str, object]]:
    return [dict(row) for row in ledger[:_MAX_REJECTIONS_EXPOSED]]


def _search_stage(
    stage: dict[str, object],
    getter: Getter,
    searcher: Searcher,
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
    *,
    second_pass: bool,
    on_query: Callable[[str, str], None] | None = None,
    on_degrade: Callable[[str], None] | None = None,
    ledger: list[dict[str, object]] | None = None,
    api_ok: Callable[[str], bool] | None = None,
) -> None:
    stage_id = str(stage["id"])
    name = str(stage["name"])
    searched: list[str] = stage["searched_terms"] if isinstance(stage.get("searched_terms"), list) else []
    stage["searched_terms"] = searched
    raw_queries = [str(item) for item in (stage.get("queries") or []) if str(item).strip()]
    if second_pass:
        queries = [f"{name} claude skill OR agent skill"]
    else:
        queries = raw_queries or [name]
    for terms in queries:
        if on_query is not None:
            on_query(name, terms)
        if terms not in searched:
            searched.append(terms)
        # Every source is queried before any candidate is ingested: one search
        # call each, so a tight stage slice can never squeeze a channel out, and
        # GitHub (first) owns its own template with the domain filter while the
        # skills.sh template searches in-site with no domain filter at all.
        github_items = _github_search(f"SKILL.md {terms}", getter)
        skills_sh_items = _skills_sh_search(
            SEARCH_TEMPLATES[SKILLS_SH_SOURCE](terms), getter, on_degrade=on_degrade
        )
        try:
            web_hits = searcher(SEARCH_TEMPLATES["github"](terms))
        except (OSError, TimeoutError, ValueError, RuntimeError):
            web_hits = []
        for item in github_items:
            _ingest_repo(
                item, stage, getter, collected, seen, now, registry=False, ledger=ledger,
                api_ok=api_ok,
            )
        for item in skills_sh_items:
            _ingest_repo(
                item,
                stage,
                getter,
                collected,
                seen,
                now,
                registry=False,
                ledger=ledger,
                source=SKILLS_SH_SOURCE,
                api_ok=api_ok,
            )
        for ref in _names_from_web(web_hits):
            source = str(ref.get("source") or "GitHub")
            full_name = str(ref.get("full_name") or "")
            if source not in {"GitHub", SKILLS_SH_SOURCE} or not full_name:
                # Preserved, never silently discarded: recorded as unresolved.
                _record_rejection(ledger, ref, "unresolved_ref")
                continue
            repo = _repo_lookup(full_name, getter, api_ok)
            if repo is None:
                continue
            if source == SKILLS_SH_SOURCE:
                merged = dict(repo)
                merged.update(
                    {
                        key: value
                        for key, value in ref.items()
                        if key not in {"full_name"} and value not in (None, "")
                    }
                )
                merged["source"] = SKILLS_SH_SOURCE
                _ingest_repo(
                    merged,
                    stage,
                    getter,
                    collected,
                    seen,
                    now,
                    registry=False,
                    ledger=ledger,
                    source=SKILLS_SH_SOURCE,
                    api_ok=api_ok,
                )
                continue
            _ingest_repo(
                repo, stage, getter, collected, seen, now, registry=False, ledger=ledger,
                api_ok=api_ok,
            )
        if _stage_skills(collected, stage_id):
            return


def _collect_known_roots(
    getter: Getter,
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
    *,
    ledger: list[dict[str, object]] | None = None,
    api_ok: Callable[[str], bool] | None = None,
) -> None:
    dummy_stage = {"id": "", "name": ""}
    for url in KNOWN_ROOTS:
        if api_ok is not None and not api_ok(url):
            continue
        try:
            payload = getter(url)
        except (OSError, TimeoutError, ValueError, RuntimeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            for item in payload["items"]:
                if isinstance(item, dict):
                    _ingest_repo(
                        item, dummy_stage, getter, collected, seen, now, registry=True,
                        ledger=ledger, api_ok=api_ok,
                    )
            continue
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                html_url = str(entry.get("html_url") or entry.get("url") or "")
                match = GITHUB_REPO_RE.search(html_url)
                if match is None:
                    continue
                repo = {
                    "full_name": f"{match.group(1)}/{match.group(2)}",
                    "description": str(entry.get("name") or ""),
                    "stargazers_count": 0,
                    "html_url": html_url,
                    "pushed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "owner": {"login": match.group(1)},
                }
                _ingest_repo(
                    repo, dummy_stage, getter, collected, seen, now, registry=True,
                    ledger=ledger, api_ok=api_ok,
                )


def _github_search(terms: str, getter: Getter) -> list[dict[str, object]]:
    query = f"{terms} fork:false"
    url = f"{SEARCH_URL}?{urlencode({'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': str(GITHUB_PER_PAGE)})}"
    try:
        payload = getter(url)
    except (OSError, TimeoutError, ValueError, RuntimeError):
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _github_repo_from_ref(value: object) -> str:
    """Best-effort `owner/repo` from a repo name, URL, or skills.sh `source`."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = GITHUB_REPO_RE.search(text)
    if match is not None:
        return f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
    parts = [part for part in text.strip("/").split("/") if part]
    if len(parts) >= 2 and "." not in parts[0]:
        return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return ""


def _skills_sh_items(payload: object) -> list[dict[str, object]] | None:
    """Decode a skills.sh /api/search payload defensively.

    None means "not usable" (wrong type, malformed JSON, non-list `skills`); a row
    with a missing/None `installs` decodes to 0 and is judged by the quality gate
    instead of raising.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            return None
    if isinstance(payload, list):
        rows: object = payload
    elif isinstance(payload, dict):
        rows = payload.get("skills")
    else:
        return None
    if not isinstance(rows, list):
        return None
    hits: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        skill_id = str(row.get("id") or "").strip()
        if not skill_id:
            continue
        source_repo = _github_repo_from_ref(row.get("source"))
        hits.append(
            {
                "source": SKILLS_SH_SOURCE,
                "source_id": skill_id,
                "skillId": str(row.get("skillId") or "").strip(),
                "name": str(row.get("name") or row.get("skillId") or skill_id.rsplit("/", 1)[-1]).strip(),
                "installs": _as_int(row.get("installs")),
                "installs_missing": row.get("installs") is None,
                "source_repo": source_repo,
                "full_name": source_repo,
                "url": f"https://{SKILLS_SH_SOURCE}/{skill_id}",
                "description": str(row.get("description") or "")[:400],
            }
        )
    return hits


def _skills_sh_search(
    query: str,
    getter: Getter,
    *,
    on_degrade: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    """Search skills.sh through the shared injectable HTTP indirection.

    Free, unauthenticated public JSON endpoint (GET /api/search?q=...); the only
    documented-per-project option, since skills.sh prices no official API and
    /api/skill/<id> answers 401. Failures never raise: they degrade, and GitHub
    results are kept.
    """
    terms = SEARCH_TEMPLATES[SKILLS_SH_SOURCE](query)
    if not terms:
        return []
    url = f"{SKILLS_SH_SEARCH_URL}?{urlencode({'q': terms})}"
    try:
        payload = _try_get(getter, url)
    except (_RateLimited, _StageQuota) as exc:
        _LOG.debug("skills.sh search unavailable (%s): %s", type(exc).__name__, url)
        return []
    except Exception as exc:  # defensive: an adapter bug must not block GitHub
        _LOG.debug("skills.sh search failed: %s", exc)
        return []
    hits = _skills_sh_items(payload)
    if hits is None:
        _LOG.debug("skills.sh payload not usable: %s", url)
        if on_degrade is not None:
            on_degrade(f"{SKILLS_SH_SOURCE}:unparseable")
        return []
    # Only the install-sorted head is worth fetching SKILL.md for.
    return hits[:SKILLS_SH_PER_QUERY]


def _repo_lookup(full_name: str, getter: Getter, api_ok: Callable[[str], bool] | None = None) -> dict[str, object] | None:
    url = f"https://api.github.com/repos/{full_name}"
    if api_ok is not None and not api_ok(url):
        # The core quota is parked: a stub keeps the candidate usable (its URL
        # and name are known) instead of dropping it for a missing star count.
        return {"full_name": full_name, "html_url": f"https://github.com/{full_name}"}
    try:
        payload = getter(url)
    except (OSError, TimeoutError, ValueError, RuntimeError):
        return {"full_name": full_name, "html_url": f"https://github.com/{full_name}"}
    if isinstance(payload, dict) and payload.get("full_name"):
        return payload
    return {"full_name": full_name, "html_url": f"https://github.com/{full_name}"}


def _ingest_repo(
    item: dict[str, object],
    stage: dict[str, object],
    getter: Getter,
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
    *,
    registry: bool,
    ledger: list[dict[str, object]] | None = None,
    source: str | None = None,
    api_ok: Callable[[str], bool] | None = None,
) -> None:
    """Turn one candidate into a collected hit when it clears the quality gate.

    Source vocabulary is GitHub / registry / skills.sh. GitHub and registry hits
    must name a repo; a skills.sh hit is identified by its own id, so it may still
    ingest (as an unresolved candidate) when no `owner/repo` can be resolved.
    """
    declared = str(source or item.get("source") or "").strip()
    if not declared:
        declared = "registry" if registry else "GitHub"
    full_name = str(item.get("full_name") or "").strip()
    if "/" not in full_name:
        full_name = _github_repo_from_ref(
            item.get("source_repo") or item.get("source_id") or item.get("html_url") or item.get("url")
        )
    source_id = str(item.get("source_id") or "").strip()
    if not full_name and declared != SKILLS_SH_SOURCE:
        return
    if not source_id:
        source_id = full_name
    if not source_id:
        return
    owner = full_name.split("/", 1)[0] if full_name else ""
    if owner and owner.lower() in SKIP_OWNERS:
        return
    repo_key = full_name.lower()
    key = (source_id if declared == SKILLS_SH_SOURCE else (full_name or source_id)).lower()
    if declared == SKILLS_SH_SOURCE:
        name = str(item.get("name") or item.get("skillId") or source_id.rsplit("/", 1)[-1]).strip()
    else:
        name = full_name.split("/", 1)[1] if full_name else str(item.get("name") or source_id)
    provisional = {"source_id": source_id, "full_name": full_name, "name": name}
    if _matches_seen(provisional, seen):
        return
    if full_name:
        try:
            skill_md, skill_path = fetch_skill_md(full_name, getter, name, api_ok=api_ok)
        except _RateLimited:
            # A rate limit is not evidence that the repo lacks a skill, but it is
            # also not evidence that it HAS one: leave skill_path empty so the
            # quality gate reports `no_skill_md` instead of accepting a candidate
            # whose description was never actually located.
            skill_md, skill_path = "", ""
        except _StageQuota:
            # This stage already spent its share: record it instead of spending
            # more budget (or accepting a skill we could not verify).
            _record_rejection(
                ledger,
                {"source": declared, "source_id": source_id, "name": name},
                "stage_quota",
            )
            return
    else:
        skill_md, skill_path = "", ""
    hit = {
        "name": name,
        "description": str(item.get("description") or ""),
        "does": str(item.get("description") or ""),
        "url": str(item.get("html_url") or item.get("url") or f"https://github.com/{full_name}"),
        "stars": _as_int(item.get("stargazers_count") or item.get("stars")),
        "installs": _as_int(item.get("installs")),
        "pushed_at": str(item.get("pushed_at") or ""),
        "source_id": source_id,
        "full_name": full_name or source_id,
        "repo": full_name,
        "source": declared,
        "skill_md": skill_md,
        "skill_path": skill_path,
        "topics": _as_str_list(item.get("topics")),
        "icon_url": str(item.get("icon_url") or item.get("owner_avatar") or ""),
        "registry": registry,
        "stage_id": str(stage.get("id") or ""),
        "stage_name": str(stage.get("name") or ""),
    }
    ok, reason = quality_reason(hit, now=now)
    if not ok:
        _record_rejection(ledger, hit, reason)
        return
    # The fetched `skill_path` can be stricter than the provisional slot, so the
    # second check catches a row the first pass could not recognise as a duplicate.
    if _matches_seen(hit, seen):
        return
    # One entry per repo, whatever source found it first (COVERAGE.md dedupe). A
    # skills.sh id is `<owner>/<repo>/<skill>`, so one repo can host many distinct
    # skills and must not be deduped away after the first.
    _mark_seen(hit, seen)
    collected.append(hit)


def _skill_path(template: str, name: str) -> str:
    return template.format(name=name) if "{name}" in template else template


def _url_path(path: str) -> str:
    """Percent-encode a repo path for a URL, without double-encoding one.

    Skill directories are routinely named in Chinese (`ai剪口播/SKILL.md`). An
    unencoded path makes urllib raise UnicodeEncodeError before any request is
    made, which used to surface as a raw `github:UnicodeEncodeError` in the UI and
    marked the whole source as degraded.
    """
    return quote(str(path or ""), safe="/%")


def _repo_root_listing(
    full_name: str,
    getter: Getter,
    state: dict[str, object],
) -> object:
    try:
        return _try_get(getter, f"https://api.github.com/repos/{full_name}/contents")
    except _RateLimited:
        state["rate_limited"] = True
    except _StageQuota as exc:
        state["quota"] = exc
    return None


def _layout_present(listing: object, path: str) -> bool:
    """True when the repo root listing advertises a layout's top directory."""
    top = path.split("/", 1)[0]
    if top == path:
        return True
    names = {
        str(entry.get("name") or "").lower()
        for entry in listing
        if isinstance(entry, dict)
    } if isinstance(listing, list) else set()
    return top.lower() in names


def fetch_skill_md(
    full_name: str,
    getter: Getter,
    skill_name: str = "",
    api_ok: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    """Locate the skill description file in every layout COVERAGE.md promises.

    ``{name}`` is the **skill** directory name, not the repo basename: a skills.sh
    monorepo such as ``genmedia-labs/skills`` keeps its skills in sibling
    directories (``video-edit/SKILL.md``), so substituting the repo name would
    404 every probe and reject the source's most popular entries.

    Cost control: the root layout is a single raw fetch in the common case, and
    nested layouts are only probed for directories the repo's own root listing
    advertises (one cached contents call), so the shared fetch budget survives.
    When the GitHub API quota is parked (``api_ok`` answers False) the listing is
    skipped and every documented layout is probed on raw.githubusercontent.com
    instead: those probes are not quota-metered, so the candidate still gets a
    fair chance instead of being rejected as `no_skill_md`.
    """
    name = (skill_name or "").strip() or full_name.rsplit("/", 1)[-1]
    state: dict[str, object] = {"rate_limited": False, "quota": None}
    listing_url = f"https://api.github.com/repos/{full_name}/contents"
    use_api = api_ok is None or bool(api_ok(listing_url))

    def probe(path: str, *, contents: bool = True) -> tuple[str, str]:
        raw_url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{_url_path(path)}"
        try:
            payload = _try_get(getter, raw_url)
        except _RateLimited:
            state["rate_limited"] = True
            payload = None
        except _StageQuota as exc:
            state["quota"] = exc
            return "", ""
        text = _as_markdown(payload)
        if text:
            return text, path
        if not contents:
            return "", ""
        api_url = f"https://api.github.com/repos/{full_name}/contents/{_url_path(path)}"
        try:
            payload = _try_get(getter, api_url)
        except _RateLimited:
            state["rate_limited"] = True
            payload = None
        except _StageQuota as exc:
            state["quota"] = exc
            return "", ""
        try:
            text, found = _from_contents(payload, getter)
        except _RateLimited:
            state["rate_limited"] = True
            text, found = "", ""
        except _StageQuota as exc:
            state["quota"] = exc
            text, found = "", ""
        if text:
            return text, found or path
        return "", ""

    text, found = probe(_skill_path(SKILL_PATHS[0], name), contents=use_api)
    if text:
        return text, found
    listing = _repo_root_listing(full_name, getter, state) if use_api else None
    if state["quota"] is not None:
        raise state["quota"]
    known_layouts = isinstance(listing, list)
    nested = [_skill_path(template, name) for template in SKILL_PATHS[1:]]
    if known_layouts:
        nested = [path for path in nested if _layout_present(listing, path)]
    for path in nested:
        text, found = probe(path, contents=known_layouts)
        if text:
            return text, found
        if state["quota"] is not None:
            break
    if state["rate_limited"]:
        raise _RateLimited(full_name)
    if state["quota"] is not None:
        raise state["quota"]
    return "", ""


def _try_get(getter: Getter, url: str) -> object:
    try:
        return getter(url)
    except (_RateLimited, _StageQuota):
        raise
    except (OSError, TimeoutError, ValueError, RuntimeError, TypeError) as exc:
        if _is_rate_limit(exc):
            raise _RateLimited(str(exc)) from exc
        return None


def _as_markdown(payload: object) -> str:
    if isinstance(payload, str) and payload.strip() and "<html" not in payload.lower()[:80]:
        return payload
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            import base64

            encoding = str(payload.get("encoding") or "base64")
            if encoding == "base64":
                try:
                    return base64.b64decode(content).decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    return ""
            return content
    return ""


def _from_contents(payload: object, getter: Getter) -> tuple[str, str]:
    text = _as_markdown(payload)
    if text:
        path = ""
        if isinstance(payload, dict):
            path = str(payload.get("path") or "")
        return text, path
    if not isinstance(payload, list):
        return "", ""
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        path = str(entry.get("path") or name)
        if name == "SKILL.md" or path.endswith("SKILL.md"):
            download = str(entry.get("download_url") or "")
            if download:
                nested = _as_markdown(_try_get(getter, download))
                if nested:
                    return nested, path
            html_url = str(entry.get("url") or "")
            if html_url:
                nested = _as_markdown(_try_get(getter, html_url))
                if nested:
                    return nested, path
    return "", ""


def _assign_pending(collected: list[dict[str, object]], stages: list[dict[str, object]]) -> None:
    by_id = {str(stage["id"]): stage for stage in stages}
    fallback = str(stages[0]["id"]) if stages else ""
    for skill in collected:
        if str(skill.get("stage_id") or ""):
            if not skill.get("stage_name"):
                skill["stage_name"] = str(by_id.get(str(skill["stage_id"]), {}).get("name") or "")
            continue
        assigned = _assign_stage(skill, stages) or fallback
        skill["stage_id"] = assigned
        skill["stage_name"] = str(by_id.get(assigned, {}).get("name") or "")


def _plan_jobs(snapshot: dict[str, object], repos: list[dict[str, object]]) -> list[dict[str, object]]:
    """The cover + poster job list for one skill package (ids only, no rendering)."""
    jobs: list[dict[str, object]] = [{"id": "cover", "kind": "cover"}]
    jobs.extend(
        {"id": f"{int(repo['rank']):02d}-{_safe_name(str(repo['name']))}", "kind": "poster"}
        for repo in repos
    )
    return jobs


def _prune_stale_pages(images_dir: Path, keep_ids: set[str]) -> list[str]:
    """Drop pages left over from a previous render into the same package dir.

    The skill lane reuses one package directory per query digest (and a shared
    ``latest``), so without this a shrinking result set keeps last run's
    ``*.html``/``*.png`` and the printed page totals stop matching the package.
    """
    removed: list[str] = []
    for pattern in ("*.html", "*.png"):
        for path in images_dir.glob(pattern):
            if path.stem not in keep_ids:
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(path.name)
    return removed


def render_skill_package(
    snapshot: dict[str, object],
    out_dir: Path,
    *,
    theme: str = "dark",
    palette: dict[str, str] | None = None,
    screenshot_fn: Callable[..., Path] | None = None,
    visual_renderer: Callable[..., list[dict[str, object]]] | None = None,
    copy_writer: Callable[[list[dict[str, object]]], list[dict[str, object]]] | None = None,
    on_image: Callable[[dict[str, object]], None] | None = None,
    poster_template: str = "1",
    cover_only: bool = False,
) -> list[dict[str, object]]:
    from .visual.weekly import render_weekly_visual

    images_dir = Path(out_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    # One aggregate for the whole sheet: whatever the package currently holds
    # (searched + pasted) is ordered as one workflow, so the cover summarises the
    # same set the individual posters are cut from.
    skills = aggregate_skills(snapshot)
    repos = skills_as_repos(skills)
    planned = _plan_jobs(snapshot, repos)
    if cover_only:
        keep = {path.stem for path in images_dir.glob("*.html")}
        keep.update(path.stem for path in images_dir.glob("*.png"))
        keep.add("cover")
        _prune_stale_pages(images_dir, keep)
    else:
        _prune_stale_pages(images_dir, {str(job["id"]) for job in planned})
    if copy_writer is not None and not cover_only:
        from .copy_validate import require_enriched_copy

        enriched = copy_writer(repos)
        if not isinstance(enriched, list) or not enriched or not all(
            isinstance(row, dict) for row in enriched
        ):
            raise LLMError(
                "llm_enrichment_failed",
                "LLM_ENRICHMENT_FAILED: enrichment not completed",
            )
        require_enriched_copy(enriched)
        repos = enriched
    cover_title = str(snapshot.get("user_title") or "").strip() or str(
        snapshot.get("title") or "Skill 查找结果"
    )
    jobs: list[dict[str, object]] = [
        {
            "id": "cover",
            "kind": "cover",
            "rank": 0,
            "title": cover_title,
            "visual_title": cover_title,
            "visual_subtitle": str(snapshot.get("theme") or ""),
            "visual_kicker": "SKILL FINDER",
            "visual_footer": "Skill 查找器 · 按覆盖阶段整理",
            "url": "",
            "file": "",
            "selected": True,
        }
    ]
    jobs.extend(
        {
            "id": f"{int(repo['rank']):02d}-{_safe_name(str(repo['name']))}",
            "kind": "poster",
            "rank": int(repo["rank"]),
            "title": str(repo["name"]),
            "url": "",
            "file": "",
            "selected": True,
        }
        for repo in repos
    )
    if cover_only:
        jobs = [job for job in jobs if job.get("kind") == "cover"]
    if visual_renderer is not None:
        # Keep the injected screenshot hook useful for lightweight renderers
        # (tests and local adapters) while the production path below remains a
        # single weekly visual workflow.
        if screenshot_fn is not None:
            render_weekly_visual(
                repos,
                [jobs[0]],
                images_dir,
                theme=theme,
                palette=palette,
                screenshot_fn=screenshot_fn,
                poster_template=poster_template,
            )
        cards = visual_renderer(
            repos, jobs, images_dir, theme=theme, palette=palette, poster_template=poster_template
        )
    else:
        cards = render_weekly_visual(
            repos,
            jobs,
            images_dir,
            theme=theme,
            palette=palette,
            screenshot_fn=screenshot_fn,
            # B5(a)3: a skill poster must not print GitHub weekly branding, the ISO
            # week, or the weekly footer. ``issue=""`` blanks the week label.
            series="",
            issue="",
            source="skill 查找器",
            footer="skill 查找器 · 按覆盖阶段整理",
            kicker="SKILL FINDER",
            poster_template=poster_template,
        )
    images: list[dict[str, object]] = []
    for row in cards:
        filename = str(row.get("file") or f"{row.get('id')}.png")
        patched = dict(row)
        patched["url"] = f"/api/generated/skills/{filename}"
        if not patched.get("file"):
            patched["file"] = filename
        images.append(patched)
        if on_image is not None:
            on_image(patched)
    return images


MANUAL_STAGE_ID = "manual"
# The display name is copy-facing: "手动补充这一环" used to be printed on the
# poster of every skill that matched no production step. The id stays `manual`
# because stored packages reference it.
MANUAL_STAGE_NAME = "通用工具"
# SKILL.md front-matter keys: never prose, so neither the poster copy nor the
# one-line description may quote them.
_SKILL_META_PREFIXES = ("name:", "description:", "license:", "allowed-tools:", "version:", "author:")


def _first_paragraph(markdown: str, limit: int = 200) -> str:
    """First readable prose line of a SKILL.md, used when the repo has no blurb."""
    for line in str(markdown or "").splitlines():
        text = re.sub(r"^[-*+]\s+", "", line.strip())
        if len(text) < 6 or text.startswith(("#", ">", "|", "```", "---", "![")):
            continue
        if text.lower().startswith(_SKILL_META_PREFIXES):
            continue
        return text[:limit]
    return ""


def _manual_hit(
    candidate: dict[str, object],
    getter: Getter,
    api_ok: Callable[[str], bool] | None = None,
) -> dict[str, object]:
    """Turn one pasted link into a collected-skill record.

    A pasted link is an explicit user choice, so it never goes through the
    quality gate (stars / installs / freshness): the user already decided this
    skill belongs in the package. SKILL.md is still fetched, because both the
    intro and the poster copy are written from it, and the link's own path hint
    is probed first so a precise paste costs one raw request.
    """
    source = str(candidate.get("source") or "GitHub")
    source_id = str(candidate.get("source_id") or "").strip()
    full_name = str(candidate.get("full_name") or "").strip()
    name = str(candidate.get("name") or source_id.rsplit("/", 1)[-1] or "skill").strip()
    skill_path = str(candidate.get("skill_path") or "").strip()
    skill_md = ""
    raw_url = str(candidate.get("raw_url") or "").strip()
    if not raw_url and full_name and skill_path:
        raw_url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{_url_path(skill_path)}"
    if raw_url:
        skill_md = _as_markdown(_try_get(getter, raw_url))
    if not skill_md and full_name:
        try:
            skill_md, found = fetch_skill_md(full_name, getter, name, api_ok=api_ok)
        except (_RateLimited, _StageQuota):
            skill_md, found = "", ""
        if skill_md:
            skill_path = found or skill_path
    repo: dict[str, object] = {}
    if full_name:
        repo = _repo_lookup(full_name, getter, api_ok) or {}
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    description = str(repo.get("description") or "").strip() or _first_paragraph(skill_md)
    return {
        "name": name,
        "description": description,
        "does": description,
        "url": str(candidate.get("url") or repo.get("html_url") or ""),
        "stars": _as_int(repo.get("stargazers_count")),
        "installs": _as_int(candidate.get("installs")),
        "pushed_at": str(repo.get("pushed_at") or ""),
        "source_id": source_id or full_name,
        "full_name": full_name or source_id,
        "repo": full_name,
        "source": source,
        "skill_md": skill_md,
        "skill_path": skill_path,
        "topics": _as_str_list(repo.get("topics")),
        "icon_url": str(owner.get("avatar_url") or ""),
        "registry": False,
        "manual": True,
        "stage_id": "",
        "stage_name": "",
        "skill_verified": bool(skill_md),
    }


def _skill_key(skill: dict[str, object]) -> str:
    return str(skill.get("source_id") or skill.get("full_name") or skill.get("name") or "").lower()


def _manual_key(skill: dict[str, object]) -> str:
    """Pasted-skill identity: repo **and** slot, so monorepo siblings both stay."""
    slot = str(skill.get("skill_path") or skill.get("name") or "").lower()
    return f"{_skill_key(skill)}|{slot}"


def _ensure_manual_stage(stages: list[dict[str, object]]) -> None:
    if not any(str(stage.get("id") or "") == MANUAL_STAGE_ID for stage in stages):
        stages.append(
            {
                "id": MANUAL_STAGE_ID,
                "name": MANUAL_STAGE_NAME,
                "queries": [],
                "required": False,
                # A pasted-skill bucket is not a search target: the search loops
                # skip stages flagged this way.
                "manual": True,
            }
        )


def _best_stage(hit: dict[str, object], stages: list[dict[str, object]]) -> tuple[str, int]:
    """Weighted best-fit stage for one skill, from what the skill says it is.

    ``_assign_stage`` counts every token anywhere in the record, which filed a
    skill called ``video-edit`` under 写作与文案 because its SKILL.md happened to
    mention a prompt. The name (5) and the blurb (2) are the evidence; a document
    match only counts against a real coverage stage, never against the generic
    workflow buckets. A skill whose material names no step lands in the 通用工具
    bucket, which is the honest answer.
    """
    name = str(hit.get("name") or "").lower()
    description = str(hit.get("description") or hit.get("does") or "").lower()
    body = str(hit.get("skill_md") or hit.get("skill_excerpt") or "").lower()
    best = ""
    best_score = 0
    for stage in stages:
        tokens = [str(stage.get("name") or "")] + [
            str(item) for item in (stage.get("queries") or [])
        ]
        # A coverage map's queries were written to find these skills, so a
        # document match is evidence there. The generic workflow buckets match on
        # name and blurb only: their single-word tokens ("数据", "script") appear
        # in half of all documents and would outvote what the skill says it is.
        body_weight = 0 if stage.get("offline") else 1
        score = 0
        for token in tokens:
            lowered = token.strip().lower()
            if not lowered:
                continue
            if lowered in name:
                score += 5
            elif lowered in description:
                score += 2
            elif body_weight and lowered in body:
                score += body_weight
        if score > best_score:
            best = str(stage.get("id") or "")
            best_score = score
    return best, best_score


def _stage_for_manual(hit: dict[str, object], stages: list[dict[str, object]]) -> str:
    """File a pasted skill into the package's own workflow, not a bucket.

    A pasted skill is still matched against the real stages first (a coverage map
    from a previous search, or the fallback :data:`WORKFLOW_STAGES`), so its
    poster names the production step it belongs to. 通用工具 is only what is left
    when a skill matches nothing at all.
    """
    stage_id, score = _best_stage(hit, [stage for stage in stages if _is_searchable(stage)])
    if score > 0:
        return stage_id
    _workflow_stage_rows(stages)
    stage_id, score = _best_stage(hit, [stage for stage in stages if stage.get("offline")])
    if score > 0:
        return stage_id
    _ensure_manual_stage(stages)
    return MANUAL_STAGE_ID


def _reinject_manual(
    stored: object,
    stages: list[dict[str, object]],
    collected: list[dict[str, object]],
    seen: set[str],
) -> int:
    """Carry user-pasted skills into a new search instead of dropping them."""
    rows = [row for row in stored if isinstance(row, dict)] if isinstance(stored, list) else []
    by_id = {str(stage.get("id") or ""): stage for stage in stages}
    added = 0
    for row in rows:
        if not _skill_key(row):
            continue
        hit = dict(row)
        if _matches_seen(hit, seen):
            continue
        stage_id = str(hit.get("stage_id") or "")
        if stage_id not in by_id:
            stage_id = _stage_for_manual(hit, stages)
            by_id = {str(stage.get("id") or ""): stage for stage in stages}
        hit["stage_id"] = stage_id
        hit["stage_name"] = str(by_id.get(stage_id, {}).get("name") or MANUAL_STAGE_NAME)
        hit["manual"] = True
        hit["gap"] = False
        _mark_seen(hit, seen)
        collected.append(hit)
        added += 1
    return added


def _fallback_article(
    skills: list[dict[str, object]],
    *,
    title: str = "",
    goal: str = "",
) -> dict[str, str]:
    """Mechanical intro used when no article model is configured.

    A package must still ship an introduction — a keyword search and a paste both
    work with no model at all — so the collected skills are listed under the
    workflow stage each one was filed into. ``title``/``goal`` let the caller name
    the piece after what the user actually typed instead of the old hard-coded
    手动补充 skill, which also used to leak onto the cover poster.
    """
    grouped: dict[str, list[str]] = {}
    for skill in skills:
        stage = str(skill.get("stage_name") or skill.get("stage_id") or "").strip() or "未归组"
        name = str(skill.get("name") or skill.get("source_id") or "")
        description = str(skill.get("description") or skill.get("does") or "").strip()
        url = str(skill.get("url") or "")
        link = f"（{url}）" if url else ""
        grouped.setdefault(stage, []).append(
            f"- {name}：{description or '收录的 skill'}{link}".strip()
        )
    lines: list[str] = []
    for stage, rows in grouped.items():
        lines.append(f"【{stage}】")
        lines.extend(rows)
    intent = str(goal or "").strip()
    if skills:
        head = f"共收录 {len(skills)} 个 skill"
        head += f"，目标：{intent}。" if intent else "。"
        head += "按工作流顺序："
    else:
        head = "还没有收录 skill。"
    body = "\n".join([head, *lines]) if lines else head
    fallback_title = str(title or "").strip() or (
        f"{intent} Skill 汇总" if intent else "Skill 汇总"
    )
    return {"title": fallback_title, "body": body + "\n\nAIGC"}


def fetch_manual_hits(
    candidates: list[dict[str, object]],
    *,
    getter: Getter | None = None,
    api_ok: Callable[[str], bool] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """Fetch the slow part of a paste: SKILL.md and repo metadata per link.

    Kept separate from the merge so the caller can do its network work without
    holding a lock on the stored package, then merge into the freshest snapshot.
    """
    fetch = getter or _default_get
    hits: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        hit = _manual_hit(candidate, fetch, api_ok)
        if not _skill_key(hit):
            skipped.append({"input": str(candidate.get("url") or ""), "reason": "unresolved"})
            continue
        hits.append(hit)
    return hits, skipped


def merge_manual_hits(
    snapshot: dict[str, object] | None,
    hits: list[dict[str, object]],
    *,
    complete: Completer | None = None,
    output_root: Path | None = None,
    skipped: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Merge already-fetched pasted skills into the current package and rewrite its intro.

    Pasted skills join the same workflow as searched ones: they are filed into
    the best-fitting production step (or into the 通用工具 bucket when nothing
    matches), they are kept when the skill cap trims the list, they are named in
    the intro, and the render stage turns them into posters. ``snapshot`` may be
    None — pasting before any search seeds the workflow chain instead of failing.
    """
    base = dict(snapshot) if isinstance(snapshot, dict) else {}
    existing = [row for row in (base.get("skills") or []) if isinstance(row, dict)]
    stored_manual = [row for row in (base.get("manual_skills") or []) if isinstance(row, dict)]
    stages = [dict(row) for row in (base.get("stages") or []) if isinstance(row, dict)]
    if not stages:
        # A paste-only package gets the fallback production chain, not a single
        # catch-all bucket: the posters must name a real step.
        _workflow_stage_rows(stages)
    by_stage = {str(stage.get("id") or ""): str(stage.get("name") or "") for stage in stages}
    merged: list[dict[str, object]] = []
    for hit in hits:
        if not isinstance(hit, dict) or not _skill_key(hit):
            continue
        hit = dict(hit)
        stage_id = _stage_for_manual(hit, stages)
        by_stage = {str(stage.get("id") or ""): str(stage.get("name") or "") for stage in stages}
        hit["stage_id"] = stage_id
        hit["stage_name"] = by_stage.get(stage_id) or MANUAL_STAGE_NAME
        merged.append(hit)
    keys = {_skill_key(hit) for hit in merged}
    slots = {_manual_key(hit) for hit in merged}
    stored_slots = {_manual_key(row) for row in stored_manual}
    # Only a *searched* row is a repo-level twin: another pasted skill from the
    # same monorepo is a new skill, not a replacement of its sibling.
    searched_repos = {_skill_key(row) for row in existing if not row.get("manual")}
    # "added" must mean new: replacing a slot the user pasted before (or a searched
    # twin of the same repo) is a replacement, and the UI reports the two apart.
    replaced_count = sum(
        1 for hit in merged if _manual_key(hit) in stored_slots or _skill_key(hit) in searched_repos
    )

    def replaced(row: dict[str, object]) -> bool:
        # A stored paste is replaced per slot; a searched twin per repo, because
        # the pasted record is the newer intent for that source.
        return _manual_key(row) in slots if row.get("manual") else _skill_key(row) in keys

    # Stored pastes dedupe per slot (a monorepo keeps its siblings).
    kept_manual = [row for row in stored_manual if _manual_key(row) not in slots]
    manual_rows = kept_manual + merged
    public: list[dict[str, object]] = [
        row for row in existing if not replaced(row)
    ] + [_public_skill(row) for row in merged]
    order = {str(stage.get("id") or ""): index for index, stage in enumerate(stages)}
    public.sort(key=lambda row: order.get(str(row.get("stage_id") or ""), len(order)))
    filled = _filled_stages(stages, public)
    groups = _skill_groups(filled, public)
    query = str(base.get("query") or "").strip() or (
        str(merged[0].get("name") or "") if merged else ""
    ) or "粘贴的 skill"
    coverage = {
        "goal": base.get("goal") or query,
        "title": base.get("title") or query,
        "theme": base.get("theme") or query,
        "chain": list(base.get("chain") or []),
        "stages": filled,
        "groups": groups,
    }
    article: dict[str, str] = {}
    if complete is not None:
        try:
            article = write_article(query, coverage, public, complete)
        except LLMError:
            # The model is the only thing that failed: the pasted skills must
            # still be introduced, so fall back to the mechanical list.
            article = {}
    if not article:
        # Without a model the mechanical intro is grouped by workflow stage, and
        # named after the title the user set (else the package's own name).
        article = _fallback_article(
            public,
            title=str(base.get("user_title") or base.get("title") or ""),
            goal=str(coverage.get("goal") or query),
        )
    result = dict(base)
    result.update(
        {
            "query": query,
            "goal": coverage["goal"],
            "chain": coverage["chain"],
            "title": str(article.get("title") or base.get("title") or query),
            "theme": str(base.get("theme") or query),
            "stages": _visible_stages(stages, public),
            "skills": public,
            "groups": groups,
            "article": article,
            # Posters are built from the whole package, so a merge invalidates
            # whatever was rendered before it; the gallery re-queues instead.
            "images": [],
            "phase": "skills_ready",
            "manual_skills": manual_rows,
            # The package's own limit, carried on every write so the UI can name
            # it whatever path produced the snapshot.
            "skill_cap": SKILL_CAP,
            # Bumped on every skill-set change: the render stage refuses to write
            # its older snapshot back over a package that moved under it.
            "revision": _next_revision(base.get("revision")),
            "paste_replaced": replaced_count,
        }
    )
    root = output_root
    if root is None and str(base.get("package_path") or ""):
        root = Path(str(base["package_path"])).parent
    if root is not None:
        result["package_path"] = str(write_package(root, result, manual_rows))
    # Last paste's skips must not linger in the package payload.
    result.pop("paste_skipped", None)
    if skipped:
        result["paste_skipped"] = list(skipped)
    # A pasted link with no readable SKILL.md still enters the package (the user
    # asked for it), but its poster can only use template copy — say so.
    result.pop("paste_unverified", None)
    unverified = [str(hit.get("name") or "") for hit in merged if not hit.get("skill_verified")]
    if unverified:
        result["paste_unverified"] = unverified
    return result


def add_manual_skills(
    snapshot: dict[str, object] | None,
    candidates: list[dict[str, object]],
    *,
    getter: Getter | None = None,
    complete: Completer | None = None,
    api_ok: Callable[[str], bool] | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Fetch + merge in one call (the shape tests and scripts use)."""
    hits, skipped = fetch_manual_hits(candidates, getter=getter, api_ok=api_ok)
    return merge_manual_hits(
        snapshot, hits, complete=complete, output_root=output_root, skipped=skipped
    )


def _next_revision(value: object) -> int:
    try:
        return int(value or 0) + 1
    except (TypeError, ValueError):
        return 1


def _assign_stage(hit: dict[str, object], stages: list[dict[str, object]]) -> str:
    blob = f"{hit.get('name')} {hit.get('description')} {hit.get('skill_md')}".lower()
    best = ""
    best_score = 0
    for stage in stages:
        tokens = [str(stage.get("name") or "")] + [str(item) for item in (stage.get("queries") or [])]
        score = sum(1 for token in tokens if token and token.lower() in blob)
        if score > best_score:
            best = str(stage["id"])
            best_score = score
    return best


def _stage_skills(collected: list[dict[str, object]], stage_id: str) -> list[dict[str, object]]:
    return [item for item in collected if str(item.get("stage_id") or "") == stage_id]


def _names_from_web(results: object) -> list[dict[str, object]]:
    """Normalize web-search rows into candidate refs, keeping non-GitHub links.

    GitHub rows ingest exactly as before. skills.sh rows are preserved with the
    repo they came from (`https://skills.sh/<owner>/<repo>/<skill>`) so their
    SKILL.md can still be fetched from GitHub; a `/trending`-style link is kept
    as a section ref. Nothing is dropped on the floor any more.
    """
    refs: list[dict[str, object]] = []
    seen: set[str] = set()
    if not isinstance(results, list):
        return refs
    for item in results:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(key) or "") for key in ("url", "href", "title", "snippet"))
        link = str(item.get("url") or item.get("href") or "").strip()
        match = GITHUB_REPO_RE.search(blob)
        if match is not None:
            owner, repo = match.group(1), match.group(2).rstrip(".")
            if owner.lower() in SKIP_OWNERS:
                continue
            full_name = f"{owner}/{repo.rstrip('.git')}"
            key = "gh:" + full_name.lower()
            if key in seen:
                continue
            seen.add(key)
            refs.append({"source": "GitHub", "full_name": full_name, "url": link})
            continue
        sh_match = SKILLS_SH_LINK_RE.search(blob)
        if sh_match is not None:
            first = sh_match.group(1)
            second = sh_match.group(2) or ""
            third = sh_match.group(3) or ""
            if first.lower() in SKIP_OWNERS:
                continue
            key = "sh:" + "/".join(part for part in (first, second, third) if part).lower()
            if key in seen:
                continue
            seen.add(key)
            ref: dict[str, object] = {
                "source": SKILLS_SH_SOURCE,
                "source_id": "/".join(part for part in (first, second, third) if part),
                "url": link or f"https://{SKILLS_SH_SOURCE}/{first}",
            }
            if second:
                ref["full_name"] = f"{first}/{second}"
                ref["name"] = third or second
            else:
                ref["section"] = first
                ref["name"] = first
            refs.append(ref)
            continue
        if link:
            refs.append({"source": "web", "url": link, "name": str(item.get("title") or "")[:120]})
    return refs


def _skill_excerpt(skill: dict[str, object], limit: int = 600) -> str:
    """Bounded, readable slice of SKILL.md used as material by the copy layer."""
    raw = str(skill.get("skill_md") or "")
    if not raw:
        # Rebuilding a stored (already public) record must not blank the excerpt
        # the previous run computed: the paste path re-publicizes the package.
        return str(skill.get("skill_excerpt") or "")[:limit]
    lines: list[str] = []
    size = 0
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith(("---", "```", "|", "!", ">", "<", "#!")):
            continue
        text = re.sub(r"^#{1,6}\s*", "", text)
        text = re.sub(r"^[-*+]\s+", "", text)
        text = re.sub(r"^\d+[.)]\s+", "", text)
        if text.lower().startswith(_SKILL_META_PREFIXES):
            continue
        if len(text) < 3:
            continue
        lines.append(text)
        size += len(text)
        if size >= limit:
            break
    return "\n".join(lines)[:limit].strip()


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _public_skill(skill: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(skill.get("name") or ""),
        "skill_excerpt": _skill_excerpt(skill),
        "topics": _as_str_list(skill.get("topics")),
        "icon_url": str(skill.get("icon_url") or ""),
        "description": str(skill.get("description") or skill.get("does") or ""),
        "does": str(skill.get("does") or skill.get("description") or ""),
        "url": str(skill.get("url") or ""),
        "stars": int(skill.get("stars") or 0),
        "installs": int(skill.get("installs") or 0),
        "pushed_at": str(skill.get("pushed_at") or ""),
        "source_id": str(skill.get("source_id") or skill.get("full_name") or ""),
        "source": str(skill.get("source") or "GitHub"),
        "stage_id": str(skill.get("stage_id") or ""),
        "stage_name": str(skill.get("stage_name") or ""),
        "skill_path": str(skill.get("skill_path") or ""),
        # Pasted-by-hand skills survive the cap and are labelled in the package.
        "manual": bool(skill.get("manual")),
    }


def _default_get(url: str) -> object:
    """Production HTTP path: skills.sh JSON goes through its own stdlib getter."""
    if _source_of_url(url) == SKILLS_SH_SOURCE:
        return _default_skills_sh_get(url)
    return default_github_get(url, timeout=8)


def _default_skills_sh_get(url: str, timeout: float = 20) -> object:
    """Stdlib GET for the free, unauthenticated skills.sh search endpoint.

    Measured 1-7s normally and up to ~12s when the endpoint is under burst load
    (the 2026-xx diagnostic run saw 10.7-12.2s answers), so the timeout is set
    well above the fast path: slower than this counts as a degrade, and the
    per-stage time slice keeps one slow query from starving the tail stages.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": HTTP_USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # The status code stays in the message so _is_rate_limit can see 403/429.
        raise RuntimeError(f"{SKILLS_SH_SOURCE}_http_{exc.code}") from exc
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"{SKILLS_SH_SOURCE}_unreachable") from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{SKILLS_SH_SOURCE}_invalid_json") from exc


def _default_web_search(query: str) -> list[dict[str, str]]:
    from .background import search_web

    return search_web(query)


def _parse_time(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return cleaned[:80] or "skill"
