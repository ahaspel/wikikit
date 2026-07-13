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


def _anchor_display(raw: str) -> str:
    """The one recursed slot (the PEEL side) — `{{anchor+|id|display}}`'s visible text (arg-1,
    or the id if that's all there is).  The pure-target spellings (`{{anchor|…}}` /
    `{{section|…}}` / `{{anchor#id}}`) have no visible content → ''."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw.strip()))
    parts = _split_top_pipes(inner)
    if parts[0].strip().lower() != "anchor+":
        return ""
    args = parts[1:]
    if not args:
        return ""
    return args[1] if len(args) > 1 else args[0]


def _wrap_anchor(raw, body, ctx):
    """ANCHOR wrap (a `_PR_WRAP` row): ``{{anchor|…}}`` / ``{{anchor+|…}}`` / ``{{section|…}}``
    → ``«ANCHOR:slug|name»`` point anchors; ``anchor+`` also appends its recursed display
    ``body``.  Folds the old `process_anchor` — the display is now a classified child slot."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw.strip()))
    parts = _split_top_pipes(inner)
    tmpl = parts[0].strip().lower()
    args = parts[1:]
    if tmpl == "anchor+":
        # id = args[0]; the visible display (arg-1 / the id) is the recursed `body`.
        return (_anchor(args[0]) + body) if args else ""
    if tmpl.startswith("anchor#"):
        # `{{anchor#Exterior}}` — a no-pipe spelling; the id rides after the `#`.
        return _anchor(parts[0].split("#", 1)[1])
    # `{{anchor|a|b|c}}` / `{{section|Name}}` — pure targets, no visible text.
    return "".join(_anchor(a) for a in args)
