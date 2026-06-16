"""SECTION element handler.

`<section begin="X"/>` / `<section end/>` are Wikisource transclusion markers.
They carry *boundary identity* (the name `X`, used as the stable-ID tiebreaker
`article.section_name`) but have **no visual output**.  Per the honesty model
([[consumed-markers-to-producers]]), the walker recognizes the tag and carries it
raw; this producer is its sole consumer — it renders NOTHING.
"""

from __future__ import annotations


def _process_section(raw: str) -> str:
    """A section transclusion marker renders to nothing — it's a boundary
    signal, not content.  (The name is boundary metadata, handled elsewhere.)"""
    return ""


# ── {{section|Name}} — the subsection ANCHOR (a different "section" construct) ──
# Not the `<section>` boundary tag above.  It marked a MINOR (run-in italic)
# subsection as a Wikisource link target, but those anchors were orphaned: a
# resolved `…#Name` link builds `#section-<slug>` — the MAJOR-section «SEC» anchors
# stamp_sections places — never the bare slug this used to emit, so they did no one
# any good.  Consume the template and emit nothing (the run-in ``''Name''.—``
# heading beside it stays as italic prose); only major sections are anchored now.


def _process_section_anchor(raw: str) -> str:
    """`{{section|Name}}` → nothing (minor subsections are not anchored)."""
    return ""
