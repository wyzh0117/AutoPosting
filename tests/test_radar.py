import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoposting.decision import DecisionService
from autoposting.decision.adapters.jev import JevAdapter
from autoposting.db import Database
from autoposting.models import FeedItem, Source
from autoposting.radar import build_radar, cluster_candidates, engagement_by_platform
from autoposting.workbench import HotspotService


NOW = "2026-09-19T12:00:00+00:00"


def source(source_id, url, role, source_type="", tier="S3"):
    return {
        "source_id": source_id,
        "url": url,
        "name": source_id,
        "role": role,
        "source_type": source_type,
        "tier": tier,
    }


def candidate(item_id, title, sources, observed_at, engagement=None):
    return {
        "item_id": item_id,
        "title": title,
        "content_text": title,
        "observed_at": observed_at,
        "sources": sources,
        "engagement": engagement or {},
    }


class RadarTests(unittest.TestCase):
    def test_clusters_sources_and_prefers_official_canonical(self):
        candidates = [
            candidate(
                "official-1",
                "OpenAI launches Orion model",
                [source("openai", "https://openai.com/news/orion", "canonical", tier="S1")],
                "2026-09-19T10:00:00+00:00",
            ),
            candidate(
                "media-1",
                "OpenAI launches Orion model - what developers need to know",
                [source("tech-media", "https://example.com/orion", "supporting")],
                "2026-09-19T11:00:00+00:00",
            ),
            candidate(
                "hn-1",
                "OpenAI launches Orion model",
                [source("hn", "https://news.ycombinator.com/item?id=1", "discussion", "rss")],
                "2026-09-19T11:30:00+00:00",
                {"score": 90, "comments": 40},
            ),
            candidate(
                "other-1",
                "A different database release",
                [source("other", "https://example.org/database", "supporting")],
                "2026-09-19T11:00:00+00:00",
            ),
        ]

        events = cluster_candidates(candidates)

        self.assertEqual(2, len(events))
        event = next(row for row in events if row["representative_item_id"] == "official-1")
        self.assertEqual(["official-1", "media-1", "hn-1"], event["item_ids"])
        self.assertEqual("canonical", event["canonical_source"]["role"])
        self.assertEqual("official-1", event["canonical_item_id"])
        self.assertEqual("supporting", event["supporting_sources"][0]["role"])
        self.assertEqual("discussion", event["discussion_sources"][0]["role"])

    def test_official_and_reddit_sources_share_a_stable_cluster(self):
        candidates = [
            candidate(
                "official-orion",
                "OpenAI launches Orion model",
                [
                    source(
                        "official",
                        "https://openai.com/news/orion",
                        "canonical",
                        "official",
                        "S1",
                    )
                ],
                "2026-09-19T10:00:00+00:00",
            ),
            candidate(
                "reddit-orion",
                "OpenAI launches Orion model",
                [
                    source(
                        "reddit-local-llm",
                        "https://www.reddit.com/r/LocalLLaMA/comments/abc/orion",
                        "discussion",
                        "reddit",
                        "S4",
                    )
                ],
                "2026-09-19T11:00:00+00:00",
            ),
        ]

        first = cluster_candidates(candidates)[0]
        refreshed = cluster_candidates(candidates)[0]

        self.assertEqual(1, len(cluster_candidates(candidates)))
        self.assertEqual(first["event_id"], refreshed["event_id"])
        self.assertEqual({"official", "reddit-local-llm"}, set(first["source_ids"]))
        self.assertEqual(2, first["independent_source_count"])
        self.assertEqual("canonical", first["canonical_source"]["role"])
        self.assertEqual("discussion", first["discussion_sources"][0]["role"])

    def test_jev_participates_in_unified_ranking_with_sixty_forty_mix(self):
        candidates = [
            candidate(
                "objective-high",
                "OpenAI announces global security model release",
                [source("openai", "https://openai.com/security", "canonical", tier="S1")],
                NOW,
            ),
            candidate(
                "semantic-high",
                "Developers discuss a new tool",
                [source("media", "https://media.example/tool", "supporting", tier="S3")],
                NOW,
            ),
        ]

        def fake_jev(payload):
            score = 100 if payload["candidate"]["title"].startswith("Developers") else 0
            return {
                "schema_version": "jev-assessment-v1",
                "score": score,
                "confidence": 0.95,
                "decision": "include",
                "reasons": ["fake Jev ranking"],
            }

        events = build_radar(
            candidates,
            now=NOW,
            decision_service=DecisionService(JevAdapter(fake_jev)),
        )

        self.assertEqual("Developers discuss a new tool", events[0]["title"])
        decision = events[0]["decision"]
        self.assertEqual("ok", decision["status"])
        self.assertFalse(decision["degraded"])
        self.assertEqual(
            round(0.6 * decision["objective_score"] + 0.4 * decision["semantic_score"], 4),
            decision["final_score"],
        )
        self.assertEqual(decision["final_score"], events[0]["priority_score"])

    def test_jev_failure_keeps_objective_ranking_and_emits_degraded_diagnostic(self):
        def broken_jev(_payload):
            raise TimeoutError("Authorization: Bearer radar-secret https://example.invalid/jev")

        event = build_radar(
            [
                candidate(
                    "objective-only",
                    "OpenAI announces global security model release",
                    [source("openai", "https://openai.com/security", "canonical", tier="S1")],
                    NOW,
                )
            ],
            now=NOW,
            decision_service=DecisionService(JevAdapter(broken_jev)),
        )[0]

        decision = event["decision"]
        self.assertTrue(decision["degraded"])
        self.assertEqual("timeout", decision["diagnostics"][0]["cause_code"])
        self.assertEqual(decision["objective_score"], decision["final_score"])
        self.assertEqual(decision["final_score"], event["priority_score"])
        detail = str(decision["diagnostics"][0]["detail"])
        self.assertNotIn("radar-secret", detail)
        self.assertNotIn("https://example.invalid/jev", detail)

    def test_scores_are_separate_and_evidence_statuses_do_not_mix(self):
        events = build_radar(
            [
                candidate(
                    "official-1",
                    "Company releases a major security model update",
                    [source("official", "https://official.example/update", "canonical", tier="S1")],
                    "2026-09-19T11:00:00+00:00",
                ),
                candidate(
                    "reddit-1",
                    "Company releases a major security model update discussion",
                    [source("reddit", "https://reddit.com/r/ai/comments/1", "discussion", "reddit")],
                    "2026-09-19T11:30:00+00:00",
                    {"score": 400, "comments": 100},
                ),
            ],
            now=NOW,
        )

        event = events[0]
        self.assertIn("importance_score", event)
        self.assertIn("attention_score", event)
        self.assertIn("priority_score", event)
        self.assertGreaterEqual(event["priority_score"], 0.0)
        self.assertLessEqual(event["priority_score"], 100.0)
        statuses = {fact["status"] for fact in event["evidence_pack"]["facts"]}
        self.assertTrue(statuses <= {"SUPPORTED", "INFERRED", "CONFLICTED", "UNSUPPORTED"})
        self.assertNotIn("INFERRED", {fact["status"] for fact in event["evidence_pack"]["supported_facts"]})
        self.assertTrue(any(fact["status"] == "INFERRED" for fact in event["evidence_pack"]["inference"]))

    def test_engagement_is_platform_normalized_and_duplicate_domains_do_not_add_coverage(self):
        candidates = [
            candidate(
                "hn-1",
                "Same event release",
                [source("hn", "https://news.ycombinator.com/item?id=1", "discussion", "rss")],
                NOW,
                {"score": 100},
            ),
            candidate(
                "media-1",
                "Same event release report",
                [source("media-a", "https://example.com/a", "supporting")],
                NOW,
            ),
            candidate(
                "media-2",
                "Same event release detailed report",
                [source("media-b", "https://example.com/b", "supporting")],
                NOW,
            ),
        ]
        event = cluster_candidates(candidates)[0]
        self.assertEqual(2, event["independent_source_count"])
        self.assertEqual(100.0, engagement_by_platform(event)["hn"])
        scored = build_radar(candidates, now=NOW)[0]
        self.assertLessEqual(scored["attention_breakdown"]["normalized_engagement"], 1.0)

    def test_product_token_conflict_stays_separate_but_headline_qualifier_is_stable(self):
        orion = candidate(
            "orion-1",
            "OpenAI releases Orion model",
            [source("official", "https://openai.com/orion", "canonical", tier="S1")],
            NOW,
        )
        orion_variant = candidate(
            "orion-2",
            "OpenAI announces Orion model today",
            [source("media", "https://media.example/orion", "supporting")],
            NOW,
        )
        nova = candidate(
            "nova-1",
            "OpenAI releases Nova model",
            [source("media-2", "https://media.example/nova", "supporting")],
            NOW,
        )

        events = cluster_candidates([orion, orion_variant, nova])

        self.assertEqual(2, len(events))
        orion_event = next(event for event in events if "orion-1" in event["item_ids"])
        self.assertEqual({"orion-1", "orion-2"}, set(orion_event["item_ids"]))
        self.assertNotIn("nova-1", orion_event["item_ids"])
        first_id = build_radar([orion], now=NOW)[0]["event_id"]
        variant_id = build_radar([orion_variant], now=NOW)[0]["event_id"]
        self.assertEqual(first_id, variant_id)
        self.assertEqual(first_id, build_radar([orion, orion_variant], now=NOW)[0]["event_id"])

        scored = build_radar(
            [orion_variant],
            snapshots=[
                {
                    "event_id": first_id,
                    "platform": "generic",
                    "metric": "engagement",
                    "value": 1,
                    "observed_at": "2026-09-19T11:00:00+00:00",
                }
            ],
            now=NOW,
        )[0]
        self.assertEqual(first_id, scored["event_id"])
        self.assertGreaterEqual(scored["attention_breakdown"]["snapshot_momentum"], 0.0)

    def test_importance_and_attention_can_promote_different_events(self):
        important = build_radar(
            [
                candidate(
                    "important",
                    "OpenAI announces global security risk model release",
                    [source("openai", "https://openai.com/security", "canonical", tier="S1")],
                    NOW,
                )
            ],
            now=NOW,
        )[0]
        hot = build_radar(
            [
                candidate(
                    "hot",
                    "Developers discuss a new tool",
                    [
                        source("hn", "https://news.ycombinator.com/item?id=1", "discussion"),
                        source("reddit", "https://reddit.com/r/ai/1", "discussion", "reddit"),
                        source("x", "https://x.com/user/1", "discussion", "x"),
                        source("code", "https://github.com/acme/tool", "community", "github"),
                    ],
                    NOW,
                    {"score": 200, "comments": 100, "likes": 2000, "reposts": 500, "replies": 300},
                )
            ],
            now=NOW,
        )[0]

        self.assertGreaterEqual(important["importance_score"], 70.0)
        self.assertLess(important["attention_score"], important["importance_score"])
        self.assertGreaterEqual(hot["attention_score"], 70.0)
        self.assertGreater(hot["attention_score"], hot["importance_score"])

    def test_snapshot_migration_and_idempotent_reads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            db.initialize()
            first = db.record_engagement_snapshot("event-1", "hn", "score", 10, NOW)
            second = db.record_engagement_snapshot("event-1", "hn", "score", 20, NOW)
            db.record_engagement_snapshot("event-1", "hn", "score", 30, "2026-09-19T13:00:00+00:00")
            rows = db.list_engagement_snapshots("event-1")

            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(2, len(rows))
            self.assertEqual(20.0, rows[0]["value"])
            self.assertEqual(30.0, db.latest_engagement_snapshot("event-1", "hn", "score")["value"])

    def test_radar_does_not_write_snapshots_implicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            build_radar(
                [candidate("one", "A new model release", [source("official", "https://official.example/a", "canonical")], NOW)],
                now=NOW,
            )
            self.assertEqual([], db.list_engagement_snapshots())


class RadarCollectionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()
        self.official = Source(
            "official", "Official", "rss", "https://openai.com/news.xml", "S1", ("models",), True, 15
        )
        self.media = Source(
            "media", "Media", "rss", "https://media.example/feed.xml", "S3", ("models",), True, 15
        )
        self.sources = (self.official, self.media)
        for source_row in self.sources:
            self.db.upsert_source(source_row)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_window_is_event_level_but_keeps_legacy_candidate_shape(self):
        now = datetime.now(timezone.utc)
        self.db.insert_item(
            self.official,
            FeedItem(
                "official-1",
                "https://openai.com/news/orion",
                "OpenAI announces Orion model",
                "OpenAI announces Orion model details",
                now.isoformat(),
                {},
            ),
        )
        self.db.insert_item(
            self.media,
            FeedItem(
                "media-1",
                "https://media.example/orion",
                "OpenAI announces Orion model report",
                "OpenAI announces Orion model report with context",
                now.isoformat(),
                {},
            ),
        )
        service = HotspotService(self.db, self.sources, fetcher=lambda _: b"<rss><channel/></rss>")

        payload = service.list_window(24, translate=False)

        self.assertEqual(1, len(payload["candidates"]))
        self.assertEqual({"official", "media"}, set(payload["candidates"][0]["source_ids"]))
        self.assertEqual(1, len(payload["radar"]["events"]))
        event = payload["radar"]["events"][0]
        self.assertEqual(payload["candidates"][0]["item_id"], event["representative_item_id"])
        self.assertEqual(
            [row["representative_item_id"] for row in payload["radar"]["events"]],
            [row["item_id"] for row in payload["candidates"]],
        )
        self.assertEqual("canonical", event["canonical_source"]["role"])
        self.assertTrue(event["evidence_pack"]["supported_facts"])

    def test_get_window_is_read_only_and_collect_writes_snapshots(self):
        self.db.insert_item(
            self.official,
            FeedItem(
                "engaged",
                "https://openai.com/news/engaged",
                "OpenAI model release",
                "OpenAI model release",
                datetime.now(timezone.utc).isoformat(),
                {"score": "80"},
            ),
        )
        service = HotspotService(self.db, self.sources, fetcher=lambda _: b"<rss><channel/></rss>")

        self.assertEqual([], self.db.list_engagement_snapshots())
        service.list_window(24, translate=False)
        self.assertEqual([], self.db.list_engagement_snapshots())

        service.collect(24)
        snapshots = self.db.list_engagement_snapshots()
        self.assertTrue(snapshots)
        self.assertEqual("engagement", snapshots[0]["metric"])

    def test_community_scope_still_uses_radar_without_mixing_editorial_items(self):
        reddit = Source(
            "reddit_ai", "Reddit", "reddit", "r/LocalLLaMA", "S4", ("models",), True, 15
        )
        self.db.upsert_source(reddit)
        self.db.insert_item(
            reddit,
            FeedItem(
                "reddit-1",
                "https://www.reddit.com/r/LocalLLaMA/comments/1",
                "Community model discussion",
                "Community model discussion",
                datetime.now(timezone.utc).isoformat(),
                {"score": "300", "comments": "80"},
            ),
        )
        service = HotspotService(self.db, (*self.sources, reddit), fetcher=lambda _: b"<rss><channel/></rss>")

        payload = service.list_window(24, translate=False, scope="community")

        self.assertEqual(1, len(payload["candidates"]))
        self.assertEqual(1, len(payload["radar"]["events"]))
        self.assertEqual("discussion", payload["radar"]["events"][0]["canonical_source"]["role"])
        self.assertTrue(payload["radar"]["events"][0]["evidence_pack"]["uncertainty"])

    def test_primary_candidates_follow_priority_order(self):
        now = datetime.now(timezone.utc)
        self.db.insert_item(
            self.official,
            FeedItem(
                "important",
                "https://openai.com/news/security",
                "OpenAI announces global security risk model release",
                "important",
                now.isoformat(),
                {},
            ),
        )
        self.db.insert_item(
            self.media,
            FeedItem(
                "ordinary",
                "https://media.example/tool",
                "Developers discuss a new tool",
                "ordinary",
                (now - timedelta(minutes=1)).isoformat(),
                {"score": "300", "comments": "120"},
            ),
        )
        service = HotspotService(self.db, self.sources, fetcher=lambda _: b"<rss><channel/></rss>")

        payload = service.list_window(24, translate=False)
        events = payload["radar"]["events"]
        by_rep = {event["representative_item_id"]: event for event in events}
        candidate_reps = [row["item_id"] for row in payload["candidates"]]

        self.assertEqual(candidate_reps, [event["representative_item_id"] for event in events[: len(candidate_reps)]])
        priorities = [float(by_rep[item_id]["priority_score"]) for item_id in candidate_reps]
        self.assertEqual(priorities, sorted(priorities, reverse=True))


if __name__ == "__main__":
    unittest.main()
