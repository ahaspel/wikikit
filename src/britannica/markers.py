"""Inline marker delimiters used throughout the pipeline.

These markers survive all cleaning stages and are rendered by the viewer.
Each uses a unique control character or Unicode character pair as delimiters
to avoid conflicts with wiki markup, HTML, and template syntax.

Pipeline flow:
  fetcher creates markers using internal delimiters (\x00, \x01, \x02, \x03)
  → fetcher converts to readable format at end of cleaning
  → cleaners/reflow pass markers through unchanged
  → boundary detection skips markers during heading detection
  → viewer renders markers as HTML elements
"""

# Image markers: {{IMG:filename}} or {{IMG:filename|caption}}
IMG_OPEN = "{{IMG:"
IMG_CLOSE = "}}"

# Footnote markers: «FN:text»
FN_OPEN = "\u00abFN:"
FN_CLOSE = "\u00bb"

# Table markers: {{TABLE:\nrow1\nrow2\n}TABLE}
TABLE_OPEN = "{{TABLE:"
TABLE_CLOSE = "}TABLE}"

# Link markers: «LN:target|display»
LN_OPEN = "\u00abLN:"
LN_CLOSE = "\u00bb"

# Internal delimiters (used during fetcher cleaning, converted before output)
_INTERNAL_IMG = "\x00"        # wraps IMG markers during cleaning
_INTERNAL_FN = "\x01"         # wraps FN markers during cleaning
_INTERNAL_TABLE = "\x02"      # protects table row newlines during reflow
_INTERNAL_LINK = "\x03"       # wraps LN markers during cleaning
