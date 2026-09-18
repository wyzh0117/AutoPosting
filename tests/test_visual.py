import json
import tempfile
import unittest
from pathlib import Path

from autoposting.visual.pipeline import (
    PNG_HEIGHT,
    PNG_WIDTH,
    render_examples,
    render_page,
)
from autoposting.visual.rules import apply_project_rules, apply_ranking_rules, format_compact_number
from autoposting.visual.schema import ContentValidationError, load_example, validate_content
from autoposting.visual.tokens import load_tokens, resolve_tokens, token


class DesignTokenTests(unittest.TestCase):
    def test_canvas_is_fixed_3_by_4(self) -> None:
        tokens = load_tokens()
        self.assertEqual(1080, tokens["canvas"]["width"])
        self.assertEqual(1440, tokens["canvas"]["height"])
        self.assertEqual("3:4", tokens["canvas"]["aspect"])
        self.assertEqual(1080, PNG_WIDTH)
        self.assertEqual(1440, PNG_HEIGHT)
        self.assertEqual("#0B0D12", token("color.background"))
        self.assertEqual(16, token("radius.card"))
        self.assertEqual(100, tokens["typography"]["h1"]["size"])
        self.assertEqual(58, tokens["typography"]["h2"]["size"])

    def test_light_theme_switches_surface_group(self) -> None:
        dark = resolve_tokens("dark")
        light = resolve_tokens("light")
        self.assertEqual("#0B0D12", dark["color"]["background"])
        self.assertNotEqual(dark["color"]["background"], light["color"]["background"])
        self.assertNotEqual(dark["color"]["text"]["primary"], light["color"]["text"]["primary"])
        html = render_page(load_example("ranking"), screenshot=False, tokens=light)["html_text"]
        self.assertIn(light["color"]["background"], html)

    def test_palette_overrides_main_secondary_accent(self) -> None:
        tokens = resolve_tokens(
            "dark",
            {"main": "#112233", "secondary": "#334455", "accent": "#FF6600"},
        )
        self.assertEqual("#112233", tokens["color"]["background"])
        self.assertEqual("#334455", tokens["color"]["surface"])
        self.assertEqual("#FF6600", tokens["color"]["primary"])
        self.assertEqual("#FF6600", tokens["color"]["progressFill"])
        html = render_page(load_example("ranking"), screenshot=False, tokens=tokens)["html_text"]
        self.assertIn("#112233", html)
        self.assertIn("#FF6600", html)

    def test_weekly_palette_reaches_template_and_cover_contains_all_repos(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        repos = [
            {"rank": index, "name": f"repo-{index}", "kind": "CLI", "summary": "summary", "stars": index}
            for index in range(1, 11)
        ]
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        render_weekly_visual(
            repos,
            [{"id": "cover", "kind": "cover"}],
            temp,
            screenshot=False,
            palette={"main": "#112233", "secondary": "#334455", "accent": "#FF6600"},
        )
        html = (temp / "cover.html").read_text(encoding="utf-8")
        body = html.split("<body>", 1)[-1]
        self.assertEqual(10, body.count('class="c-smart-cover-item"'))
        self.assertIn("#112233", html.lower())
        self.assertIn("#334455", html.lower())
        self.assertIn("#ff6600", html.lower())


class ContentSchemaTests(unittest.TestCase):
    def test_example_ranking_validates(self) -> None:
        payload = load_example("ranking")
        validate_content("ranking", payload)
        self.assertEqual("ranking-overview", payload["page"])
        self.assertGreaterEqual(len(payload["items"]), 3)
        self.assertNotIn("agency-agents", json.dumps(load_tokens()))

    def test_example_project_validates(self) -> None:
        payload = load_example("project")
        validate_content("project", payload)
        self.assertEqual("agency-agents", payload["name"])
        self.assertIsInstance(payload["features"], list)

    def test_missing_title_is_rejected(self) -> None:
        payload = dict(load_example("ranking"))
        payload.pop("title")
        with self.assertRaises(ContentValidationError):
            validate_content("ranking", payload)

    def test_blank_lane_text_renders_but_missing_title_still_fails(self) -> None:
        from autoposting.visual.adapter import ranking_from_repos
        from autoposting.visual.pipeline import render_page

        payload = ranking_from_repos(
            [{"rank": 1, "name": "alpha", "summary": "s"}],
            brand="",
            series="",
            issue="",
            title="",
            subtitle="",
        )
        html = render_page(payload, screenshot=False)["html_text"]
        self.assertIn("c-ranking-item", html)
        broken = dict(payload)
        broken.pop("title")
        with self.assertRaises(ContentValidationError):
            render_page(broken, screenshot=False)


class RenderingRuleTests(unittest.TestCase):
    def test_compact_number_and_progress(self) -> None:
        self.assertEqual("11k", format_compact_number(11000))
        payload = apply_ranking_rules(
            {
                "page": "ranking-overview",
                "brand": "AutoPosting",
                "series": "GitHub 本周爆火 AI 项目榜",
                "issue": "第1期",
                "dateRange": "2026-09-01 — 2026-09-07",
                "title": "GitHub",
                "subtitle": "本周爆火 AI 项目榜",
                "items": [
                    {
                        "rank": 1,
                        "name": "alpha",
                        "description": "d",
                        "metric": {"label": "Stars", "value": 20000},
                    },
                    {
                        "rank": 2,
                        "name": "beta",
                        "description": "d",
                        "metric": {"label": "Stars", "value": 10000},
                    },
                ],
                "tags": [],
            }
        )
        self.assertEqual("20k", payload["items"][0]["metric"]["display"])
        self.assertAlmostEqual(1.0, payload["items"][0]["progress"])
        self.assertAlmostEqual(0.5, payload["items"][1]["progress"])

    def test_drops_items_beyond_ten_and_clips_long_copy(self) -> None:
        items = [
            {
                "rank": index + 1,
                "name": "n",
                "description": "这是一句会被裁切的非常长的项目介绍" * 6,
                "metric": {"label": "Stars", "value": 100 - index},
            }
            for index in range(12)
        ]
        payload = apply_ranking_rules(
            {
                "page": "ranking-overview",
                "brand": "B",
                "series": "S",
                "issue": "1",
                "dateRange": "d",
                "title": "T",
                "subtitle": "U",
                "items": items,
                "tags": [],
            }
        )
        self.assertEqual(10, len(payload["items"]))
        self.assertLessEqual(len(payload["items"][0]["description"]), 42)

    def test_project_rules_format_url_and_features(self) -> None:
        payload = apply_project_rules(
            {
                "page": "project-detail",
                "brand": "AutoPosting",
                "series": "S",
                "issue": "1",
                "pageNumber": "02 / 11",
                "rank": 1,
                "category": "AI Agent / 多智能体",
                "name": "agency-agents",
                "tagline": "把一个 AI 团队塞进电脑里",
                "keyMessage": "将多个不同职责的 AI Agent 组织成协作团队。",
                "features": ["a", "b", "c", "d", "e", "f", "g"],
                "workflow": ["接任务", "分工", "协作", "反馈"],
                "audience": ["研究者"],
                "url": "https://github.com/example/agency-agents/tree/main/very/long/path",
                "metrics": [{"label": "Stars", "value": 11000}],
            }
        )
        self.assertEqual(5, len(payload["features"]))
        self.assertEqual("11k", payload["metrics"][0]["display"])
        self.assertTrue(payload["urlDisplay"].endswith("…") or "github.com" in payload["urlDisplay"])


class BrandAndIssueTests(unittest.TestCase):
    def test_weekly_pages_use_author_id_avatar_and_iso_week(self) -> None:
        from datetime import datetime, timezone

        from autoposting.github_weekly import iso_week_issue
        from autoposting.visual.adapter import project_from_repo, ranking_from_repos
        from autoposting.visual.weekly import render_weekly_visual

        issue = iso_week_issue(datetime(2026, 9, 10, tzinfo=timezone.utc))
        self.assertEqual("2026年第37周", issue)
        ranking = ranking_from_repos(
            [{"name": "alpha", "rank": 1, "stars": 12, "summary": "s", "kind": "CLI"}],
            issue=issue,
            date_range="2026-09-03 — 2026-09-10",
        )
        self.assertEqual("EE_萤飞", ranking["brand"])
        self.assertEqual("2026年第37周", ranking["issue"])
        self.assertIn("data:image/png;base64,", ranking.get("avatar") or "")
        html = render_page(ranking, screenshot=False)["html_text"]
        self.assertIn("EE_萤飞", html)
        self.assertIn("2026年第37周", html)
        self.assertIn("c-brand-avatar", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertNotIn(">本周</div>", html)

        project = project_from_repo(
            {
                "rank": 1,
                "name": "alpha",
                "kind": "CLI",
                "description": "命令行调用模型",
                "summary": "在终端里直接调用模型拿结果。",
                "does": "把提示词发给模型。",
                "how": "在项目目录执行命令。",
                "audience": "习惯开终端的人。",
                "scene": "需要对着仓库立刻问模型的时候。",
                "url": "https://github.com/example/alpha",
            },
            issue=issue,
        )
        self.assertEqual("EE_萤飞", project["brand"])
        self.assertEqual("2026年第37周", project["issue"])
        project_html = render_page(project, screenshot=False)["html_text"]
        self.assertIn("EE_萤飞", project_html)
        self.assertIn("c-brand-avatar", project_html)

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        rendered = render_weekly_visual(
            [{"name": "alpha", "rank": 1, "stars": 12, "summary": "s", "kind": "CLI"}],
            [{"id": "cover", "kind": "cover", "rank": 0, "title": "cover"}],
            temp,
            screenshot=False,
        )
        self.assertTrue(rendered)
        cover_html = (temp / "cover.html").read_text(encoding="utf-8")
        self.assertIn("EE_萤飞", cover_html)
        self.assertIn(iso_week_issue(), cover_html)
        self.assertIn("c-brand-avatar", cover_html)

    def test_weekly_visual_uses_cover_canvas_and_alternating_poster_templates(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        repos = [
            {
                "rank": index,
                "name": f"sentinel-{index}",
                "kind": "CLI",
                "summary": "动态 sentinel",
                "description": "动态 sentinel",
                "does": "动态 sentinel",
                "how": "动态 sentinel",
                "scene": "动态 sentinel",
                "audience": "相关的人",
                "url": "https://github.com/acme/sentinel",
            }
            for index in range(1, 7)
        ]
        jobs = [
            {"id": "cover", "kind": "cover", "rank": 0},
            {"id": "01-sentinel-1", "kind": "poster", "rank": 1},
            {"id": "02-sentinel-2", "kind": "poster", "rank": 2},
        ]
        rendered = render_weekly_visual(repos, jobs, temp, screenshot=False)
        self.assertEqual([job["id"] for job in jobs], [row["id"] for row in rendered])
        cover = (temp / "cover.html").read_text(encoding="utf-8")
        odd = (temp / "01-sentinel-1.html").read_text(encoding="utf-8")
        even = (temp / "02-sentinel-2.html").read_text(encoding="utf-8")
        self.assertIn('class="page page-ranking template-cover"', cover)
        self.assertIn('class="page page-project template-poster-a"', odd)
        self.assertIn('class="page page-project template-poster-b"', even)
        self.assertIn("#06155a", odd)
        self.assertIn("clip-path", even)
        self.assertIn("sentinel-1", odd)
        self.assertIn("c-project-shell", odd)
        self.assertIn("c-project-shell", even)
        self.assertIn("c-bullet-panel", odd)
        self.assertIn("c-bullet-panel", even)
        for poster, selector in ((odd, ".template-poster-a.page-project"), (even, ".template-poster-b.page-project")):
            self.assertIn(selector, poster)
            self.assertIn("padding: 56px 48px 52px 48px;", poster)
            self.assertIn(".c-project-shell { padding: 0; }", poster)
        self.assertNotIn("Smart Credit Management", cover)
        self.assertNotIn('<article class="c-ranking-item">', cover)
        self.assertIn("c-smart-cover-list", cover)
        self.assertEqual(len(repos), cover.count('class="c-smart-cover-item"'))
        self.assertIn(".c-smart-cover-metric { text-align: right; color: #f3f0e8;", cover)
        self.assertNotIn("c-poster-a-lines", odd)
        self.assertNotIn("c-poster-b-info", even)

    def test_weekly_visual_uses_repo_rank_for_stale_or_missing_job_rank(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        repos = [
            {
                "rank": 1,
                "name": "repo-one",
                "kind": "CLI",
                "summary": "第一个项目的一句话。",
                "does": "完成第一项工作。",
                "how": "按说明执行。",
                "audience": "需要这一步的人。",
                "scene": "要做这一步的时候。",
                "description": "one",
                "url": "https://github.com/acme/repo-one",
            },
            {
                "rank": 2,
                "name": "repo-two",
                "kind": "CLI",
                "summary": "第二个项目的一句话。",
                "does": "完成第二项工作。",
                "how": "按说明执行。",
                "audience": "需要这一步的人。",
                "scene": "要做这一步的时候。",
                "description": "two",
                "url": "https://github.com/acme/repo-two",
            },
        ]
        jobs = [
            {"id": "legacy-one", "kind": "poster", "title": "repo-one"},
            {"id": "legacy-two", "kind": "poster", "rank": 99, "title": "repo-two"},
        ]
        render_weekly_visual(repos, jobs, temp, screenshot=False)
        self.assertIn("template-poster-a", (temp / "legacy-one.html").read_text(encoding="utf-8"))
        self.assertIn("template-poster-b", (temp / "legacy-two.html").read_text(encoding="utf-8"))

    def test_weekly_detail_reuses_all_project_modules_and_clamps_content(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        long_text = "超长动态内容 " * 80
        render_weekly_visual(
            [{
                "rank": 2,
                "name": "long-poster",
                "kind": "Agent",
                "summary": long_text,
                "description": long_text,
                "does": long_text,
                "how": long_text,
                "audience": long_text,
                "scene": long_text,
                "url": "https://github.com/acme/long-poster",
            }],
            [{"id": "02-long-poster", "kind": "poster", "rank": 2, "title": "long-poster"}],
            temp,
            screenshot=False,
        )
        html = (temp / "02-long-poster.html").read_text(encoding="utf-8")
        body = html.split("<body>", 1)[-1]
        self.assertIn("c-project-shell", body)
        self.assertEqual(4, body.count('class="c-panel c-bullet-panel'))
        for label in ("一句话看懂", "做什么？", "如何实现？", "适合哪些人群", "使用场景"):
            self.assertIn(label, body)
        self.assertIn("动态内容", body)
        self.assertIn('class="page page-project template-poster-b"', html)
        self.assertNotIn("c-poster-b-info", body)
        self.assertIn("-webkit-line-clamp: 3", html)
        self.assertIn("overflow: hidden", html)
        self.assertIn(".c-project-shell", html)
        self.assertIn("min-height: 0", html)
        self.assertRegex(html, r"\.c-bullet-panel[^{]*\{[^}]*overflow:\s*hidden")
        self.assertRegex(html, r"\.c-bullet-panel \.c-feature-list li \{[^}]*font-size: 1em")
        self.assertIn("function fitPosterCopy()", html)
        fit_js = html[html.index("function fitPosterCopy()") :]
        self.assertIn(".c-feature-list", fit_js)
        self.assertIn("width: 1080px;", html)


class HtmlRendererTests(unittest.TestCase):
    def test_ranking_html_uses_tokens_and_components(self) -> None:
        html = render_page(load_example("ranking"), screenshot=False)["html_text"]
        self.assertIn("c-brand-header", html)
        self.assertIn("c-ranking-item", html)
        self.assertIn("c-progress-bar", html)
        self.assertIn("--color-background", html)
        self.assertIn("#0B0D12", html)
        self.assertIn("GitHub", html)
        self.assertIn("本周爆火 AI 项目榜", html)
        self.assertIn("agency-agents", html)
        self.assertIn("1080px", html)
        self.assertIn("1440px", html)

    def test_project_html_contains_required_blocks(self) -> None:
        html = render_page(load_example("project"), screenshot=False)["html_text"]
        self.assertIn("c-quote-card", html)
        self.assertIn("c-feature-list", html)
        self.assertIn("c-bullet-panel", html)
        self.assertNotIn("c-bullet-featured", html)
        self.assertIn("agency-agents", html)
        self.assertIn("把一个 AI 团队塞进电脑里", html)
        self.assertIn("一句话看懂", html)
        self.assertIn("做什么", html)
        self.assertIn("如何实现", html)
        self.assertIn("适合哪些人群", html)
        self.assertIn("使用场景", html)
        self.assertIn("c-project-shell", html)
        self.assertIn("c-project-pair", html)
        self.assertIn("c-masthead", html)
        self.assertIn("c-masthead-logo", html)
        self.assertIn("c-feature-icon", html)
        self.assertIn("<svg", html)
        self.assertIn(".c-masthead-logo .c-project-logo", html)
        self.assertIn("border: none", html)
        body = html.split("<body>", 1)[-1]
        self.assertNotIn("c-hero-visual", body)
        self.assertNotIn("c-story", body)
        self.assertNotIn("功能示意", body)
        self.assertNotIn("c-metric-panel", body)
        self.assertNotIn("c-workflow", body)
        self.assertLess(body.find("一句话看懂"), body.find("做什么"))
        self.assertLess(body.find("做什么"), body.find("如何实现"))
        self.assertLess(body.find("如何实现"), body.find("适合哪些人群"))
        self.assertLess(body.find("适合哪些人群"), body.find("使用场景"))
        self.assertNotIn("开源仓库，安装步骤和文档在项目主页", html)
        self.assertNotIn("max-height: 36%", html)
        self.assertRegex(html, r"\.c-project-shell \{[^}]*justify-content: flex-start")
        self.assertRegex(html, r"\.c-project-pair \{[^}]*grid-template-columns: 1fr 1fr")
        self.assertRegex(html, r"\.c-bullet-panel \{[^}]*height: auto")

    def test_audience_and_scene_use_title_icons_not_row_icons(self) -> None:
        html = render_page(load_example("project"), screenshot=False)["html_text"]
        body = html.split("<body>", 1)[-1]
        panels = _bullet_panels(body)
        self.assertIn("做什么？", panels)
        self.assertIn("适合哪些人群", panels)
        self.assertIn("使用场景", panels)
        self.assertIn("c-feature-icon", panels["做什么？"]["items"])
        self.assertIn("<svg", panels["适合哪些人群"]["title"])
        self.assertIn("<svg", panels["使用场景"]["title"])
        self.assertNotEqual(panels["适合哪些人群"]["title"], panels["使用场景"]["title"])
        self.assertNotIn("c-feature-icon", panels["适合哪些人群"]["items"])
        self.assertNotIn("c-feature-icon", panels["使用场景"]["items"])
        self.assertIn("<li><span>", panels["适合哪些人群"]["items"])
        self.assertIn("<li><span>", panels["使用场景"]["items"])
        self.assertNotIn("  ", panels["适合哪些人群"]["items"])


class MarkCatalogTests(unittest.TestCase):
    def test_github_avatar_is_not_a_product_logo(self) -> None:
        from autoposting.visual.marks import is_author_avatar

        self.assertTrue(is_author_avatar("https://github.com/acme.png"))
        self.assertTrue(is_author_avatar("https://avatars.githubusercontent.com/u/12?v=4"))
        self.assertFalse(
            is_author_avatar("https://raw.githubusercontent.com/acme/tool/HEAD/docs/logo.png")
        )

    def test_catalog_reuses_tagged_svg_and_skips_stretched_tags(self) -> None:
        from autoposting.visual.marks import resolve_mark

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        first = resolve_mark(
            name="agency-agents",
            kind="Agent",
            topics=["agent", "multi-agent"],
            description="把不同 AI 角色组成团队",
            catalog_dir=temp,
        )
        self.assertTrue(first["svg"].lstrip().startswith("<svg"))
        self.assertIn("agent", first["tags"])
        self.assertNotIn("rag", first["tags"])
        second = resolve_mark(
            name="crew-lite",
            kind="Agent",
            topics=["agent"],
            description="多个助手一起协作",
            catalog_dir=temp,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["svg"], second["svg"])
        cli = resolve_mark(
            name="fast-cli",
            kind="CLI",
            topics=["cli"],
            description="在终端里调用模型",
            catalog_dir=temp,
        )
        self.assertNotEqual(first["id"], cli["id"])
        self.assertNotIn("agent", cli["tags"])
        self.assertIn("cli", cli["tags"])


class PosterCopyTests(unittest.TestCase):
    def test_generic_install_line_is_not_the_one_liner(self) -> None:
        from autoposting.visual.adapter import project_from_repo

        payload = project_from_repo(
            {
                "rank": 1,
                "name": "agency-agents",
                "kind": "Agent",
                "description": "把前端、运营、内容等 AI 角色组成一个小团队。",
                "summary": "把前端、运营、内容等 AI 角色组成一个小团队。",
                "does": "组织前端、运营、内容、社区等不同角色一起协作",
                "how": "开源仓库，安装步骤和文档在项目主页。",
                "scene": "本地把开发、内容和运营串成一条流水线",
                "icon_url": "https://github.com/acme.png",
                "url": "https://github.com/acme/agency-agents",
                "stars": 11000,
            }
        )
        self.assertNotIn("项目主页", payload["keyMessage"])
        self.assertNotIn("安装步骤", payload["keyMessage"])
        self.assertFalse(payload["keyMessage"].startswith("它"))
        self.assertIn("角色", payload["keyMessage"])
        self.assertFalse(payload.get("tagline"))
        self.assertFalse(payload.get("logo"))
        self.assertGreaterEqual(len(payload["features"]), 2)
        self.assertEqual(["开源仓库，安装步骤和文档在项目主页。"], payload["how"])
        self.assertGreaterEqual(len(payload["scene"]), 1)

    def test_long_does_becomes_short_bullets(self) -> None:
        from autoposting.visual.adapter import to_bullets

        items = to_bullets("组织前端角色，安排运营写内容，让社区机器人和开发助手一起协作")
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(all(len(item) <= 36 for item in items))

    def test_space_separated_cjk_becomes_one_item_per_line(self) -> None:
        from autoposting.visual.adapter import project_from_repo, to_bullets

        items = to_bullets("做架构设计的人 写技术文档的人 要快速上手的人")
        self.assertEqual(
            ["做架构设计的人", "写技术文档的人", "要快速上手的人"],
            items,
        )
        payload = project_from_repo(
            {
                "rank": 1,
                "name": "agency-agents",
                "kind": "Agent",
                "audience": "做架构设计的人 写技术文档的人 要快速上手的人",
                "scene": "本地把开发、内容和运营串成一条流水线 先用一个真实任务看完整协作",
            }
        )
        self.assertEqual(
            ["做架构设计的人", "写技术文档的人", "要快速上手的人"],
            payload["audience"],
        )
        self.assertTrue(all(" " not in item for item in payload["audience"]))
        self.assertGreaterEqual(len(payload["scene"]), 2)
        self.assertTrue(all(" " not in item for item in payload["scene"]))

    def test_to_bullets_does_not_invent_ellipsis(self) -> None:
        from autoposting.visual.adapter import to_bullets

        items = to_bullets("给不会命令行的同事把复杂操作收成按钮并当场演示带动效的页面")
        self.assertTrue(items)
        self.assertTrue(all("…" not in item and "..." not in item for item in items))
        self.assertTrue(any("带动效" in item for item in items))

    def test_adapter_keeps_ellipsis_and_english_and_it_led_lines(self) -> None:
        from autoposting.visual.adapter import project_from_repo, to_bullets

        self.assertEqual(["做带动效的演…"], to_bullets("做带动效的演…"))
        self.assertEqual(
            ["Agent skill for beautiful", "verifiable architecture"],
            to_bullets("Agent skill for beautiful, verifiable architecture"),
        )
        payload = project_from_repo(
            {
                "rank": 1,
                "name": "agency-agents",
                "kind": "Agent",
                "summary": "它把不同职责的角色编成一支小团队。",
                "does": "它把不同职责的角色编成一支小团队。\nIt organizes frontend agents.",
                "how": "先定角色和各自职责",
                "audience": "想搭小型 AI 团队的人",
                "scene": "做带动效的演…\n给不会命令行的同事试用",
            }
        )
        self.assertTrue(any(item.lstrip().startswith("它") for item in payload["features"]))
        self.assertTrue(any("It organizes" in item for item in payload["features"]))
        self.assertTrue(any("…" in item for item in payload["scene"]))

    def test_specific_chinese_summary_is_kept_without_identity_formula(self) -> None:
        from autoposting.visual.adapter import project_from_repo

        payload = project_from_repo(
            {
                "rank": 1,
                "name": "cutter",
                "kind": "视频",
                "summary": "把长素材按脚本切成能发的粗剪。",
                "description": "video cutter",
            }
        )
        self.assertEqual("把长素材按脚本切成能发的粗剪。", payload["keyMessage"])

    def test_empty_copy_is_not_filled_with_repo_name_formula(self) -> None:
        from autoposting.visual.adapter import project_from_repo

        one = project_from_repo({"rank": 1, "name": "alpha-cli", "kind": "CLI"})
        two = project_from_repo({"rank": 2, "name": "beta-cli", "kind": "CLI"})
        self.assertEqual("", one["keyMessage"])
        self.assertEqual("", two["keyMessage"])
        self.assertEqual([], one["features"])

    def test_markdown_markers_are_stripped_from_poster_fields(self) -> None:
        from autoposting.visual.adapter import project_from_repo

        payload = project_from_repo(
            {
                "rank": 1,
                "name": "cutter",
                "kind": "视频",
                "summary": "把**长素材**切开。",
                "does": "- 先**粗剪**。\n- 再导出成片。",
                "how": "# 步骤\n先把素材放进目录",
                "scene": "交期近的时候",
                "audience": "剪辑的人",
            }
        )
        self.assertNotIn("**", payload["keyMessage"])
        self.assertIn("长素材", payload["keyMessage"])
        joined = " ".join(payload["features"] + payload["how"])
        self.assertNotIn("**", joined)
        self.assertNotIn("# 步骤", joined)
        self.assertTrue(any("粗剪" in item for item in payload["features"]))

    def test_placeholder_one_liner_label_is_not_the_key_message(self) -> None:
        from autoposting.visual.adapter import project_from_repo

        payload = project_from_repo(
            {
                "rank": 1,
                "name": "cutter",
                "kind": "视频",
                "summary": "一句话看懂",
            }
        )
        self.assertNotEqual("一句话看懂", payload["keyMessage"])
        self.assertEqual("", payload["keyMessage"])

    def test_render_weekly_visual_cache_busts_image_url(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = Path(temp.name) / "github-weekly"
        rendered = render_weekly_visual(
            [
                {
                    "rank": 1,
                    "name": "demo-cli",
                    "kind": "CLI",
                    "owner": "",
                    "summary": "它可以帮你在终端里直接调用 AI，少绕一层界面。",
                    "does": "A tiny LLM CLI agent.",
                    "how": "装到本机命令行",
                    "audience": "习惯开终端的人。",
                    "scene": "需要对着仓库立刻问模型的时候。",
                    "description": "A tiny LLM CLI agent.",
                    "url": "https://github.com/acme/demo-cli",
                }
            ],
            [{"id": "01-demo-cli", "kind": "poster", "rank": 1, "title": "demo-cli"}],
            out,
            screenshot=False,
        )
        self.assertTrue(rendered[0].get("url"))
        self.assertIn("?v=", str(rendered[0]["url"]))
        html = (out / "01-demo-cli.html").read_text(encoding="utf-8")
        self.assertIn("终端", html)


class FeatureIconTests(unittest.TestCase):
    def test_mixed_panel_bullets_use_distinct_glyphs(self) -> None:
        from autoposting.visual.icons import feature_icon

        labels = [
            "把不同职责的助手编成一支能一起干活的小团队。",
            "在终端里直接跟模型说话，不必再绕一层网页。",
            "把文档放进去之后，可以用大白话追问里面写了什么。",
            "用几行代码把 AI 接到自己的程序里。",
            "把 AI 能力收成网页上的按钮，打开就能用。",
            "把模型在自己的机器上跑起来、试起来。",
            "提供能直接练、能直接评的样本。",
            "把 AI 接到已经在用的工具里，不用换窗口。",
        ]
        glyphs = [_icon_glyph(feature_icon(label, f"panel-{index}")) for index, label in enumerate(labels)]
        self.assertGreaterEqual(len(set(glyphs)), 6)

    def test_generic_fallback_glyphs_vary_by_seed(self) -> None:
        from autoposting.visual.icons import feature_icon

        first = _icon_glyph(feature_icon("暂无匹配关键词的句子。", "做什么？-0"))
        second = _icon_glyph(feature_icon("暂无匹配关键词的句子。", "做什么？-1"))
        self.assertNotEqual(first, second)


def _bullet_panels(html: str) -> dict[str, dict[str, str]]:
    import re

    found: dict[str, dict[str, str]] = {}
    for title, items in re.findall(
        r'<section class="c-panel c-bullet-panel[^"]*"><h3>(.*?)</h3>'
        r'<ul class="c-feature-list">(.*?)</ul></section>',
        html,
        flags=re.S,
    ):
        label = "".join(re.sub(r"<[^>]+>", "", title).split())
        found[label] = {"title": title, "items": items}
    return found


def _icon_glyph(svg: str) -> str:
    marker = 'stroke-width="1.4" opacity="0.55"/>'
    return svg.split(marker, 1)[-1].replace("</svg>", "").strip()


class StoryExtractTests(unittest.TestCase):
    def test_readme_images_become_snake_beats(self) -> None:
        from autoposting.visual.story import extract_story_beats, snake_rows

        readme = """
# Tool
Intro paragraph.

## Install
![install](docs/install.png)

Then you run the agent.

## Workflow
<img src="https://example.com/flow.webp">

![build](https://img.shields.io/badge/build-ok)
"""
        beats = extract_story_beats(readme, owner="acme", repo="tool")
        self.assertEqual(2, len(beats))
        self.assertIn("raw.githubusercontent.com/acme/tool/HEAD/docs/install.png", beats[0]["image"])
        self.assertEqual("https://example.com/flow.webp", beats[1]["image"])
        rows = snake_rows(list("ABCD"))
        self.assertEqual([["A", "B"], ["D", "C"]], rows)


class ExamplePngTests(unittest.TestCase):
    def test_examples_write_html_and_3_by_4_png(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        results = render_examples(Path(temp.name), screenshot=True)
        self.assertEqual(["ranking-overview", "project-detail"], [row["page"] for row in results])
        for row in results:
            html_path = Path(row["html"])
            png_path = Path(row["png"])
            self.assertTrue(html_path.is_file())
            self.assertTrue(png_path.is_file())
            self.assertGreater(png_path.stat().st_size, 20_000)
            width, height = _png_size(png_path)
            self.assertEqual((PNG_WIDTH, PNG_HEIGHT), (width, height))


class WeeklyRenderPlanTests(unittest.TestCase):
    def test_render_plan_keeps_cover_and_poster_job_contract(self) -> None:
        from autoposting.image_gen import _safe_name, build_weekly_render_plan

        repos = [
            {"rank": index, "name": f"owner/tool-{index}", "summary": f"s{index}", "stars": index}
            for index in range(1, 13)
        ]
        plan = build_weekly_render_plan(repos)
        self.assertEqual(len(repos[:10]) + 1, len(plan))
        cover = plan[0]
        self.assertEqual("cover", cover["kind"])
        self.assertEqual("cover", cover["id"])
        self.assertEqual("本周爆火ai工具", cover["title"])
        self.assertEqual(0, cover["rank"])
        for repo, job in zip(repos[:10], plan[1:]):
            self.assertEqual("poster", job["kind"])
            self.assertEqual(repo["rank"], job["rank"])
            self.assertEqual(repo["name"], job["title"])
            self.assertEqual(f"{repo['rank']:02d}-{_safe_name(repo['name'])}", job["id"])

    def test_render_plan_carries_no_image_model_prompt(self) -> None:
        from autoposting.image_gen import build_weekly_render_plan

        plan = build_weekly_render_plan(
            [
                {
                    "rank": 1,
                    "name": "foo/bar",
                    "summary": "s",
                    "does": "d",
                    "scene": "sc",
                    "kind": "CLI",
                }
            ]
        )
        self.assertEqual(2, len(plan))
        for job in plan:
            self.assertNotIn("prompt", job)
        self.assertEqual(
            {"id", "kind", "rank", "title", "url", "file", "selected"},
            set(plan[0]),
        )
        self.assertEqual(set(plan[0]), set(plan[1]))

    def test_render_plan_handles_missing_rank_and_empty_repos(self) -> None:
        from autoposting.image_gen import build_weekly_render_plan

        self.assertEqual(1, len(build_weekly_render_plan([])))
        self.assertEqual("cover", build_weekly_render_plan([])[0]["kind"])
        plan = build_weekly_render_plan([{"name": "no-rank"}])
        self.assertEqual(2, len(plan))
        self.assertEqual(1, plan[1]["rank"])
        self.assertEqual("01-no-rank", plan[1]["id"])


class LaneParameterTests(unittest.TestCase):
    def test_lane_text_reaches_cover_and_poster_payloads(self) -> None:
        from autoposting.visual.adapter import project_from_repo, ranking_from_repos

        repos = [{"rank": 1, "name": "alpha", "kind": "CLI", "summary": "s", "stars": 7}]
        ranking = ranking_from_repos(
            repos,
            brand="",
            series="Skill 覆盖榜",
            issue="",
            kicker="SKILL FINDER",
            title="Skills",
            subtitle="按阶段整理",
            source="",
            footer="Skill 查找器",
            metric_label="安装量",
        )
        self.assertEqual("", ranking["brand"])
        self.assertEqual("Skill 覆盖榜", ranking["series"])
        self.assertEqual("", ranking["issue"])
        self.assertEqual("SKILL FINDER", ranking["kicker"])
        self.assertEqual("Skills", ranking["title"])
        self.assertEqual("按阶段整理", ranking["subtitle"])
        self.assertEqual("", ranking["source"])
        self.assertEqual("Skill 查找器", ranking["footer"])
        self.assertEqual("", ranking["avatar"])
        self.assertEqual("安装量", ranking["items"][0]["metric"]["label"])
        self.assertEqual(7, ranking["items"][0]["metric"]["value"])

        project = project_from_repo(repos[0], brand="", issue="", source="", footer="")
        self.assertEqual("", project["issue"])
        self.assertEqual("", project["source"])
        self.assertEqual("", project["footer"])
        self.assertEqual("", project["avatar"])

    def test_none_keeps_the_github_weekly_defaults(self) -> None:
        from autoposting.visual.adapter import ranking_from_repos

        repos = [{"rank": 1, "name": "alpha", "kind": "CLI", "summary": "s", "stars": 7}]
        weekly = ranking_from_repos(repos)
        self.assertEqual("GitHub", weekly["source"])
        self.assertEqual("WEEKLY RANKING", weekly["kicker"])
        self.assertEqual("GitHub", weekly["title"])
        self.assertEqual("本周爆火 AI 项目榜", weekly["subtitle"])
        self.assertEqual("Stars", weekly["items"][0]["metric"]["label"])
        self.assertEqual("EE_萤飞", weekly["brand"])
        self.assertTrue(weekly["issue"])
        self.assertIn("data:image/png;base64,", weekly["avatar"])


class SmartCoverGuardTests(unittest.TestCase):
    def test_cover_renders_kicker_and_real_page_total(self) -> None:
        from autoposting.github_weekly import iso_week_issue
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        repos = [
            {
                "rank": index,
                "name": f"skill-{index}",
                "kind": "CLI",
                "summary": "s",
                "does": "完成这一步。",
                "how": "按说明执行。",
                "audience": "需要这一步的人。",
                "scene": "要做这一步的时候。",
                "stars": index,
                "url": f"https://github.com/acme/skill-{index}",
            }
            for index in range(1, 4)
        ]
        render_weekly_visual(
            repos,
            [
                {
                    "id": "cover",
                    "kind": "cover",
                    "rank": 0,
                    "visual_title": "Skill 查找结果",
                    "visual_subtitle": "覆盖阶段",
                    "visual_kicker": "SKILL FINDER",
                    "visual_footer": "Skill 查找器 · 按覆盖阶段整理",
                },
                {"id": "01-skill-1", "kind": "poster", "rank": 1, "title": "skill-1"},
            ],
            temp,
            screenshot=False,
            brand="",
            series="",
            issue="",
            source="",
            footer="Skill 查找器 · 按覆盖阶段整理",
            kicker="SKILL FINDER",
        )
        cover = (temp / "cover.html").read_text(encoding="utf-8")
        poster = (temp / "01-skill-1.html").read_text(encoding="utf-8")
        self.assertIn('class="c-smart-cover-kicker">SKILL FINDER</p>', cover)
        self.assertIn("Skill 查找结果", cover)
        self.assertIn(".c-smart-cover-heading .c-smart-cover-kicker {", cover)
        self.assertIn("01 / 04", cover)
        self.assertNotIn("01 / 11", cover)
        self.assertNotIn(iso_week_issue(), cover)
        self.assertNotIn("EE_萤飞", cover)
        self.assertNotIn("GitHub", cover)
        self.assertIn("02 / 04", poster)
        self.assertNotIn("01 / 11", poster)

    def test_weekly_cover_still_uses_github_branding_and_iso_week(self) -> None:
        from autoposting.github_weekly import iso_week_issue
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        repos = [
            {"rank": index, "name": f"repo-{index}", "kind": "CLI", "summary": "s", "stars": index}
            for index in range(1, 4)
        ]
        render_weekly_visual(repos, [{"id": "cover", "kind": "cover", "rank": 0}], temp, screenshot=False)
        cover = (temp / "cover.html").read_text(encoding="utf-8")
        self.assertIn("EE_萤飞", cover)
        self.assertIn(iso_week_issue(), cover)
        self.assertIn("WEEKLY RANKING", cover)
        self.assertIn("01 / 04", cover)
        self.assertNotIn("01 / 11", cover)

    def test_ranking_overview_page_number_uses_real_item_count(self) -> None:
        from autoposting.visual.adapter import ranking_from_repos
        from autoposting.visual.pipeline import render_page

        payload = ranking_from_repos(
            [{"rank": index, "name": f"n{index}", "kind": "CLI", "summary": "s"} for index in range(1, 5)],
            issue="",
        )
        html = render_page(payload, screenshot=False)["html_text"]
        self.assertIn("01 / 05", html)
        self.assertNotIn("01 / 11", html)


class PosterTemplateSkinTests(unittest.TestCase):
    def _repos(self) -> list[dict]:
        return [
            {
                "rank": index,
                "name": f"skill-{index}",
                "kind": "CLI",
                "summary": f"给想先看懂再动手的人，拿 skill-{index} 试的时候，先判断能不能省事。",
                "description": "description",
                "does": "把素材按脚本切成粗剪。\n把字幕对齐到画面。",
                "how": "把材料放进约定目录。\n按说明跑一遍。",
                "audience": "一个人管多个账号的内容创作者",
                "scene": "手上素材堆着、成片交期又近的时候",
                "url": f"https://github.com/acme/skill-{index}",
            }
            for index in range(1, 5)
        ]

    def test_template_two_cover_uses_directory_grid_and_keeps_canvas(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        jobs = [
            {"id": "cover", "kind": "cover", "rank": 0, "visual_title": "Skill Directory"},
            {"id": "01-skill-1", "kind": "poster", "rank": 1},
        ]
        rendered = render_weekly_visual(
            self._repos(), jobs, temp, screenshot=False, poster_template="2"
        )
        self.assertEqual(["cover", "01-skill-1"], [row["id"] for row in rendered])
        cover = (temp / "cover.html").read_text(encoding="utf-8")
        poster = (temp / "01-skill-1.html").read_text(encoding="utf-8")
        self.assertIn("skin-toc", cover)
        self.assertIn("toc-grid", cover)
        self.assertIn("toc-item", cover)
        self.assertIn("Skill Directory", cover)
        self.assertIn("skill-1", cover)
        self.assertIn("width: 1080px", cover)
        self.assertIn("height: 1440px", cover)
        self.assertNotIn("width: 1200px", cover)
        self.assertIn("skin-toc", poster)
        self.assertIn("c-project-shell", poster)
        self.assertIn("一句话看懂", poster)
        self.assertIn("做什么？", poster)
        self.assertIn("如何实现？", poster)
        self.assertIn("适合哪些人群", poster)
        self.assertIn("使用场景", poster)
        self.assertLess(poster.find("一句话看懂"), poster.find("做什么"))
        self.assertLess(poster.find("做什么"), poster.find("如何实现"))
        self.assertLess(poster.find("如何实现"), poster.find("适合哪些人群"))
        self.assertLess(poster.find("适合哪些人群"), poster.find("使用场景"))
        self.assertIn("width: 1080px", poster)
        self.assertIn("height: 1440px", poster)

    def test_template_one_stays_geometric_smart_cover(self) -> None:
        from autoposting.visual.weekly import render_weekly_visual

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        jobs = [
            {"id": "cover", "kind": "cover", "rank": 0},
            {"id": "01-skill-1", "kind": "poster", "rank": 1},
        ]
        render_weekly_visual(self._repos(), jobs, temp, screenshot=False, poster_template="1")
        cover = (temp / "cover.html").read_text(encoding="utf-8")
        poster = (temp / "01-skill-1.html").read_text(encoding="utf-8")
        cover_body = cover.split("<body>", 1)[-1]
        poster_body = poster.split("<body>", 1)[-1]
        self.assertIn("c-smart-cover-list", cover)
        self.assertIn('class="page page-ranking template-cover"', cover_body)
        self.assertNotIn("skin-toc", cover_body)
        self.assertNotIn("toc-grid", cover_body)
        self.assertIn("c-project-shell", poster)
        self.assertNotIn("skin-toc", poster_body)

    def test_template_switch_does_not_rewrite_copy_fields(self) -> None:
        from autoposting.visual.adapter import project_from_repo
        from autoposting.visual.weekly import render_weekly_visual

        repo = self._repos()[0]
        geometric = project_from_repo(repo)
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        render_weekly_visual(
            [repo],
            [{"id": "01-skill-1", "kind": "poster", "rank": 1}],
            temp,
            screenshot=False,
            poster_template="2",
        )
        poster = (temp / "01-skill-1.html").read_text(encoding="utf-8")
        self.assertIn(geometric["keyMessage"], poster)
        self.assertIn(geometric["features"][0], poster)
        self.assertEqual("把素材按脚本切成粗剪。", geometric["features"][0])


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"not a png: {path}")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
