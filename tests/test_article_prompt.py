import unittest
from pathlib import Path

from autoposting.article_prompt import (
    ARTICLE_BASE_PROMPT_FILENAME,
    ARTICLE_STYLE_PROMPT_FILES,
    ARTICLE_STYLE_PROMPT_IDS,
    article_prompt_paths,
    article_style_prompt_paths,
    article_prompt_version,
    build_article_prompt,
    load_article_prompt_doc,
    load_article_style_prompt,
    parse_article_prompt_doc,
)
from autoposting.llm import LLMService

from test_llm import FakeRunner, _db
import tempfile


class ArticlePromptDocTests(unittest.TestCase):
    def test_only_named_style_prompt_files_remain(self) -> None:
        expected_files = {
            "AI专家": "AI专家.md",
            "经验分享": "经验分享.md",
            "klinkenborg_style": "klinkenborg.md",
        }
        self.assertEqual(("AI专家", "经验分享", "klinkenborg_style"), ARTICLE_STYLE_PROMPT_IDS)
        self.assertEqual(expected_files, ARTICLE_STYLE_PROMPT_FILES)
        prompts = {}
        for style_id in ARTICLE_STYLE_PROMPT_IDS:
            prompts[style_id] = load_article_style_prompt(style_id)
            self.assertTrue(prompts[style_id])
            self.assertEqual(expected_files[style_id], article_style_prompt_paths(style_id)[0].name)
        self.assertEqual(3, len(set(prompts.values())))
        self.assertIn("结构留给写作者，不要留给读者", prompts["AI专家"])
        self.assertIn("一般人容易忽略的角度", prompts["AI专家"])
        self.assertIn("拆解", prompts["经验分享"])
        self.assertIn("梳理", prompts["经验分享"])
        self.assertIn("解读", prompts["经验分享"])
        self.assertIn("分享", prompts["经验分享"])
        self.assertIn("句子这一最小可控单位", prompts["klinkenborg_style"])
        self.assertIn("自告奋勇句", prompts["klinkenborg_style"])
        style_dir = article_style_prompt_paths("AI专家")[0].parent
        leftover = {path.name for path in style_dir.glob("*.md")}
        self.assertEqual({"AI专家.md", "经验分享.md", "klinkenborg.md"}, leftover)
        for invalid in ("8", "1", "7", "9", "../AI专家", "klinkenborg"):
            with self.assertRaises(ValueError):
                load_article_style_prompt(invalid)

    def test_repo_doc_is_the_live_source_for_both_platforms(self) -> None:
        path = next(p for p in article_prompt_paths() if p.is_file())
        self.assertEqual(ARTICLE_BASE_PROMPT_FILENAME, path.name)
        self.assertEqual("docs", path.parent.name)
        text = load_article_prompt_doc()
        self.assertIn("文章生成基础约束", text)
        self.assertEqual("article-v13", article_prompt_version())
        self.assertNotIn("一般人容易看漏的角度", text)
        expert = load_article_style_prompt("AI专家")
        # 找切口 retired: the style doc must not describe a chosen_hooks contract.
        self.assertNotIn("chosen_hooks", expert)
        self.assertIn("不要发明社会切口", expert)
        wechat = build_article_prompt(
            "wechat",
            ("lab_blog",),
            {
                "title": "t",
                "summary": "s",
                "background": {"research_context": {"evidence": []}},
            },
            style="短句",
            preset_style=expert,
        )
        self.assertIn("一般人容易忽略的角度", wechat)
        self.assertLess(wechat.index("一般人容易忽略的角度"), wechat.index("短句"))
        self.assertIn("lab_blog", wechat)
        self.assertIn("写作风格要求：", wechat)
        self.assertIn("短句", wechat)
        self.assertIn('"title": "t"', wechat)
        self.assertGreater(wechat.rindex("键名必须是 title 和 body"), wechat.rindex('"summary": "s"'))
        # 找切口 retired: the surviving opening rule must still be present.
        self.assertNotIn("chosen_hooks", wechat)
        self.assertIn("不要发明社会切口", wechat)
        self.assertIn("公开报道曾关注", wechat)
        preset = build_article_prompt(
            "wechat", ("lab_blog",), {"title": "t"}, preset_style=expert
        )
        self.assertIn(f"写作风格要求：\n{expert}", preset)
        notes = build_article_prompt(
            "xiaohongshu",
            ("lab_blog",),
            {"title": "t", "summary": "s", "background": {}},
            preset_style=expert,
        )
        self.assertIn("更快、更短", notes)
        self.assertIn("caption", notes)
        self.assertIn("- ", notes)
        self.assertNotIn("前因与脉络", notes)

    def test_editing_the_doc_changes_the_model_prompt(self) -> None:
        custom = (
            "version: article-test\n\n"
            "## 共用\n共用段 {source_ids}\n\n"
            "## 微信\n微信段\n{style_block}数据：\n{data}\n"
        )
        prompt = build_article_prompt(
            "wechat",
            ("media_feed",),
            {"title": "x"},
            doc_text=custom,
        )
        self.assertIn("共用段 media_feed", prompt)
        self.assertIn("微信段", prompt)
        self.assertEqual("article-test", parse_article_prompt_doc(custom)["version"])

    def test_generate_article_reads_the_repo_doc(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        fake = FakeRunner(
            stdout='{"title": "模型一正式发布", "body": "前因与脉络。lab_blog AIGC"}'
        )
        service = LLMService(_db(temp), runner=fake)
        service.generate_article("wechat", "标题", "摘要", ("lab_blog",))
        prompt = fake.calls[0][2]
        self.assertIn("文章生成基础约束", load_article_prompt_doc())
        self.assertIn(load_article_style_prompt("AI专家"), prompt)
        self.assertIn("一般人容易忽略的角度", prompt)
        self.assertIn("结构留给写作者，不要留给读者", prompt)
