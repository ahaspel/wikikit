"""Shared string utilities."""

import re


def section_slug(name: str) -> str:
    """URL-safe slug from a wikisource section name (or any string).

    Preserves ASCII letters/digits, lowercases, collapses runs of other
    chars to a single hyphen. Strips surrounding hyphens.
    """
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


_MARKER_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")


def strip_markers(s: str) -> str:
    """Drop `«…»`-style markers, leaving the plain display text.

    Shared by the shoulder producer (to mint a slug from a heading's text)
    and export (to read a heading's title) so both see the same plain text —
    one regex, not a copy per caller.
    """
    return _MARKER_RE.sub("", s)


# After the primary headword, a heading carries the rest of a person's name, a
# regnal/place qualifier, or a sobriquet — introduced by a comma or by "surnamed".
# Balanced (…) / […] are descriptors or alt-spellings; a *dangling* open bracket is
# a truncated descriptor (2 malformed headings, e.g. "ALBERT (FRANCIS …" unclosed).
_BRACKET_RE = re.compile(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])")
_DANGLING_BRACKET_RE = re.compile(r"\s*[([].*$")
_SOBRIQUET_RE = re.compile(r"\bsurnamed\b", re.IGNORECASE)


def primary_headword(heading: str) -> str:
    """The canonical headword — the identity/slug/match term — from a `«TITLE»` heading.

    The heading is the full bold headword line: ``AARSSENS, or Aarssen, FRANCIS
    VAN`` (surname + given names), ``WILLIAM II, King of England`` (regnal name +
    qualifier), ``ALEXANDER III, king of Macedon, surnamed the Great``.  The
    headword is the primary term the rest merely describes — ``AARSSENS``,
    ``WILLIAM II``, ``ALEXANDER III`` — kept intentionally non-unique (it's the
    match key, not the identity; a same-headword collision gets a numeric tiebreak).

    Rules, applied in order — the order matters:

    1. Drop markers, then remove ``(…)`` / ``[…]`` brackets.  A trailing one is a
       descriptor (``ACTON (JOHN EMERICH…)`` → ``ACTON``); an inline one is an
       alt-spelling the name flows around, so removing it keeps the surrounds
       (``ADAM (or Adan) DE LE HALE`` → ``ADAM DE LE HALE``; ``ATTAR [or Otto] OF
       ROSES`` → ``ATTAR OF ROSES``).  Doing this FIRST also disposes of the 25
       headings whose comma sits *inside* a bracket, which would otherwise mis-cut.
       A dangling (unclosed) bracket is a truncated descriptor and is cut to end.
    2. Cut at a ``surnamed …`` sobriquet, then at the first ``,`` (the given-name
       or qualifier clause).
    3. Collapse whitespace.

    A heading with no comma/bracket — 66% of the corpus, 88% single-word — passes
    through unchanged; a genuine multi-word title (``ACTS OF THE APOSTLES``,
    ``ALCÁZAR DE SAN JUAN``) is kept whole, which is correct: it IS the headword.
    """
    h = _BRACKET_RE.sub("", strip_markers(heading or ""))
    h = _DANGLING_BRACKET_RE.sub("", h)
    h = _SOBRIQUET_RE.split(h, maxsplit=1)[0]
    h = h.split(",")[0]
    return re.sub(r"\s+", " ", h).strip()
