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


def until_stable(text: str, transform) -> str:
    """Apply ``transform`` until it stops changing ``text``.

    THE FIXED POINT, bounded by the data instead of by a number.  Five call sites
    wrote ``for _ in range(N)`` around an unwrap — 3, 5, 5, 6 and 8, each a claim
    about how deep its input nests that nothing checked, and each failing the same
    silent way: the N+1th level comes back still wrapped, as content.  Two of them
    did not even break early, so they paid for every pass whether or not anything
    changed ([[feedback_total_functions_not_cleanup_passes]]).

    An unwrap SHRINKS its input, so the fixed point is reached in at most one pass
    per construct.  That is the termination argument, and it is checked rather
    than assumed: a transform that stops shrinking has either finished or is
    oscillating, and both end the loop.  Nothing here caps depth.
    """
    previous = len(text)
    while True:
        changed = transform(text)
        if changed == text:
            return text
        if len(changed) >= previous:
            return changed          # not shrinking: finished, or would spin
        previous = len(changed)
        text = changed


def collapse_spaces(s: str) -> str:
    """Whitespace runs — spaces, tabs, newlines — folded to ONE space, ends trimmed.

    The most-copied line in the codebase.  It existed as
    `contributors.resolver._normalise_spaces`, as
    `image_assets.normalize_score_content` (keying `<score>` tags to their
    pre-rendered PNGs), and — with `.lower()` on the end — as `_norm` in BOTH
    `topic_geo` and `topic_subject`.  Four names, one sentence.

    Cheap to duplicate and therefore duplicated, which is exactly why it is worth
    owning: `normalize_score_content` is a CONTENT-ADDRESSED KEY, so the day
    someone "improves" one copy is the day score lookups silently miss
    ([[feedback_tune_dont_fork]]).
    """
    return re.sub(r"\s+", " ", s or "").strip()


def excerpt(text: str, idx: int, span: int = 60) -> str:
    """``span`` characters either side of ``idx``, newlines flattened to spaces.

    What a finding shows a human so they can see the site without opening the
    file.  Two diagnostics wrote it out with different default widths (60 and
    100) — the WIDTH is the caller's choice, the window is not
    ([[feedback_tune_dont_fork]]).
    """
    start = max(0, idx - span)
    end = min(len(text), idx + span)
    return text[start:end].replace("\n", " ").replace("\r", "")


def collapse_key(s: str) -> str:
    """`collapse_spaces` lowercased — the LOOKUP-KEY form of a string.

    `topic_geo` and `topic_subject` each kept a private `_norm` for this, and
    after `collapse_spaces` was shared they were still two one-line wrappers
    saying the same thing.  A darling half-killed is still a darling
    ([[feedback_kill_all_darlings]]).
    """
    return collapse_spaces(s).lower()


# Words a title leaves lowercase unless they open or close it — the house style
# EB1911's own shoulder headings use ("Origins of Poland.", "The Knights of the
# Sword.", "Beginnings of the Polish Constitution.").
_TITLE_MINOR = frozenset(
    "a an and as at but by for from in nor of on or the to up via with".split())
_TITLE_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def title_case(text: str) -> str:
    """Title-case ``text`` in the edition's house style.

    `str.title()` capitalises the minor words ("Mensuration Of Graphs"); this
    lowercases them except at either end, which is the style EB1911's own
    shoulder headings use.

    IT SHARES `str.title()`'S WORD-BOUNDARY BEHAVIOUR and does not try to be
    cleverer: a letter run after any non-letter starts a new word, so a
    transcriber's bracketed restoration comes out "Lavren[Tia Membra]".  Deciding
    that `[` inside a Latin inscription is not a word break needs knowledge of
    what the text IS, which a case function does not have — the caller that knows
    it is an inscription is the one that should not be title-casing it
    ([[feedback_hard_means_unencoded_knowledge]]).
    """
    words = list(_TITLE_WORD_RE.finditer(text or ""))
    if not words:
        return text or ""
    out = list(text)
    for i, m in enumerate(words):
        w = m.group(0)
        first, last = i == 0, i == len(words) - 1
        lower = w.lower()
        repl = lower if (lower in _TITLE_MINOR and not first and not last) \
            else lower[:1].upper() + lower[1:]
        out[m.start():m.end()] = repl
    return "".join(out)


def page_range(page_start, page_end) -> str:
    """A printed page citation: ``pp. 320–358``, or ``p. 320`` for one page.

    One owner because two renderers cite the same thing — the site's citation
    line and the TEI `<biblScope>` — and an en-dash that drifts to a hyphen in
    one of them is the kind of difference nobody notices until the two outputs
    are compared.  Missing values render empty rather than "None".
    """
    a = "" if page_start is None else str(page_start)
    b = "" if page_end is None else str(page_end)
    return f"p. {a}" if a == b else f"pp. {a}–{b}"
