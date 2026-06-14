"""Collect the article's section list (TOC source) from the anchors in its
assembled body — mechanical, no recognition.

The major-section anchors `«SEC:slug|name»` are stamped pre-walk by
`transform_articles.sections.stamp_sections` (that's where ALL the recognition
lives); shoulder headings ride `«SH»…«/SH»` from their producer.  Here we just
walk the body in document order and gather both into the `sections` list the
article JSON carries — the TOC and the deep-link URLs read it.

This replaced the old `SC_RE` heuristic that *guessed* sections from the
centered-small-caps render (and was duplicated verbatim in the viewer).  There is
no guessing left: a `«SEC»` is a section because `stamp_sections` decided so.
"""
from __future__ import annotations

import re

from britannica.util.strings import section_slug

# «SEC:slug|name» (major, L1) and «SH»…«/SH» (shoulder, L2), in document order.
_ANCHOR_RE = re.compile(r"«SEC:([^|»]*)\|([^»]*)»|«SH»(.*?)«/SH»", re.DOTALL)
_MARKER = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")
_DEHYPH = re.compile(r"([a-z])-\s*([a-z])")


def _sh_text(s: str) -> str:
    """Shoulder label as plain text (markers off, line-wrap hyphen healed)."""
    s = _MARKER.sub("", s)
    s = _DEHYPH.sub(r"\1\2", s)
    return s.replace(" ", " ").strip()


def detect_sections(body: str) -> list[dict]:
    """Return the section list for `body`, in document order.

    Each entry: ``{"title", "slug", "id", "level", "kind"}``.  ``id`` is
    ``section-<slug>`` — the same id the viewer stamps on the anchor, so a
    ``…#section-<slug>`` URL resolves at render time.
    """
    sections: list[dict] = []
    active = ""  # slug of the most recent L1 section (for margin-pointer dedup)
    for m in _ANCHOR_RE.finditer(body):
        if m.group(3) is None:  # «SEC» — major section
            slug, name = m.group(1), m.group(2)
            sections.append({
                "title": name, "slug": slug, "id": f"section-{slug}",
                "level": 1, "kind": "sec",
            })
            active = slug
        else:                   # «SH» — shoulder
            name = _sh_text(m.group(3))
            slug = section_slug(name)
            if slug and slug == active:
                continue  # margin pointer (repeats the active section) — omit
            sections.append({
                "title": name, "slug": slug, "id": f"section-{slug}",
                "level": 2, "kind": "sh",
            })
    return sections
