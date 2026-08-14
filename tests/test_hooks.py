import unittest

from autoposting.hooks import (
    EMPTY_HOOKS_MESSAGE,
    extract_rule_phrases,
    filter_results,
    find_hooks,
    normalize_hook_id,
    sanitize_chosen_hooks,
    select_search_queue,
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


class HookSearchTests(unittest.TestCase):
    def test_title_core_does_not_fill_search_queue(self) -> None:
        title = "中国医生用 GPT-5.6 破解 Crouzeix 猜想"
        extras = (
            [{"phrase": "GPT-5.6", "kind": "other"}, {"phrase": "Crouzeix", "kind": "person"}]
            + [{"phrase": f"项目{index}试点班", "kind": "program"} for index in range(8)]
        )
        queue = select_search_queue(extras, title, limit=8)
        self.assertEqual(8, len(queue))
        self.assertTrue(all("试点班" in item["phrase"] for item in queue))

    def test_find_hooks_returns_controversy_card_only(self) -> None:
        def searcher(query: str) -> list[dict[str, str]]:
            if "4+4" in query or "试点班" in query:
                return [
                    {
                        "title": "协和4+4试点班争议",
                        "url": "https://news.example/4plus4",
                        "snippet": "该培养方案曾引发舆论。",
                    }
                ]
            return [
                {
                    "title": query.replace(" 争议", "") + " 人物简介",
                    "url": "https://news.example/bio",
                    "snippet": "履历介绍，无风波。",
                }
            ]

        hooks = find_hooks(
            "中国医生用 GPT-5.6 破解 22 年数学难题",
            FIXTURE,
            searcher,
        )
        self.assertTrue(hooks)
        self.assertTrue(any("4+4" in card["phrase"] for card in hooks))
        self.assertTrue(all("争议" in " ".join(r["title"] + r["snippet"] for r in card["results"]) for card in hooks))
        self.assertIn("公开检索出现", hooks[0]["why"])

    def test_find_hooks_empty_when_searcher_returns_nothing(self) -> None:
        self.assertEqual([], find_hooks("标题", "没有专名", lambda query: []))

    def test_single_search_failure_does_not_kill_others(self) -> None:
        def searcher(query: str) -> list[dict[str, str]]:
            if "地质" in query:
                raise OSError("timeout")
            if "试点班" in query or "4+4" in query:
                return [
                    {
                        "title": "协和4+4试点班风波",
                        "url": "https://news.example/4plus4",
                        "snippet": "舆论质疑选拔公平。",
                    }
                ]
            return []

        hooks = find_hooks("数学题", FIXTURE, searcher)
        self.assertTrue(any("4+4" in card["phrase"] for card in hooks))
