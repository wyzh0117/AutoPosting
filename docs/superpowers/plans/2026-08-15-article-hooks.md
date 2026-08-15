# Article Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a workbench 「找切口」 step that live-searches buried phrases for public controversy and lets the user multi-select hooks that then force the article lead.

**Architecture:** New `hooks.py` extracts phrases, calls existing `search_web`, and filters on phrase + controversy markers. The workbench exposes `POST /api/hooks/find`; the browser holds the card list and checkbox order. `POST /api/articles` accepts optional `chosen_hooks` and puts them in the article background. `ARTICLE_PROMPT.md` becomes `article-v4` with rules that apply only when that array is non-empty.

**Tech Stack:** Python 3.11 standard library, SQLite, existing DuckDuckGo `search_web`, unittest.

**Spec:** `docs/superpowers/specs/2026-08-15-article-hooks-design.md`

## Global Constraints

- Python 3.11 standard library and SQLite only. No third-party runtime dependencies.
- Reuse `autoposting.background.search_web`. Do not add a heat-index archive or vector store.
- 「找切口」 is a separate button. Selecting a candidate or clicking 「生成文章」 must not search for hooks.
- Search pages stay material only: never write them into `FactList` or assign `source_id`.
- Missing / empty `chosen_hooks` must keep the 0.3.4 generate path (plus the new prompt version string).
- Tests: `PYTHONPATH=src:tests python3 -m unittest …` — this repo does not use pytest.
- This git repo’s only commit is the spec. **Never `git add .`.** Stage only the files listed in that task’s commit step. Leave unrelated untracked project files alone.

## File map

- Create `src/autoposting/hooks.py` — extract, filter, `find_hooks`, `sanitize_chosen_hooks`
- Create `tests/test_hooks.py` — offline hook tests with a fake searcher
- Modify `src/autoposting/llm.py` — `extract_hook_candidates`; later bump `HTTP_USER_AGENT`
- Modify `src/autoposting/workbench.py` — `Workbench.find_hooks`; `generate_article(..., chosen_hooks=None)`
- Modify `src/autoposting/web.py` — API + 「找切口」 UI; later bump `APP_VERSION` / badge
- Modify `docs/ARTICLE_PROMPT.md` — `article-v4` hook rules
- Modify `src/autoposting/article_prompt.py` — default version fallback
- Modify `tests/test_article_prompt.py`, `tests/test_workbench.py`, `tests/test_web.py`, `tests/test_llm.py`
- Modify `docs/DECISIONS.md`, `docs/WEB_FUNCTIONAL_TEST.md`, `README.md`
- After the feature works: version `0.4.0` in the five lockstep places, `docs/PATCH_0.4.0.md`, archive tarball

---

### Task 1: Rule extract, match, and filter

**Files:**
- Create: `src/autoposting/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: none
- Produces:
  - `CONTROVERSY_MARKERS = ("争议", "舆论", "风波", "质疑", "热议", "丑闻")`
  - `PROGRAM_MARKERS = ("试点班", "医学院", "计划", "通道")`
  - `EMPTY_HOOKS_MESSAGE = "未发现可用切口"`
  - `normalize_hook_id(phrase: str) -> str`
  - `extract_rule_phrases(excerpt: str) -> list[dict[str, str]]` each `{"phrase", "kind"}` with `kind` in `org|program|person|other`
  - `filter_results(phrase: str, results: list[dict[str, str]]) -> list[dict[str, str]]`
  - `sanitize_chosen_hooks(raw: object) -> list[dict[str, object]]`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_hooks.HookUnitTests -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'autoposting.hooks'`

- [ ] **Step 3: Write minimal implementation**

`src/autoposting/hooks.py`:

```python
from __future__ import annotations

import re

from .background import _tokens

CONTROVERSY_MARKERS = ("争议", "舆论", "风波", "质疑", "热议", "丑闻")
PROGRAM_MARKERS = ("试点班", "医学院", "计划", "通道")
EMPTY_HOOKS_MESSAGE = "未发现可用切口"
_HOOK_ID_QUOTES = str.maketrans("", "", "\"'“”‘’「」")


def normalize_hook_id(phrase: str) -> str:
    text = " ".join((phrase or "").translate(_HOOK_ID_QUOTES).split()).casefold()
    return re.sub(r"\s+", "", text)


def compact_phrase(phrase: str) -> str:
    return normalize_hook_id(phrase)


def extract_rule_phrases(excerpt: str) -> list[dict[str, str]]:
    text = excerpt or ""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(phrase: str, kind: str) -> None:
        cleaned = " ".join(phrase.split())
        hook_id = normalize_hook_id(cleaned)
        if len(cleaned) < 2 or hook_id in seen:
            return
        seen.add(hook_id)
        found.append({"phrase": cleaned, "kind": kind})

    for marker in PROGRAM_MARKERS:
        for match in re.finditer(
            r"[\u4e00-\u9fffA-Za-z0-9+＋\-—]{0,16}" + re.escape(marker),
            text,
        ):
            add(match.group(0), "program")
    for token in _tokens(text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) < 4:
            continue
        kind = "other"
        if any(mark in token for mark in PROGRAM_MARKERS):
            kind = "program"
        elif token.endswith("大学") or token.endswith("学院") or token.endswith("医院"):
            kind = "org"
        add(token, kind)
    return found


def result_blob(result: dict[str, str]) -> str:
    return f"{result.get('title', '')} {result.get('snippet', '')}"


def result_matches_phrase(result: dict[str, str], phrase: str) -> bool:
    blob = result_blob(result)
    compact_blob = compact_phrase(blob)
    return phrase in blob or compact_phrase(phrase) in compact_blob


def result_has_controversy(result: dict[str, str]) -> bool:
    blob = result_blob(result)
    return any(marker in blob for marker in CONTROVERSY_MARKERS)


def filter_results(phrase: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for result in results:
        if result_matches_phrase(result, phrase) and result_has_controversy(result):
            kept.append(result)
    return kept


def sanitize_chosen_hooks(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        phrase = item.get("phrase")
        if not isinstance(phrase, str) or not phrase.strip():
            continue
        phrase = phrase.strip()
        hook_id = item.get("hook_id")
        if not isinstance(hook_id, str) or not hook_id.strip():
            hook_id = normalize_hook_id(phrase)
        results = item.get("results")
        if not isinstance(results, list):
            results = []
        why = item.get("why") if isinstance(item.get("why"), str) else ""
        kind = item.get("kind") if item.get("kind") in {"org", "program", "person", "other"} else "other"
        cleaned.append(
            {
                "hook_id": hook_id,
                "phrase": phrase,
                "kind": kind,
                "why": why,
                "results": [
                    {
                        "title": str(row.get("title") or ""),
                        "url": str(row.get("url") or ""),
                        "snippet": str(row.get("snippet") or ""),
                    }
                    for row in results
                    if isinstance(row, dict)
                ][:3],
            }
        )
    return cleaned
```

If `test_extract_keeps_4plus4_program_unsplit` fails because `_tokens` splits on quotes, keep the `PROGRAM_MARKERS` regex path as the source of the full phrase. Do not invent extra NLP.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_hooks.HookUnitTests -v`

Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/hooks.py tests/test_hooks.py
git commit -m "Add hook phrase extract and controversy filter."
```

---

### Task 2: Merge, demote title terms, search, and rank cards

**Files:**
- Modify: `src/autoposting/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 1 functions
- Produces:
  - `merge_candidates(*groups: list[dict[str, str]]) -> list[dict[str, str]]` — same `hook_id` keeps the longer phrase
  - `select_search_queue(candidates: list[dict[str, str]], title: str, limit: int = 8) -> list[dict[str, str]]` — `program`/`org` first, then `person`, then `other`; title tokens go last so they do not fill the 8 slots
  - `build_hook_card(phrase: str, kind: str, results: list[dict[str, str]]) -> dict[str, object]`
  - `find_hooks(title: str, excerpt: str, searcher, extra_candidates: list[dict[str, str]] | None = None) -> list[dict[str, object]]`
  - `searcher` signature: `(query: str) -> list[dict[str, str]]`
  - Query is `f"{phrase} 争议"`
  - At most 4 worker threads, 8s per query, 25s wall clock; unfinished queries dropped
  - Rank by `(len(filtered), controversy-marker count)` descending, max 8 cards
  - `why` = `公开检索出现：{first_hit_title}` — no invented cause

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hooks.py`:

```python
from autoposting.hooks import find_hooks, select_search_queue


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_hooks.HookSearchTests -v`

Expected: FAIL with `ImportError` or `AttributeError: find_hooks`

- [ ] **Step 3: Implement merge, queue, search, rank**

In `hooks.py` add `concurrent.futures.ThreadPoolExecutor`, `time.monotonic`, constants `MAX_SEARCH = 8`, `MAX_CARDS = 8`, `SEARCH_WORKERS = 4`, `SEARCH_TIMEOUT_SECONDS = 8`, `FIND_DEADLINE_SECONDS = 25`.

`select_search_queue`: split candidates into title-overlapping vs not (token overlap with `_tokens(title)`). Sort each bucket `program=0, org=1, person=2, other=3`, then longer phrase. Concatenate non-title then title, cut to `limit`.

`find_hooks`: `merge_candidates(extract_rule_phrases(excerpt), extra_candidates or [])` → `select_search_queue` → submit `searcher(f"{phrase} 争议")` in a pool → `filter_results` → drop empty → `build_hook_card` → sort → `[:8]`. Catch `(OSError, TimeoutError, ValueError)` per future as empty results.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_hooks -v`

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/hooks.py tests/test_hooks.py
git commit -m "Search hook phrases and rank controversy cards."
```

---

### Task 3: Optional LLM extract that fails soft

**Files:**
- Modify: `src/autoposting/hooks.py` — `EXTRACT_CANDIDATES_PROMPT`
- Modify: `src/autoposting/llm.py` — `LLMService.extract_hook_candidates`
- Modify: `tests/test_hooks.py` or `tests/test_llm.py`
- Modify: `tests/test_workbench.py` `FakeModel.__call__` so existing article tests keep working if they later see this prompt

**Interfaces:**
- Consumes: `parse_model_json`, `LLMService._run`, `resolve_settings`
- Produces: `LLMService.extract_hook_candidates(self, excerpt: str) -> list[dict[str, str]]`  
  Returns `[]` when model is `unconfigured`, `base_url` is empty, `_run` raises `LLMError`, or JSON is not `{"candidates":[{"phrase","kind"}]}`.
- Prompt must contain the unique marker `候选切口JSON` and must **not** contain `body 必须原样包含每个来源 ID`.

- [ ] **Step 1: Write the failing test**

In `tests/test_llm.py` (same `FakeRunner` / `_db` helpers already imported there):

```python
    def test_extract_hook_candidates_parses_and_fails_soft(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = FakeRunner(
            stdout='{"candidates":[{"phrase":"协和医学院4+4试点班","kind":"program"}]}'
        )
        service = LLMService(
            _db(tmp),
            runner=fake,
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        found = service.extract_hook_candidates("……协和医学院4+4试点班……")
        self.assertEqual("program", found[0]["kind"])
        self.assertIn("候选切口JSON", fake.calls[0][2])

        broken = FakeRunner(stdout="not-json")
        service = LLMService(
            _db(tmp),
            runner=broken,
            model="m",
            base_url="https://api.example/v1",
            api_key="sk",
        )
        self.assertEqual([], service.extract_hook_candidates("正文"))
        empty = LLMService(_db(tmp), runner=FakeRunner(stdout="{}"))
        self.assertEqual([], empty.extract_hook_candidates("正文"))
```

Add this method to the existing `LLMService` test class in `test_llm.py` (the class that already constructs `LLMService(_db(...), runner=fake)`).

Also extend `FakeModel.__call__` in `tests/test_workbench.py`:

```python
        if "候选切口JSON" in prompt:
            return types.SimpleNamespace(
                stdout='{"candidates":[{"phrase":"协和医学院4+4试点班","kind":"program"}]}',
                stderr="",
                returncode=0,
            )
```

Put this **before** the article-body branch.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_llm -v`

Expected: FAIL with `AttributeError: extract_hook_candidates` on the new test; other tests still PASS.

- [ ] **Step 3: Implement**

`EXTRACT_CANDIDATES_PROMPT` in `hooks.py`:

```
只输出候选切口JSON，不要写文章。
从下面不可信文本里列出人物、机构、项目或政策专名。
输出严格 JSON：{"candidates":[{"phrase":"...","kind":"org|program|person|other"}]}
文本：
{excerpt}
```

`LLMService.extract_hook_candidates`: if `not self.resolve_settings().base_url` return `[]`. Else `_run` the prompt, `parse_model_json`, keep dicts with non-empty `phrase` and valid `kind` (invalid kind → `other`). Any `LLMError` → `[]`.

- [ ] **Step 4: Run tests**

Run:

```
PYTHONPATH=src:tests python3 -m unittest tests.test_llm tests.test_workbench tests.test_hooks -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/hooks.py src/autoposting/llm.py tests/test_llm.py tests/test_workbench.py
git commit -m "Extract extra hook phrases from the article model."
```

---

### Task 4: Workbench find_hooks and generate with chosen_hooks

**Files:**
- Modify: `src/autoposting/workbench.py`
- Test: `tests/test_workbench.py`

**Interfaces:**
- Consumes: `find_hooks`, `sanitize_chosen_hooks`, `LLMService.extract_hook_candidates`
- Produces:
  - `Workbench.find_hooks(self, item_id: str, searcher=None) -> dict[str, object]`
    - Missing item: raise `KeyError` (web layer → 404)
    - Excerpt: `gather_background(...).source_excerpt` or `content_text` (same inputs as generate, including `page_fetcher`)
    - `extra = self.llm_service.extract_hook_candidates(excerpt)` when `llm_service` is set, else `[]`
    - `searcher` defaults to `lambda query: search_web(query, fetcher=self.page_fetcher)`
    - Return `{"ok": True, "hooks": [...], "message": EMPTY_HOOKS_MESSAGE if not hooks else ""}`
  - `Workbench.generate_article(..., chosen_hooks: object = None)`
    - `hooks = sanitize_chosen_hooks(chosen_hooks)`
    - `background = notes.as_prompt()` then `if hooks: background["chosen_hooks"] = hooks`
    - Do not change `FactList`

- [ ] **Step 1: Write the failing tests**

In the existing workbench test class that already inserts lab/media items (same `setUp` as `test_generate_article`):

```python
    HOOK_BODY = (
        "本科是北大地质学，2020 年通过协和医学院“4+4 试点班”转入临床医学。"
    )

    def _one_item(self, title: str, body: str) -> str:
        self.db.insert_item(
            self.sources[0],
            FeedItem("hook-1", "https://lab.example/hook-1", title, body, None, {}),
        )
        return str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])

    def test_find_hooks_uses_injected_searcher(self) -> None:
        item_id = self._one_item("中国医生用 GPT-5.6 破解数学题", self.HOOK_BODY)

        def searcher(query: str) -> list[dict[str, str]]:
            if "试点班" in query or "4+4" in query:
                return [
                    {
                        "title": "协和4+4试点班争议",
                        "url": "https://news.example/4plus4",
                        "snippet": "舆论质疑。",
                    }
                ]
            return []

        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=FakeModel()))
        payload = workbench.find_hooks(item_id, searcher=searcher)
        self.assertTrue(payload["ok"])
        self.assertTrue(any("4+4" in card["phrase"] for card in payload["hooks"]))

    def test_generate_article_without_hooks_omits_chosen_hooks_payload(self) -> None:
        item_id = self._one_item("Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        workbench.generate_article(item_id, "wechat")
        self.assertNotIn('"chosen_hooks"', model.calls[0][2])

    def test_generate_article_with_hooks_puts_them_in_prompt_order(self) -> None:
        item_id = self._one_item("Model One news", "Model One news 内容")
        model = FakeModel()
        workbench = Workbench(self.db, llm_service=LLMService(self.db, runner=model))
        workbench.generate_article(
            item_id,
            "wechat",
            chosen_hooks=[
                {"phrase": "协和4+4试点班", "why": "公开检索出现争议"},
                {"phrase": "第二切口"},
            ],
        )
        prompt = model.calls[-1][2]
        self.assertIn('"chosen_hooks"', prompt)
        self.assertLess(prompt.index("协和4+4试点班"), prompt.index("第二切口"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_workbench.WorkbenchTests.test_find_hooks_uses_injected_searcher tests.test_workbench.WorkbenchTests.test_generate_article_with_hooks_puts_them_in_prompt_order -v`

Expected: FAIL with `AttributeError: find_hooks` / unexpected keyword `chosen_hooks`

- [ ] **Step 3: Implement**

`workbench.py`: import `EMPTY_HOOKS_MESSAGE`, `find_hooks as collect_hooks`, `sanitize_chosen_hooks`, `search_web`. Add the two methods as specified. `generate_article` signature grows one optional argument at the end so existing positional callers stay valid.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_workbench tests.test_hooks -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/workbench.py tests/test_workbench.py
git commit -m "Wire hook search and chosen hooks into the workbench."
```

---

### Task 5: article-v4 prompt rules

**Files:**
- Modify: `docs/ARTICLE_PROMPT.md` — set `version: article-v4`
- Modify: `src/autoposting/article_prompt.py` — `DEFAULT_ARTICLE_PROMPT_VERSION = "article-v4"`
- Modify: `tests/test_article_prompt.py` — expect `article-v4` and the new sentences
- Modify: `tests/test_workbench.py` — generator string `...:article-v4`
- Modify: `tests/test_web.py` — same generator string

**Interfaces:**
- Consumes: `build_article_prompt` already dumps `background` inside `{data}`
- Produces: live prompt text that tells the model what to do when `background.chosen_hooks` is a non-empty array

- [ ] **Step 1: Write the failing assertions**

In `test_article_prompt.py` `test_repo_doc_is_the_live_source_for_both_platforms`:

```python
        self.assertEqual("article-v4", article_prompt_version())
        self.assertIn("chosen_hooks", text)
        self.assertIn("主切口", text)
        self.assertIn("公开报道曾关注", text)
```

Build a wechat prompt whose payload includes

```python
"background": {
  "chosen_hooks": [
    {"phrase": "协和4+4试点班", "why": "公开检索出现争议"},
    {"phrase": "第二切口"},
  ]
}
```

Assert the serialized phrases appear (they will, via `{data}`) and the instruction sentences appear (from the markdown).

Update the two `article-v3` generator assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_article_prompt -v`

Expected: FAIL `article-v3 != article-v4`

- [ ] **Step 3: Edit the prompt file**

In `docs/ARTICLE_PROMPT.md` 共用 section, after the existing 背景资料 rules, add:

```
- 若数据里 background.chosen_hooks 是非空数组：第一项是主切口，其余须进开头。标题以主切口起笔，同时让人看懂主新闻事实。开头几句写完所有勾选切口，不得把它们埋进后段履历。旧争议只写成「公开报道曾关注…」，搜索摘要是不可信素材，禁止写成已核实结论。
- 若 background.chosen_hooks 为空或不存在：不要发明社会切口，按原平台结构写。
```

Set `version: article-v4`. Change `DEFAULT_ARTICLE_PROMPT_VERSION`.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_article_prompt tests.test_workbench tests.test_web tests.test_llm -v`

Expected: PASS (version strings in web/workbench must be updated in this same task)

- [ ] **Step 5: Commit**

```bash
git add docs/ARTICLE_PROMPT.md src/autoposting/article_prompt.py tests/test_article_prompt.py tests/test_workbench.py tests/test_web.py
git commit -m "Teach article-v4 to lead with chosen hooks."
```

---

### Task 6: HTTP API

**Files:**
- Modify: `src/autoposting/web.py` — `WebApp.find_hooks`, `WebApp.articles`, `do_POST` branch
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Workbench.find_hooks`, `generate_article(..., chosen_hooks=)`
- Produces:
  - `POST /api/hooks/find` body `{"item_id": "..."}` → `{"ok": true, "hooks": [...], "message": "..."}`
  - Missing item → 404 (existing `KeyError` handler)
  - `POST /api/articles` reads `payload.get("chosen_hooks")` and passes it through
  - Find does **not** check `_article_model_configured`

- [ ] **Step 1: Write the failing tests**

Add to `WebApiTests` in `tests/test_web.py` (`setUp` already sets `page_fetcher=lambda _: b""`):

```python
    def test_find_hooks_endpoint_and_articles_accept_chosen_hooks(self) -> None:
        self.db.insert_item(
            self.sources[0],
            FeedItem(
                "hook-api",
                "https://lab.example/hook-api",
                "中国医生用 GPT-5.6 破解数学题",
                "本科经协和医学院4+4试点班转入临床医学。",
                None,
                {},
            ),
        )
        item_id = str(self.db.connect().execute("SELECT item_id FROM items LIMIT 1").fetchone()["item_id"])

        def fake_fetch(url: str) -> bytes:
            if "4" in url and "试点" in url or "4%2B4" in url or "%E8%AF%95%E7%82%B9" in url:
                return (
                    b"<html><body>"
                    b'<a class="result__a" href="https://news.example/4plus4">协和4+4试点班争议</a>'
                    b'<a class="result__snippet">该项目曾引发舆论质疑。</a>'
                    b"</body></html>"
                )
            return b"<html></html>"

        self.app.workbench.page_fetcher = fake_fetch

        status, payload = self._request("POST", "/api/hooks/find", {"item_id": "missing"})
        self.assertEqual(404, status)

        status, payload = self._request("POST", "/api/hooks/find", {"item_id": item_id})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertIn("hooks", payload)

        status, article = self._request(
            "POST",
            "/api/articles",
            {
                "item_id": item_id,
                "platform": "wechat",
                "chosen_hooks": [{"phrase": "协和4+4试点班", "why": "公开检索出现争议"}],
            },
        )
        self.assertEqual(200, status)
        self.assertIn("AIGC", article["body"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_web.WebApiTests.test_find_hooks_endpoint_and_articles_accept_chosen_hooks -v`

Expected: FAIL 404 on `/api/hooks/find`

- [ ] **Step 3: Implement**

```python
    def find_hooks(self, item_id: str) -> dict[str, object]:
        return self.workbench.find_hooks(item_id)

    def articles(..., chosen_hooks: object = None):
        ...
        return {
            "ok": True,
            **self.workbench.generate_article(
                item_id, platform, model, provider, effort, self.article_style, chosen_hooks
            ),
        }
```

In `do_POST`:

```python
                elif self.path == "/api/hooks/find":
                    result = app.find_hooks(self._require_string(payload, "item_id"))
                elif self.path == "/api/articles":
                    result = app.articles(
                        ...
                        payload.get("chosen_hooks") if isinstance(payload, dict) else None,
                    )
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_web -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/web.py tests/test_web.py
git commit -m "Expose hook search and chosen_hooks on the web API."
```

---

### Task 7: Workbench 「找切口」 UI (multi-select)

**Files:**
- Modify: `src/autoposting/web.py` `INDEX_HTML` (markup, CSS, JS)
- Test: `tests/test_web.py` page-contract tests

**Interfaces:**
- Consumes: `/api/hooks/find`, `/api/articles` `chosen_hooks`
- Produces: the approved workbench behavior from the spec

Behavior to implement (all of these must appear as strings in the HTML so contract tests can lock them):

- Button `#find-hooks-btn` labeled `找切口`, sibling of `#generate-btn`, `disabled` until a hotspot is selected. `selectHotspot` enables both; no item → both stay disabled.
- `#hook-list` above the article scroll (does not replace title/body). Each card: checkbox, phrase, kind, why, up to 3 result titles/snippets with `href` + `target="_blank"`.
- `state.hooks = []`, `state.chosenHookIds = []` (selection order). Checking appends id; unchecking removes it. First selected labeled `主切口`, others `须进开头`.
- `selectHotspot` and a new `clearHooks()` empty both arrays and the list. `resetArticle` (platform change) does **not** clear hooks.
- Clicking 「找切口」 again replaces the list and clears checkboxes. Calls only `/api/hooks/find`. Status uses `payload.message` when `hooks` is empty (`未发现可用切口`).
- `generate()` sends `chosen_hooks` as the full card objects in `state.chosenHookIds` order. If none checked, omit the field or send `[]` — both are sanitized to “no hooks”.
- Generate does not call `/api/hooks/find`.
- Find failure: `status("article-status", …)` banner; generate stays clickable. Generate failure keeps the cards.

- [ ] **Step 1: Write the failing page-contract test**

```python
    def test_find_hooks_workbench_contract(self) -> None:
        html = self._index()
        self.assertIn("找切口", html)
        self.assertIn('id="find-hooks-btn"', html)
        self.assertIn('id="hook-list"', html)
        self.assertIn('api("/api/hooks/find"', html)
        self.assertIn("chosen_hooks", html)
        self.assertIn("主切口", html)
        self.assertIn("须进开头", html)
        self.assertIn("state.chosenHookIds", html)
        self.assertIn("function clearHooks()", html)
        self.assertIn("clearHooks()", html)
        self.assertLess(html.index("找切口"), html.index("生成文章"))
        self.assertNotIn('api("/api/hooks/find"', html.split("function generate()")[1].split("function persistArticle()")[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_web.WebApiTests.test_find_hooks_workbench_contract -v`

Expected: FAIL `找切口` not found

- [ ] **Step 3: Implement the UI**

In the article `panel-header`, add the button before generate:

```html
      <button type="button" id="find-hooks-btn" disabled>找切口</button>
      <button type="button" id="generate-btn" class="btn-magic" disabled>生成文章</button>
```

After `#article-status`:

```html
    <ul id="hook-list" hidden></ul>
```

Style `#hook-list` like existing cards (`--bg-input`, checkbox row, muted snippet). Keep it compact; max-height + inner scroll if more than four cards.

JS sketch (place next to `generate()`):

```javascript
state.hooks = [];
state.chosenHookIds = [];

function clearHooks() {
  state.hooks = [];
  state.chosenHookIds = [];
  $("hook-list").textContent = "";
  $("hook-list").hidden = true;
}

function selectedHookCards() {
  return state.chosenHookIds.map(function (id) {
    return state.hooks.find(function (hook) { return hook.hook_id === id; });
  }).filter(Boolean);
}

function renderHooks(hooks) {
  state.hooks = hooks || [];
  state.chosenHookIds = [];
  var list = $("hook-list");
  list.textContent = "";
  state.hooks.forEach(function (hook) {
    /* checkbox change: push/splice state.chosenHookIds; relabel 主切口 / 须进开头 */
  });
  list.hidden = state.hooks.length === 0;
}

function findHooks() {
  if (!state.item) { return; }
  var button = $("find-hooks-btn");
  busy(button, true);
  api("/api/hooks/find", {item_id: state.item.item_id}).then(function (data) {
    renderHooks(data.hooks || []);
    status("article-status", (data.hooks && data.hooks.length) ? "" : (data.message || "未发现可用切口"), (data.hooks && data.hooks.length) ? "" : "error");
  }).catch(function (error) {
    status("article-status", showApiError("找切口失败：", error), "error");
  }).then(function () { busy(button, false); });
}
```

In `selectHotspot`: `clearHooks(); $("find-hooks-btn").disabled = false;`  
In `generate` payload: `chosen_hooks: selectedHookCards()` only when length > 0.  
Bind `$("find-hooks-btn").addEventListener("click", findHooks);`

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_web tests.test_workbench -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/autoposting/web.py tests/test_web.py
git commit -m "Add the 找切口 multi-select control to the workbench."
```

---

### Task 8: Version 0.4.0, docs, patch archive

**Files:**
- Modify: `pyproject.toml` `version = "0.4.0"`
- Modify: `src/autoposting/__init__.py` `__version__ = "0.4.0"`
- Modify: `src/autoposting/web.py` `APP_VERSION` and `v0.4.0 · 本地服务`
- Modify: `src/autoposting/llm.py` `HTTP_USER_AGENT = "AutoPosting/0.4.0"`
- Modify: version assertions in `tests/test_web.py` (`0.4.0` twice) and `tests/test_llm.py` (`AutoPosting/0.4.0`)
- Modify: `docs/DECISIONS.md` — add adopted item 9
- Modify: `docs/WEB_FUNCTIONAL_TEST.md` — new section after 「选一条候选，生成微信稿」
- Modify: `README.md` Web API list — document `POST /api/hooks/find` and `chosen_hooks`
- Create: `docs/PATCH_0.4.0.md`
- Create: `archives/autoposting-patch-0.4.0-update-20260815.tar.gz` + `.sha256`

**Interfaces:** none new

- [ ] **Step 1: Write the failing version tests**

Change the three assertions to `0.4.0` / `AutoPosting/0.4.0` first.

- [ ] **Step 2: Run them to see FAIL**

Run: `PYTHONPATH=src:tests python3 -m unittest tests.test_web.WebApiTests.test_version_badge tests.test_web.WebApiTests.test_status_and_index_placeholder tests.test_llm -v`

Expected: FAIL `0.3.4 != 0.4.0`

- [ ] **Step 3: Bump the five lockstep places and docs**

`docs/DECISIONS.md` adopted list, new item:

```
9. 成稿切口：独立「找切口」按钮、现场搜 `{短语} 争议`、人工多选。检索页只当素材。勾选后标题以主切口起笔，其余切口进开头。不建热搜库，不在点开新闻或点生成时搜索。
```

`docs/WEB_FUNCTIONAL_TEST.md` add:

```
### 7b. 找切口（有社会电荷的候选才测）

1. 选中一条后，「找切口」可点。「生成文章」仍可在未找切口时直接点。
2. 点「找切口」，等待列表。
3. 若出现卡片：勾选两张（先勾主切口），再生成。标题应跟第一张，开头含两张；争议表述为公开报道。
4. 换一条无电荷候选：提示「未发现可用切口」，仍可生成。

通过标准：不点找切口的生成与以前相同；勾选后的稿按切口起笔。
```

README: one bullet under Web API for `/api/hooks/find` and `chosen_hooks` on `/api/articles`.

`docs/PATCH_0.4.0.md`: what changed, boundaries (no FactList change, no auto-rewrite), verification (unittest count, self-check, `GET /api/status` version `0.4.0`, browser steps 7b).

- [ ] **Step 4: Run the full suite and self-check**

```
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m autoposting.cli self-check --workspace data/self-check
```

Expected: all tests PASS, self-check `ok`.

- [ ] **Step 5: Archive (only the allow-list) and commit**

```bash
tar -czf archives/autoposting-patch-0.4.0-update-20260815.tar.gz \
  src tests docs config README.md AGENTS.md pyproject.toml \
  service_control.py AutoPosting服务控制.command
shasum -a 256 archives/autoposting-patch-0.4.0-update-20260815.tar.gz \
  > archives/autoposting-patch-0.4.0-update-20260815.tar.gz.sha256
```

Do **not** put `data/`, `output/`, or nested `archives/*.tar.gz` inside the tarball.

```bash
git add pyproject.toml src/autoposting/__init__.py src/autoposting/web.py \
  src/autoposting/llm.py tests/test_web.py tests/test_llm.py \
  docs/DECISIONS.md docs/WEB_FUNCTIONAL_TEST.md docs/PATCH_0.4.0.md README.md \
  archives/autoposting-patch-0.4.0-update-20260815.tar.gz \
  archives/autoposting-patch-0.4.0-update-20260815.tar.gz.sha256
git commit -m "Ship article hooks as 0.4.0 with patch notes."
```

If those project files are still untracked from before this feature, still add **only** the files this task changed, not the whole tree.

---

## Spec coverage

| Spec requirement | Task |
|---|---|
| Independent 「找切口」 button, no search on select/generate | 7 |
| Live `{phrase} 争议` via existing `search_web` | 2, 4 |
| Rule + optional LLM extract, 4+4 stays one phrase | 1, 3 |
| Title-term demotion, max 8 searched / 8 cards | 2 |
| Filter = phrase match AND controversy marker | 1 |
| Multi-select, click order = priority, 主切口 / 须进开头 | 7 |
| Session-only state, clear on item change or re-find | 7 |
| `POST /api/hooks/find` + `chosen_hooks` on articles | 6 |
| Prompt: lead with hooks, 「公开报道曾关注」 | 5 |
| No FactList / source_id for search pages | 4 |
| Empty hooks = old generate path | 4, 7 |
| Soft-fail extract and per-query search | 2, 3 |
| Tests + functional acceptance 1–5 | 1–7, 8 (WEB_FUNCTIONAL_TEST 7b) |
| 0.4.0 lockstep + PATCH + archive | 8 |
| Out of scope: heat archive, vectors, page fetch, shock scores | not scheduled |
