"""Inline marker delimiters used throughout the pipeline.

These markers survive all cleaning stages and are rendered by the viewer.
Each uses a unique control character or Unicode character pair as delimiters
to avoid conflicts with wiki markup, HTML, and template syntax.

Pipeline flow:
  fetcher creates markers using internal delimiters (\x00-\x08)
  → fetcher converts to readable format at end of cleaning
  → cleaners/reflow pass markers through unchanged
  → boundary detection skips markers during heading detection
  → viewer renders markers as HTML elements

IMPORTANT: All internal delimiter assignments live here. The fetch script
imports these constants — never hard-code control characters elsewhere.
"""

# ── Readable markers (survive in cleaned text, rendered by viewer) ──────────

# Image markers: {{IMG:filename}} or {{IMG:filename|caption}}
IMG_OPEN = "{{IMG:"
IMG_CLOSE = "}}"

# Footnote markers: «FN:text«/FN»
FN_OPEN = "\u00abFN:"
FN_CLOSE = "\u00ab/FN\u00bb"

# Table markers: {{TABLE:\nrow1\nrow2\n}TABLE}
TABLE_OPEN = "{{TABLE:"
TABLE_CLOSE = "}TABLE}"

# Verse markers: {{VERSE:\nline1\nline2\n}VERSE}
VERSE_OPEN = "{{VERSE:"
VERSE_CLOSE = "}VERSE}"

# Legend markers: structured figure legends
# Format: {{LEGEND:### Subhead.\nA. entry one.\nB. entry two.\n…}LEGEND}
# Lines starting with "### " are subheadings; everything else is an entry.
LEGEND_OPEN = "{{LEGEND:"
LEGEND_CLOSE = "}LEGEND}"

# Link markers: «LN:target|display«/LN»
LN_OPEN = "\u00abLN:"
LN_CLOSE = "\u00ab/LN\u00bb"

# Preformatted block markers: «PRE:text«/PRE» (structural formulas, etc.)
PRE_OPEN = "\u00abPRE:"
PRE_CLOSE = "\u00ab/PRE\u00bb"

# Math markers: «MATH:latex«/MATH»
MATH_OPEN = "\u00abMATH:"
MATH_CLOSE = "\u00ab/MATH\u00bb"

# Section markers: «SEC:name»
SEC_OPEN = "\u00abSEC:"
SEC_CLOSE = "\u00bb"

# Bold/italic/small-caps markers
BOLD_OPEN = "\u00abB\u00bb"
BOLD_CLOSE = "\u00ab/B\u00bb"
ITALIC_OPEN = "\u00abI\u00bb"
ITALIC_CLOSE = "\u00ab/I\u00bb"
SMALLCAPS_OPEN = "\u00abSC\u00bb"
SMALLCAPS_CLOSE = "\u00ab/SC\u00bb"

# Shoulder heading markers: «SH»text«/SH» (internal section headings in long articles)
SHOULDER_OPEN = "\u00abSH\u00bb"
SHOULDER_CLOSE = "\u00ab/SH\u00bb"

# ── Internal delimiters (used during fetch cleaning, converted before output) ─

_INTERNAL_IMG = "\x00"
_INTERNAL_FN = "\x01"
_INTERNAL_TABLE = "\x02"
_INTERNAL_LINK = "\x03"
_INTERNAL_MATH = "\x04"
_INTERNAL_VERSE = "\x05"
_INTERNAL_FORMAT = "\x06"   # bold, italic, small-caps
_INTERNAL_SEC = "\x07"
_INTERNAL_PRE = "\x08"


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


# Image markers — full ``{{IMG:filename|optional caption}}`` block.
# ``IMG_RE`` matches the whole marker (use to strip), ``IMG_PARTS_RE``
# captures filename and optional caption.
IMG_RE = _re.compile(r"\{\{IMG:[^}]*\}\}")
IMG_PARTS_RE = _re.compile(r"\{\{IMG:([^|}]+)(?:\|([^{}]*))?\}\}")


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
    "{{IMG-INLINE:",
    "{{TABLE:",
    "{{TABLEH:",
    "{{LEGEND:",
    "{{VERSE:",
)
