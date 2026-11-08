"""Anchors for the article's own jumps to a printed landmark — post-walk.

A jump within the page (`[[#39|§ 39]]`, `[[#1|(1)]]`, `[[#Oil Testing|Oil
Testing]]`) is bound by the export to a heading or anchor of the same article
(`LinkResolver._resolve_colon`, empty base).  Most targets ARE headings.  Three
kinds of landmark the book prints are not, and their jumps shipped as plain
text, because nothing on the page carried the name (2026-11-08: 14 of 83 jumps,
in NUMBER, WAVE and OILS):

* a numbered PARAGRAPH — NUMBER's "§ 39" is the paragraph printed "39. …";
* an EQUATION number — WAVE's "(1)" is the formula printed ". . . . (1)";
* a run-in SUBHEAD — OILS's "Oil Testing" is the paragraph opening
  "«I»Oil Testing«/I».—".

So for each jump the article makes, and only those — an anchor is a link target
in the section index, which other references match against — when no heading or
anchor already answers it, ``stamp_jump_anchors`` puts ``«ANCHOR»`` named
exactly the jump's target at the one landmark it names.  The jump's display
says which kind: "(1)" is an equation; a bare or "§" number a paragraph; words a
subhead.  A paragraph or subhead printed twice, or not at all, gets no anchor —
the jump stays text, as before.  An equation is referred to after it is printed,
and its numbering restarts by part, so a jump takes the nearest equation of its
number before it; a later landmark of one number is named "N second" (as
figures are, `_figure_anchors`) and its jump retargeted.
"""
from __future__ import annotations

import re

from wikikit.export.sections import detect_sections, match_section
from wikikit.markers import iter_ln_markers, strip_marker_tokens
from wikikit.pipeline.stages.elements._anchor import anchor_marker
from wikikit.pipeline.stages.elements._figure_anchors import _ORDINALS
from wikikit.pipeline.stages.elements._link import ln_marker
from wikikit.util.strings import anchor_slug

# Marker tokens that may stand between a paragraph break and its first words:
# anchors, a section marker, a centring / font / span opener.
_LEAD = r"(?:«[A-Z_]+(?:\[[^\]»]*\])?(?::[^»]*)?»\s*)*"
_PARA_START = r"(?:^|«P»|«BR»)" + _LEAD
# Between a display formula and its printed number: dots, spaces, a comma, and
# the cell boundary when the number sits in a table cell of its own.
_EQ_GAP = r"(?:[\s.,; ]|«/TD»|«TD(?:\[[^\]»]*\])?»|«/CTR»|«CTR»)*"
_RUNIN_STYLES = ("I", "B", "SC")


def _jumps(body: str):
    """[LnMarker] — every same-article jump, in document order."""
    return [m for m in iter_ln_markers(body)
            if (m.target or "").strip().startswith("#") and len((m.target or "").strip()) > 1]


def _jump_name(m) -> str:
    return m.target.strip()[1:].strip()


def _paragraphs(body: str, num: str):
    """Positions of paragraphs printed "N. …"."""
    rx = re.compile(_PARA_START + r"(?P<n>" + re.escape(num) + r")\.\s")
    return [m.start("n") for m in rx.finditer(body)]


def _equations(body: str, num: str):
    """Positions of the equation number "(N)" printed after a display formula."""
    rx = re.compile(r"«/MATH»" + _EQ_GAP + r"(?P<n>\(" + re.escape(num) + r"\))")
    return [m.start("n") for m in rx.finditer(body)]


def _subheads(body: str, name: str):
    """Positions of run-in subheads — a paragraph opening with ``name`` in
    italics, bold or small capitals, then its full stop or dash."""
    want = name.strip().rstrip(".").casefold()
    hits = []
    for style in _RUNIN_STYLES:
        rx = re.compile(_PARA_START + rf"(?P<h>«{style}»(?P<t>(?:(?!«/{style}»).)*)«/{style}»)\s*[.—:]")
        for m in rx.finditer(body):
            if strip_marker_tokens(m.group("t"), "").strip().rstrip(".").casefold() == want:
                hits.append(m.start("h"))
    return sorted(hits)


def _landmark(body: str, jump) -> int | None:
    """Where the jump points, or None.  A paragraph or subhead must be printed
    exactly once — "§ 39" may point forward ("see § 39 below"), so nothing
    here chooses between two.  An EQUATION is referred to after it is printed,
    and numbering restarts by part (WAVE prints (1)–(9) twice): the nearest
    equation of that number BEFORE the jump, else the first after it."""
    name = _jump_name(jump)
    display = strip_marker_tokens(jump.display or "", "").strip()
    if name.isdigit() and display.startswith("(") and display.endswith(")"):
        found = _equations(body, name)
        before = [p for p in found if p < jump.start]
        return before[-1] if before else (found[0] if found else None)
    found = _paragraphs(body, name) if name.isdigit() else _subheads(body, name)
    return found[0] if len(found) == 1 else None


def stamp_jump_anchors(body: str) -> str:
    """``«ANCHOR»`` at the landmark each of the article's own jumps points to,
    when no heading or anchor answers it — named as the jump names it, or
    "N second" (and the jump retargeted) for a later landmark of one number."""
    jumps = _jumps(body)
    if not jumps:
        return body
    # A heading or anchor answers a jump when it IS that name, with or without its
    # numbering ("#History" → "V. History") — the strict tier, and those jumps
    # already land.  The export's substring step is no answer: it finds "1"
    # inside "Fig. 1" (WAVE's equation (1)), and an exact anchor outranks it.
    sections = detect_sections(body)
    landmark = {}                                  # jump -> position
    for j in jumps:
        if match_section(sections, _jump_name(j), strict=True) is None:
            at = _landmark(body, j)
            if at is not None:
                landmark[j] = at
    # One anchor per landmark; a number's later landmarks take "N second", …
    anchor_name, used = {}, {}
    for at in sorted(set(landmark.values())):
        base = next(_jump_name(j) for j in landmark if landmark[j] == at)
        n = used.get(base, 0)
        used[base] = n + 1
        anchor_name[at] = base if n == 0 else f"{base} {_ORDINALS[min(n, len(_ORDINALS)) - 1]}"
    edits = [(at, at, anchor_marker(anchor_slug(nm), nm)) for at, nm in anchor_name.items()]
    for j, at in landmark.items():
        if anchor_name[at] != _jump_name(j):
            edits.append((j.start, j.end, ln_marker("#" + anchor_name[at], j.display)))
    for a, b, text in sorted(edits, key=lambda e: e[0], reverse=True):
        body = body[:a] + text + body[b:]
    return body
