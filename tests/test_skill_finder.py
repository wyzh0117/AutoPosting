import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autoposting.db import Database
from autoposting.llm import IMAGE_ASPECT_RATIO, LLMError
from autoposting.models import Source
from autoposting.skill_cover import COVER_HEIGHT, COVER_WIDTH, skill_cover_html
from autoposting.skill_finder import (
    FETCH_DEADLINE_SECONDS,
    MAX_FETCHES,
    _FetchBudget,
    decompose_coverage,
    find_skills,
    passes_quality,
    reset_skill_finder_state,
    skills_as_repos,
)
from autoposting.web import INDEX_HTML, WebApp, create_server


PODCAST_CHAIN = [
    {"id": "c1", "name": "选题", "role": "入口"},
    {"id": "c2", "name": "脚本", "role": "准备"},
    {"id": "c3", "name": "录音", "role": "制作"},
    {"id": "c4", "name": "剪辑", "role": "制作"},
    {"id": "c5", "name": "封面", "role": "装配"},
    {"id": "c6", "name": "发布", "role": "交付"},
]

PODCAST_STAGES = [
    {
        "id": "topic",
        "name": "选题",
        "queries": ["播客选题", "podcast topic"],
        "required": True,
    },
    {
        "id": "script",
        "name": "脚本",
        "queries": ["播客脚本", "podcast script"],
        "required": True,
    },
    {
        "id": "record",
        "name": "录音",
        "queries": ["播客录音", "podcast recording"],
        "required": True,
    },
    {
        "id": "edit",
        "name": "剪辑",
        "queries": ["播客剪辑", "podcast edit"],
        "required": True,
    },
    {
        "id": "cover",
        "name": "封面",
        "queries": ["播客封面", "podcast cover art"],
        "required": True,
    },
    {
        "id": "publish",
        "name": "发布",
        "queries": ["播客发布", "podcast publish"],
        "required": True,
    },
]


def _coverage_json() -> str:
    return json.dumps(
        {
            "goal": "从零做出一档可上线播客",
            "title": "从零做播客技能包",
            "theme": "播客从选题到上线",
            "chain": PODCAST_CHAIN,
            "stages": PODCAST_STAGES,
        },
        ensure_ascii=False,
    )


def _repo_item(
    full_name: str,
    *,
    stars: int,
    description: str,
    pushed_at: str,
    topics: list[str] | None = None,
) -> dict[str, object]:
    owner, name = full_name.split("/", 1)
    return {
        "full_name": full_name,
        "name": name,
        "description": description,
        "stargazers_count": stars,
        "html_url": f"https://github.com/{full_name}",
        "language": "Python",
        "topics": topics or ["claude-code-skill"],
        "owner": {"login": owner, "avatar_url": f"https://github.com/{owner}.png"},
        "pushed_at": pushed_at,
        "created_at": pushed_at,
    }


def _skill_md(name: str, stage: str) -> str:
    return f"# {name}\n\n解决播客流程里的{stage}。\n"


class SkillFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.fresh = "2026-08-20T00:00:00Z"
        self.stale = "2024-01-01T00:00:00Z"

    def _complete(self, prompt: str) -> str:
        if "打包结果" in prompt:
            ids = [
                "acme/podcast-topic",
                "acme/podcast-script",
                "acme/podcast-record",
                "acme/podcast-edit",
                "acme/podcast-cover",
                "acme/podcast-publish",
            ]
            body = "从零做播客。地图：选题到发布。\n" + "\n".join(
                f"{item} 用于对应阶段" for item in ids
            )
            for item in ids:
                self.assertIn(item, prompt)
            return json.dumps(
                {"title": "从零做播客技能包", "body": body + "\nAIGC"},
                ensure_ascii=False,
            )
        return _coverage_json()

    def _github_get(self, url: str, *, gap_first_pass: bool = True) -> object:
        parsed = urlparse(url)
        if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if "stale-empty" in query:
                return {
                    "items": [
                        _repo_item(
                            "old/stale-empty",
                            stars=0,
                            description="empty shell",
                            pushed_at=self.stale,
                        )
                    ]
                }
            if "封面" in query or "cover art" in query:
                if gap_first_pass and "claude skill" not in query and "filename:SKILL.md" not in query:
                    return {"items": []}
                return {
                    "items": [
                        _repo_item(
                            "acme/podcast-cover",
                            stars=42,
                            description="播客封面 skill",
                            pushed_at=self.fresh,
                        )
                    ]
                }
            mapping = (
                ("选题", "acme/podcast-topic", "播客选题"),
                ("topic", "acme/podcast-topic", "播客选题"),
                ("脚本", "acme/podcast-script", "播客脚本"),
                ("script", "acme/podcast-script", "播客脚本"),
                ("录音", "acme/podcast-record", "播客录音"),
                ("recording", "acme/podcast-record", "播客录音"),
                ("剪辑", "acme/podcast-edit", "播客剪辑"),
                ("edit", "acme/podcast-edit", "播客剪辑"),
                ("发布", "acme/podcast-publish", "播客发布"),
                ("publish", "acme/podcast-publish", "播客发布"),
            )
            for needle, full_name, desc in mapping:
                if needle in query:
                    return {
                        "items": [
                            _repo_item(
                                full_name,
                                stars=80,
                                description=desc,
                                pushed_at=self.fresh,
                            )
                        ]
                    }
            return {"items": []}
        if "anthropics/skills" in url or "topic:claude-code-skill" in url or "topic:agent-skills" in url:
            raise RuntimeError("github_fetch_failed:optional-root")
        if "raw.githubusercontent.com" in url or "/contents/" in url:
            if "stale-empty" in url:
                raise RuntimeError("github_fetch_failed:missing-skill")
            for full_name, stage in (
                ("acme/podcast-topic", "选题"),
                ("acme/podcast-script", "脚本"),
                ("acme/podcast-record", "录音"),
                ("acme/podcast-edit", "剪辑"),
                ("acme/podcast-cover", "封面"),
                ("acme/podcast-publish", "发布"),
            ):
                if full_name in url:
                    return _skill_md(full_name.split("/", 1)[1], stage)
            raise RuntimeError("github_fetch_failed:unknown")
        raise RuntimeError(f"github_fetch_failed:{url}")

    def test_coverage_decompose_and_second_pass_fills_gap(self) -> None:
        result = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        self.assertGreaterEqual(len(result["stages"]), 6)
        cover = next(stage for stage in result["stages"] if stage["id"] == "cover")
        self.assertFalse(cover.get("gap"))
        names = {str(skill["source_id"]) for skill in result["skills"]}
        self.assertIn("acme/podcast-cover", names)
        self.assertIn("acme/podcast-topic", names)

    def test_quality_gate_drops_zero_star_stale_without_skill_md(self) -> None:
        hit = {
            "name": "stale-empty",
            "source_id": "old/stale-empty",
            "stars": 0,
            "pushed_at": self.stale,
            "skill_md": "",
            "skill_path": "",
            "url": "https://github.com/old/stale-empty",
            "registry": False,
        }
        self.assertFalse(passes_quality(hit, now=self.now))
        result = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        ids = {str(skill["source_id"]) for skill in result["skills"]}
        self.assertNotIn("old/stale-empty", ids)

    def test_package_article_contains_every_source_id(self) -> None:
        result = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        body = str(result["article"]["body"])
        for skill in result["skills"]:
            self.assertIn(str(skill["source_id"]), body)
            self.assertIn(str(skill["name"]), body)
        package = Path(str(result["package_path"]))
        self.assertTrue((package / "index.json").is_file())
        self.assertTrue((package / "images").is_dir())

    def test_llm_missing_raises_skill_search_failed(self) -> None:
        with self.assertRaises(LLMError) as caught:
            find_skills(
                "从零做播客",
                complete=None,
                github_get=lambda _url: {"items": []},
                web_search=lambda _query: [],
                now=self.now,
                output_root=self.root / "skills",
            )
        self.assertEqual("skill_search_failed", caught.exception.code)
        self.assertFalse(any(self.root.rglob("index.json")))

    def test_network_budget_caps_http_instead_of_crawling_every_repo(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if "api.github.com/search/repositories" in url:
                return {
                    "items": [
                        _repo_item(
                            f"flood/repo-{index}",
                            stars=50,
                            description="generic skill",
                            pushed_at=self.fresh,
                        )
                        for index in range(10)
                    ]
                }
            if "raw.githubusercontent.com" in url or "/contents/" in url:
                return "# flood\n\nskill\n"
            return {"full_name": "flood/repo-0"}

        def complete(prompt: str) -> str:
            if "打包结果" in prompt:
                return json.dumps(
                    {"title": "预算内技能包", "body": "收录来源见列表。\nAIGC"},
                    ensure_ascii=False,
                )
            return _coverage_json()

        result = find_skills(
            "从零做播客",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            max_fetches=18,
            fetch_deadline_seconds=30,
        )
        self.assertLessEqual(len(calls), 18)
        self.assertTrue(result["stages"])
        self.assertTrue(result["skills"])

    def test_on_event_emits_skills_before_images(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            on_event=on_event,
        )
        kinds = [kind for kind, _payload in events]
        self.assertIn("skill", kinds)
        self.assertIn("skills_ready", kinds)
        first_skill = kinds.index("skill")
        ready = kinds.index("skills_ready")
        self.assertLess(first_skill, ready)
        self.assertTrue(all(kind != "image" for kind in kinds[: ready + 1]))
        if "image" in kinds:
            self.assertGreater(kinds.index("image"), ready)
        self.assertTrue(result["skills"])
        self.assertEqual("skills_ready", result.get("phase"))

    def test_gap_stages_absent_from_stages_skills_article_and_repos(self) -> None:
        article_prompt: dict[str, str] = {}

        def complete(prompt: str) -> str:
            if "打包结果" in prompt:
                article_prompt["text"] = prompt
                self.assertNotIn('"gap": true', prompt)
                self.assertNotIn("缺口", prompt)
                body = "从零做播客。地图：选题到发布。\nacme/podcast-topic 选题\nAIGC"
                return json.dumps({"title": "从零做播客技能包", "body": body}, ensure_ascii=False)
            stages = [dict(item) for item in PODCAST_STAGES]
            return json.dumps(
                {"title": "从零做播客技能包", "theme": "播客从选题到上线", "stages": stages},
                ensure_ascii=False,
            )

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                query = parse_qs(parsed.query).get("q", [""])[0]
                if "选题" in query or "topic" in query:
                    return {
                        "items": [
                            _repo_item(
                                "acme/podcast-topic",
                                stars=80,
                                description="播客选题",
                                pushed_at=self.fresh,
                            )
                        ]
                    }
                return {"items": []}
            if "anthropics/skills" in url:
                raise RuntimeError("github_fetch_failed:optional-root")
            if "raw.githubusercontent.com" in url or "/contents/" in url:
                if "acme/podcast-topic" in url:
                    return _skill_md("podcast-topic", "选题")
                raise RuntimeError("github_fetch_failed:missing")
            raise RuntimeError(f"github_fetch_failed:{url}")

        result = find_skills(
            "从零做播客",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        self.assertTrue(result["stages"])
        self.assertTrue(all(not stage.get("gap") for stage in result["stages"]))
        self.assertEqual(["topic"], [str(stage["id"]) for stage in result["stages"]])
        self.assertTrue(all(not skill.get("gap") for skill in result["skills"]))
        self.assertEqual(["acme/podcast-topic"], [str(item["source_id"]) for item in result["skills"]])
        body = str(result["article"]["body"])
        self.assertNotIn("缺口", body)
        self.assertIn("acme/podcast-topic", article_prompt["text"])
        repos = skills_as_repos(result["skills"])
        self.assertEqual(1, len(repos))
        self.assertNotIn("缺口", json.dumps(repos, ensure_ascii=False))

    def test_same_query_reuses_skills_when_github_rate_limits(self) -> None:
        first = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-a",
        )
        self.assertGreaterEqual(len(first["skills"]), 1)
        first_ids = {str(skill["source_id"]) for skill in first["skills"]}

        def blocked(_url: str) -> object:
            raise RuntimeError("github_fetch_failed:403 rate limit")

        second = find_skills(
            "  从零做播客  ",
            complete=self._complete,
            github_get=blocked,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-b",
        )
        second_ids = {str(skill["source_id"]) for skill in second["skills"]}
        self.assertTrue(first_ids & second_ids, msg=f"first={first_ids} second={second_ids}")
        self.assertGreaterEqual(len(second["skills"]), 1)

    def test_pipeline_emits_seven_progress_steps_then_article_and_skills_ready(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            on_event=on_event,
        )
        progress_steps = [
            str((payload.get("progress") or {}).get("step") or "")
            for kind, payload in events
            if kind == "progress"
        ]
        ordered: list[str] = []
        for step in progress_steps:
            if step and step not in ordered:
                ordered.append(step)
        self.assertEqual(
            ["understanding", "chain", "decompose", "keywords", "search", "group", "compose"],
            ordered,
        )
        kinds = [kind for kind, _payload in events]
        self.assertIn("article", kinds)
        self.assertIn("skills_ready", kinds)
        compose_at = next(
            index
            for index, (kind, payload) in enumerate(events)
            if kind == "progress" and str((payload.get("progress") or {}).get("step") or "") == "compose"
        )
        self.assertLess(compose_at, kinds.index("article"))
        self.assertLess(kinds.index("article"), kinds.index("skills_ready"))
        progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
        self.assertEqual(7, progress.get("step_total"))
        self.assertEqual("从零做出一档可上线播客", result.get("goal"))
        self.assertTrue(result.get("chain"))
        self.assertEqual("c1", str((result.get("chain") or [{}])[0].get("id") or ""))
        self.assertTrue(result.get("groups"))
        self.assertEqual("skills_ready", result.get("phase"))
        labels = {
            str((payload.get("progress") or {}).get("step") or ""): str(
                (payload.get("progress") or {}).get("label") or ""
            )
            for kind, payload in events
            if kind == "progress"
        }
        self.assertEqual("理解需求", labels.get("understanding"))
        self.assertEqual("还原生产链", labels.get("chain"))
        self.assertEqual("分解工作流", labels.get("decompose"))
        self.assertEqual("生成关键词", labels.get("keywords"))
        self.assertEqual("查找 Skill", labels.get("search"))
        self.assertEqual("还原分组", labels.get("group"))
        self.assertEqual("组合工作流", labels.get("compose"))

    def test_pipeline_snapshot_progress_elapsed_ms_increases_across_events(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            on_event=on_event,
        )
        elapsed = [
            int((payload.get("progress") or {}).get("elapsed_ms") or 0)
            for kind, payload in events
            if kind == "progress"
        ]
        self.assertGreaterEqual(len(elapsed), 7)
        self.assertEqual(elapsed, sorted(elapsed))
        self.assertGreater(elapsed[-1], elapsed[0])
        started = [
            str((payload.get("progress") or {}).get("started_at") or "")
            for kind, payload in events
            if kind == "progress"
        ]
        self.assertTrue(all(started))
        self.assertEqual(1, len(set(started)))

    def test_pipeline_search_progress_names_module_and_keyword(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        find_skills(
            "从零做播客",
            complete=self._complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            on_event=on_event,
        )
        search_details = [
            str((payload.get("progress") or {}).get("detail") or "")
            for kind, payload in events
            if kind == "progress"
            and str((payload.get("progress") or {}).get("step") or "") == "search"
        ]
        self.assertTrue(search_details)
        named = [detail for detail in search_details if detail.startswith("正在检索：") and " · " in detail]
        self.assertTrue(named, msg=f"search details={search_details}")
        joined = "\n".join(named)
        self.assertIn("选题", joined)
        self.assertTrue(any("播客选题" in detail or "podcast topic" in detail for detail in named))

    def test_pipeline_at_most_two_llm_completions(self) -> None:
        prompts: list[str] = []

        def complete(prompt: str) -> str:
            prompts.append(prompt)
            return self._complete(prompt)

        find_skills(
            "从零做播客",
            complete=complete,
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        self.assertLessEqual(len(prompts), 2)
        self.assertGreaterEqual(len(prompts), 2)

    def test_pipeline_default_fetch_deadline_is_240_injected_still_works(self) -> None:
        self.assertEqual(80, MAX_FETCHES)
        self.assertEqual(240, FETCH_DEADLINE_SECONDS)
        import inspect

        params = inspect.signature(find_skills).parameters
        self.assertEqual(80, params["max_fetches"].default)
        self.assertEqual(240, params["fetch_deadline_seconds"].default)
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            return self._github_get(url, gap_first_pass=False)

        def complete(prompt: str) -> str:
            if "打包结果" in prompt:
                return json.dumps(
                    {"title": "从零做播客技能包", "body": "先选题再发布。\nAIGC"},
                    ensure_ascii=False,
                )
            return _coverage_json()

        result = find_skills(
            "从零做播客",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-deadline",
            max_fetches=12,
            fetch_deadline_seconds=30,
        )
        self.assertLessEqual(len(calls), 12)
        self.assertTrue(result["skills"])

    def test_pipeline_groups_omit_empty_modules_article_has_source_ids_no_gap(self) -> None:
        article_prompt: dict[str, str] = {}

        def complete(prompt: str) -> str:
            if "打包结果" in prompt:
                article_prompt["text"] = prompt
                self.assertNotIn('"gap": true', prompt)
                self.assertNotIn("缺口", prompt)
                body = "先选题再脚本。\nacme/podcast-topic 选题\nAIGC"
                return json.dumps({"title": "从零做播客技能包", "body": body}, ensure_ascii=False)
            return _coverage_json()

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                query = parse_qs(parsed.query).get("q", [""])[0]
                if "选题" in query or "topic" in query:
                    return {
                        "items": [
                            _repo_item(
                                "acme/podcast-topic",
                                stars=80,
                                description="播客选题",
                                pushed_at=self.fresh,
                            )
                        ]
                    }
                return {"items": []}
            if "anthropics/skills" in url:
                raise RuntimeError("github_fetch_failed:optional-root")
            if "raw.githubusercontent.com" in url or "/contents/" in url:
                if "acme/podcast-topic" in url:
                    return _skill_md("podcast-topic", "选题")
                raise RuntimeError("github_fetch_failed:missing")
            raise RuntimeError(f"github_fetch_failed:{url}")

        result = find_skills(
            "从零做播客",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-groups",
        )
        self.assertEqual(["topic"], [str(stage["id"]) for stage in result["stages"]])
        groups = result.get("groups") if isinstance(result.get("groups"), list) else []
        self.assertEqual(["topic"], [str(group.get("stage_id") or "") for group in groups])
        self.assertEqual(["acme/podcast-topic"], list(groups[0].get("skill_ids") or []))
        self.assertTrue(all(not stage.get("gap") for stage in result["stages"]))
        self.assertTrue(all(not skill.get("gap") for skill in result["skills"]))
        body = str(result["article"]["body"])
        self.assertIn("acme/podcast-topic", body)
        self.assertNotIn("缺口", body)
        self.assertNotIn("gap", json.dumps(result["stages"], ensure_ascii=False))

    def test_pipeline_no_sleep_padding_in_skill_finder(self) -> None:
        import inspect

        from autoposting import skill_finder as mod

        source = inspect.getsource(mod)
        self.assertNotIn("time.sleep(", source)

    def test_empty_fetch_results_are_retried_instead_of_cached_as_no_skills(self) -> None:
        calls = 0

        def search(_query: str) -> list[dict[str, str]]:
            nonlocal calls
            calls += 1
            return [] if calls == 1 else [{"title": "a skill"}]

        budget = _FetchBudget(
            lambda _url: {"items": []},
            search,
            max_fetches=4,
            deadline=time.monotonic() + 30,
        )
        self.assertEqual([], budget.search("same term"))
        self.assertEqual([{"title": "a skill"}], budget.search("same term"))
        self.assertEqual(2, calls)

    def test_repeated_query_reuses_one_coverage_plan(self) -> None:
        calls = 0

        def complete(_prompt: str) -> str:
            nonlocal calls
            calls += 1
            return _coverage_json()

        first = decompose_coverage("同一个查找词", complete)
        second = decompose_coverage("同一个查找词", lambda _prompt: "not used")
        self.assertEqual(first, second)
        self.assertEqual(1, calls)
        first["stages"][0]["gap"] = True
        self.assertFalse(second["stages"][0].get("gap"))

    def test_nonempty_rate_limit_get_and_search_are_not_cached(self) -> None:
        get_calls = 0

        def getter(_url: str) -> object:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return {"status": 403, "message": "rate limit"}
            return {"items": [{"full_name": "acme/skill"}]}

        budget = _FetchBudget(
            getter,
            lambda _query: [],
            max_fetches=4,
            deadline=time.monotonic() + 30,
        )
        self.assertEqual(403, budget.get("https://example.test/repo")["status"])
        self.assertEqual(
            [{"full_name": "acme/skill"}],
            budget.get("https://example.test/repo")["items"],
        )
        self.assertEqual(2, get_calls)

        search_calls = 0

        def search(_query: str) -> list[dict[str, str]]:
            nonlocal search_calls
            search_calls += 1
            if search_calls == 1:
                return [{"status_code": 429, "error": "too many requests"}]
            return [{"title": "skill"}]

        budget = _FetchBudget(
            lambda _url: {},
            search,
            max_fetches=4,
            deadline=time.monotonic() + 30,
        )
        self.assertEqual([], budget.search("same term"))
        self.assertEqual([{"title": "skill"}], budget.search("same term"))
        self.assertEqual(2, search_calls)

    def test_nested_cache_values_are_isolated_on_read_and_write(self) -> None:
        payload = {"items": [{"metadata": {"labels": []}}]}
        budget = _FetchBudget(
            lambda _url: payload,
            lambda _query: [{"metadata": {"labels": []}}],
            max_fetches=4,
            deadline=time.monotonic() + 30,
        )
        first = budget.get("https://example.test/nested")
        first["items"][0]["metadata"]["labels"].append("mutated")
        second = budget.get("https://example.test/nested")
        self.assertEqual([], second["items"][0]["metadata"]["labels"])

        first_search = budget.search("nested")
        first_search[0]["metadata"]["labels"].append("mutated")
        second_search = budget.search("nested")
        self.assertEqual([], second_search[0]["metadata"]["labels"])

    def test_find_same_query_is_stable_and_different_query_is_isolated(self) -> None:
        def plan(label: str) -> str:
            stages = [
                {
                    "id": f"stage-{index}",
                    "name": f"模块{index}",
                    "queries": [f"{label} 模块{index}"],
                    "required": True,
                }
                for index in range(1, 7)
            ]
            return json.dumps(
                {
                    "goal": label,
                    "title": label,
                    "theme": label,
                    "chain": [],
                    "stages": stages,
                },
                ensure_ascii=False,
            )

        def complete(prompt: str) -> str:
            if "打包结果" in prompt:
                return json.dumps({"title": "结果", "body": "AIGC"}, ensure_ascii=False)
            label = "alpha" if "alpha" in prompt else "beta"
            return plan(label)

        def getter(url: str) -> object:
            if "api.github.com/repos/anthropics/skills/contents" in url:
                return []
            if "api.github.com/search/repositories" in url:
                if "alpha" not in url:
                    return {"items": []}
                return {
                    "items": [
                        _repo_item(
                            "acme/alpha-skill",
                            stars=50,
                            description="alpha",
                            pushed_at=self.fresh,
                        )
                    ]
                }
            if "raw.githubusercontent.com/acme/alpha-skill" in url:
                return "# alpha\n\nskill\n"
            return {}

        first = find_skills(
            " alpha ",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "stable-a",
        )
        second = find_skills(
            "alpha",
            complete=complete,
            github_get=lambda _url: {"status": 429, "message": "rate limit"},
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "stable-b",
        )
        self.assertEqual(
            {str(skill["source_id"]) for skill in first["skills"]},
            {str(skill["source_id"]) for skill in second["skills"]},
        )
        self.assertFalse(first.get("degraded"))
        self.assertTrue(second.get("degraded"))

        different = find_skills(
            "beta",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "isolated",
        )
        self.assertFalse(different["skills"])


class SkillCoverTests(unittest.TestCase):
    def test_cover_html_is_three_by_four_dazibao(self) -> None:
        html = skill_cover_html(
            title="从零做播客技能包",
            theme="播客从选题到上线",
            count=6,
            stages=["选题", "脚本", "录音", "剪辑", "封面", "发布"],
        )
        self.assertIn(f"{COVER_WIDTH}px", html)
        self.assertIn(f"{COVER_HEIGHT}px", html)
        self.assertEqual(1080, COVER_WIDTH)
        self.assertEqual(1440, COVER_HEIGHT)
        self.assertIn("#06155a", html)
        self.assertIn("clip-path", html)
        self.assertIn("rgba(112,128,185", html)
        self.assertNotIn("SKILL 查找器", html)
        self.assertIn("从零做播客技能包", html)
        self.assertNotIn("9:16", html)

    def test_render_skill_cover_uses_injected_screenshot_size(self) -> None:
        from autoposting.skill_cover import render_skill_cover

        seen: dict[str, object] = {}

        def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
            seen["width"] = width
            seen["height"] = height
            text = html_path.read_text(encoding="utf-8")
            self.assertIn("1080px", text)
            self.assertIn("1440px", text)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            return png_path

        payload = render_skill_cover(
            title="从零做播客技能包",
            theme="播客从选题到上线",
            count=6,
            stages=["选题", "脚本"],
            screenshot_fn=fake_shot,
        )
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(1080, seen["width"])
        self.assertEqual(1440, seen["height"])


class SkillFinderApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        self.sources = (
            Source("lab_blog", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("models",), True, 15),
        )
        for source in self.sources:
            self.db.upsert_source(source)
        self.app = WebApp(
            self.db,
            self.sources,
            self.root / "packages",
            fetcher=lambda _: b"",
            page_fetcher=lambda _: b"",
        )
        self.server = create_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, dict[str, object]]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_search_rejects_empty_query(self) -> None:
        status, payload = self._request("POST", "/api/skills/search", {"query": ""})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])

    def test_search_returns_502_when_llm_missing(self) -> None:
        status, payload = self._request("POST", "/api/skills/search", {"query": "从零做播客"})
        self.assertEqual(502, status)
        self.assertEqual("skill_search_failed", payload["error"])
        self.assertTrue(payload.get("error_code"))

    def test_image_aspect_ratio_stays_nine_sixteen(self) -> None:
        self.assertEqual("9:16", IMAGE_ASPECT_RATIO)

    def test_skills_lane_shows_studio_gallery_and_autorender(self) -> None:
        html = INDEX_HTML
        self.assertIn('id="skills-studio"', html)
        self.assertIn('id="skills-gallery"', html)
        self.assertIn("body.lane-skills #skills-studio", html)
        self.assertIn("body.lane-skills #skills-gallery", html)
        self.assertIn("skills_ready", html)
        self.assertIn("renderSkillImages()", html)
        search_fn = html[html.index("function searchSkills()") : html.index("function renderSkillImages()")]
        self.assertIn("skills_ready", search_fn)
        self.assertIn("renderSkillImages()", search_fn)
        self.assertNotIn('class="is-gap"', html)
        gallery_fn = html[html.index("function renderSkillsGallery()") : html.index("function toggleSkillImage(")]
        self.assertTrue(
            "会自动出图" in gallery_fn or "排队中" in gallery_fn or "queued" in gallery_fn,
            msg="gallery empty state must be visible",
        )

    def test_skills_snapshot_restores_article_from_package(self) -> None:
        package = self.root / "skills" / "pkg"
        package.mkdir(parents=True)
        (package / "article.md").write_text("# 从零做播客技能包\n\n完整介绍文\nAIGC\n", encoding="utf-8")
        self.app.database.save_skill_finder(
            {
                "query": "从零做播客",
                "title": "从零做播客技能包",
                "article": {"title": "", "body": ""},
                "package_path": str(package),
                "skills": [],
                "stages": [],
                "images": [],
                "phase": "done",
            }
        )
        payload = self.app.skills_snapshot()
        self.assertIn("完整介绍文", str((payload.get("article") or {}).get("body") or ""))
        saved = self.app.database.get_skill_finder() or {}
        self.assertIn("完整介绍文", str((saved.get("article") or {}).get("body") or ""))

    def test_skills_lane_keeps_article_summary_above_gallery(self) -> None:
        html = INDEX_HTML
        self.assertIn('id="skills-article"', html)
        self.assertIn('id="skills-body"', html)
        self.assertLess(
            html.index('id="skills-article"'),
            html.index('id="skills-gallery"'),
            msg="介绍文必须在画廊上方，不能被 3:4 图顶出视野",
        )
        self.assertIn("查找完成后会生成介绍文", html)
        poll_fn = html[html.index("function skillArticleReady(") : html.index("function searchSkills()")]
        compact = poll_fn.replace(" ", "").replace("\n", "")
        self.assertIn("article.body", compact)
        self.assertIn("skillArticleReady", poll_fn)
        self.assertNotIn(
            'if (phase === "skills_ready" || phase === "rendering" || phase === "done") { return data; }',
            " ".join(poll_fn.split()),
        )

    def test_skills_render_keeps_article_written_during_render(self) -> None:
        package = self.root / "skills" / "pkg"
        (package / "images").mkdir(parents=True)
        self.app.database.save_skill_finder(
            {
                "query": "从零做播客",
                "title": "从零做播客技能包",
                "theme": "播客从选题到上线",
                "stages": [{"id": "topic", "name": "选题", "queries": ["播客选题"], "required": True}],
                "skills": [
                    {
                        "name": "podcast-topic",
                        "source_id": "acme/podcast-topic",
                        "stage_id": "topic",
                        "stage_name": "选题",
                        "stars": 12,
                        "url": "https://github.com/acme/podcast-topic",
                        "skill_path": "SKILL.md",
                        "description": "选题",
                    }
                ],
                "article": {"title": "", "body": ""},
                "images": [],
                "package_path": str(package),
                "phase": "skills_ready",
            }
        )
        article = {"title": "从零做播客技能包", "body": "地图：选题到发布。\nacme/podcast-topic\nAIGC"}

        def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
            current = dict(self.app.database.get_skill_finder() or {})
            current["article"] = article
            self.app.database.save_skill_finder(current)
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            return png_path

        def fake_render(repos, jobs, out_dir, theme="dark", palette=None):
            out_dir.mkdir(parents=True, exist_ok=True)
            rendered = []
            for job in jobs:
                filename = f"{job['id']}.png"
                (out_dir / filename).write_bytes(b"png")
                row = dict(job)
                row["file"] = filename
                rendered.append(row)
            return rendered

        self.app.skill_screenshot = fake_shot
        self.app.visual_renderer = fake_render
        payload = self.app.skills_render({})
        saved = self.app.database.get_skill_finder() or {}
        self.assertEqual(article["body"], (payload.get("article") or {}).get("body"))
        self.assertEqual(article["body"], (saved.get("article") or {}).get("body"))
        self.assertEqual("done", payload.get("phase"))

    def test_skill_search_btn_css_keeps_lookup_on_one_line(self) -> None:
        html = INDEX_HTML
        self.assertIn('id="skill-search-btn"', html)
        self.assertIn(">查找</button>", html)
        css = html[html.index("body.lane-skills #skill-search-row") : html.index("body.lane-skills #skill-stage-chips")]
        self.assertIn("#skill-search-btn", css)
        self.assertIn("white-space:nowrap", css)
        self.assertIn("min-width", css)
        self.assertIn("height:40px", css)
        self.assertIn("width:auto", css)
        self.assertIn("min-width:0", css)
        self.assertIn("flex:1", css)
        self.assertIn("display:inline-flex", css)
        self.assertIn("align-items:center", css)
        self.assertIn("justify-content:center", css)

    def test_paint_wait_drops_sliding_meter_keeps_abort(self) -> None:
        html = INDEX_HTML
        paint = html[html.index("function paintWait()") : html.index("function formatTime(")]
        self.assertNotIn("wait-meter", paint)
        self.assertNotIn("wait-bar", paint)
        self.assertNotIn("waitSlide", html)
        self.assertIn("wait-stop", paint)
        self.assertIn("waitStop(", paint)
        self.assertIn("停止", paint)
        self.assertIn("wait-spinner", paint)
        wait_stop = html[html.index("function waitStop(") : html.index("function scheduleWaitHide(")]
        self.assertIn(".abort.abort()", wait_stop)
        self.assertIn('waitFail("已停止"', wait_stop)
        search = html[html.index("function searchSkills()") : html.index("function renderSkillImages()")]
        self.assertIn("abortSkillSearch", search)
        poll = html[html.index("function pollSkillSnapshot()") : html.index("function searchSkills()")]
        self.assertIn("abortSkillSearch", poll)
        banner_css = html[html.index("#wait-banner{") : html.index("#wait-jobs{") + 160]
        self.assertIn("pointer-events:none", banner_css)
        stop_css = html[html.index(".wait-stop{") : html.index(".wait-stop:hover")]
        self.assertIn("pointer-events:auto", stop_css)
        wait_css = html[html.index("#wait-banner{") : html.index("main{")]
        self.assertNotIn(".wait-meter", wait_css)
        self.assertNotIn(".wait-bar", wait_css)

    def test_poll_skill_snapshot_continues_seven_search_phases(self) -> None:
        html = INDEX_HTML
        poll = html[html.index("function pollSkillSnapshot()") : html.index("function searchSkills()")]
        for phase in ('"understanding"', '"chain"', '"decompose"', '"keywords"', '"search"', '"group"', '"compose"'):
            self.assertIn(phase, poll)
        self.assertIn("skillArticleReady", poll)
        self.assertIn("renderSkillImages()", poll)
        self.assertIn('phase === "skills_ready"', poll)
        self.assertIn('phase === "error"', poll)
        self.assertIn("pollSkillSnapshot()", poll)
        self.assertIn("setTimeout", poll)

    def test_skill_search_wait_detail_uses_progress_step_index(self) -> None:
        html = INDEX_HTML
        paint = html[html.index("function paintWait()") : html.index("function formatTime(")]
        self.assertIn("skill_search", paint)
        self.assertIn("skillsPackage.progress", paint)
        self.assertIn("step_index", paint)
        self.assertIn("step_total", paint)
        self.assertIn(".label", paint)
        self.assertIn("第 ", paint)
        self.assertIn(" 步 · ", paint)
        self.assertIn("查找中", paint)


if __name__ == "__main__":
    unittest.main()
