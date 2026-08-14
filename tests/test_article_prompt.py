import unittest
from pathlib import Path

from autoposting.article_prompt import (
    ARTICLE_PROMPT_FILENAME,
    article_prompt_paths,
    article_prompt_version,
    build_article_prompt,
    load_article_prompt_doc,
    parse_article_prompt_doc,
)
from autoposting.llm import LLMService

from test_llm import FakeRunner, _db
import tempfile


class ArticlePromptDocTests(unittest.TestCase):
    def test_repo_doc_is_the_live_source_for_both_platforms(self) -> None:
        path = next(p for p in article_prompt_paths() if p.is_file())
        self.assertEqual(ARTICLE_PROMPT_FILENAME, path.name)
        self.assertEqual("docs", path.parent.name)
        text = load_article_prompt_doc()
        self.assertIn("唯一调教入口", text)
        self.assertEqual("article-v4", article_prompt_version())
        self.assertIn("chosen_hooks", text)
        self.assertIn("主切口", text)
        self.assertIn("公开报道曾关注", text)
        wechat = build_article_prompt(
            "wechat",
            ("lab_blog",),
            {
                "title": "t",
                "summary": "s",
                "background": {
                    "chosen_hooks": [
                        {"phrase": "协和4+4试点班", "why": "公开检索出现争议"},
                        {"phrase": "第二切口"},
                    ]
                },
            },
            style="短句",
        )
        self.assertIn("前因与脉络", wechat)
        self.assertIn("lab_blog", wechat)
        self.assertIn("写作风格要求：", wechat)
        self.assertIn("短句", wechat)
        self.assertIn('"title": "t"', wechat)
        self.assertIn("协和4+4试点班", wechat)
        self.assertIn("第二切口", wechat)
        self.assertIn("主切口", wechat)
        self.assertIn("公开报道曾关注", wechat)
        notes = build_article_prompt(
            "xiaohongshu",
            ("lab_blog",),
            {"title": "t", "summary": "s", "background": {}},
        )
        self.assertIn("开场钩子", notes)
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
        self.assertIn("唯一调教入口", load_article_prompt_doc())
        self.assertIn("这件事是什么", prompt)
        self.assertIn("某某称", prompt)
