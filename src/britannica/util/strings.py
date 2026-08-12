"""Shared string utilities."""

import re

from britannica.markers import strip_marker_tokens


def section_slug(name: str) -> str:
    """URL-safe slug from a wikisource section name (or any string).

    Preserves ASCII letters/digits, lowercases, collapses runs of other
    chars to a single hyphen. Strips surrounding hyphens.
    """
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


HTML_TAG_RE = re.compile(r"<[^>]*>")


def strip_html_tags(text: str, repl: str = "") -> str:
    """Drop HTML/XHTML tags, leaving their text.

    ``repl=" "`` where the tag was a WORD BOUNDARY (`a<br>b` must read "a b", not
    "ab") — that difference is real, and it is the only variation the twenty
    hand-rolled copies of this actually carried, besides `+` vs `*`.

    DOMAIN: exact on RENDERED html, where every `<` opens a tag.  On raw source a
    bare `<` can be content — math (`a<e`), OCR debris, PLINY's prose
    `<Secundus>` — and this run would swallow to the next `>`.  Measured over the
    inputs the callers actually pass (37,226 titles, 24,645 contributor fields,
    5,172 front-matter descriptions): ZERO cases where it removes more than a
    name-keyed strip would.  It is NOT safe on a whole article body — 75 bodies,
    950,168 characters — so if a caller ever starts passing one, key on the tag
    NAME set instead (see `render.leaks._ESC_TAG_RE`, which does exactly that for
    the escaped form and explains why).
    """
    return HTML_TAG_RE.sub(repl, text or "")


def strip_markers(s: str) -> str:
    """Drop `«…»`-style markers, leaving the plain display text.

    Shared by the shoulder producer (to mint a slug from a heading's text)
    and export (to read a heading's title) so both see the same plain text.

    This USED to carry its own `«/?[A-Za-z]+(?:\\[[^\\]]*\\])?»` while claiming
    "one regex, not a copy per caller" — true within this module, and a second
    definition of the marker lexicon across the codebase.  It differed from the
    real one in both directions: allowing lowercase names, it ate OCR mojibake
    like `«ff»` as though it were a marker; requiring a closing `»`, it could not
    see the SPLIT form, so `«LN:Iron|Iron.` minted the slug `ln-iron-iron`.
    Three section anchors carried a literal `ln-` because of it.
    """
    return strip_marker_tokens(s, "")


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
