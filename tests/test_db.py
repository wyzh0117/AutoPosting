import tempfile
import unittest
from pathlib import Path

from autoposting.db import Database


class HotspotFavoriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "autoposting.db")
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_favorite_add_list_remove_round_trip(self) -> None:
        first = self.db.add_hotspot_favorite(
            "item-a",
            "Alpha title",
            "Alpha summary",
            {"item_id": "item-a", "title": "Alpha title", "summary": "Alpha summary"},
        )
        second = self.db.add_hotspot_favorite(
            "item-b",
            "Beta title",
            "Beta summary",
            {"item_id": "item-b", "title": "Beta title", "summary": "Beta summary"},
        )

        listed = self.db.list_hotspot_favorites()
        self.assertEqual(["item-b", "item-a"], [row["item_id"] for row in listed])
        self.assertEqual("Beta title", listed[0]["title"])
        self.assertEqual("Beta summary", listed[0]["summary"])
        self.assertEqual("item-b", listed[0]["payload"]["item_id"])
        self.assertTrue(first["favorited_at"])
        self.assertTrue(second["favorited_at"])
        self.assertGreaterEqual(second["favorited_at"], first["favorited_at"])

        removed = self.db.remove_hotspot_favorite("item-b")
        self.assertTrue(removed)
        remaining = self.db.list_hotspot_favorites()
        self.assertEqual(["item-a"], [row["item_id"] for row in remaining])
        self.assertFalse(self.db.remove_hotspot_favorite("item-b"))
