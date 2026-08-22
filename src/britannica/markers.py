"""Marker-stream helpers shared across the pipeline.

The article ``body`` is a stream of ``«…»`` / ``{{X:…}}`` markers that the
producers emit and the viewer decodes.  This module holds the small set of
shared helpers that read that stream from the Python side: the title-marker
strip utility, the ``{{IMG:…}}`` grammar (mirrored in viewer.html), the single
marker→plain-text converter used by the search index and previews, and the
canonical lists of rendered marker names.

The page-marker helpers (``PAGE_MARKER_RE`` / ``strip_page_markers``) are GONE.
Page position never enters the marker stream now — it is lifted to keys in
``preprocess.stream_with_keys`` and re-inserted in ``render.page_markers``, the
only two places that know about it ([[project_page_position_out_of_band]]).  The
strips were verified to be no-ops first: 0 occurrences of the token across 37,226
article bodies and 37,228 exported JSONs.
"""

# ── Shared compiled regexes and helpers ─────────────────────────────────────

import re as _re
from typing import Iterator as _Iterator, NamedTuple as _NamedTuple


# ── The marker LEXICON — what a marker token looks like, defined ONCE ────────
#
# Every consumer that needs to recognize or remove marker syntax imports from
# here.  The alternative is what this codebase actually had: `«[^«»]*»` in five
# modules, `«[^»]*»` in eight more — two spellings of one rule, behaving
# differently the moment a marker nests, and no way for anyone to keep thirteen
# sites in step because nothing said they were the same rule.
#
# These match the marker TOKEN, never a span between two guillemets.  The span
# spelling is wrong on this corpus: unproofread OCR leaves stray `«`/`»` in
# garbled Greek and math, so a span runs from one to the next and deletes the
# prose between — 376,360 characters across 904 bodies, measured.
#
# Names are UPPERCASE, which is what separates a marker from a French quotation
# or an OCR artefact (`«ff»`).  Three shapes, tried in this order:
#   `«SEC:slug|name»`  a POINT marker — its payload is ARGUMENTS, not content, so
#                      the whole thing goes.  Removing only the delimiter left
#                      `santa-laura-mount-athos»` in the search text of 1,221
#                      articles, which is how this was caught.
#   `«TITLE:`          a SPLIT opener — its payload IS content and stays; there is
#                      no `»` to close on, so the `:` alternative takes it.
#   `«I»` / `«/I»`     a bare wrapper.
# The payload run excludes `«»`, so a point marker can never swallow a following
# marker, and a split opener can never be mistaken for one.
# The attribute run is `[^«»]*`, NOT `[^\]]*`: a real attribute carries nested
# brackets — `«SPAN[title:farm [tribute] of the county]»`, `«SPAN[title:2＝[2,1＋√m]²]»`
# — and stopping at the first `]` leaves the whole marker unmatched and visible.
# Excluding only the guillemets keeps the run inside one marker regardless.
MARKER_TOKEN_RE = _re.compile(
    r"«/?[A-Z][A-Za-z0-9_]*(?:\[[^«»]*\])?(?::[^«»]*»|:|»)")
BRACE_MARKER_TOKEN_RE = _re.compile(
    r"\{\{[A-Z][A-Za-z0-9_]*(?:\[[^\]]*\])?:|\}[A-Z]+\}")
_MARKER_NAME_RE = _re.compile(r"«/?([A-Za-z0-9_]+)")


def marker_names(text: str) -> list[str]:
    """Every marker NAME in ``text`` — the registry checks' shared extractor."""
    return [_MARKER_NAME_RE.match(m.group(0)).group(1)
            for m in MARKER_TOKEN_RE.finditer(text or "")]


def unaccounted_guillemets(text: str, context: int = 45
                           ) -> list[tuple[str, str]]:
    """``(signature, context)`` for every ``«`` no marker token accounts for.

    A guillemet is our marker DELIMITER, so one standing outside a well-formed
    token is either a marker we MANGLED or a guillemet the source itself had.
    This function only finds them; only the raw source can say which — see
    ``tools/diagnostics/mangled_markers.py``, which asks that question.

    The oracle in :mod:`britannica.render.leaks` cannot: its ``marker`` check
    matches well-formed tokens, and a mangled one is by definition not one.
    ``subpage_target`` split a link target on ``/`` and turned ``«/I»`` into
    ``«#I»`` — invisible to every check we had, and it broke 17 links.

    The SIGNATURE is the next three NON-WHITESPACE characters, which is what
    identifies one: `#I»` and `BR)` are ours, `wan` and `-ar` are the source's
    Greek OCR.  Whitespace is excluded because it is the one thing the pipeline
    legitimately rewrites — the source's `«w\\nand` is the output's `«w and` and
    the HTML's `«w<p>and`, all the same guillemet.  (Pass tag-stripped text; a
    tag is not whitespace but renders in place of it.)

    Comparing signatures rather than COUNTS is what makes the comparison survive
    legitimate repetition — a footnote renders both inline and in the Notes
    list, so its guillemets appear twice in ``rendered_html`` and once in the
    source without anything being wrong.
    """
    if not text or "«" not in text:
        return []
    rest = MARKER_TOKEN_RE.sub(" ", text)
    out = []
    for m in _re.finditer("«", rest):
        tail = _re.sub(r"\s+", "", rest[m.end():m.end() + 40])[:3]
        out.append((tail, rest[max(0, m.start() - context):m.start() + context]))
    return out


def strip_marker_tokens(text: str, repl: str = " ") -> str:
    """``text`` with every marker DELIMITER removed, content left in place.

    Not a converter: it drops syntax only.  For text a reader sees, use
    :func:`markers_to_text`, which knows a link collapses to its display and a
    footnote leaves the prose entirely.
    """
    return BRACE_MARKER_TOKEN_RE.sub(
        repl, MARKER_TOKEN_RE.sub(repl, text or ""))


# ── The balanced marker scan — how a consumer CROSSES a marker span ──────────
#
# Markers nest («TABLE» in «TABLE», «I» in «SC»), so a span-shaped regex
# (`«TABLE.*?«/TABLE»`) stops at the FIRST close rather than the matching one.
# For a STRIPPER that loses a few words silently; for a TRANSLATOR it emits the
# wrong text inside the wrong construct, which is how `export/markdown.py`
# shipped 8,140 raw markers across 560 articles before it was rewritten around
# this scan.  There is exactly one right way to do it, so it lives here, beside
# the lexicon, rather than once per consumer ([[feedback_tune_dont_fork]]).
#
# Promoted from `export/markdown.py` (where it was `_balanced_end` /
# `_sub_balanced`) when the TEI writer needed the same crossing.  A second
# consumer is the moment to move it, not the moment to copy it.


def balanced_end(text: str, j: int, open_re: "_re.Pattern | str", close: str) -> int:
    """Index just past the DEPTH-matched ``close`` for an opener ending at ``j``.

    -1 when the span is unbalanced.  Callers leave unbalanced markers RAW rather
    than guessing a close: a visible marker is a reported leak, a guessed close
    is silently wrong text ([[feedback_honesty_surface_failures]]).
    """
    if isinstance(open_re, str):          # a literal opener IS a pattern, escaped
        open_re = _re.compile(_re.escape(open_re))
    depth = 1
    while depth:
        nxt = open_re.search(text, j)
        nc = text.find(close, j)
        if nc < 0:
            return -1
        if nxt is not None and nxt.start() < nc:
            depth, j = depth + 1, nxt.end()
        else:
            depth, j = depth - 1, nc + len(close)
    return j


def sub_balanced(text: str, open_re: "_re.Pattern", close: str, render) -> str:
    """Replace every balanced ``open_re``…``close`` span with ``render(m, inner)``."""
    out, i = [], 0
    while True:
        m = open_re.search(text, i)
        if m is None:
            out.append(text[i:])
            return "".join(out)
        end = balanced_end(text, m.end(), open_re, close)
        if end < 0:                       # unbalanced — carry it out raw, visibly
            out.append(text[i:m.end()])
            i = m.end()
            continue
        out.append(text[i:m.start()])
        out.append(render(m, text[m.end():end - len(close)]))
        i = end


# ── Shared marker OPENERS ────────────────────────────────────────────────────
#
# An opener is the marker's GRAMMAR, not a decoder's private detail, so the
# moment a second decoder needs one it belongs here.  These four were each
# spelled twice — `«FN…»` in the markdown decoder and the site decoder, `«TR…»`
# in the markdown decoder and the TEI one, the two rule markers in the site
# decoder and the TEI one — which is the same drift that put `«[^«»]*»` in five
# modules and `«[^»]*»` in eight more.  The THIRD consumer is what surfaced it.
FN_OPEN_RE = _re.compile(r"«FN(?:\[([^\]]*)\])?:")
TR_OPEN_RE = _re.compile(r"«TR(?:\[([^\]]*)\])?»")
DHR_RE = _re.compile(r"«DHR(?:\[[^\]]*\])?»")
DHRI_RE = _re.compile(r"«DHRI(?:\[[^\]]*\])?»")


def marker_open(name: str, attrs: str) -> str:
    """The `«NAME[attrs]»` open token — one spelling of the wire form."""
    return f"«{name}[{attrs}]»"


# ── The «SEC» heading ECHO ───────────────────────────────────────────────────
#
# «SEC:slug|name» is a POINT marker carrying the name; the VISIBLE heading is
# still in the stream right after it, as «CTR»«SC»name«/SC»«/CTR».  Any consumer
# that emits the name from the marker (a `## name`, a `<head>`) must skip that
# echo or print the heading twice.
_CTR_SC_OPEN = _re.compile(r"«CTR»\s*«SC»")


def heading_echo_end(text: str, j: int) -> int:
    """Index just past a heading echo starting at/after ``j``; -1 if none.

    THE SHAPE IS EXACT: `«CTR»«SC»…«/SC»«/CTR»`, with the `«/CTR»` immediately
    after the balanced `«/SC»`.  That strictness is the whole discipline — a scan
    to the balanced `«/CTR»` alone swallows whatever lies between, and in VALVES
    that was two images and a heading, silently.  A run that is not this shape is
    not an echo, so it stays.
    """
    lead = len(text[j:]) - len(text[j:].lstrip())
    echo = _CTR_SC_OPEN.match(text, j + lead)
    if echo is None:
        return -1
    sc_end = balanced_end(text, echo.end(), _re.compile(r"«SC»"), "«/SC»")
    if sc_end > 0 and text.startswith("«/CTR»", sc_end):
        return sc_end + len("«/CTR»")
    return -1


# ── The «LN» reader — the link marker's grammar, written ONCE ────────────────
#
# The producer's 2-part form is `«LN[kind]:target|display«/LN»` (the emitter is
# `_link.ln_marker`, the only place the field order is WRITTEN).  The display is
# the RECURSED slot, so it legitimately carries markers of its own — a printed
# cross-reference set in small caps is `«SC»Parasitic Diseases«/SC»` — and any
# `([^«]*)` display group simply fails to match those.  That spelling, repeated
# per consumer, is how the extractor went blind to every marked-up reference
# while the bake had already been fixed: the grammar lived in three places and
# only one was right.  So the reader is written ONCE, here, as a SCAN:
#   * the opener is a pattern (`«LN:` with an optional `[kind]` slot, consumed
#     at bake like `«MATH[fs=N]`),
#   * the close is found by literal scan — never by a span pattern,
#   * the target reads to the FIRST `|` (a title has none; a nested
#     `«SPAN[a:1|b:2]»` in the display does),
#   * the display is everything else, markers included,
#   * an opener with no close ends the scan — inventing a close would hide the
#     producer bug that failed to write one.
# This reads the PRE-bake form.  The baked 3-part `«LN:filename|target|display»`
# is decoded by the independent open/close substitutions in render/inline.py,
# export/markdown.py and the viewer — decoders, not readers of this grammar —
# and the bake itself runs strictly before any 3-part marker exists.
_LN_OPEN_KIND_RE = _re.compile(r"«LN(?:\[([a-z_]*)\])?:")
_LN_CLOSE = "«/LN»"


class LnMarker(_NamedTuple):
    kind: "str | None"      # the `[kind]` slot, None when absent
    target: str             # to the first `|` — never carries markers
    display: str            # to the marker's own close — may carry markers
    start: int              # offset of `«LN` in the text
    end: int                # offset just past `«/LN»`


def iter_ln_markers(text: str) -> "_Iterator[LnMarker]":
    """Every well-formed 2-part `«LN[kind]:target|display«/LN»` in ``text``.

    THE «LN» reader — every consumer that needs the marker's fields iterates
    this instead of spelling the grammar again (see the comment above for why
    a per-consumer regex is how references get silently dropped).
    ``text[m.start:m.end]`` is the marker's full surface text.
    """
    i = 0
    while True:
        m = _LN_OPEN_KIND_RE.search(text, i)
        if m is None:
            return
        close = text.find(_LN_CLOSE, m.end())
        if close < 0:
            return
        target, _sep, display = text[m.end():close].partition("|")
        i = close + len(_LN_CLOSE)
        yield LnMarker(m.group(1), target, display, m.start(), i)


_AL_OPEN = "«AL:"
_AL_CLOSE = "«/AL»"


class AlMarker(_NamedTuple):
    target: str
    display: str
    start: int
    end: int


def iter_al_markers(text: str) -> "_Iterator[AlMarker]":
    """Every well-formed `«AL:target|display«/AL»` in ``text``.

    THE «AL» reader, for the same reason `iter_ln_markers` is the «LN» one:
    the grammar was already spelled twice (the xref extractor and the 5.4
    bake, each with its own regex), and a third spelling is how the «LN»
    display fork happened — a reader whose display slot can't carry markers
    silently drops the reference.  The display is the RECURSED slot (an
    author signature prints as `«SC»r. v. h.«/SC»`); the target reads to the
    first `|`.  A marker with no close is not yielded — the caller leaves it
    visible rather than inventing a close.
    """
    i = 0
    while True:
        start = text.find(_AL_OPEN, i)
        if start < 0:
            return
        close = text.find(_AL_CLOSE, start + len(_AL_OPEN))
        if close < 0:
            return
        target, _sep, display = text[start + len(_AL_OPEN):close].partition("|")
        i = close + len(_AL_CLOSE)
        if not _sep:
            continue      # pipe-less = malformed; left visible, like every reader before
        yield AlMarker(target, display, start, i)


def sub_al_markers(text: str, repl) -> str:
    """Rewrite every well-formed «AL» through ``repl(AlMarker) -> str`` —
    the mechanical iterate-and-splice every «AL» rewriter shares, so a
    consumer supplies only its policy, never the grammar."""
    out, i = [], 0
    for m in iter_al_markers(text):
        out.append(text[i:m.start])
        out.append(repl(m))
        i = m.end
    out.append(text[i:])
    return "".join(out)


def sub_ln_markers(text: str, repl) -> str:
    """Rewrite every well-formed 2-part «LN» through ``repl(LnMarker) -> str``
    — the «LN» twin of ``sub_al_markers``; ``text[m.start:m.end]`` is the
    marker's own surface for a repl that leaves some markers standing."""
    out, i = [], 0
    for m in iter_ln_markers(text):
        out.append(text[i:m.start])
        out.append(repl(m))
        i = m.end
    out.append(text[i:])
    return "".join(out)


# Title-formatting markers: bold (`«B»…«/B»`), italic (`«I»…«/I»`),
# small-caps (`«SC»…«/SC»`).  Stored in `Article.title` so the viewer
# can render multi-bold / small-caps titles like
# `«B»AGRICOLA«/B» (originally «SC»Schneider«/SC», …), «B»JOHANNES«/B>`
# with the source's typographic distinctions intact.
#
# Callers that need plain-text titles (filename slugs, search indexes,
# breadcrumb labels, page <title> elements, etc.) use the strip helper
# below.
_TITLE_MARKER_RE = _re.compile(r"«/?(?:B|I|SC)»")


def strip_title_markers(title: str) -> str:
    """Remove `«B»`/`«I»`/`«SC»` formatting markers from a title,
    leaving the content intact.  Use for any plain-text consumer."""
    return _TITLE_MARKER_RE.sub("", title)


# Image markers — full ``{{IMG:filename|meta…|optional caption}}`` block.
#
# Grammar (the article encodes this; the renderer is the sole decoder):
#
#     {{IMG:filename[|align=center|left|right][|width=N][|height=N][|caption]}}
#
# ``filename`` is the first ``|``-separated segment; then zero or more
# layout-metadata fields (``align``/``width``/``height``, in that order),
# emitted only when non-default; then the caption is the rest (so it may
# freely contain ``|`` and ``=``).  Alignment is whatever the image's own
# params carry (``center``/``left``/``right``); a bare image carries none and
# renders inline by HTML default — there is no separate "inline" alignment,
# because the raw never marks one (an image's layout is its surrounding
# ``{{center}}`` / line-breaks / table cell, never a property of the image).
#
# The metadata alternation is value-typed (``align`` is a side word,
# ``width``/``height`` are digits) so a prose caption can never be
# mistaken for a field — the only way the meta block matches is a literal
# ``align=left`` / ``width=375`` segment, which captions never start with.
# Backward-compatible: a marker with no meta fields parses exactly as
# before (group 2 empty, group 3 = caption).
#
# ``IMG_PARTS_RE`` captures (filename, meta-block, caption); parse the
# meta-block with ``parse_img_meta``.  The same regex source is mirrored
# verbatim in viewer.html.
_IMG_META_FIELD = r"align=(?:center|left|right)|width=\d+|height=\d+"
IMG_PARTS_RE = _re.compile(
    r"\{\{IMG:([^|}]+)"
    r"((?:\|(?:" + _IMG_META_FIELD + r"))*)"
    r"(?:\|([^{}]*))?\}\}"
)

_IMG_META_KV_RE = _re.compile(r"(align|width|height)=([^|]+)")


def parse_img_meta(meta_block: str) -> dict:
    """Parse the meta-block (group 2 of ``IMG_PARTS_RE``) into a dict.

    ``width``/``height`` come back as ints; ``align`` as a string.
    Empty block → empty dict.
    """
    out: dict[str, object] = {}
    for key, val in _IMG_META_KV_RE.findall(meta_block):
        out[key] = int(val) if key in ("width", "height") else val
    return out


# List markers — `«OL[type:X]»`/`«UL»` … `«LI»`…`«/LI»` … `«/OL»`/`«/UL»`.
#
# A LIST is what the source states outright: `#` (wikitext ordered list),
# `<ol>`/`<ul>`, `{{ordered list|type=…}}`.  Compare `:`, which states an INDENT
# and is carried as one (`_indent.py`) — "if the raw source marks a list, we
# treat it as a list; if it marks indents, we treat them as indents" (user,
# 2026-08-17, [[project_outline_arc]]).
#
# The NESTING is the source's own: a sublist rides INSIDE its parent `«LI»`,
# exactly as `<ol><li>text<ol>…</ol></li></ol>` writes it.  No depth ladder, no
# sparse→dense remap — those belonged to the inferred outline this replaces.
#
# The NUMBERING is the source's too, carried as the `type` param
# (`upper-roman`, `upper-alpha`, `lower-alpha`, `lower-roman`, decimal default)
# and rendered by the browser's own list numbering.  The outline machinery used
# to MINT the numeral into the item text ("I. ", "A. ") and then render the list
# with `list-style-type:none` — which made the numeral indistinguishable from
# prose to the search index, the markdown export and any restyling.  Verified
# against the scans (user, 2026-08-17): GEOLOGY p658 and ALBUMIN p553 both print
# their numerals, so the numbering is the source's fact, not our decoration.
OL_OPEN_RE = _re.compile(r"«OL(?:\[type:([\w-]+)\])?»")
LIST_MARKERS: tuple[str, ...] = ("«OL", "«UL»", "«LI»", "«/OL»", "«/UL»", "«/LI»")


def ol_open(list_type: str = "") -> str:
    """The open tag for an ordered list, carrying its stated numbering type."""
    return f"«OL[type:{list_type}]»" if list_type else "«OL»"


def list_type(open_tag: str) -> str:
    """The `type` a list's open tag states; `""` when it states none (decimal)."""
    m = OL_OPEN_RE.match(open_tag)
    return (m.group(1) or "") if m else ""


# Table markers — the `«TABLE[cols:N|wide]` … `«/TABLE»` block.
#
# Grammar (the producer emits it; the render is the sole decoder):
#
#     «TABLE[cols:N[|wide]]…»  …rows…  «/TABLE»
#
# `cols` is the column count the table producer counted.  `wide` is a MEASURED
# fact, not a property of the source: `tools/diagnostics/measure_table_widths.py`
# renders every span in a real browser against the fixed 590px body column and
# `tools/pipeline/annotate_table_markers.py` stamps the ones that overflow, so
# the render can wrap exactly those in the Expand figure.
#
# Table spans NEST, so the close is found by DEPTH — a lazy `«TABLE\[.*?«/TABLE»`
# stops at the first inner close and tears the span in half.
#
# Four sites spelled this grammar independently: the render's Expand wrapper,
# the annotator that stamps `wide`, the width cache's key (which strips `wide`,
# so re-measuring an already-annotated corpus lands on the same key), and the
# measurer's `cols` read — two of them carrying the same regex source verbatim.
# Every one of those disagreements fails SILENTLY: a stamp the decoder cannot
# parse simply stops producing Expand buttons, and a key that drifts from the
# writer's misses the whole cache ([[feedback_dont_grow_catchalls]]).  One
# grammar, one owner, four callers.
TABLE_OPEN, TABLE_CLOSE = "«TABLE[", "«/TABLE»"
_TABLE_PARAMS_RE = _re.compile(r"«TABLE\[cols:(\d+)(\|wide)?")


def _table_open_tag(m: "_re.Match") -> str:
    """The open tag rebuilt from a params match, minus the `wide` flag."""
    return f"{TABLE_OPEN}cols:{m.group(1)}"


def iter_table_spans(text: str) -> "_Iterator[tuple[int, str]]":
    """``(offset, span)`` for every TOP-LEVEL «TABLE» span, in order.

    An unterminated open is stepped over rather than ending the walk: it is a
    producer bug that surfaces as raw marker text in the output either way, and
    a well-formed table after it must still be seen.
    """
    i = 0
    while True:
        a = text.find(TABLE_OPEN, i)
        if a == -1:
            return
        end = balanced_end(text, a + len(TABLE_OPEN), TABLE_OPEN, TABLE_CLOSE)
        if end < 0:
            i = a + len(TABLE_OPEN)
            continue
        yield a, text[a:end]
        i = end


def table_cols(span: str) -> int:
    """The `cols` param of a span's own open tag (0 if it has none)."""
    m = _TABLE_PARAMS_RE.match(span)
    return int(m.group(1)) if m else 0



# Open-prefixes for the `{{X:…}}`-shape markers that survive cleaning
# and reach the viewer.  Single source of truth — both the body-text
# template-strip regex and the post-clean quality-report checks
# reference this tuple to decide what counts as a legitimate marker
# vs. stray template residue.  Add a new entry whenever you introduce
# a new rendered marker, OR add it to both consumers separately and
# inevitably end up with one out of sync (see the IMG-INLINE
# stray_close_braces regression on 2026-05-17 for the canonical
# failure mode).  Format: literal prefix INCLUDING the opening
# ``{{`` braces.
RENDERED_MARKER_OPENS: tuple[str, ...] = (
    "{{IMG:",
    "{{TABLE:",
    "{{TABLEH:",
    "{{LEGEND:",
    "{{VERSE:",
)


# Every guillemet («…») marker NAME a producer EMITS — the companion to
# RENDERED_MARKER_OPENS for the `«NAME…»` family (RENDERED_MARKER_OPENS
# covers only the `{{X:…}}` braces).  NAME only — no delimiters, no
# `[attr]` payload (`DIV`/`SPAN`/sizes carry one).
#
# "Emitted", NOT "decoded by the viewer" — that was this comment's old claim and
# it was wrong in a way that cost real output.  A name absent because it never
# reaches the viewer is still a shape the OTHER consumers must handle: `AL` is
# resolved away before export, yet it is emitted, and a converter grounded in this
# list needs to know it exists.  `export/markdown.py` grounds its "TOTAL by
# construction" claim here, so
# what this list omits is exactly what that file has no rule for — the five weeks
# of raw `«OUTLINE»` in the download bundle came from this omission, not from a
# bug in the emitter.
#
# It is now ENFORCED rather than asserted: `tests/unit/test_marker_registry.py`
# fails on any name the producers emit into the transform snapshots and this list
# doesn't carry, and the quality report counts the same thing corpus-wide as
# `unregistered_marker`.  Add a new entry here AND mirror it in the viewer
# whenever you introduce a new `«…»` marker (see the IMG-INLINE
# stray_close_braces drift note on RENDERED_MARKER_OPENS above).
RENDERED_GUILLEMET_MARKER_NAMES: tuple[str, ...] = (
    # inline styling / typography (decodeInlineMarkers + applySizeMarkers)
    #
    # There is no SS / SR.  They were the sans-serif and explicit-serif pair, and
    # NOTHING EMITTED THEM: 0 occurrences across 37,226 article bodies, and 0
    # `class="sans-serif"` / `class="explicit-serif"` across 37,226 rendered HTML.
    # The source construct is alive — `{{sans-serif|Α}}` on 20 pages, `{{serif}}` /
    # `{{Serif}}` on 78, concentrated in ALPHABET where letters are discussed AS
    # letters — but it is carried by the STYLE-SPAN path instead
    # («SPAN[style:font-family:sans-serif]», 117 in ALPHABET alone), which carries
    # the CSS the template implies rather than a marker someone must interpret.
    #
    # Deleted because dead code is not inert.  `export/markdown.py` held a rule for
    # this pair mapping «SS»→<sub> and «SR»→<sup> — which they never were — and on
    # 2026-08-22 that rule was read as authoritative and reported as a shipped bug
    # corrupting the alphabet articles.  It could not fire.  Dead code hands
    # confident wrong answers to whoever reads it next ([[feedback_dead_is_wrong]]).
    "B", "I", "SC", "MIRROR", "CTR", "FR", "FL",
    "DIV", "SPAN", "BR", "BAR", "DHR",
    "XL",
    # links.  AL (the author link) never reaches the viewer — it resolves to LN
    # before export — but it IS emitted, so a consumer grounded in this list has to
    # carry a rule for it.
    #
    # There is no BIOLINK.  A contributor bio's `{{EB1911 article link|…}}` is an
    # ORDINARY article link and produces «LN» from the same producer as every
    # other one; the private marker that used to exist here was a second
    # recogniser for the same template, and it disagreed with the real one about
    # which argument was the target.
    "LN", "AL",
    # cell- and block-level content; SEC is the major-section anchor point marker
    # «SEC:slug|name» (stamp_section_anchors); SH the shoulder heading; ANCHOR the
    # «ANCHOR:slug|name» link target (kind="anchor" downstream, kept out of the TOC)
    "FN", "MATH", "EQN", "SEC", "SH", "ANCHOR",
    # recursive table structure (decodeInlineMarkers) — chem is a TABLE too now
    "TABLE", "TR", "TD", "TH", "CAPTION",
    # block structure.  P is the paragraph (199,411 in the corpus — the single most
    # common marker of all, and unlisted until the registry was enforced); TITLE the
    # in-stream H1; DHRI a proportional rule.
    "P", "TITLE", "DHRI",
    # lists: the ordered/unordered forms and the item.  (The OUTLINE family that
    # stood here is gone — `:` is an INDENT, carried as «DIV[padding-left]», and
    # what the source marks as a list is a list, [[project_outline_arc]].)
    "OL", "UL", "LI",
)


# ── Marker stream → plain text ───────────────────────────────────────────────
# The article ``body`` is a marker stream (the viewer's input).  Plain-text
# consumers — the Meilisearch full-text index and the ``index.json`` body_start
# preview — need the prose, not the markers.  This is the ONE converter they all
# call, so the strip policy lives in exactly one place.  (Three drifting copies
# of this logic, each missing a different marker, are what let «TITLE:…«/TITLE»
# and «SPAN[title:…]» leak into the search dropdown.)
#
# Policy, grounded in RENDERED_GUILLEMET_MARKER_NAMES / RENDERED_MARKER_OPENS:
#   • DROP whole (marker + payload) the non-prose block / structural markers:
#     the title (it is the separate ``title`` field), footnotes, math / chem /
#     equation displays, tables, images, verse, legends, outlines, section
#     anchors.  These are the SPLIT markers (``«X:…«/X»``) — they nest a ``«``,
#     so the generic inline sweep below cannot touch them; they must go first.
#   • Links (``«LN:…«/LN»`` / ``«XL:…«/XL»``) → their display text (last field).
#   • Everything else is inline prose typography — paragraph «P», «I»/«B»/«SC»,
#     «SPAN[…]»/«DIV[…]», the size family, «SH», «CTR», «BR», … — which wraps
#     real text: drop the delimiters, KEEP the content between them.  One
#     generic ``«[^«»]*»`` sweep does this for every such marker (present and
#     future), so a newly-added inline marker needs no change here.
_DROP_MARKER_RE = _re.compile(
    r"«TITLE:[\s\S]*?«/TITLE»"
    r"|«FN(?:\[[^\]]*\])?:[\s\S]*?«/FN»"
    r"|«MATH(?:\[[^\]]*\])?:[\s\S]*?«/MATH»"
    r"|«TABLE\[[\s\S]*?«/TABLE»"
    r"|«EQN:[^»]*»[\s\S]*?«/EQN»"
    r"|\{\{IMG:[^}]*\}\}"
    r"|\{\{TABLEH?:[\s\S]*?\}TABLE\}"
    r"|\{\{VERSE:[\s\S]*?\}VERSE\}"
    r"|\{\{LEGEND:[\s\S]*?\}LEGEND\}"
)
# A link display can carry nested markers; strip them by the LEXICON above, not
# by a span.  The span spelling deleted real prose from the search text of
# three articles whose unproofread OCR leaves stray guillemets — it ran from
# one to the next and took the clause between.
_INLINE_MARKER_RE = MARKER_TOKEN_RE
# «AL» is the DEFERRED author-link marker (walk → 5.4 resolves it against the
# finished roster); it never survives into the render/search path, but degrade a
# stray one to its display (the initials) like any other link, not to residue.
# `«LN` takes an optional `[kind]` param (`«LN[qv]:…»` — the reference KIND a
# producer stamped for the 5.4 resolve; dropped at bake, so only PRE-bake bodies
# carry it).  The slot is tolerated wherever «LN is parsed, like «MATH[fs=N].
_LINK_RE = _re.compile(r"«(?:LN|XL|AL)(?:\[[a-z_]*\])?:([\s\S]*?)«/(?:LN|XL|AL)»")
# Carried presentational HTML — the SAFE-HTML set decode_inline un-escapes for the render
# (`sub|sup|small|big|br`).  In PLAIN text it carries nothing search wants, so strip the tag
# and KEEP the content (`H<sub>2</sub>O` → `H2O`); a raw `<br>` line break becomes a separator.
_RAW_HTML_RE = _re.compile(r"<\s*/?\s*(?:sub|sup|small|big)\s*>", _re.I)
_RAW_BR_RE = _re.compile(r"<\s*br\s*/?\s*>", _re.I)


def _link_display(m: "_re.Match") -> str:
    """A link → its display text (the field after the last top-level ``|``),
    with any nested inline markers (`«I»q.v.«/I»`) stripped to plain text."""
    inner = m.group(1)
    disp = inner.rsplit("|", 1)[-1] if "|" in inner else inner
    return _INLINE_MARKER_RE.sub("", disp)


def collapse_links(text: str) -> str:
    """Every link → its DISPLAY text, dropping target and filename fields.

    A link's fields are addresses, not prose: `«LN:14-0147-abc.json|HYDROMEDUSAE|
    the medusae«/LN»` reads as "the medusae".  Stripping only the delimiter leaves
    the filename behind, which put `json`, `dd33a0` and every stable-id fragment
    into the EPUB search index as searchable words.  Any consumer producing text
    for a READER or an INDEX wants this before `strip_marker_tokens`.
    """
    return _LINK_RE.sub(_link_display, text)


def markers_to_text(text: str, *, sep: str = " ") -> str:
    """Convert a marker-stream ``body`` into plain text (search / previews).

    The sole marker→text converter (see the policy comment above).  Block
    markers are replaced with ``sep`` so adjacent words stay separated; inline
    markers lose their delimiters but keep their text; links collapse to their
    display.  Whitespace is NOT collapsed and newlines are preserved, so a
    caller can still do line-based work (e.g. the preview skips a leading
    caption line); use ``" ".join(markers_to_text(b).split())`` for a flat
    string.
    """
    text = _DROP_MARKER_RE.sub(sep, text)
    text = collapse_links(text)
    text = _INLINE_MARKER_RE.sub("", text)
    text = _RAW_BR_RE.sub(sep, text)          # <br> line break → word separator
    text = _RAW_HTML_RE.sub("", text)         # <sub>/<sup>/<small>/<big> → keep content, drop tag
    return text
