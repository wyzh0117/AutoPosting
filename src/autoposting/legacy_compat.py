"""Thin compatibility translation for retired lane wording.

The two-iteration refactor retired the lane product machinery: the lane tabs,
``the retired lane body class prefix*`` theming, the GitHub/Skill lanes and the ``lane`` query parameter
are gone from the product.  What remains is one legitimate business question
that the old lanes happened to answer: whether a candidate comes from the
editorial subscription set or from community sources.

That question is now expressed directly as a *scope* contract:

``official``
    Editorial subscriptions only (the retired ``hotspots`` lane).
``community``
    Community sources only (the retired ``viewpoints`` lane).
``all``
    Both (the retired ``mixed`` lane, and the default).

Nothing persists a lane, so no persisted-payload parser is needed.  This module
only keeps the retired ``lane`` request parameter working for external callers
(scripts, bookmarks, the CLI façade) so that an old request keeps returning the
same rows it used to; internal code speaks ``scope`` and never imports the
retired names for logic.
"""

from __future__ import annotations

SCOPE_OFFICIAL = "official"
SCOPE_COMMUNITY = "community"
SCOPE_ALL = "all"

VALID_SCOPES: tuple[str, ...] = (SCOPE_OFFICIAL, SCOPE_COMMUNITY, SCOPE_ALL)

DEFAULT_SCOPE = SCOPE_ALL

#: The retired ``lane`` parameter mapped onto the scope it used to mean.
LEGACY_LANE_SCOPES: dict[str, str] = {
    "hotspots": SCOPE_OFFICIAL,
    "viewpoints": SCOPE_COMMUNITY,
    "mixed": SCOPE_ALL,
}

SCOPE_ERROR = "scope must be official, community, or all"
LANE_ERROR = "lane must be hotspots, viewpoints, or mixed"


def translate_legacy_lane(lane: object) -> str:
    """Return the scope a retired ``lane`` value meant.

    Raises :class:`ValueError` for anything else.  A translation helper that
    returned ``None`` would let a caller write ``translate(...) or SCOPE_ALL``,
    which is precisely the silent widening this boundary exists to prevent.
    """
    if isinstance(lane, str):
        translated = LEGACY_LANE_SCOPES.get(lane.strip().lower())
        if translated is not None:
            return translated
    raise ValueError(LANE_ERROR)


def normalize_scope(scope: object) -> str:
    """Return a validated scope; a blank value means :data:`DEFAULT_SCOPE`."""
    if scope is None or (isinstance(scope, str) and not scope.strip()):
        return DEFAULT_SCOPE
    if isinstance(scope, str):
        candidate = scope.strip().lower()
        if candidate in VALID_SCOPES:
            return candidate
    raise ValueError(SCOPE_ERROR)


def resolve_scope(scope: object = None, lane: object = None) -> str:
    """Resolve the request scope, falling back to the retired ``lane`` alias.

    ``scope`` wins whenever it names a real scope; ``lane`` is only consulted
    when ``scope`` is absent, so a caller that sends both always gets the
    explicit scope.  An unknown value on either parameter is an error rather
    than a silent default, because a filter typo must not widen the result set.
    """
    if scope is None or (isinstance(scope, str) and not scope.strip()):
        if lane is None or (isinstance(lane, str) and not lane.strip()):
            return DEFAULT_SCOPE
        return translate_legacy_lane(lane)
    return normalize_scope(scope)