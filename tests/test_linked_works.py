import tempfile
import unittest
from pathlib import Path

from autoposting.background import gather_background
from autoposting.db import Database
from autoposting.linked_works import extract_github_repos, fetch_linked_works
from autoposting.models import Source


class LinkedWorksTests(unittest.TestCase):
    def test_extract_github_repos_from_selftext_and_skips_chrome(self) -> None:
        text = (
            "I open-sourced the agent here https://github.com/acme/llm-cli "
            "and wrote notes at https://github.com/sponsors/acme "
            "plus a gist https://gist.github.com/acme/abc."
        )
        self.assertEqual([("acme", "llm-cli")], extract_github_repos(text))

    def test_fetch_readme_and_attach_to_background(self) -> None:
        pages = {
            "https://raw.githubusercontent.com/acme/llm-cli/HEAD/README.md": (
                b"# llm-cli\nA tiny agent CLI.\n\n## Use\nRun `llm ask`.\n"
            )
        }

        def fetcher(url: str) -> bytes:
            if url in pages:
                return pages[url]
            raise OSError("offline")

        works = fetch_linked_works(
            "demo: https://github.com/acme/llm-cli",
            fetcher=fetcher,
        )
        self.assertEqual(1, len(works))
        self.assertEqual("acme/llm-cli", works[0]["full_name"])
        self.assertIn("tiny agent CLI", works[0]["readme"])
        self.assertIn("llm ask", works[0]["readme"])

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = Database(Path(temp.name) / "t.db")
        db.initialize()
        source = Source("reddit_ai", "r/LocalLLaMA", "reddit", "LocalLLaMA", "S4", ("ai",), True, 15)
        db.upsert_source(source)
        note = gather_background(
            db,
            "I built a CLI agent",
            "Repo: https://github.com/acme/llm-cli\nComments",
            fetcher=fetcher,
        )
        self.assertTrue(note.linked_works)
        self.assertIn("tiny agent CLI", note.as_prompt()["linked_works"][0]["readme"])
