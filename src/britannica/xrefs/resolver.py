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
"""
from __future__ import annotations

import re

from britannica.db.models import Article, CrossReference
from britannica.xrefs.scoring import find_fuzzy_match


# Disambiguator word (lowercase) → set of literal words that, if found
# in a candidate's body opening, indicate a match.  Keep transparent
# and narrow; expand as concrete collision cases reveal blind spots.
_DISAMBIGUATOR_SYNONYMS: dict[str, set[str]] = {
    # Geographic — the ZÜRICH (city) case + friends
    "city":       {"city", "town", "capital", "burgh", "seaport", "municipality"},
    "town":       {"town", "city", "village", "burgh"},
    "village":    {"village", "hamlet", "town"},
    "canton":     {"canton", "cantonal"},
    "county":     {"county", "shire"},
    "state":      {"state", "commonwealth"},
    "province":   {"province", "territory"},
    "region":     {"region", "district", "territory"},
    "river":      {"river", "stream", "tributary", "affluent"},
    "lake":       {"lake", "loch", "lough"},
    "mountain":   {"mountain", "peak", "ridge", "summit"},
    "island":     {"island", "isle"},

    # Office / title — ABBAS I (shah) etc.
    "saint":      {"saint"},
    "pope":       {"pope", "pontiff"},
    "king":       {"king", "monarch"},
    "queen":      {"queen"},
    "emperor":    {"emperor"},
    "empress":    {"empress"},
    "prince":     {"prince"},
    "duke":       {"duke"},
    "earl":       {"earl", "count"},
    "baron":      {"baron"},
    "bishop":     {"bishop", "archbishop"},
    "priest":     {"priest", "divine", "cleric"},
    "shah":       {"shah"},
    "pasha":      {"pasha"},
    "sultan":     {"sultan"},
    "caliph":     {"caliph"},
    "patriarch":  {"patriarch"},

    # Occupation — ABERNETHY (surgeon), ABBOT (divine)
    "writer":     {"writer", "author", "novelist", "dramatist"},
    "poet":       {"poet"},
    "painter":    {"painter", "artist"},
    "sculptor":   {"sculptor"},
    "composer":   {"composer"},
    "philosopher":{"philosopher"},
    "historian":  {"historian"},
    "general":    {"general", "commander"},
    "admiral":    {"admiral"},
    "physician":  {"physician", "surgeon", "doctor"},
    "mathematician": {"mathematician"},
    "astronomer": {"astronomer"},
    "statesman":  {"statesman", "politician"},
    "jurist":     {"jurist"},
    "dramatist":  {"dramatist", "playwright"},
    "surgeon":    {"surgeon", "physician"},
    "divine":     {"divine", "priest", "cleric", "theologian"},
}


_LN_DISPLAY_RE = re.compile(r"«LN:[^|]*\|([^«]*)«/LN»")
_PAREN_DISAMBIG_RE = re.compile(r"\(([^)]+)\)")
_PAGE_MARKER_RE = re.compile(r"\x01PAGE:\d+\x01")
_INNER_MARKER_RE = re.compile(r"«/?[A-Z]+(?::[^«»]*)?»")


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


def _body_opening(body: str, chars: int = 300) -> str:
    """First chunk of body with PAGE / marker scaffolding stripped."""
    if not body:
        return ""
    b = _PAGE_MARKER_RE.sub("", body)
    b = _INNER_MARKER_RE.sub("", b)
    return b[:chars]


def _matches_disambiguator(disambiguator: str, body_opening: str) -> bool:
    """True if disambiguator (or a synonym) appears as a word in body."""
    words = _DISAMBIGUATOR_SYNONYMS.get(disambiguator, {disambiguator})
    text = body_opening.lower()
    for w in words:
        if re.search(r"\b" + re.escape(w) + r"\b", text):
            return True
    return False


def disambiguate_among(
    xref: CrossReference, candidates: list[Article]
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
            if _matches_disambiguator(
                disambiguator, _body_opening(c.body or "")
            )
        ]
        if len(matched) == 1:
            return matched[0].id

    # Rule 3: fallback — first remaining candidate.  Deterministic
    # and matches the "first-wins" behavior of the pre-fix code.
    return remaining[0].id


def resolve_xref_exact(
    xref: CrossReference, articles: list[Article]
) -> int | None:
    """Resolve an xref against an article list by exact title match,
    applying collision-aware disambiguation when two or more match."""
    target = xref.normalized_target.strip().upper()
    matches: list[Article] = []
    for article in articles:
        if article.article_type == "plate":
            continue
        if article.title.strip().upper() == target:
            matches.append(article)
    if not matches:
        return None
    return disambiguate_among(xref, matches)


def resolve_xref_fuzzy(
    xref: CrossReference, title_map: dict[str, int]
) -> int | None:
    target = xref.normalized_target.strip().upper()
    return find_fuzzy_match(target, title_map)
