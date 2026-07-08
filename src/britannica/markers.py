"""Marker-stream helpers shared across the pipeline.

The article ``body`` is a stream of ``«…»`` / ``{{X:…}}`` markers that the
producers emit and the viewer decodes.  This module holds the small set of
shared helpers that read that stream from the Python side: the page-marker
and title-marker strip utilities, the ``{{IMG:…}}`` grammar (mirrored in
viewer.html), the single marker→plain-text converter used by the search
index and previews, and the canonical lists of rendered marker names.
"""

# ── Shared compiled regexes and helpers ─────────────────────────────────────

import re as _re

# Page marker — emitted between source pages during article assembly.
# Lives at the boundary between source pages so downstream stages can still
# locate the original page when needed. Form: \x01PAGE:N\x01
PAGE_MARKER_RE = _re.compile(r"\x01PAGE:\d+\x01")
PAGE_MARKER_CAPTURE_RE = _re.compile(r"\x01PAGE:(\d+)\x01")


def strip_page_markers(text: str, replacement: str = "") -> str:
    """Remove all `\\x01PAGE:N\\x01` markers from ``text``.

    Pass ``replacement=" "`` for search-index contexts where adjacent words
    must remain distinct after the marker is removed.
    """
    return PAGE_MARKER_RE.sub(replacement, text)


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


# Guillemet («…») marker NAMES the viewer decodes — the companion to
# RENDERED_MARKER_OPENS for the `«NAME…»` family (RENDERED_MARKER_OPENS
# covers only the `{{X:…}}` braces).  Single source of truth, mirrored
# verbatim in viewer.html's `decodeInlineMarkers` + `applySizeMarkers` +
# `formatCell` (and the block-level EQN/SEC/SH/TABLE handlers).
# The quality report references this to tell a legitimate rendered marker
# from stray residue: a `«NAME…»` whose NAME is here renders; anything
# else is a leak.  Add a new entry here AND mirror it in the viewer
# whenever you introduce a new `«…»` marker — keeping the two in lockstep
# is exactly what this constant exists to enforce (see the IMG-INLINE
# stray_close_braces drift note on RENDERED_MARKER_OPENS above).  NAME
# only — no delimiters, no `[attr]` payload (`DIV`/`SPAN`/sizes carry one).
RENDERED_GUILLEMET_MARKER_NAMES: tuple[str, ...] = (
    # inline styling / typography (decodeInlineMarkers + applySizeMarkers)
    "B", "I", "SC", "SS", "SR", "U", "STK", "MIRROR", "CTR", "FR", "FL",
    "DIV", "SPAN", "BR", "BAR", "DHR", "BRACE2",
    "XXL", "XL", "LG", "XXS", "XS", "SM", "FS", "LH",
    # links
    "LN",
    # cell- and block-level content; SEC is the major-section anchor point marker
    # «SEC:slug|name» (stamp_section_anchors); SH the shoulder heading; ANCHOR the
    # «ANCHOR:slug|name» link target (kind="anchor" downstream, kept out of the TOC)
    "FN", "MATH", "EQNGROUP", "EQN", "SEC", "SH", "ANCHOR",
    # recursive table structure (decodeInlineMarkers) — chem is a TABLE too now
    "TABLE", "TR", "TD", "TH", "CAPTION",
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
    r"|«EQNGROUP»[\s\S]*?«/EQNGROUP»"
    r"|«EQN:[^»]*»[\s\S]*?«/EQN»"
    r"|«(?:OUTLINE|PLATE_OUTLINE):[\s\S]*?«/(?:OUTLINE|PLATE_OUTLINE)»"
    r"|\{\{IMG:[^}]*\}\}"
    r"|\{\{TABLEH?:[\s\S]*?\}TABLE\}"
    r"|\{\{VERSE:[\s\S]*?\}VERSE\}"
    r"|\{\{LEGEND:[\s\S]*?\}LEGEND\}"
)
_INLINE_MARKER_RE = _re.compile(r"«[^«»]*»")
_LINK_RE = _re.compile(r"«(?:LN|XL):([\s\S]*?)«/(?:LN|XL)»")


def _link_display(m: "_re.Match") -> str:
    """A link → its display text (the field after the last top-level ``|``),
    with any nested inline markers (`«I»q.v.«/I»`) stripped to plain text."""
    inner = m.group(1)
    disp = inner.rsplit("|", 1)[-1] if "|" in inner else inner
    return _INLINE_MARKER_RE.sub("", disp)


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
    text = strip_page_markers(text, replacement=sep)
    text = _DROP_MARKER_RE.sub(sep, text)
    text = _LINK_RE.sub(_link_display, text)
    text = _INLINE_MARKER_RE.sub("", text)
    return text
