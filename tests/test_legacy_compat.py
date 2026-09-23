"""The scope contract and its one remaining legacy alias.

The official/community filter is real business: it is how the workbench shows
editorial sources separately from community ones. It is expressed as ``scope``
everywhere. The retired ``lane`` vocabulary survives only as a request-level
alias, and only so old links and old bookmarks keep working — it must never
widen a filter the caller did not ask for.
"""

import unittest

from autoposting.legacy_compat import (
    DEFAULT_SCOPE,
    LANE_ERROR,
    LEGACY_LANE_SCOPES,
    SCOPE_ALL,
    SCOPE_COMMUNITY,
    SCOPE_ERROR,
    SCOPE_OFFICIAL,
    VALID_SCOPES,
    normalize_scope,
    resolve_scope,
    translate_legacy_lane,
)


class LegacyLaneTranslationTests(unittest.TestCase):
    def test_every_retired_lane_maps_to_a_scope(self) -> None:
        self.assertEqual(SCOPE_OFFICIAL, translate_legacy_lane("hotspots"))
        self.assertEqual(SCOPE_COMMUNITY, translate_legacy_lane("viewpoints"))
        self.assertEqual(SCOPE_ALL, translate_legacy_lane("mixed"))

    def test_lane_translation_is_forgiving_about_case_and_space(self) -> None:
        for value in ("Hotspots", "HOTSPOTS", " hotspots "):
            with self.subTest(value=value):
                self.assertEqual(SCOPE_OFFICIAL, translate_legacy_lane(value))

    def test_unknown_lane_raises_instead_of_widening(self) -> None:
        # Silently falling back to `all` is exactly the failure mode this whole
        # refactor is supposed to remove: a filter the user asked for disappears.
        for value in ("unknown", "", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    translate_legacy_lane(value)
                self.assertEqual(LANE_ERROR, str(raised.exception))

    def test_the_lane_table_is_exactly_the_retired_vocabulary(self) -> None:
        self.assertEqual({"hotspots", "viewpoints", "mixed"}, set(LEGACY_LANE_SCOPES))


class ScopeNormalizationTests(unittest.TestCase):
    def test_scope_normalizes_case_and_space(self) -> None:
        for value in ("official", " Official ", "OFFICIAL"):
            with self.subTest(value=value):
                self.assertEqual(SCOPE_OFFICIAL, normalize_scope(value))

    def test_empty_scope_means_the_default_scope(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(DEFAULT_SCOPE, normalize_scope(value))

    def test_unknown_scope_raises(self) -> None:
        with self.assertRaises(ValueError) as raised:
            normalize_scope("bogus")
        self.assertEqual(SCOPE_ERROR, str(raised.exception))

    def test_the_scope_set_is_closed(self) -> None:
        self.assertEqual({SCOPE_OFFICIAL, SCOPE_COMMUNITY, SCOPE_ALL}, set(VALID_SCOPES))
        self.assertIn(DEFAULT_SCOPE, VALID_SCOPES)


class ScopeResolutionTests(unittest.TestCase):
    def test_scope_wins_when_both_are_present(self) -> None:
        # A caller that sends both means the new contract; the alias must not
        # override an explicit scope.
        self.assertEqual(SCOPE_COMMUNITY, resolve_scope("community", "hotspots"))

    def test_lane_is_used_only_when_scope_is_absent(self) -> None:
        self.assertEqual(SCOPE_OFFICIAL, resolve_scope(None, "hotspots"))
        self.assertEqual(SCOPE_COMMUNITY, resolve_scope(None, "viewpoints"))
        self.assertEqual(SCOPE_ALL, resolve_scope(None, "mixed"))

    def test_no_filter_means_the_default_scope(self) -> None:
        self.assertEqual(DEFAULT_SCOPE, resolve_scope())
        self.assertEqual(DEFAULT_SCOPE, resolve_scope(None, None))
        self.assertEqual(DEFAULT_SCOPE, resolve_scope("", ""))

    def test_an_unknown_value_never_silently_widens(self) -> None:
        with self.assertRaises(ValueError) as raised:
            resolve_scope("bogus")
        self.assertEqual(SCOPE_ERROR, str(raised.exception))

        with self.assertRaises(ValueError) as raised:
            resolve_scope(None, "bogus")
        self.assertEqual(LANE_ERROR, str(raised.exception))

        # A caller that sent a valid lane and an invalid scope must hear about
        # the invalid scope rather than get the lane's meaning.
        with self.assertRaises(ValueError) as raised:
            resolve_scope("bogus", "hotspots")
        self.assertEqual(SCOPE_ERROR, str(raised.exception))


if __name__ == "__main__":
    unittest.main()