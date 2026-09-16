import os
import tempfile
import unittest
from pathlib import Path

from autoposting.image_gen import (
    build_weekly_image_jobs,
    load_image_prompt,
    render_weekly_images,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class WeeklyImageGenTests(unittest.TestCase):
    def test_jobs_are_eleven_and_share_style_bible(self) -> None:
        repos = [
            {
                "rank": index + 1,
                "name": f"tool-{index}",
                "full_name": f"acme/tool-{index}",
                "summary": f"summary {index}",
                "does": "does",
                "scene": "scene",
                "how": "how",
                "kind": "CLI",
                "stars_label": "1.2k",
            }
            for index in range(10)
        ]
        jobs = build_weekly_image_jobs(repos)
        style = load_image_prompt("style")
        self.assertEqual(11, len(jobs))
        self.assertEqual("cover", jobs[0]["kind"])
        self.assertEqual("poster", jobs[1]["kind"])
        self.assertTrue(all(style in job["prompt"] for job in jobs))
        self.assertIn("tool-0", jobs[1]["prompt"])
        self.assertIn("3:4", jobs[0]["prompt"])

    def test_render_writes_pngs_in_display_order(self) -> None:
        repos = [
            {
                "rank": 1,
                "name": "llm-cli",
                "full_name": "acme/llm-cli",
                "summary": "cli",
                "does": "d",
                "scene": "s",
                "how": "h",
                "kind": "CLI",
                "stars_label": "1k",
            }
        ]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = Path(temp.name)
        prompts: list[str] = []

        def generate(prompt: str) -> bytes:
            prompts.append(prompt)
            return TINY_PNG

        jobs = render_weekly_images(build_weekly_image_jobs(repos), generate, out)
        self.assertEqual(2, len(jobs))
        self.assertEqual(["cover", "01-llm-cli"], [job["id"] for job in jobs])
        self.assertTrue(all(job["selected"] for job in jobs))
        self.assertTrue((out / "cover.png").is_file())
        self.assertTrue((out / "01-llm-cli.png").is_file())
        self.assertTrue(all("/api/generated/github-weekly/" in job["url"] for job in jobs))
        self.assertTrue(prompts)

    def test_weekly_prompts_come_from_markdown_files(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        prompt_dir = Path(temp.name)
        (prompt_dir / "STYLE.md").write_text("STYLE-TOKEN-XYZ\n", encoding="utf-8")
        (prompt_dir / "cover.md").write_text(
            "{style}\nCOVER-FOR {ranking}\n", encoding="utf-8"
        )
        (prompt_dir / "poster.md").write_text(
            "{style}\nPOSTER-FOR {name} #{rank}\n", encoding="utf-8"
        )
        previous = os.environ.get("AUTOPOSTING_IMAGE_PROMPT_DIR")
        os.environ["AUTOPOSTING_IMAGE_PROMPT_DIR"] = str(prompt_dir)
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("AUTOPOSTING_IMAGE_PROMPT_DIR", previous)
                if previous is not None
                else os.environ.pop("AUTOPOSTING_IMAGE_PROMPT_DIR", None)
            )
        )
        jobs = build_weekly_image_jobs(
            [
                {
                    "rank": 1,
                    "name": "llm-cli",
                    "full_name": "acme/llm-cli",
                    "summary": "cli",
                    "does": "d",
                    "scene": "s",
                    "kind": "CLI",
                }
            ]
        )
        self.assertIn("STYLE-TOKEN-XYZ", jobs[0]["prompt"])
        self.assertIn("COVER-FOR", jobs[0]["prompt"])
        self.assertIn("llm-cli", jobs[0]["prompt"])
        self.assertIn("POSTER-FOR llm-cli", jobs[1]["prompt"])
        self.assertIn("#1", jobs[1]["prompt"])

    def test_missing_markdown_prompts_fail_before_generation(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        previous = os.environ.get("AUTOPOSTING_IMAGE_PROMPT_DIR")
        os.environ["AUTOPOSTING_IMAGE_PROMPT_DIR"] = temp.name
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("AUTOPOSTING_IMAGE_PROMPT_DIR", previous)
                if previous is not None
                else os.environ.pop("AUTOPOSTING_IMAGE_PROMPT_DIR", None)
            )
        )
        with self.assertRaises(FileNotFoundError):
            build_weekly_image_jobs([])
