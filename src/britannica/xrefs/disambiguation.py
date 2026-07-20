"""Shared, DB-free kind vocabulary + lead-kind reader.

When two or more articles share a title (ZÜRICH canton vs ZÜRICH city,
ABEL biblical vs chemist vs musician, ABERDEEN Scotland vs South Dakota),
the discriminator is a *kind* word — and every EB1911 article states its
own kind in its opening clause ("one of the cantons of…", "the capital
of…", "(1827-1902), English chemist").  ``lead_kind`` reads that first
is-a noun; ``pick_by_kind`` settles a collision on it when a caller knows
the wanted kind (the vol29 classified-TOC resolver's bucket — ``Towns`` →
``town``; the kind index; the contributor footprint match).

The old whole-opening word-grep (``matches_disambiguator``) and the display-
parenthetical hint reader (``hint_kind``) retired with the xref cascade they
served — article xrefs now prose-fish through ``LinkResolver``.
"""
from __future__ import annotations

import re

from britannica.markers import PAGE_MARKER_RE as _PAGE_MARKER_RE

_INNER_MARKER_RE = re.compile(r"«/?[A-Z]+(?::[^«»]*)?»")


def body_opening(body: str, chars: int = 300) -> str:
    """First chunk of body with PAGE / marker scaffolding stripped."""
    if not body:
        return ""
    b = _PAGE_MARKER_RE.sub("", body)
    b = _INNER_MARKER_RE.sub("", b)
    return b[:chars]


# ── The article's OWN kind, read off its lead (first is-a) ────────────────
# A whole-opening word-grep mis-fires when a *city* names its canton ("the
# capital of the canton of Zürich").  The sharper signal is the FIRST is-a
# noun: the canton says "one of the cantons" first, the city says "the
# capital" first.  `lead_kind` returns that earliest kind; later nouns are
# decoys.
_LEAD_NOUNS: list[tuple[str, str]] = [
    # places
    ("capital", "city"), ("chief town", "city"), ("chief city", "city"),
    ("seaport", "town"), ("town", "town"), ("city", "city"),
    ("borough", "town"), ("village", "town"), ("watering-place", "town"),
    ("canton", "division"), ("county", "division"), ("shire", "division"),
    ("province", "division"), ("department", "division"),
    ("government", "division"), ("district", "division"),
    ("kingdom", "division"), ("duchy", "division"),
    ("principality", "division"), ("governorate", "division"),
    ("prefecture", "division"), ("arrondissement", "division"),
    ("state", "division"), ("commonwealth", "division"),
    ("river", "river"), ("stream", "river"), ("tributary", "river"),
    ("affluent", "river"),
    ("lake", "lake"), ("loch", "lake"), ("lough", "lake"),
    ("mountain", "mountain"), ("peak", "mountain"), ("volcano", "mountain"),
    ("island", "island"), ("isle", "island"), ("archipelago", "island"),
    # persons
    ("chemist", "chemist"), ("mathematician", "mathematician"),
    ("physicist", "physicist"), ("astronomer", "astronomer"),
    ("naturalist", "naturalist"), ("zoologist", "naturalist"),
    ("botanist", "naturalist"), ("geologist", "geologist"),
    ("philosopher", "philosopher"), ("historian", "historian"),
    ("theologian", "theologian"), ("divine", "theologian"),
    ("poet", "poet"), ("novelist", "writer"), ("dramatist", "writer"),
    ("playwright", "writer"), ("author", "writer"),
    ("painter", "painter"), ("sculptor", "sculptor"),
    ("architect", "architect"), ("composer", "musician"),
    ("musician", "musician"), ("organist", "musician"),
    ("physician", "physician"), ("surgeon", "physician"),
    ("general", "general"), ("admiral", "admiral"),
    ("statesman", "statesman"), ("politician", "statesman"),
    ("engineer", "engineer"), ("king", "king"), ("emperor", "emperor"),
    ("pope", "pope"), ("saint", "saint"), ("bishop", "bishop"),
    ("actor", "actor"), ("actress", "actor"), ("tragedian", "actor"),
    ("comedian", "actor"), ("dancer", "actor"),
    # ethnic groups (a "Races and Tribes" bucket) and natural kinds (a
    # "Birds"/"Mammals" bucket) -- the old domain disambiguator's ETHNIC /
    # NATURE, restated as first-is-a kinds.
    ("tribe", "ethnic"), ("people", "ethnic"), ("race", "ethnic"),
    ("nation", "ethnic"), ("caste", "ethnic"), ("nomad", "ethnic"),
    ("bird", "nature"), ("mammal", "nature"), ("fish", "nature"),
    ("insect", "nature"), ("plant", "nature"), ("genus", "nature"),
    ("tree", "nature"), ("shrub", "nature"), ("reptile", "nature"),
    ("mollusc", "nature"), ("fungus", "nature"), ("flower", "nature"),
    ("batrachian", "nature"),
]

PLACE_KINDS = {"division", "town", "city", "lake", "river", "mountain",
               "island"}
PERSON_KINDS = {"chemist", "mathematician", "physicist", "astronomer",
                "naturalist", "geologist", "philosopher", "historian",
                "theologian", "poet", "writer", "painter", "sculptor",
                "architect", "musician", "physician", "general", "admiral",
                "statesman", "engineer", "king", "emperor", "pope", "saint",
                "bishop", "actor"}

# Institutional / event titles that share a place-or-person name but are
# never what a place/biography bucket points at (CALIFORNIA, UNIVERSITY OF
# under "States"; NASEBY, BATTLE OF under a town run).
INSTITUTIONAL_RE = re.compile(
    r",\s*(UNIVERSITY|COLLEGE|BATTLE|TREATY|COUNCIL|SIEGE|CONVENTION|"
    r"SYNOD|DIET|ORDER|CONFERENCE|ACT)\b", re.I)


def lead_kind(opening: str):
    """The article's own kind = the earliest is-a noun in its opening.

    Matches the singular, plus the plural ONLY in the "one of the …Xs" is-a form
    ("Zürich, one of the *cantons* of…").  A bare PARTITIVE plural is
    containment, not is-a — "a republic of 31 *states*", "the largest of the
    *islands*" — and must not match, or a country reads as a division and the
    largest-of-islands as an island (the Zürich-canton fix's overreach)."""
    text = opening.lower()[:220]
    best_pos, best_kind = len(text) + 1, None
    for word, kind in _LEAD_NOUNS:
        w = re.escape(word)
        singular = re.search(r"\b" + w + r"\b", text)
        # Plural ONLY in the "one of the …Xs" is-a form (Zürich, ONE OF THE
        # cantons of…).  Every other plural is too risky to read as this
        # article's kind: a bare partitive is containment ("a republic of 31
        # states"), and a named-plural feature ("the Jura Mountains") is
        # indistinguishable from it in prose -- deferred rather than guessed.
        plural = re.search(r"\bone of\b[^.]{0,30}?\b" + w + r"s\b", text)
        pos = min((m.start() for m in (singular, plural) if m), default=None)
        if pos is not None and pos < best_pos:
            best_pos, best_kind = pos, kind
    return best_kind


def kind_qualifies(lk: str | None, want: str) -> bool:
    """Does an article whose lead-kind is `lk` satisfy the wanted kind?
    `want` is a specific kind (division/chemist/…), 'town' (city|town), or
    the class token 'PERSON'."""
    if lk is None:
        return False
    if want == "PERSON":
        return lk in PERSON_KINDS
    if want == "town":
        return lk in ("town", "city")
    return lk == want


def pick_by_kind(candidates, want, opening_of):
    """The UNIQUE candidate whose kind qualifies for `want`, ignoring
    institutional/event decoys.  Returns None (abstain) if zero or many
    qualify, or if `want` is falsy.  `candidates` is a list of (key, title);
    `opening_of(key)` yields that candidate's body opening."""
    if not want:
        return None
    elig = [(k, t) for k, t in candidates if not INSTITUTIONAL_RE.search(t)]
    hits = [k for k, _t in elig
            if kind_qualifies(lead_kind(opening_of(k)), want)]
    return hits[0] if len(hits) == 1 else None
