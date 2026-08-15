import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoposting.db import Database
from autoposting.hotspots import HotspotConfig, HotspotDetector, Observation


UTC = timezone.utc


class HotspotTests(unittest.TestCase):
    def test_rule_triggers_and_event_write_is_idempotent(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        observations = [
            Observation("official", "S1", now - timedelta(minutes=10)),
            Observation("employee", "S2", now - timedelta(minutes=20)),
            Observation("media", "S3", now - timedelta(minutes=30)),
            Observation("official", "S1", now - timedelta(minutes=35)),
            Observation("employee", "S2", now - timedelta(minutes=40)),
            Observation("media", "S3", now - timedelta(minutes=50)),
        ]
        decision = HotspotDetector(HotspotConfig()).evaluate(
            topic="new-model",
            observations=observations,
            baseline_counts=[1, 1, 1, 1, 1, 1, 1],
            now=now,
        )

        self.assertTrue(decision.triggered)
        self.assertEqual(6, decision.current_count)
        self.assertEqual(3, decision.independent_sources)
        self.assertEqual(1.0, decision.statistical_threshold)
        self.assertEqual((), decision.reasons)

        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "autoposting.db")
            db.initialize()
            first_id = db.record_hotspot(decision)
            second_id = db.record_hotspot(decision)
            self.assertEqual(first_id, second_id)
            self.assertEqual(1, db.count_events())

    def test_high_baseline_and_cooldown_do_not_trigger(self) -> None:
        now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        observations = [
            Observation(f"source-{index % 3}", "S1" if index == 0 else "S3", now - timedelta(minutes=index))
            for index in range(6)
        ]
        detector = HotspotDetector(HotspotConfig())

        high_baseline = detector.evaluate(
            "new-model", observations, [8, 8, 8, 8, 8, 8, 8], now
        )
        cooling_down = detector.evaluate(
            "new-model",
            observations,
            [1, 1, 1, 1, 1, 1, 1],
            now,
            last_triggered_at=now - timedelta(hours=2),
        )

        self.assertFalse(high_baseline.triggered)
        self.assertIn("below_statistical_threshold", high_baseline.reasons)
        self.assertFalse(cooling_down.triggered)
        self.assertIn("cooldown_active", cooling_down.reasons)


if __name__ == "__main__":
    unittest.main()