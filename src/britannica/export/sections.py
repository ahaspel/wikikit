"""Collect the article's section list (TOC source) from the anchors in its
assembled body — mechanical, no recognition.

The major-section anchors `«SEC:slug|name»` are stamped pre-walk by
`transform_articles.sections.stamp_sections` (that's where ALL the recognition
lives); shoulder headings ride `«SH:slug»…«/SH»` from their producer.  Here we just
walk the body in document order and gather both into the `sections` list the
article JSON carries — the TOC and the deep-link URLs read it.

This replaced the old `SC_RE` heuristic that *guessed* sections from the
centered-small-caps render (and was duplicated verbatim in the viewer).  There is
no guessing left: a `«SEC»` is a section because `stamp_sections` decided so.
"""
from __future__ import annotations

import re

from britannica.util.strings import strip_markers

# «SEC:slug|name» (major, L1) and «SH:slug»…«/SH» (shoulder, L2), in doc order.
_ANCHOR_RE = re.compile(
    r"«SEC:([^|»]*)\|([^»]*)»|«SH:([^»]*)»(.*?)«/SH»", re.DOTALL)


def _sh_text(s: str) -> str:
    """Shoulder label as plain display text (markers off)."""
    s = strip_markers(s)
    return s.replace(" ", " ").strip()


def detect_sections(body: str) -> list[dict]:
    """Return the section list for `body`, in document order.

    Each entry: ``{"title", "slug", "id", "level", "kind"}``.  ``id`` is
    ``section-<slug>`` — the same id the viewer stamps on the anchor, so a
    ``…#section-<slug>`` URL resolves at render time.
    """
    sections: list[dict] = []
    for m in _ANCHOR_RE.finditer(body):
        if m.group(1) is not None:        # «SEC» — major section (L1)
            slug, name = m.group(1), m.group(2)
            level, kind = 1, "sec"
        else:                             # «SH:slug» — shoulder (L2)
            slug, name = m.group(3), _sh_text(m.group(4))
            level, kind = 2, "sh"
        sections.append({
            "title": name, "slug": slug, "id": f"section-{slug}",
            "level": level, "kind": kind,
        })
    return sections
