"""Brace producer — `{{brace2|N|dir}}`, a curly brace spanning rows OR columns.

EB1911 tables group cells with a large curly brace, written in the source with the
Wikisource `{{brace2|size|dir}}` template.  `dir` is the direction the brace's
POINT faces, which also fixes its axis:

  * `l` — a `{` pointing LEFT   (VERTICAL brace, spans `size` rows; the default)
  * `r` — a `}` pointing RIGHT  (VERTICAL brace, spans `size` rows)
  * `u` — a `⏞` pointing UP     (HORIZONTAL brace, spans `size` columns)
  * `d` — a `⏟` pointing DOWN   (HORIZONTAL brace, spans `size` columns)

The brace is EB1911 presentation we CARRY — render the glyph, stretched to its
span — not editorial furniture to drop, and certainly not its raw `N|dir`
arguments to leak (4012 of them across 440 pages did exactly that).  `u`/`d`
(AGRICULTURE Table IX's column-grouping braces, 29 across 16 pages) used to fall
through to the `l` default and render as a VERTICAL `{` — the horizontal glyph +
`scaleX` is the exact twin of the vertical `{`/`}` + `scaleY`.
"""
from __future__ import annotations

# dir → (glyph, transform axis).  l/r stretch along Y (rows); u/d along X (columns).
_BRACE = {
    "l": ("{", "scaleY"), "r": ("}", "scaleY"),
    "u": ("⏞", "scaleX"), "d": ("⏟", "scaleX"),
}


def process_brace(raw: str) -> str:
    """Render `{{brace2|N|dir}}` as a `{`/`}`/`⏞`/`⏟` glyph stretched to its span."""
    parts = [p.strip() for p in raw.strip().strip("{}").split("|")][1:]
    n = next((int(p) for p in parts if p.isdigit()), 2)
    d = next((p.lower() for p in parts if p.lower() in _BRACE), "l")
    glyph, axis = _BRACE[d]
    # The brace lives in a span-cell; stretch the glyph along its axis so it reads
    # as one long brace rather than a single-cell bracket.
    return (f"«SPAN[style:display:inline-block; transform:{axis}({n}); "
            f"transform-origin:center]»{glyph}«/SPAN»")
