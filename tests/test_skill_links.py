"""Tests for the paste-box skill link parser (``autoposting.skill_links``)."""

import ast
import inspect
import random
import string
import sys
import time
import unittest

from autoposting.skill_links import looks_like_skill_links, parse_skill_links

REPO_KEYS = {"source", "source_id", "full_name", "name", "url", "skill_path", "raw_url"}
REJECT_KEYS = {"input", "reason"}
REASONS = {"unsupported_host", "reserved_owner", "not_a_link", "duplicate"}
RESERVED = (
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
    "collections",
    "marketplace",
    "notifications",
    "trending",
)

# Representative forms pinned as whole dicts so the contract cannot drift.
GITHUB_ROOT = {
    "source": "GitHub",
    "source_id": "anthropics/skills",
    "full_name": "anthropics/skills",
    "name": "skills",
    "url": "https://github.com/anthropics/skills",
    "skill_path": "",
    "raw_url": "",
}
TREE_DIR = {
    "source": "GitHub",
    "source_id": "anthropics/skills",
    "full_name": "anthropics/skills",
    "name": "artifacts-builder",
    "url": "https://github.com/anthropics/skills",
    "skill_path": "artifacts-builder/SKILL.md",
    "raw_url": "",
}
RAW_FILE = "https://raw.githubusercontent.com/anthropics/skills/main/artifacts-builder/SKILL.md"
BLOB_SKILL = {
    "source": "GitHub",
    "source_id": "anthropics/skills",
    "full_name": "anthropics/skills",
    "name": "artifacts-builder",
    "url": "https://github.com/anthropics/skills",
    "skill_path": "artifacts-builder/SKILL.md",
    "raw_url": RAW_FILE,
}
RAW_SKILL = {
    "source": "GitHub",
    "source_id": "anthropics/skills",
    "full_name": "anthropics/skills",
    "name": "artifacts-builder",
    "url": "https://github.com/anthropics/skills",
    "skill_path": "artifacts-builder/SKILL.md",
    "raw_url": RAW_FILE,
}
SKILLS_SH_SKILL = {
    "source": "skills.sh",
    "source_id": "anthropics/skills/artifacts-builder",
    "full_name": "anthropics/skills",
    "name": "artifacts-builder",
    "url": "https://skills.sh/anthropics/skills/artifacts-builder",
    "skill_path": "",
    "raw_url": "",
}
SKILLS_SH_REPO = {
    "source": "skills.sh",
    "source_id": "anthropics/skills",
    "full_name": "anthropics/skills",
    "name": "anthropics/skills",
    "url": "https://skills.sh/anthropics/skills",
    "skill_path": "",
    "raw_url": "",
}


class AcceptedFormsTest(unittest.TestCase):
    def test_github_repo_root_exact(self):
        self.assertEqual(
            parse_skill_links("https://github.com/anthropics/skills"),
            ([GITHUB_ROOT], []),
        )

    def test_github_tree_skill_dir_exact(self):
        self.assertEqual(
            parse_skill_links(
                "https://github.com/anthropics/skills/tree/main/artifacts-builder"
            ),
            ([TREE_DIR], []),
        )

    def test_github_blob_skill_md_exact(self):
        self.assertEqual(
            parse_skill_links(
                "https://github.com/anthropics/skills/blob/main/artifacts-builder/SKILL.md"
            ),
            ([BLOB_SKILL], []),
        )

    def test_github_raw_skill_md_exact(self):
        self.assertEqual(parse_skill_links(RAW_FILE), ([RAW_SKILL], []))

    def test_skills_sh_skill_exact(self):
        self.assertEqual(
            parse_skill_links("https://skills.sh/anthropics/skills/artifacts-builder"),
            ([SKILLS_SH_SKILL], []),
        )

    def test_skills_sh_repo_exact(self):
        self.assertEqual(
            parse_skill_links("https://skills.sh/anthropics/skills"),
            ([SKILLS_SH_REPO], []),
        )

    def test_github_tree_deep_path(self):
        candidates, rejects = parse_skill_links(
            "https://github.com/acme/repo/tree/v1.2.3/skills/tool"
        )
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["skill_path"], "skills/tool/SKILL.md")
        self.assertEqual(candidates[0]["name"], "tool")
        self.assertEqual(candidates[0]["full_name"], "acme/repo")

    def test_github_tree_ending_in_skill_md(self):
        # Decision: `/tree/...` keeps the literal last path segment as `name`.
        candidates, rejects = parse_skill_links(
            "https://github.com/acme/repo/tree/main/tool/SKILL.md"
        )
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["skill_path"], "tool/SKILL.md")
        self.assertEqual(candidates[0]["name"], "SKILL.md")
        self.assertEqual(candidates[0]["raw_url"], "")

    def test_github_blob_without_skill_md_follows_tree_rules(self):
        # Decision: a blob that is not SKILL.md behaves like a `/tree/` path.
        candidates, rejects = parse_skill_links(
            "https://github.com/acme/repo/blob/main/tool/README.md"
        )
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["skill_path"], "tool/README.md/SKILL.md")
        self.assertEqual(candidates[0]["name"], "README.md")
        self.assertEqual(candidates[0]["raw_url"], "")

    def test_github_tree_ref_without_path_is_repo_root(self):
        for text in (
            "https://github.com/anthropics/skills/tree/main",
            "https://github.com/anthropics/skills/tree",
            "github.com/anthropics/skills/tree/main",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([GITHUB_ROOT], []))

    def test_github_repo_git_suffix_is_stripped(self):
        candidates, _ = parse_skill_links("https://github.com/acme/repo.git")
        self.assertEqual(candidates[0]["source_id"], "acme/repo")
        self.assertEqual(candidates[0]["name"], "repo")

    def test_http_scheme_and_uppercase_host_are_accepted(self):
        candidates, rejects = parse_skill_links("http://github.com/acme/alpha")
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["source_id"], "acme/alpha")
        self.assertEqual(candidates[0]["url"], "https://github.com/acme/alpha")
        candidates, rejects = parse_skill_links("HTTP://GitHub.com/Acme/Beta")
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["source_id"], "Acme/Beta")
        self.assertEqual(candidates[0]["url"], "https://github.com/Acme/Beta")

    def test_raw_url_other_file_uses_stem(self):
        candidates, rejects = parse_skill_links(
            "https://raw.githubusercontent.com/acme/repo/main/lib/guide.md"
        )
        self.assertEqual(rejects, [])
        self.assertEqual(candidates[0]["name"], "guide")
        self.assertEqual(candidates[0]["skill_path"], "lib/guide.md")
        self.assertEqual(
            candidates[0]["raw_url"],
            "https://raw.githubusercontent.com/acme/repo/main/lib/guide.md",
        )

    def test_raw_url_top_level_skill_md_uses_repo_name(self):
        candidates, _ = parse_skill_links(
            "https://raw.githubusercontent.com/acme/repo/main/SKILL.md"
        )
        self.assertEqual(candidates[0]["name"], "repo")
        self.assertEqual(candidates[0]["skill_path"], "SKILL.md")

    def test_bare_shorthand_is_repo_root(self):
        for text in ("anthropics/skills", "anthropics/skills.git", "www.github.com/anthropics/skills"):
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([GITHUB_ROOT], []))

    def test_bare_three_segments_is_not_guessed_as_path(self):
        text = "anthropics/skills/artifacts-builder"
        self.assertEqual(parse_skill_links(text), ([], [{"input": text, "reason": "not_a_link"}]))

    def test_host_without_scheme(self):
        cases = (
            (
                "github.com/anthropics/skills/tree/main/artifacts-builder",
                TREE_DIR,
            ),
            (RAW_FILE.removeprefix("https://"), RAW_SKILL),
            (
                "skills.sh/anthropics/skills/artifacts-builder",
                SKILLS_SH_SKILL,
            ),
            ("skills.sh/anthropics/skills", SKILLS_SH_REPO),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([expected], []))


class PasteHygieneTest(unittest.TestCase):
    def test_multi_link_mixed_separators_preserve_order(self):
        text = (
            "https://github.com/acme/alpha,https://skills.sh/acme/beta/tool\n"
            "https://raw.githubusercontent.com/acme/gamma/main/delta/SKILL.md，"
            "https://github.com/acme/delta/tree/main/epsilon\n"
        )
        candidates, rejects = parse_skill_links(text)
        self.assertEqual(rejects, [])
        self.assertEqual(
            [item["source_id"] for item in candidates],
            ["acme/alpha", "acme/beta/tool", "acme/gamma", "acme/delta"],
        )
        self.assertEqual(
            [item["source"] for item in candidates],
            ["GitHub", "skills.sh", "GitHub", "GitHub"],
        )

    def test_full_width_comma_separator(self):
        text = "https://github.com/acme/one，https://github.com/acme/two，https://skills.sh/acme/three"
        candidates, rejects = parse_skill_links(text)
        self.assertEqual(rejects, [])
        self.assertEqual(
            [item["source_id"] for item in candidates],
            ["acme/one", "acme/two", "acme/three"],
        )

    def test_trailing_punctuation_is_stripped(self):
        cases = (
            ("https://github.com/acme/alpha.", "acme/alpha"),
            ("https://github.com/acme/beta,", "acme/beta"),
            ("https://github.com/acme/gamma;", "acme/gamma"),
            ("https://github.com/acme/delta)", "acme/delta"),
            ("https://github.com/acme/epsilon）", "acme/epsilon"),
            ("https://github.com/acme/zeta]", "acme/zeta"),
            ("https://github.com/acme/eta>", "acme/eta"),
            ('"https://github.com/acme/theta"', "acme/theta"),
            ("'https://github.com/acme/iota'", "acme/iota"),
            ("“https://github.com/acme/kappa”", "acme/kappa"),
            ("‘https://github.com/acme/lambda’", "acme/lambda"),
            ("「https://github.com/acme/mu」", "acme/mu"),
            ("https://skills.sh/acme/nu/tool。", "acme/nu/tool"),
        )
        for text, source_id in cases:
            with self.subTest(text=text):
                candidates, rejects = parse_skill_links(text)
                self.assertEqual(rejects, [])
                self.assertEqual([item["source_id"] for item in candidates], [source_id])

    def test_wrapped_and_markdown_links(self):
        cases = (
            ("[artifacts-builder](https://github.com/anthropics/skills/tree/main/artifacts-builder)", TREE_DIR),
            ("<https://github.com/anthropics/skills>", GITHUB_ROOT),
            ("`https://github.com/anthropics/skills`", GITHUB_ROOT),
            ("**https://github.com/anthropics/skills**", GITHUB_ROOT),
            ("(https://github.com/anthropics/skills)", GITHUB_ROOT),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([expected], []))

    def test_list_marker_noise(self):
        text = (
            "- https://github.com/acme/alpha\n"
            "* https://github.com/acme/beta\n"
            "1. https://github.com/acme/gamma\n"
            "2、https://github.com/acme/delta\n"
            "10) https://github.com/acme/epsilon\n"
        )
        candidates, rejects = parse_skill_links(text)
        self.assertEqual(rejects, [])
        self.assertEqual(
            [item["source_id"] for item in candidates],
            ["acme/alpha", "acme/beta", "acme/gamma", "acme/delta", "acme/epsilon"],
        )

    def test_duplicate_is_case_insensitive(self):
        text = "https://github.com/Anthropics/Skills https://github.com/anthropics/skills"
        candidates, rejects = parse_skill_links(text)
        self.assertEqual([item["source_id"] for item in candidates], ["Anthropics/Skills"])
        self.assertEqual(
            rejects,
            [{"input": "https://github.com/anthropics/skills", "reason": "duplicate"}],
        )

    def test_duplicate_across_link_forms_shares_source_id(self):
        text = (
            "https://github.com/anthropics/skills/tree/main/artifacts-builder "
            "https://raw.githubusercontent.com/anthropics/skills/main/artifacts-builder/SKILL.md"
        )
        candidates, rejects = parse_skill_links(text)
        self.assertEqual(candidates, [TREE_DIR])
        self.assertEqual(rejects, [{"input": RAW_FILE, "reason": "duplicate"}])

    def test_dot_segments_are_rejected_even_percent_encoded(self):
        """A dot segment must not reach a raw fetch URL in any spelling."""
        for text in (
            "https://github.com/a/b/tree/main/../x",
            "https://github.com/a/b/tree/main/%2e%2e/x",
            "https://github.com/a/b/tree/main/%2E%2E/x",
            "https://github.com/a/b/blob/main/.%2e/x/SKILL.md",
            "a/b/../c",
        ):
            with self.subTest(text=text):
                candidates, rejects = parse_skill_links(text)
                self.assertEqual([], candidates)
                self.assertTrue(rejects, msg=f"{text} 必须被拒绝")

    def test_monorepo_siblings_are_two_skills_not_a_duplicate(self):
        """One repo often hosts many skills: both pasted dirs must survive."""
        text = (
            "https://github.com/anthropics/skills/tree/main/artifacts-builder "
            "https://github.com/anthropics/skills/tree/main/canvas-design"
        )
        candidates, rejects = parse_skill_links(text)
        self.assertEqual([], rejects)
        self.assertEqual(
            [(item["source_id"], item["skill_path"]) for item in candidates],
            [
                ("anthropics/skills", "artifacts-builder/SKILL.md"),
                ("anthropics/skills", "canvas-design/SKILL.md"),
            ],
        )


class RejectTest(unittest.TestCase):
    def test_unsupported_host(self):
        cases = (
            "https://gitlab.com/a/b",
            "https://example.com/x",
            "example.com/a/b",
            "ftp://github.com/a/b",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "unsupported_host"}]),
                )

    def test_reserved_owner_on_github(self):
        for owner in RESERVED:
            for text in (f"https://github.com/{owner}/repo", f"github.com/{owner}/repo"):
                with self.subTest(text=text):
                    self.assertEqual(
                        parse_skill_links(text),
                        ([], [{"input": text, "reason": "reserved_owner"}]),
                    )

    def test_reserved_owner_on_skills_sh(self):
        for text in ("https://skills.sh/trending", "https://skills.sh/trending/tool"):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "reserved_owner"}]),
                )

    def test_every_single_reserved_segment_on_skills_sh(self):
        for owner in RESERVED:
            text = f"https://skills.sh/{owner}"
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "reserved_owner"}]),
                )

    def test_reserved_owner_on_bare_shorthand(self):
        for text in ("topics/repo", "trending/ai", "marketplace/tools"):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "reserved_owner"}]),
                )

    def test_reserved_owner_on_raw_host(self):
        text = "https://raw.githubusercontent.com/topics/x/main/SKILL.md"
        self.assertEqual(
            parse_skill_links(text), ([], [{"input": text, "reason": "reserved_owner"}])
        )

    def test_single_dotted_word_is_not_a_link(self):
        # A lone dotted word is structurally a host only when a path follows it.
        for text in ("README.md", "notes.txt", "example.com"):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "not_a_link"}]),
                )
        self.assertEqual(
            parse_skill_links("example.com/a/b")[1],
            [{"input": "example.com/a/b", "reason": "unsupported_host"}],
        )

    def test_dot_segments_are_rejected(self):
        for text in (
            "https://github.com/a/b/blob/main/../SKILL.md",
            "https://github.com/a/b/tree/main/./x",
            "https://raw.githubusercontent.com/a/b/main/../SKILL.md",
            "https://skills.sh/a/b/../x",
            "a/b/../x",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text),
                    ([], [{"input": text, "reason": "not_a_link"}]),
                )

    def test_not_a_link(self):
        for text in (
            "你好",
            "hello",
            "just some words",
            "artifacts-builder",
            "https://github.com",
            "https://github.com/onlyowner",
            "https://raw.githubusercontent.com/acme",
            "topics",
        ):
            with self.subTest(text=text):
                candidates, rejects = parse_skill_links(text)
                self.assertEqual(candidates, [])
                self.assertTrue(rejects)
                self.assertTrue(all(item["reason"] == "not_a_link" for item in rejects))

    def test_reject_input_echoes_the_token_as_pasted(self):
        cases = (
            ("https://gitlab.com/a/b).", "unsupported_host"),
            ("**https://gitlab.com/a/b**", "unsupported_host"),
            ("1.https://gitlab.com/a/b", "unsupported_host"),
            ("1.5", "not_a_link"),
            ("-你好.", "not_a_link"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    parse_skill_links(text), ([], [{"input": text, "reason": reason}])
                )

    def test_duplicate_reject_echoes_the_token_as_pasted(self):
        text = "- https://github.com/acme/alpha.\n- https://github.com/acme/alpha."
        candidates, rejects = parse_skill_links(text)
        self.assertEqual([item["source_id"] for item in candidates], ["acme/alpha"])
        self.assertEqual(
            rejects, [{"input": "https://github.com/acme/alpha.", "reason": "duplicate"}]
        )

    def test_every_reject_reason_is_known_ascii(self):
        text = "你好 https://gitlab.com/a/b https://github.com/topics/x https://github.com/a/b https://github.com/A/B"
        _, rejects = parse_skill_links(text)
        self.assertEqual(
            [item["reason"] for item in rejects],
            ["not_a_link", "unsupported_host", "reserved_owner", "duplicate"],
        )
        for item in rejects:
            self.assertEqual(set(item), REJECT_KEYS)
            self.assertIn(item["reason"], REASONS)
            self.assertTrue(item["reason"].isascii())


class RobustnessTest(unittest.TestCase):
    def test_empty_input_returns_empty_pair(self):
        for text in ("", " ", "\n\n", "\t \t"):
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([], []))

    def test_noise_only_input_returns_empty_pair(self):
        for text in ("...", "，,,", "-", "*", "1.", "`", "()", "[]", "， ，"):
            with self.subTest(text=text):
                self.assertEqual(parse_skill_links(text), ([], []))

    def test_garbage_never_raises(self):
        for text in (
            "你好世界",
            "https://",
            "http://",
            "://",
            "////",
            "[]()",
            "\x00",
            "a" * 10000,
            "🎉/🎉",
            "https://github.com／a／b",  # full-width slashes: urlsplit raises
            "http://[::1",
            "https://user:pass@github.com",
        ):
            with self.subTest(text=text[:40]):
                candidates, rejects = parse_skill_links(text)
                self.assertIsInstance(candidates, list)
                self.assertIsInstance(rejects, list)
                self.assertEqual(candidates, [])

    def test_non_string_input_returns_empty_pair(self):
        for value in (None, 123, b"https://github.com/a/b", ["https://github.com/a/b"]):
            with self.subTest(value=repr(value)):
                self.assertEqual(parse_skill_links(value), ([], []))

    def test_random_fuzz_never_breaks_the_contract(self):
        # Deterministic seed: a paste box must never raise, and every returned
        # record must keep the documented shape.
        rng = random.Random(20240613)
        alphabet = string.printable + "你好，、（）【】🎉"
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            candidates, rejects = parse_skill_links(text)
            self.assertEqual(looks_like_skill_links(text), bool(candidates))
            for candidate in candidates:
                self.assertEqual(set(candidate), REPO_KEYS)
                self.assertTrue(all(isinstance(value, str) for value in candidate.values()))
                self.assertIn(candidate["source"], {"GitHub", "skills.sh"})
                self.assertTrue(candidate["source_id"])
                self.assertTrue(candidate["url"].startswith("https://"))
            for reject in rejects:
                self.assertEqual(set(reject), REJECT_KEYS)
                self.assertIn(reject["reason"], REASONS)
                self.assertTrue(reject["reason"].isascii())

    def test_candidate_keys_and_value_types(self):
        text = (
            "https://github.com/acme/alpha "
            "https://skills.sh/acme/beta/tool "
            "https://raw.githubusercontent.com/acme/gamma/main/delta/SKILL.md"
        )
        candidates, _ = parse_skill_links(text)
        self.assertEqual(len(candidates), 3)
        for candidate in candidates:
            self.assertEqual(set(candidate), REPO_KEYS)
            self.assertTrue(all(isinstance(value, str) for value in candidate.values()))


class ModuleContractTest(unittest.TestCase):
    """Pins the parser's stdlib-only, I/O-free, documented surface."""

    def test_module_is_stdlib_only_and_io_free(self):
        module = sys.modules[parse_skill_links.__module__]
        tree = ast.parse(inspect.getsource(module))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "re", "urllib"})
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called, msg="the parser must not touch the filesystem")

    def test_every_helper_has_a_one_line_docstring(self):
        module = sys.modules[parse_skill_links.__module__]
        helpers = [
            obj
            for name, obj in vars(module).items()
            if name.startswith("_")
            and inspect.isfunction(obj)
            and obj.__module__ == module.__name__
        ]
        self.assertGreaterEqual(len(helpers), 10)
        for helper in helpers:
            doc = inspect.getdoc(helper)
            self.assertTrue(doc, msg=f"{helper.__name__} has no docstring")
            self.assertEqual(
                len(doc.splitlines()), 1, msg=f"{helper.__name__} docstring is not one line"
            )

    def test_adversarial_input_stays_fast(self):
        # Repeated marker/punctuation peeling was quadratic once; keep it linear.
        start = time.perf_counter()
        parse_skill_links("1." * 100000)
        parse_skill_links("[" * 100000)
        parse_skill_links("(" * 100000)
        parse_skill_links("a" * 500000)
        parse_skill_links("https://github.com/acme/tool, " * 5000)
        self.assertLess(time.perf_counter() - start, 5.0)


class LooksLikeSkillLinksTest(unittest.TestCase):
    def test_true_for_parseable_text(self):
        for text in (
            "https://github.com/anthropics/skills",
            "pasted: https://skills.sh/anthropics/skills/artifacts-builder",
            "- https://github.com/acme/alpha.",
            "acme/alpha",
        ):
            with self.subTest(text=text):
                self.assertTrue(looks_like_skill_links(text))

    def test_false_for_non_links(self):
        for text in (
            "",
            "   ",
            "你好",
            "hello",
            "not a link at all",
            "https://gitlab.com/a/b",
            "https://github.com/topics/x",
        ):
            with self.subTest(text=text):
                self.assertFalse(looks_like_skill_links(text))

    def test_never_raises(self):
        for value in (None, 123, b"https://github.com/a/b", "https://github.com／a／b", "://"):
            with self.subTest(value=repr(value)):
                self.assertIs(looks_like_skill_links(value), False)


if __name__ == "__main__":
    unittest.main()
