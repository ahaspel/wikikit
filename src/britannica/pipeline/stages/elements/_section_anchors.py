"""Section recognition — the post-walk, structure-aware replacement for the
pre-walk ``stamp_sections`` regex.

A major section heading is a centered-small-caps run in the article's prose flow.
The SOURCE marks it exactly like a figure caption or a table legend label, so
section-ness is not in the bytes — it is STRUCTURAL: a section is a heading that
is NOT inside a table (or figure).  We read that off the WALKED BODY, where the
whole article's produced structure is in plain sight: every heading is present as
``«CTR»«SC»…«/SC»«/CTR»`` (fine-print ones too — ``_process_center``'s re-walk
flattens them straight in), and every table / figure is a balanced
``«TABLE[…]»…«/TABLE»`` span — OUR own marker, matched even when nested,
unlike the malformed source ``{|`` the old regex fought.

So a section is a ``«CTR»«SC»`` heading at ``«TABLE»`` depth 0.  This is the
post-walk extraction family ([[feedback_recursion_cannot_provide_context]]), run
over ``walk_article``'s body beside the contributor / xref extractors; it stamps
``«SEC:slug|name»`` in front of each recognized heading, exactly where the old
pre-walk stamp put it, so render / TOC / export read ``«SEC»`` unchanged.

Three former false-positive classes fall out structurally, no gate: figure
captions & table legends (inside ``«TABLE»``), and a ``{{c|[[Image:…]]}}``
whose ``«CTR»`` leads with an image marker, not a bare ``«SC»`` (so it never
matches the heading shape).  The only gate kept is the caption-word refusal (a
bare top-level ``Fig.`` small-caps) plus the ≥2 / shoulder-follows series test.
"""
from __future__ import annotations

import re

from britannica.markers import collapse_links, strip_marker_tokens

from britannica.util.strings import section_slug

# A heading is a «CTR» whose FIRST LINE is entirely small-caps: peel the styled runs
# (`«SC»…«/SC»`, `«SPAN…»…«/SPAN»`) and the numeral prefix, and only punctuation is
# left.  Centered PROSE leaves plain words behind — that's the discriminator, and it
# is robust to the rich forms a heading takes: nested «SC» (MAGIC's
# `«SC»I.—«SC»…«/SC»«/SC»`), a font-sized year (HOLLAND's `«SC»…«/SC» «SPAN»1579«/SPAN»
# «SC»…«/SC»`), or a «BR» sub-line (ARITHMETIC — we read line ONE).  A single clean
# «SC» run was too strict; a whole prose paragraph with «SC» in it (ORNITHOLOGY) is
# rejected because plain words survive the peel.
_CTR_SPAN = re.compile(r"«CTR»((?:(?!«/?CTR»)[\s\S])*?)«/CTR»")
_SC_SPAN = re.compile(r"«SC»(?:(?!«/SC»)[\s\S])*?«/SC»")
_SPAN_SPAN = re.compile(r"«SPAN[^»]*»(?:(?!«/SPAN»)[\s\S])*?«/SPAN»")
# A heading may be LINK + small-caps — SOMALILAND's `«LN:…|British|…«/LN» «SC»Somaliland«/SC»`
# is "British Somaliland".  The link's display is heading material, not "plain words
# outside the small-caps", so it peels with the rest for the heading TEST.  (The old
# `«[^»]*»` strip swallowed the link AND its display, which is why this read as a
# heading before — and why its name came out as bare "Somaliland", losing "British".)
_LN_SPAN = re.compile(r"«(LN|XL)(?:\[[a-z_]*\])?:(?:(?!«/\1»)[\s\S])*?«/\1»")
_BR = re.compile(r"«/?BR»")
_WORD = re.compile(r"[^\W\d]{2,}")  # 2+ letters (any script), no digits
# Caption look-alikes: the heading's OWN text says it isn't a section.
_CAPWORD = re.compile(
    r"^\s*(?:Figs?|Plate|Table|Tabular|Pl|Diagram)\b", re.IGNORECASE)
_NUMERAL = re.compile(r"^\s*(?:[IVXLCDM]+|[A-Z]|\d+)\s*[.—\-]")
_ROMAN_PREFIX = re.compile(r"^\s*(?:[IVXLCDM]+|[A-Z]|\d+)\s*[.—\-]+\s*")
# Whitespace + transparent block markers that may sit between a heading and a
# table it titles (stepped over for the "leads-a-table" test).
_LEAD_SKIP = re.compile(r"^(?:\s|«/?P»|«/?BR»)*")
# A footnote span inside a heading («SC»History of Anatomy«FN:…note…«/FN»«/SC») —
# strip the WHOLE «FN:…«/FN» (marker + body) so the note never bleeds into the name.
_FN_SPAN = re.compile(r"«FN(?:\[[^\]]*\])?:.*?«/FN»", re.DOTALL)
# A heading name is a single line of text.  A small-caps run that carries a line
# break or an image is a figure / multi-line block the author happened to wrap in
# «SC» (ACCUMULATOR's `{{c|{{sc|{{IMG…}}<br>Fig. N.}}}}`), never a section heading.
_NOT_HEADING = re.compile(r"«BR»|«IMG|\{\{\s*IMG|\[\[\s*(?:Image|File)", re.IGNORECASE)

_TABLE_MARK_OPEN = re.compile(r"«TABLE\[")
_TABLE_MARK_CLOSE = re.compile(r"«/TABLE»")


def _table_spans(body: str) -> list[tuple[int, int]]:
    """Half-open spans covered by a balanced ``«TABLE[…]»…«/TABLE»`` (nesting
    collapsed to the outermost).  These are our own emitted markers, so they
    always balance — the containment test is reliable."""
    events = sorted(
        [(m.start(), 1) for m in _TABLE_MARK_OPEN.finditer(body)]
        + [(m.end(), -1) for m in _TABLE_MARK_CLOSE.finditer(body)])
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
    # Links collapse to their DISPLAY first: a link is an address, so stripping
    # only its delimiter would put the target filename into the heading name.
    txt = re.sub(r"\s+", " ", strip_marker_tokens(
        collapse_links(_FN_SPAN.sub("", content)), "")).replace(" ", " ")
    txt = re.sub(r"[{}]", "", txt).replace("|", "/")
    return txt.strip()


def _heading_name(ctr_content: str) -> str | None:
    """The section name if `ctr_content` (a «CTR»'s inner) is a heading, else None.

    Read the FIRST line (up to a «BR»); peel its small-caps / span runs and the
    numeral prefix.  If only punctuation remains, the line is all-small-caps → a
    heading, and its name is the line's visible text.  Plain words left over → the
    «CTR» is centered prose, not a heading."""
    line1 = _BR.split(ctr_content, 1)[0]
    if "«SC»" not in line1 or _NOT_HEADING.search(line1):
        return None  # no small-caps, or a figure / image block
    bare = line1
    for _ in range(5):  # repeat to peel nested «SC»
        reduced = _LN_SPAN.sub("", _SPAN_SPAN.sub("", _SC_SPAN.sub("", bare)))
        if reduced == bare:
            break
        bare = reduced
    # Drop the footnote SPAN before the word test, exactly as `_name` does below.
    # A heading may carry one (`«SC»Relatives of the Prophet«/SC»«FN:* is prefixed
    # to names…«/FN»`), and its prose is not "words outside the small-caps" — it is
    # a note hanging off the heading.  This read correctly only by accident while
    # the marker strip was `«[^»]*»`: that span ran from `«FN:` to the first `»`
    # and ate the note's text along with the marker, so nothing was left to fail
    # the test.  Stripping tokens properly exposed it — CERAMICS lost 2 sections,
    # SOMALILAND 2, MACHINE-GUN and MAHOMET 1 each.
    bare = _ROMAN_PREFIX.sub(
        "", strip_marker_tokens(_FN_SPAN.sub("", bare), ""))
    if _WORD.search(bare):
        return None  # plain words outside the small-caps → centered prose
    name = _name(line1)
    if not name or _CAPWORD.match(name):
        return None  # empty, or a caption (Fig./Plate./Table.)
    return name


def stamp_section_anchors(body: str) -> str:
    """Stamp ``«SEC:slug|name»`` before each recognized section heading in the
    walked body, and return the annotated body.

    A section is a ``«CTR»«SC»`` heading at ``«TABLE»`` depth 0 whose name is
    not a caption word and which does not title a table below it.  There is NO
    ≥2 / shoulder-follows count gate: that was a proxy for false-positive
    protection under the old raw-text regex, and it is doubly obsolete now — the
    structural filters above do the protection, and the *display* count threshold
    lives in `_build_toc` (which draws no "Contents" box for a lone section).
    Recognize the section; let the renderer decide whether a TOC is worth drawing.
    A lone `«SEC»` is just an invisible deep-link anchor."""
    spans = _table_spans(body)

    def in_table(p: int) -> bool:
        return any(a <= p < b for a, b in spans)

    heads: list[tuple[int, str]] = []
    for m in _CTR_SPAN.finditer(body):
        if in_table(m.start()):
            continue  # legend label / figure caption — not a section
        name = _heading_name(m.group(1))
        if name is None:
            continue
        # An UNNUMBERED heading that immediately leads a table is that table's
        # TITLE, not a section — and NOT subsumed by the «TABLE» exclusion,
        # because the title sits ABOVE the table, outside its span.
        if not _NUMERAL.match(name):
            after = _LEAD_SKIP.sub("", body[m.end():m.end() + 120])
            if after.startswith("«TABLE["):
                continue
        heads.append((m.start(), name))

    for pos, name in reversed(heads):  # tail-first so positions stay valid
        slug = section_slug(_ROMAN_PREFIX.sub("", name))
        body = body[:pos] + f"«SEC:{slug}|{name}»" + body[pos:]
    return body
