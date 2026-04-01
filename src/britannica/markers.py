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
