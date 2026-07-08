"""Section recognition — the post-walk, structure-aware replacement for the
pre-walk ``stamp_sections`` regex.

A major section heading is a centered-small-caps run in the article's prose flow.
The SOURCE marks it exactly like a figure caption or a table legend label, so
section-ness is not in the bytes — it is STRUCTURAL: a section is a heading that
is NOT inside a table (or figure).  We read that off the WALKED BODY, where the
whole article's produced structure is in plain sight: every heading is present as
``«CTR»«SC»…«/SC»«/CTR»`` (fine-print ones too — ``_process_center``'s re-walk
flattens them straight in), and every table / figure is a balanced
``«HTMLTABLE:…«/HTMLTABLE»`` span — OUR own marker, matched even when nested,
unlike the malformed source ``{|`` the old regex fought.

So a section is a ``«CTR»«SC»`` heading at ``«HTMLTABLE»`` depth 0.  This is the
post-walk extraction family ([[feedback_recursion_cannot_provide_context]]), run
over ``walk_article``'s body beside the contributor / xref extractors; it stamps
``«SEC:slug|name»`` in front of each recognized heading, exactly where the old
pre-walk stamp put it, so render / TOC / export read ``«SEC»`` unchanged.

Three former false-positive classes fall out structurally, no gate: figure
captions & table legends (inside ``«HTMLTABLE»``), and a ``{{c|[[Image:…]]}}``
whose ``«CTR»`` leads with an image marker, not a bare ``«SC»`` (so it never
matches the heading shape).  The only gate kept is the caption-word refusal (a
bare top-level ``Fig.`` small-caps) plus the ≥2 / shoulder-follows series test.
"""
from __future__ import annotations

import re

from britannica.util.strings import section_slug

# A heading: a «CTR» whose ENTIRE content is an (optional numeral prefix) + EXACTLY
# ONE «SC»…«/SC» small-caps run + trailing punctuation, nothing else.  The three
# constraints do the work:
#   * the numeral prefix (before «SC») is a bare number/letter token, not free text
#     — so a «CTR» that leads with prose, an image, or a link never matches (that
#     is what excludes `{{c|[[Image:…]]}}` and centered prose that opens plainly);
#   * the run forbids «/SC» inside it, so a paragraph with two small-caps runs
#     can't be spanned as one giant heading (ORNITHOLOGY / NEWSPAPERS);
#   * after «/SC» only punctuation/space may precede «/CTR» — real text after the
#     small-caps means it is centered PROSE, not a heading.
_HEAD_RE = re.compile(
    r"«CTR»\s*(?:«ANCHOR:[^»]*»\s*)?"               # optional leading {{anchor}} → «ANCHOR»
    r"((?:[IVXLCDM]+|[A-Z]|\d+)\s*[.—\-]+\s*)?"     # optional numeral prefix (A.— / 3. / II.)
    r"«SC»((?:(?!«/SC»).)+)«/SC»"                    # exactly one small-caps run
    r"[\s.·—\-]*«/CTR»",                            # only punctuation/space after
    re.DOTALL)
# Caption look-alikes: the heading's OWN text says it isn't a section.
_CAPWORD = re.compile(
    r"^\s*(?:Fig|Plate|Table|Tabular|Pl|Diagram)\b", re.IGNORECASE)
_NUMERAL = re.compile(r"^\s*(?:[IVXLCDM]+|[A-Z]|\d+)\s*[.—\-]")
_ROMAN_PREFIX = re.compile(r"^\s*(?:[IVXLCDM]+|[A-Z]|\d+)\s*[.—\-]+\s*")
# Whitespace + transparent block markers that may sit between a heading and a
# table it titles (stepped over for the "leads-a-table" test).
_LEAD_SKIP = re.compile(r"^(?:\s|«/?P»|«/?BR»)*")
_ANY_MARKER = re.compile(r"«[^»]*»")
# A footnote span inside a heading («SC»History of Anatomy«FN:…note…«/FN»«/SC») —
# strip the WHOLE «FN:…«/FN» (marker + body) so the note never bleeds into the name.
_FN_SPAN = re.compile(r"«FN(?:\[[^\]]*\])?:.*?«/FN»", re.DOTALL)
# A heading name is a single line of text.  A small-caps run that carries a line
# break or an image is a figure / multi-line block the author happened to wrap in
# «SC» (ACCUMULATOR's `{{c|{{sc|{{IMG…}}<br>Fig. N.}}}}`), never a section heading.
_NOT_HEADING = re.compile(r"«BR»|«IMG|\{\{\s*IMG|\[\[\s*(?:Image|File)", re.IGNORECASE)

_HTMLTABLE_OPEN = re.compile(r"«HTMLTABLE:")
_HTMLTABLE_CLOSE = re.compile(r"«/HTMLTABLE»")
_SH_OPEN = "«SH:"


def _htmltable_spans(body: str) -> list[tuple[int, int]]:
    """Half-open spans covered by a balanced ``«HTMLTABLE:…«/HTMLTABLE»`` (nesting
    collapsed to the outermost).  These are our own emitted markers, so they
    always balance — the containment test is reliable."""
    events = sorted(
        [(m.start(), 1) for m in _HTMLTABLE_OPEN.finditer(body)]
        + [(m.end(), -1) for m in _HTMLTABLE_CLOSE.finditer(body)])
    spans, depth, start = [], 0, None
    for pos, delta in events:
        if delta == 1:
            if depth == 0:
                start = pos
            depth += 1
        else:
            depth -= 1
            if depth <= 0:
                if start is not None:
                    spans.append((start, pos))
                    start = None
                depth = 0
    if start is not None:
        spans.append((start, len(body)))
    return spans


def _name(content: str) -> str:
    """The heading's display text: drop every marker, normalize spaces.  The body
    is already produced (no `{{…}}` templates), so marker-strip IS the visible
    text.  INERT by contract — strip stray braces, escape the `|` delimiter."""
    txt = _ANY_MARKER.sub("", _FN_SPAN.sub("", content)).replace(" ", " ")
    txt = re.sub(r"[{}]", "", txt).replace("|", "/")
    return txt.strip()


def stamp_section_anchors(body: str) -> str:
    """Stamp ``«SEC:slug|name»`` before each recognized section heading in the
    walked body, and return the annotated body.

    A section is a ``«CTR»«SC»`` heading at ``«HTMLTABLE»`` depth 0, whose name is
    not a caption word.  The series is gated as the old recognizer: keep only if
    ≥2 survive, OR exactly one survives AND a shoulder heading (``«SH:``) follows
    it (Poland's sole "Polish Literature")."""
    spans = _htmltable_spans(body)

    def in_table(p: int) -> bool:
        return any(a <= p < b for a, b in spans)

    heads: list[tuple[int, str]] = []
    for m in _HEAD_RE.finditer(body):
        if in_table(m.start()):
            continue  # legend label / figure caption — not a section
        if _NOT_HEADING.search(m.group(2)):
            continue  # «SC» wraps a figure / multi-line block, not a heading name
        name = _name((m.group(1) or "") + m.group(2))
        if not name or _CAPWORD.match(name):
            continue  # caption (Fig./Plate./Table.)
        # An UNNUMBERED heading that immediately leads a table is that table's
        # TITLE, not a section — and NOT subsumed by the «HTMLTABLE» exclusion,
        # because the title sits ABOVE the table, outside its span.
        if not _NUMERAL.match(name):
            after = _LEAD_SKIP.sub("", body[m.end():m.end() + 120])
            if after.startswith("«HTMLTABLE:"):
                continue
        heads.append((m.start(), name))

    if len(heads) < 2:
        if not (len(heads) == 1 and _SH_OPEN in body[heads[0][0]:]):
            return body

    for pos, name in reversed(heads):  # tail-first so positions stay valid
        slug = section_slug(_ROMAN_PREFIX.sub("", name))
        body = body[:pos] + f"«SEC:{slug}|{name}»" + body[pos:]
    return body
