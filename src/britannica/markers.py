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


def balanced_end(text: str, start: int, open_tok: str,
                 close_tok: str) -> "int | None":
    """Index just past the depth-balanced close of the marker opening at
    ``start``; ``None`` if it never closes.

    THE close-finder for the nesting markers («TABLE» inside «TD», «EQN»).
    """
    depth, j = 1, start + len(open_tok)
    while depth:
        no, nc = text.find(open_tok, j), text.find(close_tok, j)
        if nc == -1:
            return None
        if no != -1 and no < nc:
            depth, j = depth + 1, no + len(open_tok)
        else:
            depth, j = depth - 1, nc + len(close_tok)
            if depth == 0:
                return j


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
        end = balanced_end(text, a, TABLE_OPEN, TABLE_CLOSE)
        if end is None:
            i = a + len(TABLE_OPEN)
            continue
        yield a, text[a:end]
        i = end


def table_cols(span: str) -> int:
    """The `cols` param of a span's own open tag (0 if it has none)."""
    m = _TABLE_PARAMS_RE.match(span)
    return int(m.group(1)) if m else 0


def table_is_wide(span: str) -> bool:
    """Whether a span's own open tag carries the measured `wide` flag."""
    m = _TABLE_PARAMS_RE.match(span)
    return bool(m and m.group(2))


def set_table_wide(span: str, wide: bool) -> str:
    """The span with its OWN `wide` flag written or removed.

    Stamp and strip are one edit with the flag flipped, which is what makes
    re-annotating an annotated corpus idempotent.
    """
    return _TABLE_PARAMS_RE.sub(
        lambda m: _table_open_tag(m) + ("|wide" if wide else ""),
        span, count=1)


def strip_table_wide(span: str) -> str:
    """The span's IDENTITY form: every `wide` flag in it, at any depth, gone.

    What the width cache keys on.  Scope is the whole subtree, not just the
    outer tag (`set_table_wide`'s job), because the key has to be stable under
    ANY annotation the span could pick up — otherwise measuring an annotated
    corpus writes entries the next measure can never find.
    """
    return _TABLE_PARAMS_RE.sub(_table_open_tag, span)


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
    "B", "I", "SC", "SS", "SR", "U", "STK", "MIRROR", "CTR", "FR", "FL",
    "DIV", "SPAN", "BR", "BAR", "DHR", "BRACE2",
    "XXL", "XL", "LG", "XXS", "XS", "SM", "FS", "LH",
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
    # outlines: the block form, its in-cell sibling, and the item.
    "OUTLINE", "IOUTLINE", "OLI",
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
    r"|«(?:OUTLINE|PLATE_OUTLINE):[\s\S]*?«/(?:OUTLINE|PLATE_OUTLINE)»"
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
