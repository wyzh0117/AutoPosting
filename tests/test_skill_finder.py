import copy
import json
import subprocess
import sys
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
from copy_fixtures import stub_copy_writer
from autoposting.llm import LLMError
from autoposting.models import Source
from autoposting.skill_finder import (
    FETCH_DEADLINE_SECONDS,
    GAP_REASON,
    KEYWORD_LIMIT,
    MANUAL_STAGE_NAME,
    MAX_FETCHES,
    MIN_STARS,
    PIPELINE_BY_STEP,
    PIPELINE_STEPS,
    SKILLS_SH_MIN_INSTALLS,
    SKILLS_SH_PER_QUERY,
    SKILLS_SH_SEARCH_URL,
    SKILLS_SH_SOURCE,
    SKILL_PATHS,
    SEARCH_TEMPLATES,
    WORKFLOW_STAGES,
    _FetchBudget,
    _fair_share,
    _ingest_repo,
    _names_from_web,
    _skills_sh_search,
    add_manual_skills,
    cap_skills,
    _dedupe_by_skill_name,
    decompose_coverage,
    extract_keywords,
    fetch_skill_md,
    find_skills,
    _keyword_stage_id,
    keyword_coverage,
    passes_quality,
    quality_reason,
    render_skill_package,
    reset_skill_finder_state,
    skills_as_repos,
    workflow_stages,
)
from autoposting.skill_links import parse_skill_links
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


class StudioStatusRoutingTests(unittest.TestCase):
    """A creation-page studio must report on the creation page.

    Both poster studios live in `#page-creation`, so a message written to the
    news-only `#hotspot-status` line is invisible exactly when the user needs it
    (it renders on another page).  Every studio-side message goes to the shared
    `#studio-status` live region instead.
    """

    STUDIO_FUNCTIONS = (
        "loadGithubWeekly",
        "renderGithubWeeklyImages",
        "copyGithubXhs",
        "exportGithubImages",
        "saveSkillTitle",
        "loadSkills",
        "searchSkills",
        "renderSkillCoverOnly",
        "renderSkillImages",
        "exportSkillImages",
    )
    NEWS_FUNCTIONS = (
        "runResearch",
        "applyHotspots",
        "translateTitles",
        "collect",
        "setNewsScope",
    )
    NEWS_LINE = 'status("hotspot-status"'
    STUDIO_LINE = 'status("studio-status"'

    def _function_body(self, name: str) -> str:
        marker = "function " + name + "("
        self.assertIn(marker, INDEX_HTML, name + " is missing")
        return INDEX_HTML.split(marker, 1)[1].split("\nfunction ", 1)[0]

    def test_every_studio_function_reports_on_the_studio_line(self) -> None:
        for name in self.STUDIO_FUNCTIONS:
            body = self._function_body(name)
            self.assertNotIn(self.NEWS_LINE, body, name + " writes the news status line")
            self.assertIn(self.STUDIO_LINE, body, name + " never reports to the user")

    def test_news_functions_keep_the_news_line(self) -> None:
        for name in self.NEWS_FUNCTIONS:
            self.assertIn(self.NEWS_LINE, self._function_body(name), name)

    def test_the_two_status_lines_are_on_different_pages(self) -> None:
        news = INDEX_HTML[INDEX_HTML.index('id="page-news"') : INDEX_HTML.index('id="page-creation"')]
        creation = INDEX_HTML[INDEX_HTML.index('id="page-creation"') : INDEX_HTML.index('id="page-publishing"')]
        self.assertIn('id="hotspot-status"', news)
        self.assertNotIn('id="hotspot-status"', creation)
        self.assertIn('id="studio-status"', creation)
        self.assertNotIn('id="studio-status"', news)


class SkillFinderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.fresh = "2026-08-20T00:00:00Z"
        self.stale = "2024-01-01T00:00:00Z"
        # Fake skills.sh rows, matched by needle in the search terms. Empty by
        # default so the offline GitHub tests keep their exact expectations.
        self.skills_sh_rows: list[tuple[str, dict[str, object]]] = []

    def _coverage(self) -> dict[str, object]:
        """显式传入的 PODCAST 覆盖计划。

        1.4.0 起 find_skills 不再让模型「解构」查询：默认计划就是用户自己的
        关键词（``keyword_coverage``）。这些用例依赖 PODCAST 的阶段名/检索词，
        所以走 ``coverage=`` 这个出口显式传计划；模型只负责写介绍文。
        """
        return {
            "goal": "从零做出一档可上线播客",
            "title": "从零做播客技能包",
            "theme": "播客从选题到上线",
            "chain": copy.deepcopy(PODCAST_CHAIN),
            "stages": copy.deepcopy(PODCAST_STAGES),
        }

    def _complete(self, prompt: str) -> str:
        # 1.4.0：complete 只用来写介绍文，不再返回覆盖计划。
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
            # write_article 必须把每个收录 skill 的 source_id 交给模型。
            self.assertIn(item, prompt)
        return json.dumps(
            {"title": "从零做播客技能包", "body": body + "\nAIGC"},
            ensure_ascii=False,
        )

    def _skills_sh_response(self, terms: str) -> dict[str, object]:
        rows = [row for needle, row in self.skills_sh_rows if needle in terms]
        return {"query": terms, "searchType": "fuzzy", "searchVersion": "legacy", "skills": rows}

    def _github_get(self, url: str, *, gap_first_pass: bool = True) -> object:
        parsed = urlparse(url)
        if parsed.netloc == SKILLS_SH_SOURCE and parsed.path == "/api/search":
            terms = parse_qs(parsed.query).get("q", [""])[0]
            return self._skills_sh_response(terms)
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

    def test_explicit_coverage_second_pass_fills_gap(self) -> None:
        # 名字里的 decompose 已经不存在了（1.4.0 不再让模型解构查询）：这条用例
        # 改为验证「显式 coverage」这条缝——必填阶段第一轮没找到时，第二次重试
        # 仍能把阶段填满。
        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
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
        # 1.4.0：默认计划就是关键词，所以直接用能命中这个仓库的关键词跑一遍，
        # 证明门槛是在真实检索里生效的（而不是只对一个人造 dict 生效）。
        result = find_skills(
            "stale-empty",
            complete=None,
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        ids = {str(skill["source_id"]) for skill in result["skills"]}
        self.assertNotIn("old/stale-empty", ids)
        self.assertEqual("skills_ready", result.get("phase"))
        # 丢掉的原因也必须在快照里说清楚，而不是静默消失。
        dropped = [row for row in result["rejected"] if row["source_id"] == "old/stale-empty"]
        self.assertTrue(dropped, msg=f"漏掉了拒绝记录：{result['rejected']}")
        self.assertEqual("no_skill_md", dropped[0]["reason"])
        # 1.4.0：一次已完成的检索必须有非空介绍文（零结果也一样），否则前端会
        # 以为「介绍文还没写好」而对着终态一直轮询。
        self.assertFalse(result["skills"])
        self.assertEqual("stale-empty", result["article"]["title"])
        self.assertIn("还没有收录 skill", str(result["article"]["body"]))

    def test_package_article_contains_every_source_id(self) -> None:
        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
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

    def test_search_without_a_model_still_delivers_a_package(self) -> None:
        # 旧用例（test_llm_missing_raises_skill_search_failed）的前提已经反转：
        # 1.4.0 里模型是可选的，只用来写介绍文，没有模型时介绍文回落到
        # _fallback_article，检索本身必须照常成功。
        result = find_skills(
            "视频 剪辑",
            complete=None,
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        self.assertEqual("skills_ready", result.get("phase"))
        self.assertTrue(result["skills"])
        body = str(result["article"]["body"])
        self.assertTrue(body.strip(), msg="没有模型也要有机械介绍文")
        for skill in result["skills"]:
            self.assertIn(str(skill["name"]), body)
        self.assertIn("AIGC", body)
        # 包目录照写：没有模型不等于没有交付物。
        package = Path(str(result["package_path"]))
        self.assertTrue((package / "index.json").is_file())

    def test_network_budget_caps_http_instead_of_crawling_every_repo(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if "skills.sh" in url:
                return {"skills": []}
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
            # 1.4.0：complete 只被叫一次（写介绍文），不再生产覆盖计划。
            self.assertIn("打包结果", prompt)
            return json.dumps(
                {"title": "预算内技能包", "body": "收录来源见列表。\nAIGC"},
                ensure_ascii=False,
            )

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
            coverage=self._coverage(),
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

    def test_gap_stages_stay_visible_with_marker_but_absent_from_article(self) -> None:
        article_prompt: dict[str, str] = {}

        def complete(prompt: str) -> str:
            article_prompt["text"] = prompt
            self.assertNotIn('"gap": true', prompt)
            self.assertNotIn("缺口", prompt)
            body = "从零做播客。地图：选题到发布。\nacme/podcast-topic 选题\nAIGC"
            return json.dumps({"title": "从零做播客技能包", "body": body}, ensure_ascii=False)

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE:
                return self._skills_sh_response("")
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
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        # B4: every coverage stage stays visible; empty ones carry an explicit gap.
        podcast_ids = [str(stage["id"]) for stage in PODCAST_STAGES]
        self.assertEqual(podcast_ids, [str(stage["id"]) for stage in result["stages"][:6]])
        # 1.4.0：检索循环之后接上 workflow 兜底行（固定顺序，去掉已经存在的 id），
        # 这样关键词检索剩下的 skill 也不会掉进没有名字的桶里。
        expected_workflow = [
            (stage_id, name)
            for stage_id, name, _tokens in WORKFLOW_STAGES
            if stage_id not in podcast_ids
        ]
        self.assertEqual(
            [stage_id for stage_id, _name in expected_workflow],
            [str(stage["id"]) for stage in result["stages"][6:]],
        )
        self.assertEqual(
            [name for _stage_id, name in expected_workflow],
            [str(stage["name"]) for stage in result["stages"][6:]],
        )
        gaps = [stage for stage in result["stages"] if stage.get("gap")]
        filled = [stage for stage in result["stages"] if not stage.get("gap")]
        self.assertEqual(["topic"], [str(stage["id"]) for stage in filled])
        self.assertEqual(
            [sid for sid in podcast_ids if sid != "topic"]
            + [stage_id for stage_id, _name in expected_workflow],
            [str(stage["id"]) for stage in gaps],
        )
        self.assertTrue(all(stage.get("gap_reason") == GAP_REASON for stage in gaps))
        # 兜底行只用于归档：检索循环从来没查过它们（没有被记过 fetches）。
        for stage in result["stages"][6:]:
            self.assertNotIn("fetches", stage, msg=f"{stage['name']} 不该被检索")
        self.assertEqual(
            ["topic"], [str(group.get("stage_id") or "") for group in result["groups"]]
        )
        self.assertTrue(all(not skill.get("gap") for skill in result["skills"]))
        self.assertEqual(["acme/podcast-topic"], [str(item["source_id"]) for item in result["skills"]])
        body = str(result["article"]["body"])
        self.assertNotIn("缺口", body)
        self.assertIn("acme/podcast-topic", article_prompt["text"])
        repos = skills_as_repos(result["skills"])
        self.assertEqual(1, len(repos))
        self.assertNotIn("缺口", json.dumps(repos, ensure_ascii=False))

    def test_rate_limited_rerun_reuses_previous_skills_across_moved_stages(self) -> None:
        # 1.4.0：包是累积的。第二次检索换了一套阶段（关键词计划），旧 skill 必须
        # 被重新归档到新工作流里而不是被丢掉；限流时更是唯一的数据来源。
        first = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-a",
        )
        self.assertGreaterEqual(len(first["skills"]), 1)
        first_ids = {str(skill["source_id"]) for skill in first["skills"]}
        first_stages = {str(skill["source_id"]): str(skill["stage_id"]) for skill in first["skills"]}

        def blocked(_url: str) -> object:
            raise RuntimeError("github_fetch_failed:403 rate limit")

        second = find_skills(
            "视频 剪辑",
            complete=None,
            previous=first,
            github_get=blocked,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-b",
        )
        second_ids = {str(skill["source_id"]) for skill in second["skills"]}
        self.assertTrue(first_ids & second_ids, msg=f"first={first_ids} second={second_ids}")
        self.assertEqual(first_ids, second_ids, msg="限流重跑不该丢掉上一轮的 skill")
        # 阶段换了：旧行必须被重新归档进新计划里，而不是留在已经不存在的阶段。
        new_stage_ids = {str(stage["id"]) for stage in second["stages"]}
        moved = 0
        for skill in second["skills"]:
            source_id = str(skill["source_id"])
            self.assertIn(str(skill["stage_id"]), new_stage_ids)
            if str(skill["stage_id"]) != first_stages[source_id]:
                moved += 1
        self.assertGreaterEqual(moved, 1, msg="换了计划后至少要有一行被重新归档")

    def test_pipeline_emits_four_progress_steps_then_article_and_skills_ready(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
            on_event=on_event,
        )
        # 1.4.0：流水线只剩 4 步，understanding/chain/decompose 已经不存在。
        self.assertEqual(
            ("keywords", "search", "group", "compose"),
            tuple(step for step, _label, _detail in PIPELINE_STEPS),
        )
        self.assertEqual(
            {
                "keywords": (1, "整理关键词", "正在把输入切成检索关键词"),
                "search": (2, "抓取来源", "正在按关键词去各来源抓取 skill"),
                "group": (3, "按工作流归组", "正在把抓到的 skill 归到工作流各环节"),
                "compose": (4, "汇总打包", "正在汇总成端到端工作流"),
            },
            PIPELINE_BY_STEP,
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
        self.assertEqual(["keywords", "search", "group", "compose"], ordered)
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
        self.assertEqual(4, progress.get("step_total"))
        # 终态快照停在最后一步（skills_ready 归到 compose）。
        self.assertEqual("compose", progress.get("step"))
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
        self.assertEqual("整理关键词", labels.get("keywords"))
        self.assertEqual("抓取来源", labels.get("search"))
        self.assertEqual("按工作流归组", labels.get("group"))
        self.assertEqual("汇总打包", labels.get("compose"))

    def test_pipeline_snapshot_progress_elapsed_ms_increases_across_events(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
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
        # 1.4.0 只有 4 步，事件至少覆盖每一步一次（检索阶段会重复产生事件）。
        self.assertGreaterEqual(len(elapsed), len(PIPELINE_STEPS))
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
        # 关键词模式下「模块名」就是用户输入的关键词本身，detail 是
        # 「正在检索：<关键词> · <关键词>」。
        events: list[tuple[str, dict[str, object]]] = []

        def on_event(kind: str, payload: dict[str, object] | None = None) -> None:
            events.append((kind, payload or {}))

        find_skills(
            "视频 剪辑",
            complete=None,
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-keyword-progress",
            on_event=on_event,
        )
        search_details = [
            str((payload.get("progress") or {}).get("detail") or "")
            for kind, payload in events
            if kind == "progress"
            and str((payload.get("progress") or {}).get("step") or "") == "search"
        ]
        self.assertTrue(search_details)
        self.assertIn("正在检索：视频 · 视频", search_details)
        self.assertIn("正在检索：剪辑 · 剪辑", search_details)
        named = [detail for detail in search_details if detail.startswith("正在检索：") and " · " in detail]
        self.assertEqual(len(search_details), len(named), msg=f"search details={search_details}")

        # 必填阶段一个都没找到时要走兜底检索词，detail 必须说清用的是哪一句。
        retry_events: list[tuple[str, dict[str, object]]] = []

        def on_retry(kind: str, payload: dict[str, object] | None = None) -> None:
            retry_events.append((kind, payload or {}))

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE:
                return self._skills_sh_response("")
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                query = parse_qs(parsed.query).get("q", [""])[0]
                if "选题" in query:
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

        find_skills(
            "从零做播客",
            complete=None,
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-retry-progress",
            on_event=on_retry,
        )
        retry_details = [
            str((payload.get("progress") or {}).get("detail") or "")
            for kind, payload in retry_events
            if kind == "progress"
            and str((payload.get("progress") or {}).get("step") or "") == "search"
        ]
        self.assertIn("正在检索：脚本 · 播客脚本", retry_details)
        self.assertIn(
            "正在检索：脚本 · 脚本 claude skill OR agent skill",
            retry_details,
            msg=f"第二次兜底检索必须报出来：{retry_details}",
        )

    def test_pipeline_makes_at_most_one_llm_completion(self) -> None:
        # 1.4.0：检索计划不再由模型产出，整条流水线最多只剩 1 次补全（介绍文）。
        from autoposting import skill_finder as mod

        prompts: list[str] = []

        def complete(prompt: str) -> str:
            prompts.append(prompt)
            return self._complete(prompt)

        find_skills(
            "从零做播客",
            complete=complete,
            coverage=self._coverage(),
            github_get=lambda url: self._github_get(url, gap_first_pass=True),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills",
        )
        self.assertEqual(1, len(prompts), msg=f"只该写一次介绍文，实际 {len(prompts)} 次")
        self.assertIn("打包结果", prompts[0])

        # complete=None：没有模型可用，等于 0 次补全，介绍文逐字来自
        # _fallback_article。用间谍包住 write_article 才能把「0 次」测出来。
        original = mod.write_article
        called: list[object] = []

        def spy(*args: object, **kwargs: object) -> object:
            called.append(args)
            return original(*args, **kwargs)

        mod.write_article = spy
        self.addCleanup(setattr, mod, "write_article", original)
        result = find_skills(
            "视频 剪辑",
            complete=None,
            github_get=lambda url: self._github_get(url, gap_first_pass=False),
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-no-model",
        )
        self.assertEqual([], called, msg="没有模型时不该有任何补全")
        self.assertTrue(result["skills"])
        # 关键词模式下 goal == title == 用户输入的查询，find_skills 会把这两个值
        # 传给 _fallback_article，所以这里必须用同一组参数逐字比对。
        expected = mod._fallback_article(
            [skill for skill in result["skills"] if not skill.get("gap")],
            title="视频 剪辑",
            goal="视频 剪辑",
        )
        self.assertEqual(expected, result["article"], msg="介绍文必须逐字来自机械兜底")

    def test_pipeline_default_fetch_budget_covers_both_sources(self) -> None:
        # Two search sources per query now share the budget, so it doubled.
        self.assertEqual(160, MAX_FETCHES)
        self.assertEqual(240, FETCH_DEADLINE_SECONDS)
        import inspect

        params = inspect.signature(find_skills).parameters
        self.assertEqual(160, params["max_fetches"].default)
        self.assertEqual(240, params["fetch_deadline_seconds"].default)
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            return self._github_get(url, gap_first_pass=False)

        def complete(prompt: str) -> str:
            # 模型只写介绍文（1.4.0）。
            self.assertIn("打包结果", prompt)
            return json.dumps(
                {"title": "从零做播客技能包", "body": "先选题再发布。\nAIGC"},
                ensure_ascii=False,
            )

        result = find_skills(
            "从零做播客",
            complete=complete,
            coverage=self._coverage(),
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
            article_prompt["text"] = prompt
            self.assertNotIn('"gap": true', prompt)
            self.assertNotIn("缺口", prompt)
            body = "先选题再脚本。\nacme/podcast-topic 选题\nAIGC"
            return json.dumps({"title": "从零做播客技能包", "body": body}, ensure_ascii=False)

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE:
                return self._skills_sh_response("")
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
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-groups",
        )
        # 显式计划原样排在最前面；1.4.0 会在检索循环之后补上 workflow 兜底行。
        self.assertEqual(
            [str(stage["id"]) for stage in PODCAST_STAGES],
            [str(stage["id"]) for stage in result["stages"][:6]],
        )
        groups = result.get("groups") if isinstance(result.get("groups"), list) else []
        self.assertEqual(["topic"], [str(group.get("stage_id") or "") for group in groups])
        self.assertEqual(["acme/podcast-topic"], list(groups[0].get("skill_ids") or []))
        self.assertEqual(
            ["topic"], [str(stage["id"]) for stage in result["stages"] if not stage.get("gap")]
        )
        self.assertTrue(all(not skill.get("gap") for skill in result["skills"]))
        body = str(result["article"]["body"])
        self.assertIn("acme/podcast-topic", body)
        self.assertNotIn("缺口", body)

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

    def test_github_rate_limit_parks_the_api_and_names_the_reason(self) -> None:
        """A 403 from api.github.com must park that quota, not read as RuntimeError."""
        calls: list[str] = []

        class _Limited(RuntimeError):
            kind = "rate_limited"
            reset_at = time.time() + 3600

        def getter(url: str) -> object:
            calls.append(url)
            raise _Limited(f"github_fetch_failed:rate limit exceeded (403):{url}")

        budget = _FetchBudget(
            getter, lambda _query: [], max_fetches=10, deadline=time.monotonic() + 30
        )
        with self.assertRaises(_Limited):
            budget.get("https://api.github.com/repos/acme/skill/contents")
        self.assertEqual(["github:rate_limited"], budget.reasons)
        # The core quota is parked...
        self.assertFalse(budget.github_api_ok("https://api.github.com/repos/acme/other/contents"))
        # ...raw CDN probes never are, so SKILL.md can still be located.
        self.assertTrue(budget.github_api_ok("https://raw.githubusercontent.com/acme/other/HEAD/SKILL.md"))
        # The search quota is a separate bucket: only its own 403 parks it.
        self.assertTrue(budget.github_api_ok("https://api.github.com/search/repositories?q=x"))
        self.assertEqual(1, len(calls))

    def test_quota_snapshot_parks_a_resource_before_the_403_arrives(self) -> None:
        from autoposting import skill_finder as mod

        budget = _FetchBudget(
            lambda _url: {}, lambda _query: [], max_fetches=10, deadline=time.monotonic() + 30
        )
        state = {"limit": 60, "remaining": 1, "reset": time.time() + 600, "resource": "core"}
        original = mod._github_quota_state
        mod._github_quota_state = lambda: dict(state)
        self.addCleanup(setattr, mod, "_github_quota_state", original)
        self.assertFalse(budget.github_api_ok("https://api.github.com/repos/acme/x/contents"))
        self.assertIn("github:rate_limited", budget.reasons)
        # A snapshot for the other bucket must not park this one.
        state["resource"] = "search"
        budget2 = _FetchBudget(
            lambda _url: {}, lambda _query: [], max_fetches=10, deadline=time.monotonic() + 30
        )
        self.assertTrue(budget2.github_api_ok("https://api.github.com/repos/acme/x/contents"))

    def test_fetch_skill_md_falls_back_to_raw_layouts_when_the_api_is_parked(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if url.endswith("/video-edit/SKILL.md"):
                return "# video-edit\n\n剪掉废话。\n"
            raise RuntimeError("github_fetch_failed:404")

        text, path = fetch_skill_md(
            "acme/skills", getter, "video-edit", api_ok=lambda _url: False
        )
        self.assertIn("剪掉废话", text)
        self.assertEqual("video-edit/SKILL.md", path)
        self.assertTrue(calls)
        self.assertFalse([url for url in calls if urlparse(url).netloc == "api.github.com"])

    def test_probe_404_is_not_reported_as_a_degraded_source(self) -> None:
        """Walking SKILL_PATHS 404s on purpose; that is not a source failure."""
        from autoposting import github_weekly as gh

        error = gh.GitHubFetchError(
            "not_found", "https://raw.githubusercontent.com/a/b/HEAD/SKILL.md", 404
        )

        def getter(_url: str) -> object:
            raise error

        budget = _FetchBudget(
            getter, lambda _query: [], max_fetches=5, deadline=time.monotonic() + 30
        )
        with self.assertRaises(gh.GitHubFetchError):
            budget.get("https://raw.githubusercontent.com/a/b/HEAD/SKILL.md")
        self.assertFalse(budget.degraded)
        self.assertEqual([], budget.reasons)
        # A real source failure still reports, and with the friendly token.
        bad = gh.GitHubFetchError(
            "unreachable", "https://raw.githubusercontent.com/a/b/HEAD/SKILL.md"
        )
        budget = _FetchBudget(
            lambda _url: (_ for _ in ()).throw(bad),
            lambda _query: [],
            max_fetches=5,
            deadline=time.monotonic() + 30,
        )
        with self.assertRaises(gh.GitHubFetchError):
            budget.get("https://raw.githubusercontent.com/a/b/HEAD/SKILL.md")
        self.assertEqual(["github:unreachable"], budget.reasons)

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
            if "skills.sh" in url:
                return self._skills_sh_response("")
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

    # ---- skills.sh source adapter (B1) -------------------------------------

    def test_skills_sh_search_decodes_json_and_uses_urlencoded_free_endpoint(self) -> None:
        payload = json.dumps(
            {
                "query": "视频制作",
                "searchType": "fuzzy",
                "searchVersion": "legacy",
                "skills": [
                    {
                        "id": "genmedia-labs/skills/video-edit",
                        "skillId": "video-edit",
                        "name": "video-edit",
                        "installs": 513911,
                        "source": "genmedia-labs/skills",
                    },
                    {
                        "id": "skills.volces.com/byted-drama",
                        "skillId": "byted-drama",
                        "name": "byted-drama",
                        "installs": 694,
                        "source": "skills.volces.com",
                    },
                ],
            },
            ensure_ascii=False,
        )
        seen: dict[str, str] = {}

        def getter(url: str) -> object:
            seen["url"] = url
            return payload

        hits = _skills_sh_search("视频制作", getter)
        self.assertTrue(seen["url"].startswith(SKILLS_SH_SEARCH_URL + "?"))
        self.assertIn("q=%E8%A7%86%E9%A2%91%E5%88%B6%E4%BD%9C", seen["url"])
        self.assertNotIn("site:github.com", seen["url"])
        self.assertEqual(2, len(hits))
        first = hits[0]
        self.assertEqual(SKILLS_SH_SOURCE, first["source"])
        self.assertEqual("genmedia-labs/skills/video-edit", first["source_id"])
        self.assertEqual("video-edit", first["name"])
        self.assertEqual(513911, first["installs"])
        self.assertEqual("genmedia-labs/skills", first["full_name"])
        # A `source` that is not a GitHub repo stays unresolved instead of crashing.
        self.assertEqual("", hits[1]["full_name"])
        self.assertEqual("skills.volces.com/byted-drama", hits[1]["source_id"])

    def test_skills_sh_search_never_raises_on_bad_payloads(self) -> None:
        bad_payloads = [
            ("not json at all", []),
            ("{broken", []),
            ({"skills": 3}, []),
            ({"skills": None}, []),
            (None, []),
            ([], []),
        ]
        for payload, expected in bad_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(expected, _skills_sh_search("video", lambda _url, p=payload: p))
        weird = _skills_sh_search(
            "video",
            lambda _url: {"skills": [{"id": "a/b", "installs": None, "source": "a/b"}]},
        )
        self.assertEqual(1, len(weird))
        self.assertEqual(0, weird[0]["installs"])
        self.assertTrue(weird[0]["installs_missing"])

    def test_skills_sh_install_floor_qualifies_where_stars_would_fail(self) -> None:
        hit = {
            "name": "video-edit",
            "source_id": "genmedia-labs/skills/video-edit",
            "source": SKILLS_SH_SOURCE,
            "installs": SKILLS_SH_MIN_INSTALLS,
            "stars": 0,
            "skill_md": "# video-edit\n\n剪辑\n",
            "skill_path": "SKILL.md",
            "pushed_at": self.stale,
            "registry": False,
        }
        ok, reason = quality_reason(hit, now=self.now)
        self.assertTrue(ok, reason)
        self.assertIn("installs", reason)
        self.assertTrue(passes_quality(hit, now=self.now))

        low = dict(hit, installs=SKILLS_SH_MIN_INSTALLS - 1)
        ok, reason = quality_reason(low, now=self.now)
        self.assertFalse(ok)
        self.assertEqual(f"installs<{SKILLS_SH_MIN_INSTALLS}", reason)

        # The install branch never rescues a GitHub hit.
        github = {
            "name": "x",
            "source_id": "acme/x",
            "source": "GitHub",
            "stars": MIN_STARS - 1,
            "installs": 10**9,
            "skill_md": "# x\n\nDo the thing.",
            "skill_path": "SKILL.md",
            "pushed_at": self.stale,
        }
        self.assertFalse(passes_quality(github, now=self.now))
        self.assertTrue(passes_quality(dict(github, stars=MIN_STARS), now=self.now))

    def test_github_hits_without_skill_file_still_fail_the_gate(self) -> None:
        hit = {
            "name": "stale-empty",
            "source_id": "old/stale-empty",
            "source": "GitHub",
            "stars": 0,
            "installs": 0,
            "skill_md": "",
            "skill_path": "",
            "url": "https://github.com/old/stale-empty",
            "registry": False,
        }
        ok, reason = quality_reason(hit, now=self.now)
        self.assertFalse(ok)
        self.assertEqual("no_skill_md", reason)

    def test_skills_sh_hits_are_collected_with_source_and_installs(self) -> None:
        self.skills_sh_rows = [
            (
                "剪辑",
                {
                    "id": "genmedia-labs/skills/video-edit",
                    "skillId": "video-edit",
                    "name": "video-edit",
                    "installs": 513911,
                    "source": "genmedia-labs/skills",
                },
            )
        ]

        def getter(url: str) -> object:
            if "genmedia-labs/skills" in url and (
                "raw.githubusercontent.com" in url or "/contents" in url
            ):
                return "# video-edit\n\n视频剪辑 skill\n"
            return self._github_get(url, gap_first_pass=False)

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-sh",
        )
        rows = [skill for skill in result["skills"] if skill["source"] == SKILLS_SH_SOURCE]
        self.assertTrue(rows, msg=json.dumps(result["skills"], ensure_ascii=False))
        row = rows[0]
        self.assertEqual("genmedia-labs/skills/video-edit", row["source_id"])
        self.assertEqual("video-edit", row["name"])
        self.assertEqual(513911, row["installs"])
        self.assertEqual("SKILL.md", row["skill_path"])
        self.assertIn(SKILLS_SH_SOURCE, {str(skill["source"]) for skill in result["skills"]})
        repos = skills_as_repos(result["skills"])
        # B5: `kind` is the capability category; `source` carries the origin.
        sh_repo = next(item for item in repos if item["full_name"] == "genmedia-labs/skills/video-edit")
        self.assertEqual(SKILLS_SH_SOURCE, sh_repo["source"])
        self.assertEqual("genmedia-labs", sh_repo["owner"])
        self.assertNotEqual(SKILLS_SH_SOURCE, sh_repo["kind"])
        self.assertEqual("视频", sh_repo["kind"])

    def test_monorepo_layout_probes_the_skill_name_not_the_repo_name(self) -> None:
        """D-1: skills.sh monorepos keep skills in sibling dirs.

        `genmedia-labs/skills` hosts `video-edit/SKILL.md`, so substituting the
        repo basename would 404 every probe and reject the source's most popular
        entries.
        """
        repo = "genmedia-labs/skills"
        skill = "video-edit"
        body = "# video-edit\n\nEdit existing video."
        seen: list[str] = []

        def getter(url: str) -> object:
            seen.append(url)
            if url.endswith("/contents"):
                return [{"name": skill, "type": "dir"}, {"name": "README.md", "type": "file"}]
            if url.endswith(f"{skill}/SKILL.md") and "raw.githubusercontent.com" in url:
                return body
            raise RuntimeError("github_http_404")

        reset_skill_finder_state()
        text, path = fetch_skill_md(repo, getter, skill)
        self.assertEqual(body, text)
        self.assertEqual(f"{skill}/SKILL.md", path)
        self.assertTrue(
            any(f"{skill}/SKILL.md" in url for url in seen),
            msg=f"the skill directory was never probed: {seen}",
        )

    def test_rate_limited_candidate_is_not_recorded_as_verified(self) -> None:
        """D-2: a 403 must not fabricate a SKILL.md path that clears the gate.

        Before this fix a rate limit produced `skill_path="SKILL.md"` with empty
        content, which satisfied the gate and made coverage reports claim files
        that were never located.
        """
        reset_skill_finder_state()

        def getter(url: str) -> object:
            raise RuntimeError("github_http_403")

        collected: list[dict[str, object]] = []
        ledger: list[dict[str, object]] = []
        _ingest_repo(
            _repo_item("acme/flood", stars=9999, description="generic skill", pushed_at=self.fresh),
            {"id": "s1", "name": "选题"},
            getter,
            collected,
            set(),
            self.now,
            registry=False,
            ledger=ledger,
        )
        self.assertEqual([], collected, msg="unverified candidate must not be collected")
        self.assertTrue(
            any(row.get("reason") == "no_skill_md" for row in ledger),
            msg=f"expected a no_skill_md rejection, got {ledger}",
        )

    def test_a_declared_skill_path_alone_does_not_clear_the_gate(self) -> None:
        """D-2: evidence must be the located file, not a path string."""
        hit = {
            "name": "unverified",
            "source_id": "acme/unverified",
            "source": "GitHub",
            "stars": MIN_STARS + 100,
            "skill_path": "SKILL.md",
            "url": "https://github.com/acme/unverified",
            "pushed_at": self.fresh,
        }
        ok, reason = quality_reason(hit, now=self.now)
        self.assertFalse(ok)
        self.assertEqual("no_skill_md", reason)
        # Carried-over verification from a previous run is still honoured.
        self.assertTrue(passes_quality(dict(hit, skill_verified=True), now=self.now))
        # ...but a real located file is the normal path.
        self.assertTrue(passes_quality(dict(hit, skill_md="# x"), now=self.now))

    def test_skills_sh_monorepo_siblings_survive_repo_level_dedupe(self) -> None:
        """One skills.sh repo can host many skills; dedupe must key on the skill."""
        reset_skill_finder_state()

        def getter(url: str) -> object:
            if url.endswith("/contents"):
                return [{"name": "video-edit", "type": "dir"}, {"name": "video-script", "type": "dir"}]
            if "raw.githubusercontent.com" in url:
                return "# skill\n\nDo the thing."
            raise RuntimeError("github_http_404")

        collected: list[dict[str, object]] = []
        seen: set[str] = set()
        stage = {"id": "edit", "name": "剪辑"}
        for skill_id in ("acme/skills/video-edit", "acme/skills/video-script"):
            _ingest_repo(
                {
                    "source": SKILLS_SH_SOURCE,
                    "source_id": skill_id,
                    "source_repo": "acme/skills",
                    "full_name": "acme/skills",
                    "name": skill_id.rsplit("/", 1)[-1],
                    "url": f"https://skills.sh/{skill_id}",
                    "installs": 5000,
                },
                stage,
                getter,
                collected,
                seen,
                self.now,
                registry=False,
            )
        self.assertEqual(
            ["video-edit", "video-script"],
            sorted(str(row["name"]) for row in collected),
            msg=f"sibling skills from one repo were deduped away: {collected}",
        )

    def test_stage_floor_keeps_one_call_for_every_later_stage(self) -> None:
        """D-10: a generous per-stage floor must not starve a wide map's tail."""
        class _Budget:
            def __init__(self, max_fetches: int) -> None:
                self.max_fetches = max_fetches
                self.calls = 0
                self.deadline = 0.0

        for budget, stages in ((24, 12), (10, 8), (160, 8)):
            with self.subTest(budget=budget, stages=stages):
                tracker = _Budget(budget)
                shares = []
                for index in range(stages):
                    shares.append(_fair_share(tracker, stages - index))
                    tracker.calls += shares[-1]
                self.assertGreaterEqual(min(shares), 1)
                self.assertLessEqual(sum(shares), budget)

    def test_skills_sh_failure_keeps_github_results_and_degrades(self) -> None:
        cases = (
            ("403", RuntimeError("skills_sh_http_403")),
            ("429", RuntimeError("skills_sh_http_429")),
            ("unreachable", OSError("skills_sh_unreachable")),
            ("timeout", TimeoutError("skills_sh_timeout")),
        )
        for label, failure in cases:
            with self.subTest(label=label):
                reset_skill_finder_state()

                def getter(url: str, failure: Exception = failure) -> object:
                    if "skills.sh" in url:
                        raise failure
                    return self._github_get(url, gap_first_pass=False)

                result = find_skills(
                    "从零做播客",
                    complete=self._complete,
                    coverage=self._coverage(),
                    github_get=getter,
                    web_search=lambda _query: [],
                    now=self.now,
                    output_root=self.root / f"skills-sh-{label}",
                )
                self.assertTrue(result["degraded"], label)
                reasons = " ".join(str(item) for item in result["degraded_reasons"])
                self.assertIn(SKILLS_SH_SOURCE, reasons, msg=reasons)
                self.assertTrue(result["skills"], label)
                self.assertTrue(
                    all(skill["source"] == "GitHub" for skill in result["skills"]), label
                )
                self.assertTrue(result["stages"], label)

    def test_unparseable_skills_sh_payload_degrades_but_never_raises(self) -> None:
        def getter(url: str) -> object:
            if "skills.sh" in url:
                return {"query": "x", "skills": "not-a-list"}
            return self._github_get(url, gap_first_pass=False)

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-sh-unparseable",
        )
        self.assertTrue(result["degraded"])
        self.assertIn(f"{SKILLS_SH_SOURCE}:unparseable", result["degraded_reasons"])
        self.assertTrue(result["skills"])

    def test_ingest_accepts_skills_sh_source_without_owner_repo(self) -> None:
        collected: list[dict[str, object]] = []
        seen: set[str] = set()
        ledger: list[dict[str, object]] = []
        _ingest_repo(
            {
                "source": SKILLS_SH_SOURCE,
                "source_id": "skills.volces.com/byted-drama",
                "name": "byted-drama",
                "installs": 694,
                "plain": True,
                "url": "https://skills.sh/skills.volces.com/byted-drama",
            },
            {"id": "edit", "name": "剪辑"},
            lambda _url: None,
            collected,
            seen,
            self.now,
            registry=False,
            ledger=ledger,
        )
        # No locatable SKILL.md: kept out of the results, but the reason is recorded.
        self.assertEqual([], collected)
        self.assertEqual(1, len(ledger))
        self.assertEqual("no_skill_md", ledger[0]["reason"])
        self.assertEqual("skills.volces.com/byted-drama", ledger[0]["source_id"])

    def test_names_from_web_preserves_non_github_links(self) -> None:
        refs = _names_from_web(
            [
                {"url": "https://github.com/acme/foo", "title": "foo"},
                {"url": "https://github.com/acme/foo/blob/main/README.md", "title": "dupe"},
                {"url": "https://skills.sh/genmedia-labs/skills/video-edit", "title": "video"},
                {"url": "https://skills.sh/trending", "title": "trending"},
                {"url": "https://example.com/blog/skill", "title": "other"},
            ]
        )
        self.assertEqual(
            ["GitHub", SKILLS_SH_SOURCE, SKILLS_SH_SOURCE, "web"],
            [ref["source"] for ref in refs],
        )
        self.assertEqual("acme/foo", refs[0]["full_name"])
        self.assertEqual("genmedia-labs/skills", refs[1]["full_name"])
        self.assertEqual("video-edit", refs[1]["name"])
        self.assertEqual("genmedia-labs/skills/video-edit", refs[1]["source_id"])
        self.assertEqual("trending", refs[2]["section"])
        self.assertEqual("https://example.com/blog/skill", refs[3]["url"])

    def test_skill_md_probes_every_documented_layout(self) -> None:
        name = "video-edit"
        self.assertEqual(
            (
                "SKILL.md",
                "{name}/SKILL.md",
                ".agents/skills/{name}/SKILL.md",
                ".claude/skills/{name}/SKILL.md",
                "skills/{name}/SKILL.md",
                ".cursor/skills/{name}/SKILL.md",
            ),
            SKILL_PATHS,
        )
        for template in SKILL_PATHS:
            path = template.format(name=name)
            with self.subTest(layout=path):

                def getter(url: str, path: str = path) -> object:
                    if "/HEAD/" in url:
                        tail = url.split("/HEAD/", 1)[-1]
                    elif "/contents/" in url:
                        tail = url.rsplit("/contents/", 1)[-1]
                    else:
                        tail = url
                    if tail == path:
                        return f"# {name}\n\n视频剪辑\n"
                    raise RuntimeError("github_fetch_failed:404")

                text, found = fetch_skill_md(f"acme/{name}", getter)
                self.assertIn("视频剪辑", text)
                self.assertEqual(path, found)

    def test_nested_layouts_are_only_probed_when_the_root_listing_advertises_them(self) -> None:
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if url.endswith("/contents"):
                return [{"name": "README.md", "type": "file"}]
            raise RuntimeError("github_fetch_failed:404")

        self.assertEqual(("", ""), fetch_skill_md("acme/video-edit", getter))
        self.assertTrue(any(url.endswith("/contents") for url in calls))
        self.assertFalse(any("skills/video-edit" in url for url in calls))

        nested = ".claude/skills/video-edit/SKILL.md"

        def advertised(url: str) -> object:
            if url.endswith("/contents"):
                return [{"name": ".claude", "type": "dir"}]
            if url.endswith("/contents/" + nested):
                return "# nested\n\n视频剪辑\n"
            raise RuntimeError("github_fetch_failed:404")

        text, found = fetch_skill_md("acme/video-edit", advertised)
        self.assertIn("nested", text)
        self.assertEqual(nested, found)

    def test_wide_coverage_map_still_searches_tail_stages(self) -> None:
        stages = [
            {
                "id": f"stage-{index:02d}",
                "name": f"模块{index}",
                "queries": [f"模块{index} 检索词"],
                "required": True,
            }
            for index in range(1, 13)
        ]
        # 宽计划通过显式 coverage 传进去（1.4.0 里模型不再解构查询）。
        coverage = {
            "goal": "宽覆盖",
            "title": "宽覆盖",
            "theme": "宽覆盖",
            "chain": [],
            "stages": copy.deepcopy(stages),
        }

        def complete(prompt: str) -> str:
            self.assertIn("打包结果", prompt)
            return json.dumps({"title": "宽覆盖", "body": "AIGC"}, ensure_ascii=False)

        searched: list[str] = []

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE:
                searched.append(parse_qs(parsed.query).get("q", [""])[0])
                return {"skills": []}
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                searched.append(parse_qs(parsed.query).get("q", [""])[0])
                return {"items": []}
            if "anthropics/skills" in url:
                return []
            return {}

        result = find_skills(
            "宽覆盖检索",
            complete=complete,
            coverage=coverage,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-wide",
            max_fetches=36,
            fetch_deadline_seconds=30,
        )
        self.assertIn("模块12", " ".join(searched))
        # 显式的 12 个阶段原样保留（后面跟的是 1.4.0 补上的 workflow 兜底行）。
        self.assertEqual(
            [f"stage-{index:02d}" for index in range(1, 13)],
            [str(stage["id"]) for stage in result["stages"][:12]],
        )
        self.assertEqual(12 + len(WORKFLOW_STAGES), len(result["stages"]))
        self.assertTrue(all(stage["gap"] for stage in result["stages"][:12]))
        self.assertTrue(all(stage.get("fetches", 0) >= 1 for stage in result["stages"][:12]))
        # 兜底行只是归档位：检索循环没有查过它们，所以没有 fetches 记录。
        self.assertTrue(all("fetches" not in stage for stage in result["stages"][12:]))
        budget = result["budget"]
        self.assertEqual(36, budget["max_fetches"])
        self.assertLessEqual(budget["calls"], 36)
        self.assertGreaterEqual(budget["stage_quota_skips"], 1)
        self.assertFalse(result["degraded"], msg=str(result["degraded_reasons"]))
        # one row per stage per pass (12 first pass + 12 gap-fill pass)
        self.assertEqual(24, len(budget["stages"]))

    def test_keyword_default_searches_every_keyword_and_skips_workflow_rows(self) -> None:
        # 1.4.0：不传 coverage 时，用户输入本身就是检索计划——每个关键词一个
        # 阶段（按输入顺序、大小写去重、最多 KEYWORD_LIMIT 个），而且这些关键词
        # 必须真的到达两个来源；workflow 兜底行只用于归档，永远不会被查询。
        searched: dict[str, list[str]] = {"github": [], "skills.sh": []}

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE and parsed.path == "/api/search":
                searched["skills.sh"].append(parse_qs(parsed.query).get("q", [""])[0])
                return {"skills": []}
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                searched["github"].append(parse_qs(parsed.query).get("q", [""])[0])
                return {"items": []}
            if "anthropics/skills" in url:
                raise RuntimeError("github_fetch_failed:optional-root")
            raise RuntimeError(f"github_fetch_failed:{url}")

        result = find_skills(
            # 空格/逗号/顿号/斜杠都算分隔符；重复的关键词只保留第一次。
            "视频, 剪辑、字幕 / 视频",
            complete=None,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-keywords",
        )
        # 默认计划就是 keyword_coverage：每个关键词一个 kw 阶段，按输入顺序、
        # 大小写去重、最多 KEYWORD_LIMIT 个，且不是必填阶段。
        plan = keyword_coverage("视频, 剪辑、字幕 / 视频")
        self.assertEqual(["视频", "剪辑", "字幕"], extract_keywords("视频, 剪辑、字幕 / 视频"))
        # id 由关键词内容派生（不是位置），否则下一次检索的 kw1 会和这一次撞车，
        # 把上一轮的 skill 悄悄贴到新关键词那条 lane 上。
        plan_ids = [str(stage["id"]) for stage in plan["stages"]]
        self.assertEqual(3, len(set(plan_ids)))
        self.assertTrue(all(stage_id.startswith("kw-") for stage_id in plan_ids))
        self.assertEqual(
            [_keyword_stage_id(name) for name in ("视频", "剪辑", "字幕")], plan_ids
        )
        self.assertEqual(["视频", "剪辑", "字幕"], [str(stage["name"]) for stage in plan["stages"]])
        for stage in plan["stages"]:
            self.assertEqual([str(stage["name"])], [str(item) for item in stage["queries"]])
            self.assertFalse(stage["required"])
            self.assertFalse(stage["gap"])
        keyword_stages = result["stages"][:3]
        self.assertEqual(plan_ids, [str(stage["id"]) for stage in keyword_stages])
        self.assertEqual(["视频", "剪辑", "字幕"], [str(stage["name"]) for stage in keyword_stages])
        self.assertTrue(all(not stage["required"] for stage in keyword_stages))
        # B4：这个 fixture 一个候选都没给，所以关键词阶段仍然可见、只是标成缺口。
        self.assertTrue(all(stage["gap"] for stage in keyword_stages))
        # 关键词阶段之后是固定顺序的 workflow 兜底行，去掉已经存在的 id。
        expected_workflow = [
            (stage_id, name)
            for stage_id, name, _tokens in WORKFLOW_STAGES
            if stage_id not in set(plan_ids)
        ]
        self.assertEqual(
            [stage_id for stage_id, _name in expected_workflow],
            [str(stage["id"]) for stage in result["stages"][3:]],
        )
        self.assertEqual(
            [name for _stage_id, name in expected_workflow],
            [str(stage["name"]) for stage in result["stages"][3:]],
        )
        self.assertEqual(3 + len(WORKFLOW_STAGES), len(result["stages"]))
        # 兜底行带着 offline 标记，检索循环靠 _is_searchable 跳过它们。
        self.assertTrue(all(row.get("offline") for row in workflow_stages()))
        self.assertTrue(all(not row["required"] for row in workflow_stages()))
        for keyword in ("视频", "剪辑", "字幕"):
            self.assertTrue(
                any(keyword in query for query in searched["github"]),
                msg=f"GitHub 没查到关键词 {keyword}：{searched['github']}",
            )
            self.assertTrue(
                any(keyword in query for query in searched["skills.sh"]),
                msg=f"skills.sh 没查到关键词 {keyword}：{searched['skills.sh']}",
            )
        # 兜底行的名字（如「视频与剪辑」）一次都不许出现在检索词里。
        joined = " ".join(searched["github"] + searched["skills.sh"])
        for _stage_id, name, _tokens in WORKFLOW_STAGES:
            self.assertNotIn(name, joined, msg=f"workflow 兜底行 {name} 不该被检索")

        # 关键词上限：超过 KEYWORD_LIMIT 的部分不再开新阶段。
        capped = find_skills(
            "一 二 三 四 五 六 七 八",
            complete=None,
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-keywords-cap",
        )
        capped_ids = [str(stage["id"]) for stage in capped["stages"][:KEYWORD_LIMIT]]
        self.assertEqual(
            [_keyword_stage_id(name) for name in ("一", "二", "三", "四", "五", "六")],
            capped_ids,
        )
        self.assertTrue(all(stage_id.startswith("kw-") for stage_id in capped_ids))
        self.assertEqual(
            ["一", "二", "三", "四", "五", "六"],
            [str(stage["name"]) for stage in capped["stages"][:KEYWORD_LIMIT]],
        )
        self.assertEqual(
            KEYWORD_LIMIT + len(WORKFLOW_STAGES), len(capped["stages"])
        )

    def test_rejected_ledger_explains_stages_that_ran_out_of_time(self) -> None:
        """1.4.0 删掉了客户端缺口提示（#skill-gap-note/gapReason），所以「这一环是
        没时间验证，不是没有候选」这句话现在就落在快照里：``rejected`` 账本带
        ``stage_quota`` 原因，阶段本身带 ``gap`` / ``gap_reason``。

        旧用例（test_gap_note_explains_candidates_that_ran_out_of_time）断言的是
        页面文案；这里改为断言同一份解释在数据里没有丢。
        """
        stages = [
            {"id": "s1", "name": "甲", "queries": ["甲 检索词"], "required": True},
            {"id": "s2", "name": "乙", "queries": ["乙 检索词"], "required": True},
        ]

        def getter(url: str) -> object:
            parsed = urlparse(url)
            if parsed.netloc == SKILLS_SH_SOURCE:
                return {"skills": []}
            if parsed.netloc == "api.github.com" and parsed.path == "/search/repositories":
                query = parse_qs(parsed.query).get("q", [""])[0]
                name = "first" if "甲" in query else "second"
                return {
                    "items": [
                        _repo_item(
                            f"acme/{name}",
                            stars=80,
                            description=name,
                            pushed_at=self.fresh,
                        )
                    ]
                }
            if "anthropics/skills" in url:
                raise RuntimeError("github_fetch_failed:optional-root")
            if "acme/first" in url:
                return _skill_md("first", "甲")
            # 第二个候选的 SKILL.md 永远定位不到，探测会一直花预算。
            raise RuntimeError("github_fetch_failed:404")

        result = find_skills(
            "配额检索",
            complete=None,
            coverage={
                "goal": "配额检索",
                "title": "配额检索",
                "theme": "配额检索",
                "chain": [],
                "stages": stages,
            },
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-quota",
            max_fetches=8,
            fetch_deadline_seconds=30,
        )
        quota_rows = [row for row in result["rejected"] if row["reason"] == "stage_quota"]
        self.assertTrue(quota_rows, msg=f"缺少 stage_quota 记录：{result['rejected']}")
        self.assertTrue(all(row["source_id"] for row in quota_rows))
        # 阶段本身仍然可见，并带上「为什么空」的说明。
        exhausted = next(stage for stage in result["stages"] if stage["id"] == "s2")
        self.assertTrue(exhausted["gap"])
        self.assertEqual(GAP_REASON, exhausted["gap_reason"])
        # 预算用完不等于上游挂了。
        self.assertGreaterEqual(result["budget"]["stage_quota_skips"], 1)

    def test_snapshot_exposes_budget_consumption_and_rejections(self) -> None:
        self.skills_sh_rows = [
            (
                "剪辑",
                {
                    "id": "tiny/skills/weak-skill",
                    "skillId": "weak-skill",
                    "name": "weak-skill",
                    "installs": 3,
                    "source": "tiny/skills",
                },
            )
        ]

        def getter(url: str) -> object:
            if "tiny/skills" in url and (
                "raw.githubusercontent.com" in url or "/contents" in url
            ):
                return "# weak-skill\n\n视频剪辑\n"
            return self._github_get(url, gap_first_pass=True)

        result = find_skills(
            "从零做播客",
            complete=self._complete,
            coverage=self._coverage(),
            github_get=getter,
            web_search=lambda _query: [],
            now=self.now,
            output_root=self.root / "skills-observable",
        )
        budget = result["budget"]
        self.assertEqual(MAX_FETCHES, budget["max_fetches"])
        self.assertGreater(budget["calls"], 0)
        self.assertEqual(budget["calls"], budget["fetch_calls"] + budget["search_calls"])
        self.assertLessEqual(sum(int(row["calls"]) for row in budget["stages"]), budget["calls"])
        self.assertTrue(any(int(row["calls"]) >= 1 for row in budget["stages"]))
        rejected = result["rejected"]
        self.assertIsInstance(rejected, list)
        self.assertTrue(rejected)
        self.assertEqual("tiny/skills/weak-skill", rejected[0]["source_id"])
        self.assertEqual(f"installs<{SKILLS_SH_MIN_INSTALLS}", rejected[0]["reason"])
        self.assertTrue(all(row.get("reason") for row in rejected))
        self.assertLessEqual(len(rejected), 20)

    def test_source_level_grep_finds_no_github_filtered_skills_sh_template(self) -> None:
        module_path = Path(sys.modules["autoposting.skill_finder"].__file__).resolve()
        grep = (
            "import pathlib, sys\n"
            "lines = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()\n"
            "mentions = [i + 1 for i, line in enumerate(lines) if 'skills.sh' in line]\n"
            "filtered = [i for i in mentions if 'site:github.com' in lines[i - 1]]\n"
            "print(len(mentions))\n"
            "print(','.join(str(i) for i in filtered))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", grep, str(module_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = done.stdout.splitlines()
        mention_count = int(lines[0])
        filtered_lines = lines[1] if len(lines) > 1 else ""
        self.assertGreaterEqual(mention_count, 1)
        self.assertEqual("", filtered_lines)
        # The dispatch table itself: GitHub keeps the filter, skills.sh has none.
        self.assertIn("site:github.com", SEARCH_TEMPLATES["github"]("视频制作"))
        self.assertEqual("视频制作", SEARCH_TEMPLATES[SKILLS_SH_SOURCE]("视频制作"))
        self.assertNotIn("github.com", SEARCH_TEMPLATES[SKILLS_SH_SOURCE]("视频制作"))

    def test_budget_stage_slices_are_fair_and_observable(self) -> None:
        budget = _FetchBudget(
            lambda _url: {},
            lambda _query: [],
            max_fetches=12,
            deadline=time.monotonic() + 30,
        )
        budget.start_stage("a", "甲", 4)
        budget.get("https://example.test/a-1")
        budget.get("https://example.test/a-2")
        self.assertEqual(2, budget.end_stage())
        budget.start_stage("b", "乙", 0)
        with self.assertRaises(RuntimeError):
            budget.get("https://example.test/b-1")
        self.assertEqual(0, budget.end_stage())
        report = budget.report()
        self.assertEqual(1, report["stage_quota_skips"])
        self.assertFalse(report["degraded"])
        self.assertEqual([2, 0], [row["calls"] for row in report["stages"]])

    def test_stage_time_slice_also_hands_every_stage_real_time(self) -> None:
        budget = _FetchBudget(
            lambda _url: {},
            lambda _query: [],
            max_fetches=100,
            deadline=time.monotonic() + 30,
        )
        budget.start_stage("a", "甲", 100, seconds=0.0)
        with self.assertRaises(RuntimeError):
            budget.get("https://example.test/a-1")
        self.assertEqual(0, budget.end_stage())
        self.assertEqual(1, budget.stage_quota_skips)
        # Running out of a stage's own slice is not an upstream failure.
        self.assertFalse(budget.degraded)
        self.assertTrue(budget.remaining())

    def test_skills_sh_rows_are_capped_per_query(self) -> None:
        payload = {
            "skills": [
                {
                    "id": f"acme/skills/s{index}",
                    "name": f"s{index}",
                    "installs": 1000 - index,
                    "source": "acme/skills",
                }
                for index in range(60)
            ]
        }
        hits = _skills_sh_search("video", lambda _url: payload)
        self.assertEqual(SKILLS_SH_PER_QUERY, len(hits))
        self.assertEqual("acme/skills/s0", hits[0]["source_id"])


class SkillPosterCopyTests(unittest.TestCase):
    """B5: skill posters must carry valid, per-skill text — not repeated filler."""

    SKILLS = [
        {
            "name": "douyin-video",
            "source_id": "yzfly/douyin-mcp-server/douyin-video",
            "source": "skills.sh",
            "installs": 1857,
            "stage_name": "剪辑",
            "url": "https://skills.sh/yzfly/douyin-mcp-server/douyin-video",
            "description": "Download and edit Douyin videos",
            "skill_excerpt": "Use this skill to clip short videos.",
        },
        {
            "name": "video-script",
            "source_id": "acme/video-script",
            "source": "GitHub",
            "installs": 300,
            "stage_name": "脚本与分镜",
            "url": "https://github.com/acme/video-script",
            "description": "把口播稿拆成分镜表",
            "skill_excerpt": "先读选题。\n再按 30 秒一段拆镜头。",
        },
        {
            "name": "thumbnail-kit",
            "source_id": "acme/thumbnail-kit",
            "source": "GitHub",
            "installs": 120,
            "stage_name": "封面与标题",
            "url": "https://github.com/acme/thumbnail-kit",
            "description": "批量生成视频封面",
        },
        {
            "name": "voice-over",
            "source_id": "acme/voice-over",
            "source": "skills.sh",
            "installs": 900,
            "stage_name": "配音与字幕",
            "url": "https://skills.sh/acme/voice-over",
            "description": "generate voiceover audio",
        },
        {
            "name": "review-metrics",
            "source_id": "acme/review-metrics",
            "source": "GitHub",
            "installs": 400,
            "stage_name": "数据复盘",
            "url": "https://github.com/acme/review-metrics",
            "description": "视频数据复盘看板和结论",
        },
    ]

    def test_search_dedupes_skills_by_name_only(self) -> None:
        rows = _dedupe_by_skill_name(
            [
                {
                    "name": "video-edit",
                    "source_id": "acme/video-edit",
                    "stars": 10,
                    "stage_id": "edit",
                },
                {
                    "name": "Video-Edit",
                    "source_id": "other/video-edit",
                    "stars": 80,
                    "stage_id": "edit",
                },
                {
                    "name": "cover-maker",
                    "source_id": "acme/cover-maker",
                    "stars": 5,
                    "stage_id": "cover",
                },
                {
                    "name": "video-edit",
                    "source_id": "pasted/video-edit",
                    "stars": 1,
                    "manual": True,
                    "stage_id": "edit",
                },
            ]
        )
        searched = [row for row in rows if not row.get("manual")]
        manuals = [row for row in rows if row.get("manual")]
        self.assertEqual(["Video-Edit", "cover-maker"], [row["name"] for row in searched])
        self.assertEqual("other/video-edit", searched[0]["source_id"])
        self.assertEqual(["video-edit"], [row["name"] for row in manuals])

    def test_kind_is_a_capability_category_never_the_source(self) -> None:
        rows = skills_as_repos(self.SKILLS)
        kinds = [str(row["kind"]) for row in rows]
        for kind in kinds:
            self.assertNotIn(kind, {"GitHub", "registry", "skills.sh"})
        # Distinct capabilities for genuinely different skills.
        self.assertIn("视频", kinds)
        self.assertIn("写作", kinds)
        self.assertIn("图像", kinds)
        self.assertEqual(len(set(kinds)), len(kinds))

    def test_facts_records_do_not_prefill_poster_boards(self) -> None:
        rows = skills_as_repos(self.SKILLS)
        self.assertEqual(len(self.SKILLS), len(rows))
        kinds = [str(row["kind"]) for row in rows]
        self.assertEqual(len(set(kinds)), len(kinds))
        for row in rows:
            self.assertFalse(row.get("enriched"))
            self.assertFalse(str(row.get("summary") or "").strip())
            self.assertFalse(str(row.get("does") or "").strip())

    def test_render_plan_carries_no_github_weekly_branding(self) -> None:
        import re
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)

            def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
                png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return png_path

            snapshot = {
                "title": "视频制作技能包",
                "theme": "从选题到发布",
                "stages": [
                    {"id": "edit", "name": "剪辑"},
                    {"id": "cover", "name": "封面与标题", "gap": True, "gap_reason": GAP_REASON},
                ],
                "skills": self.SKILLS[:2],
            }
            cards = render_skill_package(
                snapshot, out, screenshot_fn=fake_shot, copy_writer=stub_copy_writer
            )
            self.assertEqual(len(self.SKILLS[:2]) + 1, len(cards))
            self.assertEqual("cover", cards[0]["kind"])
            for html_path in out.glob("*.html"):
                text = html_path.read_text(encoding="utf-8")
                self.assertNotIn("GitHub", text, msg=f"{html_path.name} 印了 GitHub 周榜品牌")
                self.assertIsNone(
                    re.search(r"20\d\d年第", text), msg=f"{html_path.name} 印了周次"
                )
                self.assertNotIn("01 / 11", text, msg=f"{html_path.name} 页码写死")
                self.assertNotIn("9:16", text)
            cover = (out / "cover.html").read_text(encoding="utf-8")
            self.assertIn("SKILL FINDER", cover)
            self.assertIn("c-smart-cover-kicker", cover)
            poster = (out / "01-douyin-video.html").read_text(encoding="utf-8")
            # page number total is the real package size, not a hardcoded 11
            self.assertIn("02 / 03", poster)

    def test_degenerate_inputs_do_not_invent_template_boards(self) -> None:
        skills = [
            {
                "name": f"same-{i}",
                "source_id": f"acme/same-{i}",
                "installs": 50,
                "stage_name": "封面",
                "description": "批量生成视频封面",
            }
            for i in range(1, 4)
        ]
        rows = skills_as_repos(skills)
        self.assertEqual(3, len(rows))
        for row in rows:
            self.assertFalse(str(row.get("summary") or "").strip())
            self.assertNotIn("接手", str(row.get("does") or ""))
            self.assertNotIn("这一步要用的材料备齐", str(row.get("how") or ""))

    def test_install_count_is_the_popularity_signal(self) -> None:
        rows = skills_as_repos(self.SKILLS[:1])
        self.assertEqual(1857, rows[0]["stars"])
        self.assertEqual("1.9k", rows[0]["stars_label"])
        self.assertEqual(1857, rows[0]["installs"])

    def test_copy_layer_is_shared_with_the_weekly_lane(self) -> None:
        import tempfile

        seen: list[list[dict[str, object]]] = []

        def copy_writer(repos: list[dict[str, object]]) -> list[dict[str, object]]:
            seen.append(repos)
            return [
                dict(repo, summary="给做短视频的人，要交片的时候，把粗剪先做出来。",
                     does="按脚本排好素材。\n切出可用片段。", how="把素材放进去跑一遍。",
                     audience="一个人管多个账号的运营。", scene="素材堆着又要赶交期的时候。",
                     enriched=True)
                for repo in repos
            ]

        with tempfile.TemporaryDirectory() as raw:
            def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
                png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return png_path

            render_skill_package(
                {"title": "t", "stages": [], "skills": self.SKILLS[:1]},
                Path(raw),
                screenshot_fn=fake_shot,
                copy_writer=copy_writer,
            )
        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0][0]["full_name"])
        # The weekly copy merge matches on full_name, so the skill lane must set it.
        self.assertEqual("yzfly/douyin-mcp-server/douyin-video", seen[0][0]["full_name"])

    def test_rerender_prunes_pages_that_are_no_longer_in_the_package(self) -> None:
        """A shrinking result set must not leave last query's pages behind.

        The lane reuses one package directory (and a shared `latest`), so stale
        `*.html`/`*.png` would keep the previous page totals visible next to the
        new ones.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)

            def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
                png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return png_path

            # 1.4.0：海报清单来自 aggregate_skills(snapshot)，所以 fixture 里的
            # skill 必须真的在 snapshot 的 skills 里，而 gap 行必须真的不出图。
            first = render_skill_package(
                {
                    "title": "t",
                    "stages": [],
                    "skills": self.SKILLS[:3] + [dict(self.SKILLS[3], gap=True)],
                },
                out,
                screenshot_fn=fake_shot,
                copy_writer=stub_copy_writer,
            )
            self.assertEqual(4, len(first), msg="封面 + 3 张海报；gap 行不该出图")
            self.assertFalse(
                [path for path in out.iterdir() if "voice-over" in path.name],
                msg="gap 行不该留下任何页面",
            )
            poster_ids = [str(card["id"]) for card in first if card["kind"] == "poster"]
            self.assertEqual(3, len(poster_ids))
            # 顺序现在按工作流排，不再等于输入顺序，所以从实际产物里取名字。
            douyin = next(
                (poster_id for poster_id in poster_ids if "douyin-video" in poster_id), ""
            )
            self.assertTrue(douyin, msg=f"douyin-video 没有出海报：{poster_ids}")
            stale_ids = [poster_id for poster_id in poster_ids if poster_id != douyin]
            stale_html = out / f"{stale_ids[0]}.html"
            self.assertTrue(stale_html.is_file())
            self.assertTrue((out / f"{stale_ids[1]}.png").is_file())

            second = render_skill_package(
                {"title": "t", "stages": [], "skills": self.SKILLS[:1]},
                out,
                screenshot_fn=fake_shot,
                copy_writer=stub_copy_writer,
            )
            self.assertEqual(2, len(second))
            for stale_id in stale_ids:
                self.assertFalse((out / f"{stale_id}.html").exists(), msg=f"{stale_id} 上一轮的页面没有被清掉")
                self.assertFalse((out / f"{stale_id}.png").exists())
            self.assertEqual(
                ["01-douyin-video.html", "01-douyin-video.png", "cover.html", "cover.png"],
                sorted(path.name for path in out.iterdir()),
            )

    def test_copy_writer_failure_stops_render(self) -> None:
        import tempfile

        def boom(repos: list[dict[str, object]]) -> list[dict[str, object]]:
            raise LLMError("llm_api_failed", "模型挂了")

        with tempfile.TemporaryDirectory() as raw:
            def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
                png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                return png_path

            with self.assertRaises(LLMError) as raised:
                render_skill_package(
                    {"title": "t", "stages": [], "skills": self.SKILLS[:1]},
                    Path(raw),
                    screenshot_fn=fake_shot,
                    copy_writer=boom,
                )
        self.assertEqual("llm_api_failed", raised.exception.code)


class ManualSkillPasteTests(unittest.TestCase):
    """Pasted links are user state: they must reach intro, stage and poster."""

    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _getter(self):
        calls: list[str] = []

        def getter(url: str) -> object:
            calls.append(url)
            if url.endswith("/artifacts-builder/SKILL.md"):
                return "# artifacts-builder\n\n用组件搭可视化页面，做数据看板。\n"
            if url.endswith("/video-edit/SKILL.md"):
                return "# video-edit\n\n剪掉废话，按短视频节奏重排时间线。\n"
            if url.endswith("/alpha/SKILL.md"):
                return "# alpha\n\n写口播稿。\n"
            if url.endswith("/beta/SKILL.md"):
                return "# beta\n\n做封面标题。\n"
            if url.endswith("/repos/acme/toolkit"):
                return {
                    "full_name": "acme/toolkit",
                    "description": "视频工具箱",
                    "stargazers_count": 120,
                    "html_url": "https://github.com/acme/toolkit",
                    "pushed_at": "2026-09-01T00:00:00Z",
                    "owner": {"avatar_url": "https://avatars.example/acme.png"},
                    "topics": ["video"],
                }
            raise RuntimeError("github_fetch_failed:404")

        return getter, calls

    def _snapshot(self) -> dict[str, object]:
        return {
            "query": "从零做播客",
            "title": "从零做播客技能包",
            "theme": "播客从选题到上线",
            "stages": [
                {"id": "topic", "name": "选题", "queries": ["播客选题"], "required": True},
                {"id": "edit", "name": "剪辑", "queries": ["视频剪辑"], "required": True},
            ],
            "skills": [],
            "article": {"title": "从零做播客技能包", "body": "旧介绍文"},
            "images": [{"id": "cover", "url": "/api/generated/skills/cover.png"}],
            "phase": "done",
        }

    def test_pasted_links_join_the_package_with_intro_and_stage(self) -> None:
        getter, _calls = self._getter()
        candidates, rejects = parse_skill_links(
            "https://github.com/anthropics/skills/tree/main/artifacts-builder\n"
            "https://github.com/acme/toolkit/blob/main/video-edit/SKILL.md"
        )
        self.assertEqual([], rejects)
        result = add_manual_skills(self._snapshot(), candidates, getter=getter)
        names = [str(row["name"]) for row in result["skills"]]
        # 顺序现在是工作流顺序（剪辑在数据前），不再等于粘贴顺序。
        self.assertEqual(["video-edit", "artifacts-builder"], names)
        for row in result["skills"]:
            self.assertTrue(row["manual"], msg="粘贴的 skill 必须标记为 manual")
            self.assertTrue(str(row["skill_excerpt"]).strip(), msg="海报文案要有 SKILL.md 摘要")
        # 1.4.0：粘贴的 skill 按内容找最合适的工作流环节，不再统一进「手动补充」桶。
        stage_ids_by_name = {str(row["name"]): str(row["stage_id"]) for row in result["skills"]}
        self.assertEqual("video", stage_ids_by_name["video-edit"])
        self.assertEqual("data", stage_ids_by_name["artifacts-builder"])
        stage_names = {str(stage["id"]): str(stage["name"]) for stage in result["stages"]}
        self.assertEqual("视频与剪辑", stage_names["video"])
        self.assertEqual("数据与复盘", stage_names["data"])
        self.assertNotIn(MANUAL_STAGE_NAME, set(stage_names.values()))
        self.assertFalse(
            next(stage for stage in result["stages"] if stage["id"] == "video")["gap"],
            msg="有 skill 的环节不能算缺口",
        )
        body = str(result["article"]["body"])
        self.assertIn("artifacts-builder", body)
        self.assertIn("video-edit", body)
        self.assertEqual("从零做播客技能包", result["article"]["title"])
        self.assertEqual([], result["images"], msg="旧海报作废，等重新出图")
        self.assertEqual("skills_ready", result["phase"])
        self.assertEqual(2, len(result["manual_skills"]))

    def test_monorepo_siblings_both_enter_the_package(self) -> None:
        """Two skills pasted from one repo are two skills, not one."""
        getter, _calls = self._getter()
        candidates, rejects = parse_skill_links(
            "https://github.com/acme/monorepo/tree/main/alpha\n"
            "https://github.com/acme/monorepo/tree/main/beta"
        )
        self.assertEqual([], rejects)
        self.assertEqual(2, len(candidates))
        result = add_manual_skills(self._snapshot(), candidates, getter=getter)
        names = sorted(str(row["name"]) for row in result["skills"])
        self.assertEqual(["alpha", "beta"], names)
        self.assertEqual(2, len(result["manual_skills"]))
        self.assertEqual(0, result["paste_replaced"], msg="同仓库的新兄弟是新增，不是替换")
        # Re-pasting one of them replaces just that slot.
        again = add_manual_skills(result, candidates[:1], getter=getter)
        self.assertEqual(2, len(again["skills"]))
        self.assertEqual(2, len(again["manual_skills"]))
        self.assertEqual(1, again["paste_replaced"], msg="同一槽位重复粘贴才算替换")

    def test_pasted_skill_description_skips_front_matter(self) -> None:
        """A SKILL.md starts with YAML; the description must be prose, not `name:`."""
        from autoposting.skill_finder import _first_paragraph

        markdown = (
            "---\n"
            "name: web-artifacts-builder\n"
            "description: build artifacts\n"
            "license: MIT\n"
            "---\n\n"
            "# web-artifacts-builder\n\n"
            "用组件搭可视化页面，做数据看板。\n"
        )
        self.assertEqual("用组件搭可视化页面，做数据看板。", _first_paragraph(markdown))

    def test_pasted_skill_reaches_the_poster_plan(self) -> None:
        getter, _calls = self._getter()
        candidates, _rejects = parse_skill_links("https://github.com/acme/toolkit/tree/main/video-edit")
        result = add_manual_skills(self._snapshot(), candidates, getter=getter)

        def fake_shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            return png_path

        cards = render_skill_package(
            result, self.root, screenshot_fn=fake_shot, copy_writer=stub_copy_writer
        )
        self.assertEqual(2, len(cards), msg="封面 + 每个收录 skill 一张项目卡")
        self.assertEqual("cover", cards[0]["kind"])
        self.assertEqual("poster", cards[1]["kind"])
        self.assertIn("video-edit", str(cards[1]["id"]))

    def test_repeated_paste_replaces_instead_of_duplicating(self) -> None:
        getter, _calls = self._getter()
        candidates, _rejects = parse_skill_links("https://github.com/acme/toolkit/tree/main/video-edit")
        first = add_manual_skills(self._snapshot(), candidates, getter=getter)
        second = add_manual_skills(first, candidates, getter=getter)
        self.assertEqual(1, len(second["skills"]))
        self.assertEqual(1, len(second["manual_skills"]))

    def test_paste_works_without_a_search_and_without_a_model(self) -> None:
        getter, _calls = self._getter()
        candidates, _rejects = parse_skill_links("https://github.com/acme/toolkit/tree/main/video-edit")
        result = add_manual_skills(None, candidates, getter=getter)
        self.assertEqual(1, len(result["skills"]))
        self.assertEqual("skills_ready", result["phase"])
        # 没有检索也要有一套完整工作流，而且粘贴的 skill 落在真实环节里
        # （1.4.0 之前这里会是一个没有检索计划时硬造的「手动补充」桶）。
        stage_names = [str(stage["name"]) for stage in result["stages"]]
        self.assertEqual([name for _sid, name, _tokens in WORKFLOW_STAGES], stage_names)
        self.assertEqual("视频与剪辑", result["skills"][0]["stage_name"])
        self.assertNotIn(MANUAL_STAGE_NAME, stage_names)
        self.assertTrue(str(result["article"]["body"]).strip())
        self.assertIn("video-edit", str(result["article"]["body"]))

    def test_paste_writes_the_package_directory(self) -> None:
        getter, _calls = self._getter()
        candidates, _rejects = parse_skill_links("https://github.com/acme/toolkit/tree/main/video-edit")
        result = add_manual_skills(
            self._snapshot(), candidates, getter=getter, output_root=self.root / "skills"
        )
        package = Path(str(result["package_path"]))
        self.assertTrue(package.is_dir())
        self.assertIn("video-edit", (package / "article.md").read_text(encoding="utf-8"))
        self.assertTrue((package / "skills" / "acme-toolkit.md").is_file())

    def test_cap_keeps_pasted_skills_and_a_search_reinjects_them(self) -> None:
        manual = {
            "name": "video-edit",
            "source_id": "acme/toolkit",
            "manual": True,
            "stage_id": "edit",
            "skill_md": "# video-edit\n\n剪辑。\n",
        }
        many = [
            {
                "name": f"skill-{index}",
                "source_id": f"acme/skill-{index}",
                "stage_id": "topic",
                "stars": 500 - index,
            }
            for index in range(24)
        ]
        kept = cap_skills(many + [manual], PODCAST_STAGES)
        self.assertEqual(16, len(kept))
        self.assertIn(manual, kept, msg="粘贴的 skill 不能被热度排序挤掉")

        reuses = 0

        def complete(prompt: str) -> str:
            nonlocal reuses
            reuses += 1
            return _coverage_json()

        def getter(_url: str) -> object:
            raise RuntimeError("github_fetch_failed:offline")

        result = find_skills(
            "从零做播客",
            complete=complete,
            github_get=getter,
            web_search=lambda _query: [],
            output_root=self.root / "skills",
            previous={"manual_skills": [manual], "query": "从零做播客"},
        )
        names = [str(row["name"]) for row in result["skills"]]
        self.assertIn("video-edit", names, msg="再查一次也要保住粘贴的 skill")
        manual_rows = [row for row in result["skills"] if row.get("manual")]
        self.assertEqual(1, len(manual_rows))
        self.assertGreaterEqual(reuses, 1)

    def test_stage_matching_puts_a_pasted_skill_in_its_coverage_stage(self) -> None:
        getter, _calls = self._getter()
        candidates, _rejects = parse_skill_links("https://github.com/acme/toolkit/tree/main/video-edit")
        snapshot = self._snapshot()
        snapshot["stages"] = [
            {"id": "edit", "name": "剪辑", "queries": ["剪掉废话"], "required": True},
        ]
        result = add_manual_skills(snapshot, candidates, getter=getter)
        self.assertEqual("edit", result["skills"][0]["stage_id"])
        self.assertEqual("剪辑", result["skills"][0]["stage_name"])


class _MemoryCredentialStore:
    """Offline stand-in for the macOS keychain used by the web API tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, endpoint_id: str) -> str | None:
        return self.values.get(endpoint_id)

    def set(self, endpoint_id: str, api_key: str) -> None:
        self.values[endpoint_id] = api_key

    def delete(self, endpoint_id: str) -> None:
        self.values.pop(endpoint_id, None)


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
        self.credentials = _MemoryCredentialStore()
        self.fetched: list[str] = []

        def github_get(url: str) -> object:
            self.fetched.append(url)
            if url.endswith("/video-edit/SKILL.md"):
                return "# video-edit\n\n剪掉废话，按短视频节奏重排时间线。\n"
            if url.endswith("/repos/acme/toolkit"):
                return {
                    "full_name": "acme/toolkit",
                    "description": "视频工具箱",
                    "stargazers_count": 120,
                    "html_url": "https://github.com/acme/toolkit",
                    "pushed_at": "2026-09-01T00:00:00Z",
                    "owner": {"avatar_url": "https://avatars.example/acme.png"},
                }
            if urlparse(url).path == "/search/repositories":
                # 关键词检索（1.4.0 的默认路径）要能命中一个真的带 SKILL.md 的仓库。
                return {
                    "items": [
                        _repo_item(
                            "acme/video-edit",
                            stars=120,
                            description="剪掉废话，按短视频节奏重排时间线",
                            pushed_at="2026-09-01T00:00:00Z",
                        )
                    ]
                }
            raise RuntimeError("github_fetch_failed:404")

        self.app = WebApp(
            self.db,
            self.sources,
            self.root / "packages",
            fetcher=lambda _: b"",
            page_fetcher=lambda _: b"",
            github_fetcher=github_get,
            credential_store=self.credentials,
        )
        from copy_fixtures import attach_stub_poster_copy

        attach_stub_poster_copy(self.app)
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

    def test_paste_box_and_github_token_box_have_new_owners(self) -> None:
        """Pasted links are read on 资源探索; the token card lives in 设置."""
        html = INDEX_HTML
        # 资源探索 owns the inputs the studio used to read from itself.
        self.assertIn('id="resource-query"', html)
        self.assertIn('<textarea id="resource-pasted-text" rows="2" maxlength="20000"', html)
        self.assertIn('id="resource-studio-btn"', html)
        self.assertIn("function sendResourcesToStudio()", html)
        # Both entry points take their input as an argument — no DOM read inside.
        self.assertIn("function searchSkills(query)", html)
        self.assertIn("function pasteSkills(links)", html)
        self.assertIn(
            '$("resource-studio-btn").addEventListener("click", function () { sendResourcesToStudio(); });',
            html,
        )
        # The GitHub Token card exists only in 设置, and never echoes the token.
        self.assertIn('id="settings-github-token"', html)
        self.assertIn('id="settings-save-github-token"', html)
        self.assertIn('id="settings-clear-github-token"', html)
        self.assertIn('id="settings-github-status"', html)
        self.assertIn('$("settings-save-github-token").addEventListener("click"', html)
        self.assertIn('$("settings-clear-github-token").addEventListener("click"', html)
        self.assertIn("function renderGithubTokenState(", html)
        self.assertIn("/api/skills/github-token", html)
        self.assertIn("/api/skills/paste", html)
        # The studio is handed the resource-page values; it never reads a hidden field.
        handoff = html[
            html.index("function sendResourcesToStudio()") : html.index("function openWeeklyStudio()")
        ]
        self.assertIn('$("resource-query")', handoff)
        self.assertIn('$("resource-pasted-text")', handoff)
        self.assertIn("searchSkills(query)", handoff)
        self.assertIn("pasteSkills(links)", handoff)
        self.assertIn('status("studio-status"', handoff)
        # Pasting runs the same two steps a search does: package first, then posters.
        paste = html[html.index("function pasteSkills(") : html.index("function openSkillsLightbox(")]
        self.assertIn("renderSkillPackage(data)", paste)
        self.assertIn("renderSkillImages()", paste)
        self.assertIn("data.unverified", paste)
        self.assertIn("没读到 SKILL.md", paste)
        # 上限从 4000 提到 20000（MAX_PASTE_CHARS），客户端文案也要跟着说 20000。
        self.assertIn("links.length > 20000", paste)
        self.assertIn("粘贴内容过长", paste)
        self.assertIn("上限 20000", paste)
        # The retired lane DOM: every one of these ids is gone from the page.
        for gone in (
            'id="skill-paste-row"',
            'id="skill-links"',
            'id="skill-paste-btn"',
            'id="skill-paste-hint"',
            'id="skill-paste-status"',
            'id="skill-token-box"',
            'id="skill-token-save"',
            'id="skill-token-clear"',
            'id="skill-github-token"',
            'id="skill-search-row"',
            'id="skill-query"',
            'id="skill-search-btn"',
            'id="skill-clear-btn"',
            '$("skill-links")',
            '$("skill-query")',
            'id="skill-stage-chips"',
            'id="skill-gap-note"',
            "gapReason(",
            "function growSkillLinks()",
            "function resetSkillLinks()",
            "function setPasteStatus(",
        ):
            self.assertNotIn(gone, html, msg=f"{gone} 已从技能栏移除")
        # The merged source block: one chip per source, inside the visible studio.
        self.assertIn('<ul id="skills-source-chips"', html)
        self.assertIn("function renderSkillSourceChips(", html)
        source_fn = html[
            html.index("var SKILL_SOURCE_LABELS = {") : html.index("function skillIdentity(")
        ]
        self.assertIn("手动粘贴", source_fn)
        self.assertIn('"is-manual"', source_fn)
        self.assertIn('label + " " + counts[label]', source_fn)
        for label in ("GitHub", "skills.sh", "网页检索"):
            self.assertIn(label, source_fn)
        self.assertIn('id="skills-degraded-note"', html)
        # 标题框：既上封面海报也做介绍文标题。
        self.assertIn('id="skill-title"', html)
        self.assertIn("/api/skills/title", html)
        self.assertIn('$("skills-title").textContent = userTitle || articleTitle', html)
        # 清空收录 lives in the studio, next to 读取上次收录.
        self.assertIn('id="skills-clear-btn"', html)
        self.assertIn('$("skills-clear-btn").addEventListener("click", clearSkills);', html)
        self.assertIn('status("studio-status", "已清空收录的 skill（标题保留）"', html)
        self.assertIn("/api/skills/clear", html)

    def test_degraded_note_speaks_chinese_not_reason_tokens(self) -> None:
        """`github:rate_limited` must not reach the page as a raw token.

        The note is the visible #skills-degraded-note, next to the source chips.
        """
        import re

        html = INDEX_HTML
        self.assertIn('<p id="skills-degraded-note" role="status" hidden>', html)
        self.assertIn("function degradeLabel(", html)
        self.assertIn("function degradedSummary(", html)
        self.assertIn('"github:rate_limited"', html)
        self.assertIn("GitHub 接口限流", html)
        note_fn = html[
            html.index("function degradedSummary(") : html.index("var SKILL_SOURCE_LABELS = {")
        ]
        self.assertIn("githubTokenConfigured", note_fn)
        # Rate limiting points at the GitHub Token, and only when none is configured.
        self.assertIn(
            'reasons.indexOf("github:rate_limited") >= 0 && !state.githubTokenConfigured', note_fn
        )
        self.assertIn("GitHub Token", note_fn)
        render = html[html.index("function renderSkillPackage(") : html.index("function queuedSkillImages(")]
        self.assertIn("degradedSummary(reasons)", render)
        self.assertIn("degradedNote.title = reasons.join", render)
        # Visibility is driven, not decorative: the note is unhidden exactly when a
        # source degraded, and it is the Chinese summary that goes into it.
        self.assertIn("degradedNote.hidden = !degraded;", render)
        self.assertIn('degradedNote.textContent = degraded ? degradedSummary(reasons) : "";', render)
        # Every mapped reason renders Chinese; the raw `source:kind` token stays in title.
        labels_at = html.index("var DEGRADE_LABELS = {")
        labels = html[labels_at : html.index("};", labels_at)]
        values = re.findall(r':\s*"([^"]*)"', labels)
        self.assertTrue(values, msg="DEGRADE_LABELS 没有解析出任何映射")
        for value in values:
            self.assertNotIn(":", value, msg=f"原始降级记号漏进了用户可见文案：{value}")
            self.assertTrue(
                any(ord(char) > 127 for char in value), msg=f"降级文案不是中文：{value}"
            )

    def test_search_rejects_empty_query(self) -> None:
        status, payload = self._request("POST", "/api/skills/search", {"query": ""})
        self.assertEqual(400, status)
        self.assertFalse(payload["ok"])

    def test_paste_endpoint_adds_links_to_the_package(self) -> None:
        status, payload = self._request(
            "POST",
            "/api/skills/paste",
            {"links": "https://github.com/acme/toolkit/tree/main/video-edit"},
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["added"])
        self.assertEqual(1, payload["count"])
        skill = (payload.get("skills") or [])[0]
        self.assertEqual("video-edit", skill["name"])
        self.assertTrue(skill["manual"])
        self.assertIn("video-edit", str((payload.get("article") or {}).get("body")))
        # 1.4.0：粘贴的 skill 按内容归到最合适的工作流环节，不再统一丢进桶里。
        self.assertEqual("video", skill["stage_id"])
        self.assertEqual("视频与剪辑", skill["stage_name"])
        self.assertNotIn(MANUAL_STAGE_NAME, {str(row["name"]) for row in payload["stages"]})
        saved = self.app.database.get_skill_finder() or {}
        self.assertEqual(1, len(saved.get("manual_skills") or []))
        # The package directory is written too, so 出图 has a package to fill.
        self.assertTrue(Path(str(payload["package_path"])).is_dir())

    def test_paste_endpoint_reports_unusable_links(self) -> None:
        status, payload = self._request(
            "POST", "/api/skills/paste", {"links": "https://gitlab.com/a/b\n你好"}
        )
        self.assertEqual(200, status)
        self.assertEqual(0, payload["added"])
        reasons = {row["reason"] for row in payload.get("rejected") or []}
        self.assertEqual({"unsupported_host", "not_a_link"}, reasons)

    def test_paste_endpoint_validates_input(self) -> None:
        self.assertEqual(400, self._request("POST", "/api/skills/paste", {"links": ""})[0])
        self.assertEqual(400, self._request("POST", "/api/skills/paste", {"links": 3})[0])
        # 1.4.0：上限从 4000 提到 20000（MAX_PASTE_CHARS），4000 字已经合法…
        self.assertEqual(
            200, self._request("POST", "/api/skills/paste", {"links": "x" * 4001})[0]
        )
        # …真正越界才 400。
        status, payload = self._request("POST", "/api/skills/paste", {"links": "x" * 20001})
        self.assertEqual(400, status)
        self.assertIn("20000", str(payload["error"]))

    def test_paste_result_is_visible_on_the_studio_status_line(self) -> None:
        """The paste result reports into the shared #studio-status line.

        1.5.0 deletes the paste-only #skill-paste-status line: search, paste and
        render all report into the one status line the studio owns.
        """
        html = INDEX_HTML
        self.assertIn(
            '<p id="studio-status" class="page-description" role="status" aria-live="polite">', html
        )
        paste = html[html.index("function pasteSkills(") : html.index("function renderGithubTokenState(")]
        self.assertIn('status("studio-status"', paste)
        # The result names what happened and the package size it leaves behind.
        self.assertIn('parts.push("新增 " + added + " 个")', paste)
        self.assertIn('parts.push("替换 " + replaced + " 个")', paste)
        self.assertIn('"；当前共 " + count + " 个 skill"', paste)
        self.assertIn("没读到 SKILL.md", paste)
        # The length guard lives in the client because MAX_PASTE_CHARS is 20000.
        self.assertIn("links.length > 20000", paste)
        self.assertIn("粘贴内容过长", paste)
        # The dedicated line is retired: nothing may render into it.
        for gone in ('id="skill-paste-status"', "function setPasteStatus("):
            self.assertNotIn(gone, html, msg=f"{gone} 已删除，粘贴结果统一走 #studio-status")

    def test_render_status_updates_use_the_studio_status_line(self) -> None:
        """A render reports into #studio-status, the line the studio owns.

        Lanes are retired (no body.lane-* any more): the studio lives on the
        creation page and every render update goes to the one visible status line.
        """
        html = INDEX_HTML
        self.assertNotIn("body.lane-", html)
        render_fn = html[html.index("function renderSkillImages()") : html.index("function exportSkillImages()")]
        self.assertIn("var studioStatus = function", render_fn)
        self.assertIn('status("studio-status", text, kind);', render_fn)
        self.assertIn("studioStatus(", render_fn)
        self.assertIn("skill_package_changed", render_fn)
        self.assertIn("重新点「生成图片」", render_fn)
        # A failed render must also be visible on the tiles, not only on the line.
        self.assertIn("image.error = reason;", render_fn)
        self.assertIn("renderSkillsGallery();", render_fn)

    def test_paste_reports_links_without_a_readable_skill_file(self) -> None:
        """A pasted link whose SKILL.md cannot be read still enters, but is flagged."""
        status, payload = self._request(
            "POST", "/api/skills/paste", {"links": "https://github.com/acme/hidden"}
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["added"], msg="用户明确粘贴的链接不能静默丢弃")
        self.assertEqual(["hidden"], payload["unverified"])
        skill = (payload.get("skills") or [])[0]
        self.assertEqual("", skill["skill_excerpt"])
        self.assertTrue(skill["manual"])

    def test_paste_endpoint_caps_one_paste_at_the_package_skill_cap(self) -> None:
        links = "\n".join(f"https://github.com/acme/skill-{index}" for index in range(20))
        status, payload = self._request("POST", "/api/skills/paste", {"links": links})
        self.assertEqual(200, status)
        self.assertEqual(16, len(payload.get("manual_skills") or []))
        overflow = [row for row in payload.get("rejected") or [] if row["reason"] == "too_many"]
        self.assertEqual(4, len(overflow))

    def test_paste_is_refused_only_by_a_live_render(self) -> None:
        """A stale `rendering` phase (crash, restart) must not park the paste box."""
        self.app.database.save_skill_finder(
            {"query": "x", "phase": "rendering", "skills": [], "stages": [], "images": []}
        )
        status, payload = self._request(
            "POST",
            "/api/skills/paste",
            {"links": "https://github.com/acme/toolkit/tree/main/video-edit"},
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["added"])
        self.app._skill_render_active = True
        try:
            status, payload = self._request(
                "POST", "/api/skills/paste", {"links": "https://github.com/acme/hidden"}
            )
            self.assertEqual(400, status)
            self.assertEqual("skill_render_running", payload["error"])
        finally:
            self.app._skill_render_active = False
        self.assertIn("skill_render_running", INDEX_HTML)

    def test_failed_render_resets_the_phase_for_the_next_paste(self) -> None:
        """A render that blows up must not leave the package stuck in `rendering`."""
        package = self.root / "skills" / "pkg"
        (package / "images").mkdir(parents=True)
        self.app.database.save_skill_finder(
            {
                "query": "从零做播客",
                "title": "技能包",
                "stages": [{"id": "topic", "name": "选题"}],
                "skills": [{"name": "s", "source_id": "acme/s", "stage_id": "topic"}],
                "article": {"title": "t", "body": "b\nAIGC"},
                "images": [],
                "package_path": str(package),
                "phase": "skills_ready",
            }
        )

        def exploding_renderer(repos, jobs, out_dir, theme="dark", palette=None, **kwargs):
            raise RuntimeError("boom")

        self.app.visual_renderer = exploding_renderer
        with self.assertRaises(RuntimeError):
            self.app.skills_render({})
        saved = self.app.database.get_skill_finder() or {}
        self.assertEqual("skills_ready", saved.get("phase"))
        self.assertIn("boom", str(saved.get("render_error") or ""))
        status, payload = self._request(
            "POST", "/api/skills/paste", {"links": "https://github.com/acme/hidden"}
        )
        self.assertEqual(200, status, msg="失败的出图不能把粘贴入口锁死")
        self.assertEqual(2, payload["count"])

    def test_render_refuses_to_overwrite_a_package_that_moved(self) -> None:
        """A paste during a render must win; the stale render must not delete it."""
        self.app.database.save_skill_finder(
            {"query": "q", "phase": "skills_ready", "skills": [], "stages": ["x"], "images": [], "revision": 3}
        )
        stale_revision = self.app._skill_revision()
        status, payload = self._request(
            "POST",
            "/api/skills/paste",
            {"links": "https://github.com/acme/toolkit/tree/main/video-edit"},
        )
        self.assertEqual(200, status)
        self.assertEqual(4, payload["revision"], msg="粘贴必须推进 revision")
        with self.assertRaises(ValueError):
            self.app._save_skill_render_fields(
                stale_revision, {"images": [{"id": "cover"}]}, expected_phase="done"
            )
        saved = self.app.database.get_skill_finder() or {}
        self.assertEqual(["video-edit"], [row["name"] for row in saved.get("skills") or []])
        self.assertEqual("skills_ready", saved.get("phase"))

    def test_github_token_endpoint_stores_never_echoes_and_clears(self) -> None:
        status, payload = self._request(
            "POST", "/api/skills/github-token", {"token": "ghp_secret_value"}
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["configured"])
        self.assertNotIn("ghp_secret_value", json.dumps(payload))
        self.assertEqual(
            "ghp_secret_value", self.credentials.values.get("github:token")
        )
        from autoposting.github_weekly import github_token

        self.assertEqual("ghp_secret_value", github_token())
        snapshot = self.app.skills_snapshot()
        self.assertTrue(snapshot["github_token"])
        self.assertNotIn("ghp_secret_value", json.dumps(snapshot))

        status, payload = self._request("POST", "/api/skills/github-token", {"token": ""})
        self.assertEqual(200, status)
        self.assertFalse(payload["configured"])
        self.assertNotIn("github:token", self.credentials.values)
        self.assertEqual("", github_token())
        self.assertFalse(self.app.skills_snapshot()["github_token"])
        self.assertEqual(
            400, self._request("POST", "/api/skills/github-token", {"token": 5})[0]
        )

    def test_search_without_a_model_returns_200_and_a_mechanical_intro(self) -> None:
        # 旧用例（test_search_returns_502_when_llm_missing）的前提已经反转：1.4.0
        # 里模型只用来写介绍文，没配模型时检索照样成功，介绍文回落到
        # _fallback_article。502 / skill_search_failed 不该再出现。
        self.assertIsNone(self.app.skill_complete)
        # 离线：默认的网页检索会真的去打 DuckDuckGo，这里换成空结果。
        self.app.skill_web_search = lambda _query: []
        status, payload = self._request("POST", "/api/skills/search", {"query": "视频 剪辑"})
        self.assertEqual(200, status)
        self.assertNotEqual("error", payload.get("phase"))
        deadline = time.monotonic() + 30
        snapshot = payload
        while time.monotonic() < deadline:
            snapshot = self._request("GET", "/api/skills")[1]
            if snapshot.get("phase") in {"skills_ready", "done", "error"}:
                break
            time.sleep(0.05)
        self.assertEqual(
            "skills_ready", snapshot.get("phase"), msg=str(snapshot.get("error_reason") or snapshot)
        )
        skills = snapshot.get("skills") or []
        self.assertTrue(skills, msg=json.dumps(snapshot, ensure_ascii=False)[:400])
        self.assertEqual("video-edit", skills[0]["name"])
        body = str((snapshot.get("article") or {}).get("body") or "")
        self.assertIn("video-edit", body, msg="没有模型也要有机械介绍文")
        self.assertIn("AIGC", body)

    def test_studio_gallery_shows_without_autorender(self) -> None:
        """B3/D4: 查找 and 出图 are two separately controlled stages."""
        html = INDEX_HTML
        self.assertIn('id="skills-studio"', html)
        self.assertIn('id="skills-gallery"', html)
        # The studio is a laid-out block on the creation page, not a hidden lane.
        self.assertIn("#github-studio,#skills-studio{display:flex", html)
        self.assertIn('$("skills-gallery")', html)
        self.assertIn("skills_ready", html)
        self.assertIn("function renderSkillImages()", html)
        search_fn = html[html.index("function searchSkills(") : html.index("function renderSkillImages(")]
        # A finished search lays out placeholder tiles and stops there.
        self.assertNotIn("renderSkillImages()", search_fn)
        self.assertIn("queueSkillImagesForRender()", search_fn)
        self.assertIn('phase === "skills_ready"', html)
        # The only trigger for real rendering is the explicit button.
        self.assertIn('$("skills-render-btn").addEventListener("click", renderSkillImages);', html)
        # No per-tile re-render endpoint exists (D4: stage-level split only).
        self.assertNotIn("/api/skills/image", html)
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

    def test_skills_lane_puts_gallery_above_article(self) -> None:
        """B2/D3 (reversed decision): image area on top, article below.

        The original contract kept the article above the gallery; 1.3.0 reverses
        that so the skill lane matches the GitHub weekly lane (toolbar → gallery →
        text). The article stays fully readable because it is now a bounded
        internal scroll box, not because it sits first in the document.
        """
        html = INDEX_HTML
        self.assertIn('id="skills-article"', html)
        self.assertIn('id="skills-body"', html)
        self.assertLess(
            html.index('id="skills-gallery"'),
            html.index('id="skills-article"'),
            msg="D3: 图片区必须在介绍文上方",
        )
        gallery = html.index('id="skills-gallery"')
        toolbar = html.index('id="skills-render-btn"')
        self.assertIn('id="skills-cover-btn"', html)
        self.assertIn("function renderSkillCoverOnly()", html)
        self.assertIn("cover_only: true", html)
        self.assertLess(toolbar, gallery, msg="工具栏必须紧贴画廊上方")
        self.assertNotIn('id="skills-article"', html[toolbar:gallery])
        article_css = html.split("#skills-article{")[1].split("}")[0]
        self.assertIn("max-height", article_css)
        self.assertIn("overflow-y:auto", article_css)
        self.assertIn("查找完成后会生成介绍文", html)
        poll_fn = html[html.index("function skillArticleReady(") : html.index("function searchSkills(")]
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

        def fake_render(repos, jobs, out_dir, theme="dark", palette=None, **kwargs):
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
        search = html[html.index("function searchSkills(") : html.index("function renderSkillImages()")]
        self.assertIn("abortSkillSearch", search)
        poll = html[html.index("function pollSkillSnapshot(") : html.index("function searchSkills(")]
        self.assertIn("abortSkillSearch", poll)
        banner_css = html[html.index("#wait-banner{") : html.index("#wait-jobs{") + 160]
        self.assertIn("pointer-events:none", banner_css)
        stop_css = html[html.index(".wait-stop{") : html.index(".wait-stop:hover")]
        self.assertIn("pointer-events:auto", stop_css)
        wait_css = html[html.index("#wait-banner{") : html.index("main{")]
        self.assertNotIn(".wait-meter", wait_css)
        self.assertNotIn(".wait-bar", wait_css)

    def test_poll_skill_snapshot_stops_at_terminal_phases(self) -> None:
        """搜索轮询只对「进行中」的阶段续轮询。

        1.4.0：skills_ready / done 是终态——介绍文在发布该阶段之前就已经写好，
        所以在终态之后还等 article.body 永远等不到（零结果的检索会一直转圈）。
        """
        import re

        html = INDEX_HTML
        start = html.index("var SKILL_POLL_PHASES = {")
        map_text = html[start : html.index("};", start)]
        phases = set(re.findall(r'"([a-z_]+)":\s*1', map_text))
        # 只有进行中的阶段（外加旧快照里可能残留的 1.3.0 阶段名）才续轮询。
        self.assertEqual(
            {
                "searching",
                "keywords",
                "search",
                "group",
                "compose",
                "rendering",
                "understanding",
                "chain",
                "decompose",
            },
            phases,
        )
        self.assertNotIn("skills_ready", phases)
        self.assertNotIn("done", phases)
        poll = html[html.index("function pollSkillSnapshot(") : html.index("function searchSkills(")]
        terminal = poll.index('if (phase === "skills_ready" || phase === "done") { return data; }')
        # 终态在「等介绍文」之前就直接返回，不再递归。
        self.assertLess(terminal, poll.index("if (skillArticleReady(data))"))
        self.assertIn("if (!phase || SKILL_POLL_PHASES[phase]) {", poll)
        self.assertLess(terminal, poll.index("if (!phase || SKILL_POLL_PHASES[phase]) {"))
        self.assertIn(
            "setTimeout(function () { pollSkillSnapshot(query).then(resolve, reject); }, 350);", poll
        )
        self.assertIn("skillArticleReady", poll)
        # B3: polling a finished search must not kick off image generation.
        self.assertNotIn("renderSkillImages()", poll)
        self.assertIn('phase === "error"', poll)
        # The query is carried through the recursion, not read from a hidden field.
        self.assertIn("pollSkillSnapshot(query)", poll)
        self.assertIn("function pollSkillSnapshot(query)", poll)
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
