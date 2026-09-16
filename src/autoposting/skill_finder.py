"""Coverage-first skill search: decompose, fetch, gate, package, write overview."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlencode

from .github_weekly import SEARCH_URL, default_github_get
from .llm import LLMError, parse_model_json

Getter = Callable[[str], object]
Searcher = Callable[[str], list[dict[str, str]]]
Completer = Callable[[str], str]
OnEvent = Callable[[str, dict[str, object]], None]
CancelCheck = Callable[[], bool]

SKILL_CAP = 16
FRESH_DAYS = 180
MIN_STARS = 10
MAX_FETCHES = 80
FETCH_DEADLINE_SECONDS = 240
PIPELINE_STEPS: tuple[tuple[str, str, str], ...] = (
    ("understanding", "理解需求", "正在把输入收成可执行目标"),
    ("chain", "还原生产链", "正在还原从零到交付的生产链"),
    ("decompose", "分解工作流", "正在把生产链拆成可检索模块"),
    ("keywords", "生成关键词", "正在为每个模块生成检索词"),
    ("search", "查找 Skill", "正在按模块检索 SKILL.md"),
    ("group", "还原分组", "正在按模块归组已收录 skill"),
    ("compose", "组合工作流", "正在组合成端到端工作流"),
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
SKILL_PATHS = ("SKILL.md",)
KNOWN_ROOTS = (
    "https://api.github.com/repos/anthropics/skills/contents",
)


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


def passes_quality(hit: dict[str, object], now: datetime | None = None) -> bool:
    path_blob = " ".join(
        str(hit.get(key) or "")
        for key in ("skill_path", "url", "source_id")
    )
    has_file = bool(str(hit.get("skill_md") or "").strip()) or any(
        marker in path_blob for marker in ("SKILL.md", ".agents/skills", ".claude/skills")
    )
    if not has_file:
        return False
    if int(hit.get("stars") or 0) >= MIN_STARS:
        return True
    if hit.get("registry"):
        return True
    pushed = _parse_time(str(hit.get("pushed_at") or ""))
    moment = now or datetime.now(timezone.utc)
    if pushed is not None and moment - pushed <= timedelta(days=FRESH_DAYS):
        return True
    return False


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

    def remaining(self) -> bool:
        return self.calls < self.max_fetches and time.monotonic() < self.deadline

    def get(self, url: str) -> object:
        cached = _cache_get("G:" + url)
        if cached is not _MISSING:
            return cached
        if not self.remaining():
            self.degraded = True
            raise RuntimeError("skill_search_budget")
        self.calls += 1
        try:
            result = self._getter(url)
        except Exception:
            self.degraded = True
            raise
        # An empty response is not evidence that this endpoint has no data:
        # transient upstream failures and rate-limit fallbacks are commonly
        # represented as an empty list/object by injected getters.
        if _is_rate_limit_payload(result):
            self.degraded = True
        elif _has_cacheable_content(result):
            _cache_put("G:" + url, result)
        return result

    def search(self, query: str) -> list[dict[str, str]]:
        normalized = _canonical_query(query)
        cached = _cache_get("S:" + normalized)
        if cached is not _MISSING:
            return list(cached) if isinstance(cached, list) else []
        if not self.remaining():
            self.degraded = True
            return []
        self.calls += 1
        try:
            result = self._searcher(normalized)
        except (OSError, TimeoutError, ValueError, RuntimeError):
            self.degraded = True
            return []
        if _is_rate_limit_payload(result):
            self.degraded = True
            return []
        if not isinstance(result, list):
            self.degraded = True
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
) -> dict[str, object]:
    text = _canonical_query(query)
    if not text or len(text) > 200:
        raise ValueError("query must be 1-200 characters")
    if complete is None:
        raise LLMError("skill_search_failed", "模型未配置", cause_code="llm_not_configured")
    moment = now or datetime.now(timezone.utc)

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
    coverage: dict[str, object] = {"goal": goal, "title": title, "theme": theme, "chain": chain, "stages": stages}

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
            "step_total": 7,
            "started_at": started_at,
            "elapsed_ms": elapsed_ms(),
        }

    def snapshot(
        phase: str,
        article: dict[str, str] | None = None,
        package_path: str = "",
        detail: str = "",
    ) -> dict[str, object]:
        skills = [_public_skill(skill) for skill in cap_skills(collected, stages)]
        filled = _filled_stages(stages, skills)
        use_filled = phase in {"group", "compose", "skills_ready", "error"}
        progress_step = phase if phase in PIPELINE_BY_STEP else ("compose" if phase == "skills_ready" else "search")
        return {
            "query": text,
            "goal": goal,
            "chain": list(chain),
            "title": title,
            "theme": theme,
            "stages": filled if use_filled else [
                {
                    "id": stage["id"],
                    "name": stage["name"],
                    "queries": list(stage.get("queries") or []),
                    "required": bool(stage.get("required", True)),
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

    emit_progress("understanding")
    try:
        coverage = decompose_coverage(text, complete)
    except LLMError as exc:
        raise LLMError("skill_search_failed", exc.reason, cause_code=exc.code) from exc
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise LLMError("skill_search_failed", "覆盖地图解析失败", cause_code="llm_invalid_output") from exc
    goal = str(coverage.get("goal") or text)
    chain = list(coverage.get("chain") or [])
    title = str(coverage.get("title") or text)
    theme = str(coverage.get("theme") or text)
    stages = list(coverage["stages"])
    emit_progress("understanding")
    emit_progress("chain")
    emit_progress("decompose")
    emit_progress("keywords")
    budget = _FetchBudget(
        github_get or (lambda url: default_github_get(url, timeout=8)),
        web_search or _default_web_search,
        max_fetches=max(1, int(max_fetches)),
        deadline=time.monotonic() + max(1.0, float(fetch_deadline_seconds)),
    )
    getter = budget.get
    searcher = budget.search

    try:
        reused = _reuse_previous(prior, stages, collected, seen, moment)
        if reused:
            emit("stage", snapshot("search"))
            emit_new_skills(0)
        if not aborted():
            before_roots = len(collected)
            _collect_known_roots(getter, collected, seen, moment)
            emit_new_skills(before_roots)
        for stage in stages:
            if aborted():
                break
            module_name = str(stage["name"])
            queries = [str(item) for item in (stage.get("queries") or []) if str(item).strip()] or [module_name]
            emit_progress("search", f"正在检索：{module_name} · {queries[0]}")
            before = len(collected)
            _search_stage(
                stage,
                getter,
                searcher,
                collected,
                seen,
                moment,
                second_pass=False,
                on_query=lambda _name, terms, module=module_name: emit_progress(
                    "search", f"正在检索：{module} · {terms}"
                ),
            )
            if _stage_skills(collected, str(stage["id"])):
                emit("stage", snapshot("search"))
            emit_new_skills(before)
        _assign_pending(collected, stages)
        for stage in stages:
            if aborted():
                break
            if stage.get("required") and not _stage_skills(collected, str(stage["id"])):
                module_name = str(stage["name"])
                retry = f"{module_name} claude skill OR agent skill"
                emit_progress("search", f"正在检索：{module_name} · {retry}")
                before = len(collected)
                _search_stage(stage, getter, searcher, collected, seen, moment, second_pass=True)
                emit_new_skills(before)
            stage["gap"] = bool(
                stage.get("required") and not _stage_skills(collected, str(stage["id"]))
            )
        _assign_pending(collected, stages)
    except RuntimeError as exc:
        if "skill_search_budget" not in str(exc):
            raise
    skills = [skill for skill in cap_skills(collected, stages) if not skill.get("gap")]
    filled = _filled_stages(stages, skills)
    coverage = dict(coverage)
    coverage["goal"] = goal
    coverage["chain"] = chain
    coverage["stages"] = filled
    coverage["groups"] = _skill_groups(filled, skills)
    emit_progress("group")
    if aborted():
        payload = snapshot("error")
        payload["phase"] = "error"
        return payload
    emit_progress("compose")
    article = write_article(text, coverage, skills, complete)
    payload = snapshot("skills_ready", article=article)
    payload["degraded"] = bool(budget.degraded)
    payload["package_path"] = str(write_package(root, payload, skills))
    emit("article", payload)
    emit("skills_ready", payload)
    if skills and not budget.degraded:
        _save_last_snapshot(text, payload)
    return payload


def decompose_coverage(query: str, complete: Completer) -> dict[str, object]:
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


def cap_skills(
    skills: list[dict[str, object]],
    stages: list[dict[str, object]],
    limit: int = SKILL_CAP,
) -> list[dict[str, object]]:
    if len(skills) <= limit:
        return list(skills)
    by_stage: dict[str, list[dict[str, object]]] = {}
    for skill in skills:
        by_stage.setdefault(str(skill.get("stage_id") or ""), []).append(skill)
    kept: list[dict[str, object]] = []
    extras: list[dict[str, object]] = []
    for stage in stages:
        group = list(by_stage.get(str(stage["id"]), []))
        if not group:
            continue
        group.sort(key=lambda item: int(item.get("stars") or 0), reverse=True)
        kept.append(group[0])
        extras.extend(group[1:])
    extras.sort(key=lambda item: int(item.get("stars") or 0), reverse=True)
    room = max(limit - len(kept), 0)
    kept.extend(extras[:room])
    order = {str(stage["id"]): index for index, stage in enumerate(stages)}
    kept.sort(
        key=lambda item: (
            order.get(str(item.get("stage_id") or ""), 99),
            -int(item.get("stars") or 0),
        )
    )
    return kept


def _filled_stages(
    stages: list[dict[str, object]],
    skills: list[dict[str, object]],
) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for skill in skills:
        if skill.get("gap"):
            continue
        sid = str(skill.get("stage_id") or "")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
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


def _reuse_previous(
    previous: dict[str, object] | None,
    stages: list[dict[str, object]],
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
) -> int:
    if not isinstance(previous, dict):
        return 0
    stage_by_id = {str(stage["id"]): stage for stage in stages}
    stage_by_name = {str(stage.get("name") or ""): stage for stage in stages}
    added = 0
    for skill in previous.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        source_id = str(skill.get("source_id") or skill.get("full_name") or "").strip()
        if not source_id or "/" not in source_id:
            continue
        key = source_id.lower()
        name_key = source_id.rsplit("/", 1)[-1].lower()
        if key in seen or name_key in seen:
            continue
        stage = stage_by_id.get(str(skill.get("stage_id") or "")) or stage_by_name.get(
            str(skill.get("stage_name") or "")
        )
        if stage is None:
            continue
        hit = dict(skill)
        hit["source_id"] = source_id
        hit["full_name"] = source_id
        hit["name"] = str(hit.get("name") or source_id.rsplit("/", 1)[-1])
        hit["stage_id"] = str(stage["id"])
        hit["stage_name"] = str(stage.get("name") or "")
        if not str(hit.get("skill_path") or hit.get("skill_md") or "").strip():
            hit["skill_path"] = "SKILL.md"
        hit["gap"] = False
        if not passes_quality(hit, now=now):
            continue
        seen.add(key)
        seen.add(name_key)
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


def skills_as_repos(skills: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    repos: list[dict[str, object]] = []
    for index, skill in enumerate(skills, start=1):
        description = str(skill.get("does") or skill.get("description") or "")
        repos.append(
            {
                "rank": index,
                "name": str(skill.get("name") or f"skill-{index}"),
                "full_name": str(skill.get("source_id") or skill.get("name") or f"skill-{index}"),
                "owner": str(skill.get("source_id") or "").split("/", 1)[0],
                "url": str(skill.get("url") or ""),
                "stars": int(skill.get("stars") or 0),
                "stars_label": str(skill.get("stars") or 0),
                "summary": description,
                "description": description,
                "does": description,
                "scene": str(skill.get("stage_name") or skill.get("stage") or ""),
                "kind": str(skill.get("source") or "GitHub"),
                "how": "",
                "topics": [],
                "icon_url": "",
            }
        )
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
) -> None:
    stage_id = str(stage["id"])
    name = str(stage["name"])
    raw_queries = [str(item) for item in (stage.get("queries") or []) if str(item).strip()]
    if second_pass:
        queries = [f"{name} claude skill OR agent skill"]
    else:
        queries = raw_queries or [name]
    for terms in queries:
        if on_query is not None:
            on_query(name, terms)
        for item in _github_search(f"SKILL.md {terms}", getter):
            _ingest_repo(item, stage, getter, collected, seen, now, registry=False)
        try:
            web_hits = searcher(f"{terms} SKILL.md site:github.com")
        except (OSError, TimeoutError, ValueError, RuntimeError):
            web_hits = []
        for full_name in _names_from_web(web_hits):
            repo = _repo_lookup(full_name, getter)
            if repo is not None:
                _ingest_repo(repo, stage, getter, collected, seen, now, registry=False)
        if _stage_skills(collected, stage_id):
            return


def _collect_known_roots(
    getter: Getter,
    collected: list[dict[str, object]],
    seen: set[str],
    now: datetime,
) -> None:
    dummy_stage = {"id": "", "name": ""}
    for url in KNOWN_ROOTS:
        try:
            payload = getter(url)
        except (OSError, TimeoutError, ValueError, RuntimeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            for item in payload["items"]:
                if isinstance(item, dict):
                    _ingest_repo(item, dummy_stage, getter, collected, seen, now, registry=True)
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
                _ingest_repo(repo, dummy_stage, getter, collected, seen, now, registry=True)


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


def _repo_lookup(full_name: str, getter: Getter) -> dict[str, object] | None:
    url = f"https://api.github.com/repos/{full_name}"
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
) -> None:
    full_name = str(item.get("full_name") or "").strip()
    if "/" not in full_name:
        html_url = str(item.get("html_url") or item.get("url") or "")
        match = GITHUB_REPO_RE.search(html_url)
        if match is None:
            return
        full_name = f"{match.group(1)}/{match.group(2)}"
    owner, name = full_name.split("/", 1)
    if owner.lower() in SKIP_OWNERS:
        return
    key = full_name.lower()
    name_key = name.lower()
    if key in seen or name_key in seen:
        return
    try:
        skill_md, skill_path = fetch_skill_md(full_name, getter)
    except _RateLimited:
        skill_md, skill_path = "", "SKILL.md"
    hit = {
        "name": name,
        "description": str(item.get("description") or ""),
        "does": str(item.get("description") or ""),
        "url": str(item.get("html_url") or item.get("url") or f"https://github.com/{full_name}"),
        "stars": int(item.get("stargazers_count") or item.get("stars") or 0),
        "pushed_at": str(item.get("pushed_at") or ""),
        "source_id": full_name,
        "full_name": full_name,
        "source": "registry" if registry else "GitHub",
        "skill_md": skill_md,
        "skill_path": skill_path,
        "registry": registry,
        "stage_id": str(stage.get("id") or ""),
        "stage_name": str(stage.get("name") or ""),
    }
    if not passes_quality(hit, now=now):
        return
    seen.add(key)
    seen.add(name_key)
    collected.append(hit)


def fetch_skill_md(full_name: str, getter: Getter) -> tuple[str, str]:
    rate_limited = False
    for path in SKILL_PATHS:
        raw_url = f"https://raw.githubusercontent.com/{full_name}/HEAD/{path}"
        try:
            payload = _try_get(getter, raw_url)
        except _RateLimited:
            rate_limited = True
            payload = None
        text = _as_markdown(payload)
        if text:
            return text, path
        api_url = f"https://api.github.com/repos/{full_name}/contents/{path}"
        try:
            payload = _try_get(getter, api_url)
        except _RateLimited:
            rate_limited = True
            payload = None
        try:
            text, found = _from_contents(payload, getter)
        except _RateLimited:
            rate_limited = True
            text, found = "", ""
        if text:
            return text, found or path
    if rate_limited:
        raise _RateLimited(full_name)
    return "", ""


def _try_get(getter: Getter, url: str) -> object:
    try:
        return getter(url)
    except _RateLimited:
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


def render_skill_package(
    snapshot: dict[str, object],
    out_dir: Path,
    *,
    theme: str = "dark",
    palette: dict[str, str] | None = None,
    screenshot_fn: Callable[..., Path] | None = None,
    visual_renderer: Callable[..., list[dict[str, object]]] | None = None,
    generate_image: Callable[[str], bytes] | None = None,
    on_image: Callable[[dict[str, object]], None] | None = None,
) -> list[dict[str, object]]:
    from .visual.weekly import render_weekly_visual

    images_dir = Path(out_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    stages = [
        str(stage.get("name") or "")
        for stage in (snapshot.get("stages") or [])
        if isinstance(stage, dict) and stage.get("name") and not stage.get("gap")
    ]
    skills = [
        item
        for item in (snapshot.get("skills") or [])
        if isinstance(item, dict) and not item.get("gap")
    ]
    repos = skills_as_repos(skills)
    jobs: list[dict[str, object]] = [
        {
            "id": "cover",
            "kind": "cover",
            "rank": 0,
            "title": str(snapshot.get("title") or "Skill 查找结果"),
            "visual_title": str(snapshot.get("title") or "Skill 查找结果"),
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
            )
        cards = visual_renderer(repos, jobs, images_dir, theme=theme, palette=palette)
    else:
        cards = render_weekly_visual(
            repos,
            jobs,
            images_dir,
            theme=theme,
            palette=palette,
            generate_image=generate_image,
            screenshot_fn=screenshot_fn,
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


def _names_from_web(results: object) -> list[str]:
    names: list[str] = []
    if not isinstance(results, list):
        return names
    for item in results:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(key) or "") for key in ("url", "href", "title", "snippet"))
        match = GITHUB_REPO_RE.search(blob)
        if match is None:
            continue
        owner, repo = match.group(1), match.group(2)
        if owner.lower() in SKIP_OWNERS:
            continue
        names.append(f"{owner}/{repo.rstrip('.git')}")
    return names


def _public_skill(skill: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(skill.get("name") or ""),
        "description": str(skill.get("description") or skill.get("does") or ""),
        "does": str(skill.get("does") or skill.get("description") or ""),
        "url": str(skill.get("url") or ""),
        "stars": int(skill.get("stars") or 0),
        "pushed_at": str(skill.get("pushed_at") or ""),
        "source_id": str(skill.get("source_id") or skill.get("full_name") or ""),
        "source": str(skill.get("source") or "GitHub"),
        "stage_id": str(skill.get("stage_id") or ""),
        "stage_name": str(skill.get("stage_name") or ""),
        "skill_path": str(skill.get("skill_path") or ""),
    }


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
