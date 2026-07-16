"""Collect the article's section list (TOC source) from the anchors in its
assembled body — mechanical, no recognition.

The major-section anchors `«SEC:slug|name»` are stamped post-walk by
`elements._section_anchors.stamp_section_anchors` (that's where ALL the recognition
lives, over the finished body where the structure is in view); shoulder headings
ride `«SH:slug»…«/SH»` from their producer.  Here we just walk the body in document
order and gather both into the `sections` list the article JSON carries — the TOC
and the deep-link URLs read it.

This replaced the old `SC_RE` heuristic that *guessed* sections from the
centered-small-caps render (and was duplicated verbatim in the viewer).  There is
no guessing left here: a `«SEC»` is a section because `stamp_section_anchors` decided so.
"""
from __future__ import annotations

import re

from britannica.util.strings import strip_markers

# «SEC:slug|name» (major, L1), «SH:slug»…«/SH» (shoulder, L2), and «ANCHOR:slug|name»
# (link target, L3 — kept OUT of the TOC by kind=="anchor"), in doc order.
_ANCHOR_RE = re.compile(
    r"«SEC:([^|»]*)\|([^»]*)»"
    r"|«SH:([^»]*)»(.*?)«/SH»"
    r"|«ANCHOR:([^|»]*)\|([^»]*)»", re.DOTALL)


def _sh_text(s: str) -> str:
    """Shoulder label as plain display text (markers off)."""
    s = strip_markers(s)
    return s.replace(" ", " ").strip()


def detect_sections(body: str) -> list[dict]:
    """Return the section list for `body`, in document order.

    Each entry: ``{"title", "slug", "id", "level", "kind"}``.  ``id`` is
    ``section-<slug>`` — the same id the viewer stamps on the anchor, so a
    ``…#section-<slug>`` URL resolves at render time.  ``kind`` is ``"sec"`` /
    ``"sh"`` (headings, shown in the TOC) or ``"anchor"`` (a link target only —
    the Reader's Guide matcher and the resolver use it, but the TOC must skip it).
    """
    sections: list[dict] = []
    for m in _ANCHOR_RE.finditer(body):
        if m.group(1) is not None:        # «SEC» — major section (L1)
            slug, name = m.group(1), m.group(2)
            level, kind = 1, "sec"
        elif m.group(3) is not None:      # «SH:slug» — shoulder (L2)
            slug, name = m.group(3), _sh_text(m.group(4))
            level, kind = 2, "sh"
        else:                             # «ANCHOR:slug|name» — link target, NOT a heading
            slug, name = m.group(5), m.group(6)
            level, kind = 3, "anchor"
        sections.append({
            "title": name, "slug": slug, "id": f"section-{slug}",
            "level": level, "kind": kind,
        })
    return sections


def section_key(text: str) -> str:
    """The section-title match key: lowercase, alphanumerics only, so
    'History of Astronomy' matches 'historyofastronomy'.  The single Python
    owner ([[project_resolver_consolidation]]) for what was forked across the
    Reader's Guide (`_normalize_for_match`) and the topic index; the viewer's
    `sectionKey` mirrors it across the JS runtime boundary."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


_ROMAN_PREFIX_RE = re.compile(r"^[ivxlcdm]+")


def match_section(sections, name: str, *, aggressive: bool = False):
    """Resolve a section reference `name` to the matching section — a
    ``{"title", "slug", …}`` dict — within `sections`, or None.  Callers read
    its ``slug`` and/or ``title``.

    One cascade, tuned only on precision — the same per-caller knob as the xref
    resolver ([[project_resolver_consolidation]]).  Tiers: exact key → roman-
    numeral-prefix-stripped key → (aggressive only) suffix → substring.

    ``aggressive=False`` (Reader's Guide, precise): abstain on any tie — a wrong
    deep-link is a visible error, better skipped.  ``aggressive=True`` (topic
    index, recall): take the tightest (shortest-title) match on a tie."""
    target = section_key(name)
    if not target:
        return None
    exact: list = []
    prefix: list = []
    suffix: list = []
    contained: list = []
    for sec in sections:
        tkey = section_key(sec.get("title", ""))
        if not tkey:
            continue
        if tkey == target:
            exact.append(sec)
        elif _ROMAN_PREFIX_RE.sub("", tkey) == target:
            prefix.append(sec)
        elif aggressive and tkey.endswith(target):
            suffix.append(sec)
        elif target in tkey:
            contained.append(sec)
    tiers = (exact, prefix, suffix, contained) if aggressive else (exact, prefix, contained)
    for bucket in tiers:
        if len(bucket) == 1:
            return bucket[0]
        if bucket:
            if aggressive:
                # Tightest match on a tie — measured on the roman-numeral-
                # stripped key, so a "IV.—" section prefix isn't counted as
                # length (a numeral prefix is furniture, not specificity).
                return min(bucket, key=lambda s: len(
                    _ROMAN_PREFIX_RE.sub("", section_key(s.get("title", "")))))
            return None  # precise: ambiguous → skip
    return None
