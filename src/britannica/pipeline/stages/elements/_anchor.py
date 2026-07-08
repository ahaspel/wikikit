"""ANCHOR producer — Wikisource link-target anchors, carried as «ANCHOR:slug|name».

Three spellings, one family:
  • ``{{anchor|a|b|c}}``    — one or more invisible link targets (each arg an id).
  • ``{{anchor+|id|text}}`` — a target AND visible display text (an italic heading / a name).
  • ``{{section|Name}}``    — the minor-subsection target (run-in italic heading beside it).

These are GENUINE targets, not wiki crutches: the Reader's Guide deep-links and
intra-article ``[[#X]]`` references resolve onto them (67 broken cross-references proved
it while we were liquidating them).  So we CARRY them.  Each emits a point anchor
``«ANCHOR:slug|name»`` — a sibling of ``stamp_section_anchors``' ``«SEC:slug|name»``: same
``section_slug``, same ``id="section-<slug>"`` the viewer stamps, so the href lands.  The
ONE difference is that ``detect_sections`` files it as ``kind="anchor"``, keeping it OUT of
the table of contents (it is a target, not a heading).

``<section begin/end>`` — the transclusion-boundary tag — is a real wiki crutch and stays
liquidated in ``_section.py``; it is NOT this family.
"""
from __future__ import annotations

import re

from britannica.util.strings import section_slug
from britannica.pipeline.stages.elements._link import _split_top_pipes

_SANITIZE = re.compile(r"[{}«»]")


def _anchor(name: str) -> str:
    """One ``«ANCHOR:slug|name»`` point anchor for ``name`` (or "" if empty).

    The name is INERT label text by the «SEC»/«ANCHOR» contract — strip braces and
    marker glyphs and escape the ``|`` delimiter, so a recognition slip can never smuggle
    a live ``{{…}}`` or marker that the walk would re-parse."""
    name = _SANITIZE.sub("", name).replace("|", "/").strip()
    return f"«ANCHOR:{section_slug(name)}|{name}»" if name else ""


def process_anchor(raw: str, context) -> str:
    """``{{anchor|…}}`` / ``{{anchor+|…}}`` / ``{{section|…}}`` → ``«ANCHOR»`` point
    anchors, plus the recursed display text for the ``anchor+`` variant."""
    from britannica.pipeline.stages.elements import process_elements
    inner = re.sub(r"^\{\{", "", raw.strip())
    inner = re.sub(r"\}\}\s*$", "", inner)
    parts = _split_top_pipes(inner)
    tmpl = parts[0].strip().lower()
    args = parts[1:]

    if tmpl == "anchor+":
        # The one spelling with visible content: id = args[0]; display = args[1] (or the id).
        if not args:
            return ""
        display = args[1] if len(args) > 1 else args[0]
        return _anchor(args[0]) + process_elements(display, context)
    if tmpl.startswith("anchor#"):
        # `{{anchor#Exterior}}` — a no-pipe spelling; the id rides after the `#`.
        return _anchor(parts[0].split("#", 1)[1])
    # `{{anchor|a|b|c}}` / `{{section|Name}}` — pure targets, no visible text.
    return "".join(_anchor(a) for a in args)
