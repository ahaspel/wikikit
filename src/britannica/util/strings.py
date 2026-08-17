"""Shared string utilities."""

import hashlib
import re
import unicodedata

from britannica.markers import strip_marker_tokens


def content_digest(text: str, n: int | None = 16) -> str:
    """Content address for `text`: SHA-256 hex, first `n` chars (`None` = all).

    THE identity function for every cache and fingerprint keyed on a string —
    the measured math widths, the measured table widths, the EPUB math assets,
    the export fingerprint, the corpus snapshot.  Each spelled the same
    `sha256(s.encode("utf-8")).hexdigest()[:16]` inline, and two of them
    (`math_widths` reading the cache, `measure_math_widths` writing it) had to
    agree FOREVER or the reader would silently miss every entry it looked up.
    The 16-char default is the shared decision; the table cache keys on the
    full digest and says so.

    WHAT GOES INTO THE KEY IS THE CALLER'S BUSINESS: the composition stays at
    the call site (`math_key` prefixes display-mode and normalizes the LaTeX,
    `span_key` strips the `wide` annotation first), so a caller can change what
    it addresses without touching what addressing means.

    No `or ""` guard on purpose.  Every caller here keys REAL content; a `None`
    reaching this would key as the empty string and quietly collide with every
    other `None` — a silent cache hit on the wrong entry, which is the exact
    failure this consolidation exists to prevent.  A caller that genuinely
    digests optional text coerces on its own side.
    """
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return h if n is None else h[:n]


def fold_accents(s: str) -> str:
    """Drop diacritics: `Zürich` → `Zurich`, `Léon` → `Leon`.

    THE accent fold.  Ten sites spelled this same NFKD-decompose-and-discard-
    combining-marks rule inline — the resolver's name keys, the xref scorer,
    the name index, the EPUB's search fold and its A–Z collation, the vol-29
    linker and kind-matcher, the contributor vote — so a change to what counts
    as "the same name" had ten places to reach and would have reached one.

    CASE IS THE CALLER'S BUSINESS and deliberately not folded here: the sites
    disagree on purpose (the resolver lowercases, the article normalizer
    uppercases, the name index keeps case for a later `.upper()`), and each
    applies it on its own side of the fold.  Nor is punctuation touched —
    that is `section_key` / `section_slug`.

    NOT a member of this family, though it looks like one: the render's
    collation key (`render.article`) walks the decomposed stream character by
    character to expand ligatures and build a secondary tiebreak, so skipping
    combining marks is one step of a loop there, not a fold.
    """
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def section_slug(name: str) -> str:
    """URL-safe slug from a wikisource section name (or any string).

    Preserves ASCII letters/digits, lowercases, collapses runs of other
    chars to a single hyphen. Strips surrounding hyphens.
    """
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


HTML_TAG_RE = re.compile(r"<[^>]*>")

# A word LETTER for hyphenation work — any Unicode letter, not `[A-Za-z]`.
# The ONE alphabet shared by the dehyphenator (`elements._HYPHEN_RE`) and the
# corpus-map builder (`tools/build_hyphen_map.py`): with an ASCII class the
# runtime regex FRAGMENTED accented words and applied another pair's corpus
# vote to them — `arrière-pensée` matched as `re-pens` (a "drop" pair), so the
# French phrase lost its hyphen; `Saint-Germain-des-Prés` matched as `des-Pr`
# and joined to `desPrés`.  A whole word keys the map honestly, and an
# accented pair the ASCII-built map has never voted on is simply absent → the
# hyphen is left alone, the carry-by-default direction.
LETTER = r"[^\W\d_]"


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
