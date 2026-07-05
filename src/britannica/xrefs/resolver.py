"""Exact + fuzzy xref resolvers, collision-aware.

When two or more articles share the same title (580 such titles in
the corpus — ZÜRICH canton vs ZÜRICH city, ABBAS I shah vs pasha,
ABERDEEN Scotland vs South Dakota, …), picking the first one we find
misroutes every xref to one of the candidates arbitrarily.  This
module's `disambiguate_among` applies three rules in order:

  1. Self-reference filter.  If the linking article is itself a
     candidate, drop it.  Handles the common case where article A
     mentions its same-named sibling A′ (canton linking to city).

  2. Display disambiguator.  Link wikitext like
     `{{1911link|Zürich|Zürich (city)}}` carries `(city)` in the
     display text.  If exactly one remaining candidate's body
     opening matches the disambiguator (literal or via the small
     synonym set), pick it.  Heuristic by design — baseline is
     random dict-last-write silent-pick, so 60%+ right is a win.

  3. Fallback.  Return the first remaining candidate.  Preserves
     current silent-pick behavior for truly ambiguous cases so no
     existing (lucky) link regresses.

The kind vocabulary and the body-opening matcher live in
`britannica.xrefs.disambiguation` — one owner, shared with the vol29
classified-TOC resolver, which feeds the same matcher a wanted-kind
read off the index structure (bucket / category) instead of a display
parenthetical.
"""
from __future__ import annotations

import re

from britannica.db.models import Article, CrossReference
from britannica.xrefs.disambiguation import body_opening, matches_disambiguator
from britannica.xrefs.scoring import find_fuzzy_match


_LN_DISPLAY_RE = re.compile(r"«LN:[^|]*\|([^«]*)«/LN»")
_PAREN_DISAMBIG_RE = re.compile(r"\(([^)]+)\)")


def _display_disambiguator(xref: CrossReference) -> str | None:
    """Extract a parenthesized disambiguator from the xref's display,
    e.g. 'Zürich (city)' → 'city'.  Returns lowercase or None."""
    surface = xref.surface_text or ""
    m = _LN_DISPLAY_RE.search(surface)
    if not m:
        return None
    display = m.group(1)
    dm = _PAREN_DISAMBIG_RE.search(display)
    if not dm:
        return None
    word = dm.group(1).strip().lower()
    # Don't treat dates or numerals as disambiguators.
    if not word or not re.search(r"[a-z]", word):
        return None
    return word


def disambiguate_among(
    xref: CrossReference, candidates: list[Article], body_of=None
) -> int | None:
    """Apply self-reference + display-disambiguator + fallback rules
    to pick one candidate's id.  Returns None only when the candidate
    list is empty (callers should handle that upstream)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].id

    # Rule 1: drop the linking article if it's in the set.
    remaining = [c for c in candidates if c.id != xref.article_id]
    if not remaining:
        # All candidates are the linking article (shouldn't happen,
        # but guard against empty result).
        remaining = candidates
    if len(remaining) == 1:
        return remaining[0].id

    # Rule 2: display disambiguator against body opening.
    disambiguator = _display_disambiguator(xref)
    if disambiguator:
        matched = [
            c for c in remaining
            if matches_disambiguator(
                disambiguator,
                body_opening(body_of(c) if body_of else (c.body or ""))
            )
        ]
        if len(matched) == 1:
            return matched[0].id

    # Rule 3: fallback — first remaining candidate.  Deterministic
    # and matches the "first-wins" behavior of the pre-fix code.
    return remaining[0].id


def resolve_xref_fuzzy(
    xref: CrossReference, title_map: dict[str, int]
) -> int | None:
    target = xref.normalized_target.strip().upper()
    return find_fuzzy_match(target, title_map)
