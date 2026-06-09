"""SECTION element handler.

`<section begin="X"/>` / `<section end/>` are Wikisource transclusion markers.
They carry *boundary identity* (the name `X`, used as the stable-ID tiebreaker
`article.section_name`) but have **no visual output**.  Per the honesty model
([[consumed-markers-to-producers]]), the walker recognizes the tag and carries it
raw; this producer is its sole consumer — it renders NOTHING.

The name is metadata, not body content.  Today the boundary detector still
captures `section_name` by its own means; once the super-walker is the honest
segment-writer (B3) it will record the name as the article's boundary identity.
``section_name`` exposes the extractor for that step.
"""

from __future__ import annotations

import re

from britannica.util.strings import section_slug

_SECTION_NAME_RE = re.compile(
    r'<section\s+begin\s*=\s*"?([^">]*)"?\s*/?>', re.IGNORECASE)


def section_name(raw: str) -> str:
    """The `begin="X"` name of a section tag, or "" for an end tag / no name."""
    m = _SECTION_NAME_RE.match(raw.strip())
    return m.group(1).strip() if m else ""


def _process_section(raw: str) -> str:
    """A section transclusion marker renders to nothing — it's a boundary
    signal, not content.  (The name is boundary metadata, handled elsewhere.)"""
    return ""


# ── {{section|Name}} — the subsection ANCHOR (a different "section" construct) ──
# Not the `<section>` boundary tag above: this is a Wikisource link TARGET.  The
# same-article `[[#Name]]` links, the cross-article `…/Article#Name` xrefs, and the
# reader's-guide references all point at it.  No visual output — the run-in
# ``''Name''.—`` heading beside it is the visible text; we carry it as a point
# anchor so those links resolve instead of dangling.

_SECTION_ANCHOR_RE = re.compile(r"\{\{\s*section\s*\|([^}]*)\}\}", re.IGNORECASE)


def _process_section_anchor(raw: str) -> str:
    """`{{section|Name}}` → an invisible point anchor ``«ANCHOR:slug»``."""
    m = _SECTION_ANCHOR_RE.match(raw.strip())
    if not m:
        return ""
    # first non-empty pipe-arg (one source has a stray `{{section||Name}}`)
    name = next((a.strip() for a in m.group(1).split("|") if a.strip()), "")
    return f"«ANCHOR:{section_slug(name)}»" if name else ""
