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


# Image markers — full ``{{IMG:filename|meta…|optional caption}}`` block.
#
# Grammar (the article encodes this; the renderer is the sole decoder):
#
#     {{IMG:filename[|align=center|left|right][|width=N][|height=N][|caption]}}
#
# ``filename`` is the first ``|``-separated segment; then zero or more
# layout-metadata fields (``align``/``width``/``height``, in that order),
# emitted only when non-default; then the caption is the rest (so it may
# freely contain ``|`` and ``=``).  ``align=inline`` is the unified
# inline-glyph form (folded in from the old ``{{IMG-INLINE:}}`` marker);
# the inline-vs-block decision is made by the walker (SHAPE_INLINE_IMAGE
# lookahead — see ``elements/_walker.py``), the classifier maps shape to
# the INLINE_IMAGE label, and the producer stamps ``align=inline``.
#
# The metadata alternation is value-typed (``align`` is a side word,
# ``width``/``height`` are digits) so a prose caption can never be
# mistaken for a field — the only way the meta block matches is a literal
# ``align=left`` / ``width=375`` segment, which captions never start with.
# Backward-compatible: a marker with no meta fields parses exactly as
# before (group 2 empty, group 3 = caption).
#
# ``IMG_RE`` matches the whole marker (use to strip — unaffected by the
# internal fields).  ``IMG_PARTS_RE`` captures (filename, meta-block,
# caption); parse the meta-block with ``parse_img_meta``.  The same
# regex source is mirrored verbatim in viewer.html.
IMG_RE = _re.compile(r"\{\{IMG:[^}]*\}\}")

_IMG_META_FIELD = r"align=(?:center|left|right|inline)|width=\d+|height=\d+"
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


# ── TABLE cell grammar ────────────────────────────────────────────────
# Inside ``{{TABLE:…}TABLE}`` / ``{{TABLEH:…}TABLE}`` the body is rows joined
# by ``\n`` and cells joined by `` | ``.  A cell may carry an OPTIONAL prefix
# ``⟦code⟧`` encoding the producer's RESOLVED per-cell layout — alignment
# (``r``ight / ``c``enter; left is the render default → no prefix) followed by
# optional colspan digits.  Defined once here and mirrored verbatim in
# viewer.html.  Because a default (left, colspan 1) cell emits NO prefix, plain
# text cells are byte-identical to the old format and existing snapshots stay
# valid.  This is what lets the producer hand the viewer the table's real
# structure (alignment from ``{{Ts|ar/ac}}`` / ``align=``, group-header spans)
# so the viewer renders mechanically instead of guessing.
TABLE_CELL_RE = _re.compile(r"^⟦([rc]?)(\d*)⟧")

_TABLE_ALIGN_DECODE = {"r": "right", "c": "center"}
_TABLE_ALIGN_ENCODE = {"right": "r", "center": "c"}


def build_table_cell(content: str, *, align: str | None = None,
                     colspan: int = 1) -> str:
    """Emit one ``{{TABLE:}}`` cell with its resolved layout encoded as the
    ``⟦code⟧`` prefix.  Default (left / colspan 1) → no prefix, so the cell is
    identical to the bare-content form."""
    code = _TABLE_ALIGN_ENCODE.get(align or "", "")
    if colspan and colspan > 1:
        code += str(colspan)
    return f"⟦{code}⟧{content}" if code else content


def parse_table_cell(cell: str) -> tuple[str | None, int, str]:
    """Decode a ``{{TABLE:}}`` cell into ``(align, colspan, content)``.
    ``align`` is ``"right"``/``"center"`` or ``None`` (left default); ``colspan``
    is an int (1 default).  No prefix → ``(None, 1, cell)``."""
    m = TABLE_CELL_RE.match(cell)
    if not m:
        return (None, 1, cell)
    align = _TABLE_ALIGN_DECODE.get(m.group(1))
    colspan = int(m.group(2)) if m.group(2) else 1
    return (align, colspan, cell[m.end():])
