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
