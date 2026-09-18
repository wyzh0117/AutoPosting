"""The 1.4.0 skill-package contract: keyword search, aggregation, user title.

These tests cover the behaviour the skill finder lane promises after 1.4.0:

* a keyword search needs no model and scrapes the user's own words;
* the package accumulates, and 生成图片 renders everything it holds —
  searched skills and pasted links together — as one cover plus one poster each;
* the user's title reaches the cover poster and the intro panel;
* the lane reports the configured crawl sources, and the paste box takes a real
  multi-line paste;
* a screenshot failure is explained in Chinese instead of a raw Chrome log.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote

from copy_fixtures import attach_stub_poster_copy, stub_copy_writer
from autoposting.db import Database
from autoposting.models import Source
from autoposting.skill_finder import (
    MANUAL_STAGE_ID,
    MANUAL_STAGE_NAME,
    _best_stage,
    _keyword_stage_id,
    _url_path,
    aggregate_skills,
    apply_skill_selection,
    crawl_sources,
    extract_keywords,
    find_skills,
    keyword_coverage,
    fetch_manual_hits,
    render_skill_package,
    reset_skill_finder_state,
    skill_identity,
    workflow_stages,
)
from autoposting.visual import weekly as weekly_visual
from autoposting.visual.screenshot import explain_chrome_failure
from autoposting.web import INDEX_HTML, WebApp, create_server

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _offline_render(images: list[Path] | None = None):
    """A screenshot stand-in: no browser, no network, deterministic."""

    def shot(html_path: Path, png_path: Path, width=None, height=None) -> Path:
        png_path.write_bytes(FAKE_PNG)
        if images is not None:
            images.append(png_path)
        return png_path

    return shot


class _OfflineMarks:
    """Stop the poster pipeline from reaching GitHub for READMEs and avatars."""

    def __enter__(self):
        self._readme = weekly_visual.load_readme
        self._data_uri = weekly_visual.fetch_data_uri
        weekly_visual.load_readme = lambda *args, **kwargs: ""
        weekly_visual.fetch_data_uri = lambda *args, **kwargs: ""
        return self

    def __exit__(self, *exc) -> None:
        weekly_visual.load_readme = self._readme
        weekly_visual.fetch_data_uri = self._data_uri


class KeywordPlanTests(unittest.TestCase):
    """R5: the user's own keywords are the plan."""

    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_keywords_split_on_every_separator_the_box_accepts(self) -> None:
        self.assertEqual(["视频", "剪辑", "字幕", "封面"], extract_keywords("视频 剪辑、字幕\n封面"))
        self.assertEqual(["a", "b", "c"], extract_keywords("a, b/c"))
        # A single phrase is one keyword, not chopped into characters.
        self.assertEqual(["从零做一档播客"], extract_keywords("从零做一档播客"))
        # Order and de-duplication are stable, and the cap bounds one click.
        self.assertEqual(["x", "y"], extract_keywords("x y X Y"))
        self.assertEqual(6, len(extract_keywords(" ".join(f"k{i}" for i in range(12)))))

    def test_keyword_coverage_makes_one_stage_and_query_per_keyword(self) -> None:
        coverage = keyword_coverage("视频 剪辑")
        stages = coverage["stages"]
        self.assertEqual(["视频", "剪辑"], [stage["name"] for stage in stages])
        # Stage ids are derived from the keyword, not from its position: a
        # positional `kw1` would collide with the NEXT search's first keyword and
        # silently relabel a carried-over skill into that lane.
        self.assertEqual(2, len({stage["id"] for stage in stages}))
        for stage in stages:
            self.assertTrue(stage["id"].startswith("kw-"))
        again = keyword_coverage("视频 剪辑")
        self.assertEqual([row["id"] for row in stages], [row["id"] for row in again["stages"]])
        other = keyword_coverage("剪辑 视频")
        self.assertEqual(
            {row["id"] for row in stages}, {row["id"] for row in other["stages"]},
            msg="同一个关键词必须映射到同一个 lane，别的关键词不能借用它",
        )
        # A different keyword must NOT reuse another keyword's lane id.
        self.assertNotIn(
            keyword_coverage("字幕")["stages"][0]["id"],
            {row["id"] for row in stages},
        )
        self.assertEqual([["视频"], ["剪辑"]], [stage["queries"] for stage in coverage["stages"]])
        # A keyword the user typed is a wish, not a required production step: a
        # miss must not burn the second-pass retry budget on it.
        self.assertTrue(all(stage["required"] is False for stage in coverage["stages"]))

    def test_search_runs_with_no_model_and_scrapes_every_source(self) -> None:
        seen: list[str] = []

        def github_get(url: str) -> object:
            seen.append(url)
            raise RuntimeError("github_fetch_failed:404")

        def web_search(query: str) -> list[dict[str, str]]:
            seen.append("web:" + query)
            return []

        result = find_skills(
            "视频 剪辑",
            complete=None,
            github_get=github_get,
            web_search=web_search,
            output_root=self.root,
            # A real budget: with a tiny one the tail keyword legitimately loses
            # its slice, and this test is about the plan, not about starvation.
            fetch_deadline_seconds=30,
            max_fetches=60,
        )
        self.assertEqual("skills_ready", result["phase"])
        # No model at all: the mechanical intro still lands, nothing raises.
        self.assertIn("article", result)
        self.assertNotEqual("", result["article"]["title"])
        # The query lands percent-encoded inside the URL, so compare decoded.
        github_terms = [unquote(url) for url in seen if "search/repositories" in url]
        self.assertTrue(github_terms, msg="关键词必须真的去 GitHub 检索")
        for keyword in ("视频", "剪辑"):
            self.assertTrue(
                any(keyword in url for url in github_terms),
                msg=f"{keyword} 没有进入 GitHub 检索词",
            )
            self.assertTrue(
                any(url.startswith("web:") and keyword in url for url in seen),
                msg=f"{keyword} 没有进入网页检索词",
            )
        self.assertIn("skills.sh/api/search", " ".join(seen))

    def test_four_step_pipeline_and_workflow_buckets_are_not_searched(self) -> None:
        searched: list[str] = []

        def github_get(url: str) -> object:
            searched.append(url)
            raise RuntimeError("github_fetch_failed:404")

        result = find_skills(
            "视频",
            complete=None,
            github_get=github_get,
            web_search=lambda _query: [],
            output_root=self.root,
            fetch_deadline_seconds=5,
            max_fetches=6,
        )
        progress = result["progress"]
        self.assertEqual(4, progress["step_total"])
        self.assertEqual("compose", progress["step"])
        names = [stage["name"] for stage in result["stages"]]
        self.assertEqual("视频", names[0])
        for bucket in ("选题与检索", "图像与设计", "发布与自动化"):
            self.assertIn(bucket, names)
        # The fallback buckets exist to file results into, never to be queried.
        for url in searched:
            if "search/repositories" in url:
                self.assertNotIn("选题与检索", unquote(url))
                self.assertNotIn("图像与设计", unquote(url))

    def test_zero_result_search_still_writes_an_intro_body(self) -> None:
        """A finished search with no hits must not look "still running".

        The browser polls /api/skills until the search reports skills_ready; an
        empty article body after that phase would leave it polling a finished
        search forever.
        """
        result = find_skills(
            "绝无此物的一个词",
            complete=None,
            github_get=lambda url: (_ for _ in ()).throw(RuntimeError("github_fetch_failed:404")),
            web_search=lambda _query: [],
            output_root=self.root,
            fetch_deadline_seconds=5,
            max_fetches=8,
        )
        self.assertEqual("skills_ready", result["phase"])
        self.assertEqual([], [row for row in result["skills"] if not row.get("gap")])
        self.assertTrue(str(result["article"]["body"]).strip(), msg="零结果也要有介绍文正文")
        self.assertIn("还没有收录 skill", result["article"]["body"])

    def test_a_caller_may_supply_an_explicit_plan(self) -> None:
        plan = {
            "goal": "g",
            "title": "t",
            "theme": "x",
            "chain": [{"id": "c1", "name": "剪辑", "role": ""}],
            "stages": [{"id": "edit", "name": "剪辑", "queries": ["podcast edit"], "required": True}],
        }
        result = find_skills(
            "随便什么",
            complete=None,
            github_get=lambda url: (_ for _ in ()).throw(RuntimeError("github_fetch_failed:404")),
            web_search=lambda _query: [],
            output_root=self.root,
            fetch_deadline_seconds=4,
            max_fetches=4,
            coverage=plan,
        )
        self.assertEqual("剪辑", result["stages"][0]["name"])


class AggregateTests(unittest.TestCase):
    """R2: 生成图片 aggregates everything the package holds."""

    def test_union_of_searched_and_pasted_in_workflow_order(self) -> None:
        snapshot = {
            "stages": [{"id": "topic", "name": "选题"}],
            "skills": [
                {
                    "name": "topic-radar",
                    "source": "GitHub",
                    "source_id": "acme/topic-radar",
                    "description": "选题趋势",
                    "stage_id": "topic",
                    "stage_name": "选题",
                }
            ],
            "manual_skills": [
                {
                    "name": "video-edit",
                    "source": "skills.sh",
                    "source_id": "g/s/video-edit",
                    "description": "剪掉废话，按短视频节奏重排时间线",
                    "skill_md": "# video-edit\n\n视频剪辑\n",
                    "manual": True,
                    "stage_id": "",
                    "stage_name": "",
                },
                {
                    "name": "artifacts-builder",
                    "source": "GitHub",
                    "source_id": "anthropics/artifacts-builder",
                    "description": "Build web artifacts",
                    "manual": True,
                    "stage_id": MANUAL_STAGE_ID,
                    "stage_name": "手动补充",
                },
            ],
        }
        rows = aggregate_skills(snapshot)
        self.assertEqual(3, len(rows), msg="检索到的和粘贴的必须一起出图")
        by_name = {row["name"]: row for row in rows}
        # A pasted skill is filed into the production step its own material names,
        # not into the catch-all bucket.
        self.assertEqual("视频与剪辑", by_name["video-edit"]["stage_name"])
        # A skill that matches nothing keeps the bucket, under a copy-safe name.
        self.assertEqual(MANUAL_STAGE_NAME, by_name["artifacts-builder"]["stage_name"])
        self.assertEqual("通用工具", by_name["artifacts-builder"]["stage_name"])
        # Ranks are renumbered from the workflow order, not from the input order.
        self.assertEqual([1, 2, 3], [row["rank"] for row in rows])
        self.assertEqual("topic-radar", rows[0]["name"])

    def test_omitted_selection_keeps_every_skill(self) -> None:
        rows = [
            {"name": "keep-me", "source_id": "acme/keep-me"},
            {"name": "also", "source_id": "acme/also", "selected": True},
        ]
        picked = apply_skill_selection(rows, None)
        self.assertEqual(["keep-me", "also"], [row["name"] for row in picked])

    def test_explicit_false_is_dropped_when_ids_omitted(self) -> None:
        rows = [
            {"name": "keep-me", "source_id": "acme/keep-me"},
            {"name": "skip", "source_id": "acme/skip", "selected": False},
        ]
        picked = apply_skill_selection(rows, None)
        self.assertEqual(["keep-me"], [row["name"] for row in picked])

    def test_selected_ids_keep_only_matching_rows(self) -> None:
        rows = [
            {"name": "keep-me", "source_id": "acme/keep-me", "skill_path": "SKILL.md"},
            {"name": "skip", "source_id": "acme/skip", "skill_path": "SKILL.md"},
        ]
        picked = apply_skill_selection(rows, [skill_identity(rows[0])])
        self.assertEqual(["keep-me"], [row["name"] for row in picked])

    def test_empty_selected_ids_returns_nothing(self) -> None:
        rows = [{"name": "keep-me", "source_id": "acme/keep-me"}]
        self.assertEqual([], apply_skill_selection(rows, []))

    def test_a_pasted_skill_replaces_its_searched_twin(self) -> None:
        snapshot = {
            "skills": [
                {"name": "video-edit", "source": "GitHub", "source_id": "g/s", "stars": 3},
            ],
            "manual_skills": [
                {
                    "name": "video-edit",
                    "source": "skills.sh",
                    "source_id": "g/s",
                    "skill_md": "# video-edit\n\n视频剪辑\n",
                    "manual": True,
                }
            ],
        }
        rows = aggregate_skills(snapshot)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["manual"])

    def test_monorepo_siblings_keep_a_poster_each(self) -> None:
        """Two pasted skills from one repo are two skills, not one."""
        snapshot = {
            "skills": [
                {"name": "toolkit-a", "source": "GitHub", "source_id": "acme/toolkit", "stars": 50},
            ],
            "manual_skills": [
                {"name": "toolkit-a", "source": "GitHub", "source_id": "acme/toolkit",
                 "skill_path": "skills/a/SKILL.md", "description": "写作",
                 "skill_md": "# a\n\n写作\n", "manual": True},
                {"name": "toolkit-b", "source": "GitHub", "source_id": "acme/toolkit",
                 "skill_path": "skills/b/SKILL.md", "description": "视频剪辑",
                 "skill_md": "# b\n\n视频剪辑\n", "manual": True},
            ],
        }
        rows = aggregate_skills(snapshot)
        self.assertEqual(
            ["toolkit-a", "toolkit-b"], [row["name"] for row in rows],
            msg="同一 monorepo 的两个 skill 必须各出一张海报",
        )
        self.assertEqual(2, len({row["name"] for row in rows}))

    def test_render_produces_a_cover_per_skill_and_uses_the_user_title(self) -> None:
        snapshot = {
            "title": "模型写的标题",
            "user_title": "本周值得装的 3 个 Skill",
            "stages": [{"id": "topic", "name": "选题"}],
            "skills": [
                {"name": "topic-radar", "source": "GitHub", "source_id": "acme/topic-radar",
                 "description": "选题趋势", "stage_id": "topic", "stage_name": "选题"},
            ],
            "manual_skills": [
                {"name": "video-edit", "source": "skills.sh", "source_id": "g/s/video-edit",
                 "description": "短视频剪辑", "skill_md": "# 视频剪辑\n", "manual": True},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            with _OfflineMarks():
                cards = render_skill_package(
                    snapshot,
                    out,
                    screenshot_fn=_offline_render(),
                    copy_writer=stub_copy_writer,
                )
            kinds = [(card["id"], card["kind"]) for card in cards]
            self.assertEqual([("cover", "cover")], [row for row in kinds if row[1] == "cover"])
            self.assertEqual(2, len([row for row in kinds if row[1] == "poster"]))
            cover = (out / "cover.html").read_text(encoding="utf-8")
            self.assertIn("本周值得装的 3 个 Skill", cover)
            self.assertNotIn("模型写的标题", cover)

    def test_cover_only_rerender_updates_title_and_keeps_posters(self) -> None:
        snapshot = {
            "title": "旧标题",
            "user_title": "旧标题",
            "stages": [{"id": "topic", "name": "选题"}],
            "skills": [
                {"name": "topic-radar", "source": "GitHub", "source_id": "acme/topic-radar",
                 "description": "选题趋势", "stage_id": "topic", "stage_name": "选题"},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            with _OfflineMarks():
                first = render_skill_package(
                    snapshot,
                    out,
                    screenshot_fn=_offline_render(),
                    copy_writer=stub_copy_writer,
                )
            poster = next(card for card in first if card["kind"] == "poster")
            poster_html = (out / f"{poster['id']}.html").read_text(encoding="utf-8")
            snapshot["user_title"] = "只改封面的新标题"
            with _OfflineMarks():
                second = render_skill_package(
                    snapshot,
                    out,
                    screenshot_fn=_offline_render(),
                    cover_only=True,
                )
            self.assertEqual(["cover"], [card["kind"] for card in second])
            cover = (out / "cover.html").read_text(encoding="utf-8")
            self.assertIn("只改封面的新标题", cover)
            self.assertNotIn("旧标题", cover)
            self.assertTrue((out / f"{poster['id']}.html").is_file())
            self.assertEqual(poster_html, (out / f"{poster['id']}.html").read_text(encoding="utf-8"))


class SkillPackageApiTests(unittest.TestCase):
    """The lane as the browser drives it: paste, title, render, sources, clear."""

    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "autoposting.db")
        self.db.initialize()
        sources = (Source("lab", "AI Lab", "rss", "https://lab.example/feed.xml", "S1", ("ai",), True, 15),)
        for source in sources:
            self.db.upsert_source(source)
        self.calls: list[str] = []

        def github_get(url: str) -> object:
            self.calls.append(url)
            raise RuntimeError("github_fetch_failed:404")

        self.app = WebApp(
            self.db,
            sources,
            self.root / "packages",
            fetcher=lambda _url: b"",
            page_fetcher=lambda _url: b"",
            github_fetcher=github_get,
            credential_store=_MemoryCredentialStore(),
        )
        self.app.skill_screenshot = _offline_render()
        attach_stub_poster_copy(self.app)
        self.server = create_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _raw(self, path: str) -> tuple[int, bytes]:
        request = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _request(self, method: str, path: str, payload: object = None) -> tuple[int, dict[str, object]]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_paste_only_package_renders_cover_and_posters(self) -> None:
        status, pasted = self._request(
            "POST",
            "/api/skills/paste",
            {
                "links": (
                    "https://skills.sh/genmedia-labs/skills/video-edit\n"
                    "https://skills.sh/genmedia-labs/skills/ai-image-generation\n"
                )
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(2, pasted["count"])
        # Both links landed in a real production step, not the paste bucket.
        stages = {row["name"] for row in pasted["skills"]}
        self.assertNotIn(MANUAL_STAGE_NAME, stages, msg=f"粘贴的 skill 落进了兜底桶：{stages}")

        status, titled = self._request("POST", "/api/skills/title", {"title": "两个 Skill 搞定短视频"})
        self.assertEqual(200, status)
        self.assertEqual("两个 Skill 搞定短视频", titled["user_title"])

        with _OfflineMarks():
            status, rendered = self._request("POST", "/api/skills/render", {"theme": "dark"})
        self.assertEqual(200, status)
        self.assertEqual("done", rendered["phase"])
        self.assertEqual("", rendered["render_error"])
        images = rendered["images"]
        self.assertEqual("cover", images[0]["kind"])
        self.assertEqual(3, len(images), msg="封面 + 每个 skill 一张海报")
        for image in images:
            self.assertTrue(image["url"].startswith("/api/generated/skills/"))
        package = Path(str(rendered["package_path"]))
        cover = (package / "images" / "cover.html").read_text(encoding="utf-8")
        self.assertIn("两个 Skill 搞定短视频", cover)
        # The generated PNG is really served back over HTTP.
        status, body = self._raw(images[1]["url"])
        self.assertEqual(200, status)
        self.assertTrue(body.startswith(b"\x89PNG"), msg="海报必须能真的下载回来")

    def test_render_honors_selected_ids(self) -> None:
        status, pasted = self._request(
            "POST",
            "/api/skills/paste",
            {
                "links": (
                    "https://skills.sh/genmedia-labs/skills/video-edit\n"
                    "https://skills.sh/genmedia-labs/skills/ai-image-generation\n"
                )
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(2, pasted["count"])
        chosen = pasted["skills"][0]
        skipped = pasted["skills"][1]
        with _OfflineMarks():
            status, rendered = self._request(
                "POST",
                "/api/skills/render",
                {"theme": "dark", "selected_ids": [skill_identity(chosen)]},
            )
        self.assertEqual(200, status, msg=str(rendered))
        images = rendered["images"]
        self.assertEqual("cover", images[0]["kind"])
        self.assertEqual(2, len(images), msg="封面 + 只给勾选的 skill 一张海报")
        titles = [str(image.get("title") or image.get("id") or "") for image in images]
        self.assertTrue(any(str(chosen["name"]) in title for title in titles))
        self.assertFalse(any(str(skipped["name"]) in title for title in titles[1:]))
        saved_names = [row["name"] for row in rendered["skills"]]
        self.assertEqual(2, len(saved_names), msg="未勾选的 skill 仍留在列表里，只是不出图")

    def test_render_rejects_empty_selected_ids(self) -> None:
        status, pasted = self._request(
            "POST",
            "/api/skills/paste",
            {"links": "https://skills.sh/genmedia-labs/skills/video-edit\n"},
        )
        self.assertEqual(200, status)
        status, rendered = self._request(
            "POST", "/api/skills/render", {"theme": "dark", "selected_ids": []}
        )
        self.assertEqual(400, status)
        self.assertEqual("skill_none_selected", rendered.get("error"))

    def test_search_endpoint_works_without_a_model(self) -> None:
        status, payload = self._request("POST", "/api/skills/search", {"query": "视频 剪辑"})
        self.assertEqual(200, status, msg="关键词查找不该再要求先配模型")
        self.assertNotEqual("error", payload.get("phase"))
        # No model is configured on this app, so nothing may claim one was used.
        self.assertIsNone(self.app.skill_complete)

    def test_snapshot_reports_the_configured_crawl_sources(self) -> None:
        status, payload = self._request("GET", "/api/skills")
        self.assertEqual(200, status)
        sources = payload["crawl_sources"]
        self.assertEqual(4, len(sources))
        self.assertEqual(4, payload["crawl_source_count"])
        self.assertEqual({"github", "skills.sh", "known_roots", "web_search"}, {row["id"] for row in sources})
        for row in sources:
            self.assertEqual("1", row["enabled"])
            self.assertTrue(row["url"].startswith("http"))
            self.assertTrue(row["detail"])

    def test_title_survives_a_clear_and_is_validated(self) -> None:
        self._request("POST", "/api/skills/title", {"title": "留着这个标题"})
        self._request("POST", "/api/skills/paste", {"links": "https://github.com/acme/toolkit"})
        status, cleared = self._request("POST", "/api/skills/clear", {})
        self.assertEqual(200, status)
        self.assertEqual(0, cleared["count"])
        self.assertEqual("留着这个标题", cleared["user_title"])
        self.assertEqual(400, self._request("POST", "/api/skills/title", {"title": 5})[0])
        self.assertEqual(400, self._request("POST", "/api/skills/title", {"title": "x" * 121})[0])

    def test_multiline_paste_well_past_the_old_four_thousand_limit(self) -> None:
        blob = "\n".join(f"https://github.com/acme/tool-{index}" for index in range(140))
        self.assertGreater(len(blob), 4000, msg="这条用例要真的超过旧的 4000 字上限")
        status, payload = self._request("POST", "/api/skills/paste", {"links": blob})
        self.assertEqual(200, status, msg="多行粘贴不该被长度上限挡住")
        # The package cap still applies, and the overflow is reported, not silently
        # dropped or half-parsed.
        self.assertEqual(16, payload["count"])
        self.assertTrue([row for row in payload["rejected"] if row["reason"] == "too_many"])
        self.assertEqual(
            400, self._request("POST", "/api/skills/paste", {"links": "x" * 20001})[0]
        )


    def test_a_crashing_search_still_lands_on_an_error_phase(self) -> None:
        """A failed search must never leave the phase in-flight.

        The browser polls while the phase says searching; a thread that dies
        without writing `error` leaves the spinner running forever. The handler
        itself used to read the snapshot inside `except`, so a database hiccup
        there killed the thread and produced exactly that state — this makes the
        search thread's own reads fail and still expects a stored `error`.
        """
        import autoposting.web as web_module

        search_threads: set[int] = set()
        original_find = web_module.find_skills

        def boom(*_args: object, **_kwargs: object) -> object:
            search_threads.add(threading.get_ident())
            raise RuntimeError("exploding_search")

        web_module.find_skills = boom
        self.addCleanup(setattr, web_module, "find_skills", original_find)

        original_get = Database.get_skill_finder

        def flaky(db: Database) -> object:
            if threading.get_ident() in search_threads:
                raise RuntimeError("db_unavailable")
            return original_get(db)

        Database.get_skill_finder = flaky
        self.addCleanup(setattr, Database, "get_skill_finder", original_get)

        status, _payload = self._request("POST", "/api/skills/search", {"query": "视频"})
        self.assertEqual(200, status)
        deadline = time.time() + 20
        phase = ""
        while time.time() < deadline:
            phase = str((original_get(self.app.database) or {}).get("phase") or "")
            if phase == "error":
                break
            time.sleep(0.2)
        self.assertEqual("error", phase, msg="线程崩了也必须把 phase 落成 error")
        self.assertEqual(
            "RuntimeError", str((original_get(self.app.database) or {}).get("error") or "")
        )

    def test_paste_reject_detail_is_capped_but_the_count_is_exact(self) -> None:
        prose = " ".join(f"说明文字{index}" for index in range(300))
        links = "https://github.com/acme/toolkit\n" + prose
        status, payload = self._request("POST", "/api/skills/paste", {"links": links})
        self.assertEqual(200, status)
        self.assertEqual(1, payload["added"])
        self.assertLessEqual(len(payload["rejected"]), 20)
        self.assertGreater(payload["rejected_count"], 20)

    def test_the_rendered_sheet_carries_the_package_skills(self) -> None:
        self._request("POST", "/api/skills/paste", {"links": "https://github.com/acme/toolkit"})
        with _OfflineMarks():
            status, rendered = self._request("POST", "/api/skills/render", {})
        self.assertEqual(200, status)
        saved = self.db.get_skill_finder() or {}
        self.assertEqual(1, len(saved["skills"]))
        self.assertEqual(str(rendered["skills"][0]["source_id"]), str(saved["skills"][0]["source_id"]))


class CarryOverTests(unittest.TestCase):
    """Regressions found by adversarial review: a search must not eat the package.

    Every case here failed on the first 1.4.0 build. They all go through the real
    ``find_skills`` carry-over path (``_reuse_previous`` / ``_reinject_manual``),
    not through ``aggregate_skills`` with a hand-built fixture, because that is
    where the data loss lived.
    """

    def setUp(self) -> None:
        reset_skill_finder_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _search(self, query: str, previous: dict[str, object] | None = None) -> dict[str, object]:
        return find_skills(
            query,
            complete=None,
            github_get=lambda url: (_ for _ in ()).throw(RuntimeError("github_fetch_failed:404")),
            web_search=lambda _query: [],
            output_root=self.root / "out",
            fetch_deadline_seconds=6,
            max_fetches=10,
            previous=previous,
        )

    @staticmethod
    def _pasted(names: list[str], source_id: str, *, source: str = "GitHub") -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "source": source,
                "source_id": source_id,
                "full_name": source_id,
                "skill_path": f"skills/{name}/SKILL.md",
                "description": f"{name} 的说明",
                "manual": True,
                "pushed_at": "2026-09-01T00:00:00Z",
            }
            for name in names
        ]

    def test_pasted_monorepo_siblings_survive_a_search(self) -> None:
        """Four pasted skills of one repo must all still be there after a search."""
        names = ["xlsx", "algorithmic-art", "pdf", "docx"]
        rows = self._pasted(names, "anthropics/skills")
        snapshot = {"skills": [dict(row) for row in rows], "manual_skills": rows, "query": ""}
        result = self._search("字幕", previous=snapshot)
        kept = sorted(str(row.get("name")) for row in result["skills"])
        self.assertEqual(sorted(names), kept, msg=f"搜索吃掉了粘贴的兄弟 skill：{kept}")
        self.assertEqual(sorted(names), sorted(str(row.get("name")) for row in result["manual_skills"]))

    def test_same_name_skills_from_different_repos_both_survive(self) -> None:
        rows = self._pasted(
            ["ai-image-generation"], "101-skills/superpowers/ai-image-generation", source="skills.sh"
        ) + self._pasted(
            ["ai-image-generation"], "genmedia-labs/skills/ai-image-generation", source="skills.sh"
        )
        snapshot = {"skills": [dict(row) for row in rows], "manual_skills": rows, "query": ""}
        result = self._search("配音", previous=snapshot)
        ids = {str(row.get("source_id")) for row in result["skills"]}
        self.assertIn("101-skills/superpowers/ai-image-generation", ids)
        self.assertIn("genmedia-labs/skills/ai-image-generation", ids)

    def test_a_second_search_cannot_erase_the_pasted_set(self) -> None:
        """Two searches in a row still keep the pasted slots (manual_skills persists)."""
        rows = self._pasted(["xlsx", "pdf"], "anthropics/skills")
        first = self._search("视频", previous={"skills": [dict(r) for r in rows], "manual_skills": rows})
        second = self._search("字幕", previous=first)
        self.assertEqual(
            ["pdf", "xlsx"], sorted(str(row.get("name")) for row in second["manual_skills"])
        )
        self.assertEqual(
            ["pdf", "xlsx"], sorted(str(row.get("name")) for row in second["skills"])
        )

    def test_carried_over_skill_is_not_relabelled_into_the_new_keyword(self) -> None:
        rows = self._pasted(["video-edit"], "genmedia-labs/skills/video-edit", source="skills.sh")
        # A row that already sits in the 视频 lane (as a real 视频 search would
        # leave it) must stay there while the same keyword is searched again...
        lane = _keyword_stage_id("视频")
        for row in rows:
            row["stage_id"] = lane
            row["stage_name"] = "视频"
        first = self._search("视频", previous={"skills": [dict(r) for r in rows], "manual_skills": rows})
        video = next(row for row in first["skills"] if row["name"] == "video-edit")
        self.assertEqual(lane, video["stage_id"], msg="同一个关键词重查不该把 skill 挪走")
        second = self._search("字幕", previous=first)
        subtitle_lane = next(stage["id"] for stage in second["stages"] if stage["name"] == "字幕")
        carried = next(row for row in second["skills"] if row["name"] == "video-edit")
        self.assertNotEqual(
            subtitle_lane, carried["stage_id"],
            msg="上一轮的 skill 被贴到了新关键词的 lane 上（位置式 kw1 冲突）",
        )
        self.assertEqual("视频与剪辑", carried["stage_name"])

    def test_the_skill_cap_is_reported_instead_of_silently_dropping_rows(self) -> None:
        """Accumulation is cap-bounded, and the bound must be visible.

        Pasted skills are pinned, so a full package makes room by dropping the
        lowest-popularity *searched* rows. Those may only disappear together with
        a `capped_dropped` note, never silently.
        """
        import autoposting.skill_finder as mod

        rows = self._pasted(["xlsx", "pdf"], "anthropics/skills")
        many = [
            {
                "name": f"searched-{index}",
                "source": "GitHub",
                "source_id": f"acme/searched-{index}",
                "full_name": f"acme/searched-{index}",
                "stars": index,
                "pushed_at": "2026-09-01T00:00:00Z",
                "skill_path": "SKILL.md",
            }
            for index in range(mod.SKILL_CAP + 4)
        ]
        snapshot = {"skills": many + [dict(r) for r in rows], "manual_skills": rows, "query": ""}
        result = self._search("字幕", previous=snapshot)
        self.assertEqual(mod.SKILL_CAP, result["skill_cap"])
        self.assertEqual(mod.SKILL_CAP, len(result["skills"]))
        dropped = result.get("capped_dropped") or []
        self.assertTrue(dropped, msg="超过上限时必须报告让位的检索结果")
        self.assertFalse(
            [name for name in dropped if name in {"xlsx", "pdf"}],
            msg="粘贴的 skill 不能被上限挤掉",
        )
        kept = {str(row.get("name")) for row in result["skills"]}
        self.assertTrue({"xlsx", "pdf"} <= kept)


class StageFilingTests(unittest.TestCase):
    """Stage assignment must use what a skill says it is, not document noise."""

    def test_document_body_noise_does_not_decide_the_stage(self) -> None:
        stages = workflow_stages()
        by_id = {str(stage["id"]): str(stage["name"]) for stage in stages}
        cases = [
            # A spreadsheet skill whose SKILL.md is full of "script"/"copy" words
            # was filed as a writing tool before the body signal was dropped.
            ({"name": "xlsx", "description": "Create and edit Excel spreadsheets",
              "skill_md": "Use this script to copy data into a workbook. report script"},
             "数据与复盘"),
            ({"name": "video-edit", "description": "剪掉废话，按短视频节奏重排时间线",
              "skill_md": "prompt script copy"}, "视频与剪辑"),
            ({"name": "ai-image-generation", "description": "批量生成封面和配图",
              "skill_md": "image prompt"}, "图像与设计"),
        ]
        for hit, expected in cases:
            stage_id, score = _best_stage(hit, stages)
            self.assertEqual(expected, by_id.get(stage_id, "(桶)"), msg=f"{hit['name']} 归错环节")
            self.assertGreater(score, 0)
        # Nothing in the name or blurb: the honest answer is the bucket.
        bucket_id, bucket_score = _best_stage({"name": "zzz", "description": "", "skill_md": "script data"}, stages)
        self.assertEqual(0, bucket_score)
        self.assertEqual("", bucket_id)

    def test_non_ascii_skill_paths_are_percent_encoded(self) -> None:
        seen: list[str] = []

        def getter(url: str) -> object:
            seen.append(url)
            raise RuntimeError("github_fetch_failed:404")

        hits, _skipped = fetch_manual_hits(
            [
                {
                    "source": "GitHub",
                    "source_id": "pluviobyte/rnskill",
                    "full_name": "pluviobyte/rnskill",
                    "name": "ai剪口播",
                    "url": "https://github.com/pluviobyte/rnskill",
                    "skill_path": "ai剪口播/SKILL.md",
                    "raw_url": "",
                }
            ],
            getter=getter,
        )
        urls = [url for url in seen if "raw.githubusercontent.com" in url]
        self.assertTrue(urls, msg="应该先探直连的 raw 地址")
        for url in urls:
            url.encode("ascii")  # raises UnicodeEncodeError if it was not encoded
        self.assertTrue(any("%E5%89%AA" in url for url in urls), msg=f"路径没有按百分号编码：{urls[:2]}")
        self.assertEqual(1, len(hits))
        self.assertEqual("ai剪口播", hits[0]["name"])

    def test_url_path_does_not_double_encode(self) -> None:
        self.assertEqual("ai%E5%89%AA/SKILL.md", _url_path("ai剪/SKILL.md"))
        self.assertEqual("already%20encoded/SKILL.md", _url_path("already%20encoded/SKILL.md"))
        self.assertEqual("a/b/SKILL.md", _url_path("a/b/SKILL.md"))


class LaneMarkupTests(unittest.TestCase):
    """R3/R4/R6/R7/R8 as they appear in the served page."""

    def test_skill_list_and_merged_source_block(self) -> None:
        self.assertIn('id="skill-list"', INDEX_HTML)
        self.assertNotIn('id="skill-stage-chips"', INDEX_HTML)
        self.assertNotIn('id="skill-gap-note"', INDEX_HTML)
        self.assertIn('id="skill-source-chips"', INDEX_HTML)
        self.assertIn("renderSkillSourceChips", INDEX_HTML)
        self.assertIn("function renderSkillList", INDEX_HTML)

    def test_paste_box_is_a_growing_textarea(self) -> None:
        self.assertIn('<textarea id="skill-links" rows="3"', INDEX_HTML)
        self.assertNotIn('<input id="skill-links"', INDEX_HTML)
        self.assertIn("function growSkillLinks()", INDEX_HTML)
        self.assertIn("Math.min(Math.max(box.scrollHeight, 76)", INDEX_HTML)
        self.assertIn("$(\"skill-links\").addEventListener(\"input\", growSkillLinks)", INDEX_HTML)
        # Plain Enter must insert a newline; the shortcut takes a modifier.
        self.assertIn('event.key === "Enter" && (event.metaKey || event.ctrlKey)', INDEX_HTML)

    def test_title_box_travels_to_the_cover_and_the_text_area(self) -> None:
        self.assertIn('id="skill-title"', INDEX_HTML)
        self.assertIn("/api/skills/title", INDEX_HTML)
        self.assertIn('title: String((($("skill-title") || {}).value) || "").trim()', INDEX_HTML)
        self.assertIn('$("skills-title").textContent = userTitle || articleTitle', INDEX_HTML)

    def test_status_line_offers_the_crawl_source_list(self) -> None:
        self.assertIn('id="skill-source-toggle"', INDEX_HTML)
        self.assertIn('id="skill-crawl-sources"', INDEX_HTML)
        self.assertIn("当前自动抓取 skill 的源：", INDEX_HTML)
        self.assertIn('$("skill-source-toggle").addEventListener("click", toggleCrawlSources)', INDEX_HTML)

    def test_lane_text_describes_keyword_search(self) -> None:
        self.assertIn("输入关键词（空格分隔多个）", INDEX_HTML)
        self.assertNotIn("按覆盖地图", INDEX_HTML)


class ScreenshotMessageTests(unittest.TestCase):
    def test_chrome_log_is_translated_into_something_actionable(self) -> None:
        raw = (
            "[0917/022706.649199:ERROR:chrome/app/chrome_main.cc:242] "
            "Failed to create a unique user data directory for headless."
        )
        message = explain_chrome_failure(raw)
        self.assertIn("AutoPosting服务控制.command", message)
        self.assertNotIn("chrome_main.cc", message)
        self.assertEqual(
            "Chrome 没能完成截图",
            explain_chrome_failure(""),
        )
        self.assertIn("some noise", explain_chrome_failure("some noise"))

    def test_crawl_source_helper_reflects_the_token_state(self) -> None:
        plain = crawl_sources(github_token=False)
        with_token = crawl_sources(github_token=True)
        self.assertEqual([row["id"] for row in plain], [row["id"] for row in with_token])
        self.assertIn("60", plain[0]["detail"])
        self.assertIn("5000", with_token[0]["detail"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
