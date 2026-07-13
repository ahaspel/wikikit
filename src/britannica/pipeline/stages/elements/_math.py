"""MATH family producers and predicates (inline).

Owns the math templates the walker lifts out of body-text:

  Labeled display equations (ONE label, `MATH_EQUATION`)
    `{{equation|…}}` / `{{MathForm1|label|content}}` / `{{ne|...}}`.
    NOT three math things — three arg conventions of one labeling WRAPPER
    whose body is mixed content (prose, `«I»`, {{Greek}}, {{sfrac}}, an
    optional opaque `<math>` child).  So it's a DECOMPOSE, like TOC_ROW /
    DUAL_LINE: `_eqn_parse` (parse-args, the "label business") slices a
    NUMBER slot + a BODY slot, `_classify_equation_composite` recurses each
    to a CELL, and `_process_math_equation` reassembles `«EQN:LABEL»…«/EQN»`
    (decode the number, finish the body).  The `«EQN»` block is the one
    math-qua-math bit; a `<math>` tag inside stays an opaque leaf.

Inline `{{sfrac|...}}` fraction variants and `{{sub|x}}` / `{{sup|x}}`
super/subscripts are NOT walker chunks — they're typography whose
rendered output flows back into prose.  Body-text owns them via
`_convert_sfrac` and `_convert_sub_sup`.

Block-level `<math>` is a self-labeling leaf produced in `_leaf.py`; a
table of math cells is just a TABLE that recurses to those leaves (the
old `MATH_LAYOUT_*` table classification was collapsed away).  This
module is the home for inline math going forward.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._dual_line import (
    _split_top_level_pipe)


# ── Labeled-equation family (equation / MathForm1 / ne) ──────────────


_PAREN_LABEL_RE = re.compile(r"\(([^()]+)\)")


def _eqn_strip_paren_label(raw_label: str) -> str:
    """The equation number's recurse-slot — its presentational outer `(N)` parens stripped
    (the viewer supplies its own).  The stripped inner is article markup ({{sc}}, «I»,
    {{ditto}}, a number) recursed to a CELL node; the producer decodes its markers to the
    plain-text margin label.  Replaces `_eqn_label`'s paren-strip; its process/decode moved
    to the producer, so the label recurses in the tree instead of a per-label flatten."""
    paren = _PAREN_LABEL_RE.match(raw_label.strip())
    return (paren.group(1) if paren else raw_label).strip()


def _eqn_parse(raw: str) -> "tuple[str, str]":
    """`(number_slot, body_slot)` — the equation NUMBER and BODY sliced from a labeled-
    equation template by its per-name arg convention.  This is the parse-args "label
    business" (standard producer stuff): the three template names differ only in WHICH arg
    carries the number vs the body; the body itself is MIXED content (prose lead-ins, `«I»`,
    {{Greek}}, {{sfrac}}, an optional opaque `<math>` child), so it decomposes to nodes like
    anything else — the only math-qua-math bit is the `«EQN»` block the producer emits.

      * `{{equation|body[|tag=(N)|pretext=…]}}` — body is the positional(s); `tag=` the number.
      * `{{MathForm1|(N)|body}}` — number-first, body-second.
      * `{{ne|[pretext|]body[|(N)]}}` — arg0 a lead-in (EMPTY in 974/983 uses; a lone arg is
        the bare equation), then body, then number.  (Earlier this read a non-empty arg0 AS
        the body and the equation AS the number — swapping the slots on `{{ne|we have|<math>…|
        (N)}}` (GEOMETRY) and dropping the number.  Byte-identical for every empty-arg0 form.)

    Shared by the composite (which recurses each slot to a CELL) and the producer (which reads
    the empty-body early-out + the number-empty guard off it) — one parse, called twice."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    parts = _split_top_level_pipe(inner)
    if not parts:
        return "", ""
    name = parts[0].strip().lower()
    label_raw = ""
    content = ""
    if name == "equation":
        positional: list[str] = []
        for p in parts[1:]:
            s = p.strip()
            if s.lower().startswith("tag="):
                label_raw = s[4:]
            elif s.lower().startswith("pretext="):
                # Inter-equation connector ("to", "and", …); no marker support — dropped.
                continue
            else:
                positional.append(s)
        content = "|".join(positional).strip()
    elif name == "mathform1":
        if len(parts) >= 2:                       # number-first, body-second
            label_raw = parts[1]
        content = "|".join(parts[2:]).strip()
    elif name == "ne":
        args = parts[1:]
        if len(args) == 1:
            content = args[0].strip()
        elif args:
            pretext = args[0].strip()
            content = args[1].strip()
            if pretext:
                content = f"{pretext} {content}"      # lead text rides in the equation row
            if len(args) > 2:
                label_raw = args[2]
    return _eqn_strip_paren_label(label_raw), content


_INTERNAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.;:!?])")


def _process_math_equation(raw, inner, context, inner_registry) -> str:
    """Reassemble a labeled display equation from its two decomposed CELL markers.

    `_classify_equation_composite` chopped the template (`_eqn_parse`) into a NUMBER slot and
    a BODY slot and recursed each — so the body's `«I»` / {{Greek}} / {{sfrac}} / opaque
    `<math>` are real child nodes, not a re-`process_elements` flatten.  Here we read the two
    markers: decode the number to a plain-text margin label (the viewer `escapeHtml`s the
    label and can't show markup in the margin — and the slot is `»`-delimited, so a stray
    marker would collide), finish the body, and emit the self-delimiting `«EQN:LABEL»…«/EQN»`
    block.  The `«EQN»` block + finishing is the one math-qua-math bit; the slicing is
    standard parse-args."""
    from britannica.pipeline.stages.elements import _cell_markers
    from britannica.pipeline.stages.elements._title import decode_title
    label_slot, content_slot = _eqn_parse(raw)
    if not content_slot:
        return ""
    label_marker, content = (_cell_markers(inner_registry) + ["", ""])[:2]
    label = decode_title(label_marker) if label_slot else ""
    # `[ \t]+` is ASCII-only and leaves \xa0 alone: a non-breaking space is carried content,
    # not equation layout, so it rides through intact rather than flattened to a collapsible
    # space.  (ORDNANCE eqn (14)'s `{{sfrac|…}}  + {{sfrac|…}}` double space collapses here.)
    content = _INTERNAL_WHITESPACE_RE.sub(" ", content)
    # Strip space-before-punctuation — body-text's old body-only finishing.  Collapses
    # MOLECULE's literal `+ . . .` to `+...` (the source spells the ellipsis with spaces).
    content = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", content)
    # `«EQN»` is a self-delimiting block marker (like `«TABLE»`): the renderer's block scan
    # peels it in place and it renders as its own `math-system` grid.  Paragraph structure is
    # carried by `«P»`, never re-inferred from blank lines.
    return f"«EQN:{label}»{content}«/EQN»"
