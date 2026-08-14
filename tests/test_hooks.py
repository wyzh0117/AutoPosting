import unittest

from autoposting.hooks import (
    EMPTY_HOOKS_MESSAGE,
    extract_rule_phrases,
    filter_results,
    normalize_hook_id,
    sanitize_chosen_hooks,
)


FIXTURE = (
    "中国医生用 GPT-5.6 破解 22 年数学难题，Crouzeix 本人确认正确。"
    "本科是北大地质学，2020 年通过协和医学院“4+4 试点班”转入临床医学。"
)


class HookUnitTests(unittest.TestCase):
    def test_normalize_hook_id_strips_quotes_and_case(self) -> None:
        self.assertEqual(
            normalize_hook_id('协和医学院“4+4 试点班”'),
            normalize_hook_id("协和医学院 4+4试点班"),
        )
        self.assertEqual("gpt-5.6", normalize_hook_id("GPT-5.6"))

    def test_extract_keeps_4plus4_program_unsplit(self) -> None:
        phrases = extract_rule_phrases(FIXTURE)
        joined = " ".join(item["phrase"] for item in phrases)
        self.assertTrue(any("4+4" in item["phrase"] and "试点班" in item["phrase"] for item in phrases))
        self.assertFalse(any(item["phrase"] in {"的", "了", "是", "通过"} for item in phrases))
        self.assertIn("program", {item["kind"] for item in phrases if "试点班" in item["phrase"]})
        self.assertNotIn("4+4 试", joined)

    def test_filter_requires_phrase_and_controversy(self) -> None:
        kept = filter_results(
            "协和医学院4+4试点班",
            [
                {
                    "title": "协和4+4试点班争议再起",
                    "url": "https://news.example/4plus4",
                    "snippet": "该项目曾引发舆论质疑。",
                },
                {
                    "title": "北大地质学培养方案",
                    "url": "https://news.example/geo",
                    "snippet": "本科课程介绍。",
                },
            ],
        )
        self.assertEqual(1, len(kept))
        self.assertIn("4plus4", kept[0]["url"])

    def test_sanitize_drops_bad_entries_keeps_order(self) -> None:
        cleaned = sanitize_chosen_hooks(
            [
                {"phrase": "协和4+4", "why": "公开检索出现争议"},
                "nope",
                {"phrase": ""},
                {"phrase": "第二切口", "results": [{"title": "t", "url": "u", "snippet": "s"}]},
            ]
        )
        self.assertEqual(["协和4+4", "第二切口"], [item["phrase"] for item in cleaned])
        self.assertTrue(cleaned[0]["hook_id"])
        self.assertEqual(EMPTY_HOOKS_MESSAGE, "未发现可用切口")
