"""MATH family producers and predicates (inline).

Owns the math templates the walker lifts out of body-text:

  Labeled display equations (`MATH_EQUATION`, `MATH_FORMULA_LABELED`,
  `MATH_NE`)
    `{{equation|…}}` / `{{MathForm1|label|content}}` / `{{ne|...}}`.
    All three labels dispatch to ONE producer (`_process_math_equation`)
    which selects per-template arg parsing and emits the shared
    `«EQN:LABEL»content«/EQN»` marker with `\\n\\n` paragraph margins.
    These templates are declared as math by their template name —
    they have their own paragraph context and marker contract, so
    the walker recognizes them structurally.

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


def _eqn_label(raw_label: str, context) -> str:
    """The equation number for the `«EQN:LABEL»` slot.

    The label is article markup ({{sc}}, «I», {{ditto}}, a paren-wrapped
    number), so RECURSE it like any content, then decode the resulting markers
    to plain text: the viewer renders the label `escapeHtml`-ed (it can't show
    markup in the margin) and the slot is `»`-delimited, so a stray `«…»` marker
    would both display literally and collide with the marker boundary.
    `decode_title` is the shared marker→plain-text flattener — the same PLAIN
    view the dropdown/xref title uses. The outer `(N)` parens are presentational
    (the viewer supplies its own), so strip them first.

    Replaces `_clean_eqn_label`, a partial mauler that stripped parens + `''`
    (already dead — quote-runs convert `''`→«I» upstream) and leaked every other
    markup form ({{sc}}, {{ditto}}, …) raw into the label slot.
    """
    from britannica.pipeline.stages.elements import process_elements
    from britannica.pipeline.stages.elements._title import decode_title
    paren = _PAREN_LABEL_RE.match(raw_label.strip())
    inner = (paren.group(1) if paren else raw_label).strip()
    if not inner:
        return ""
    return decode_title(process_elements(inner, context, _allow_figure=False))


_INTERNAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.;:!?])")


def _process_math_equation(inner: str, context) -> str:
    """Render any labeled-display-equation template.

    One producer covers three templates that share the
    `«EQN:LABEL»content«/EQN»` rendering contract; the only
    per-template variation is HOW the label and content are sliced
    out of the args.  Selecting by template name keeps the
    dispatch out of the classifier:

      * `{{equation|content[|tag=(N)|pretext=…]}}` — content is the
        first positional; `tag=` (a named param) carries the label.
      * `{{MathForm1|label|content}}` — label-first, content-second.
      * `{{ne|content}}` / `{{ne||content}}` / `{{ne||content|(N)}}` —
        three numbered-equation shapes; first slot is the (often
        empty) label, content follows.

    Body-text used to handle these via the iterative regex+_ne_labeled
    block; now lifted at the walker.  `\\n\\n` margins isolate the
    equation as its own paragraph, matching the legacy contract.

    Internal whitespace in the rendered content is collapsed to single
    spaces — mirrors body-text's old `[ \\t]+` collapse that
    body-text used to apply to ne content when it was rendered in
    body.  Keeps byte-identity with the legacy iterative `_ne_labeled`
    path (ORDNANCE eqn (14) has `{{sfrac|...|E|r}}  + {{sfrac|...|F|r}}`
    with double space, source-faithful but historically collapsed).
    """
    parts = _split_top_level_pipe(inner)
    if not parts:
        return ""
    name = parts[0].strip().lower()
    label = ""
    content = ""
    if name == "equation":
        positional: list[str] = []
        for p in parts[1:]:
            s = p.strip()
            if s.lower().startswith("tag="):
                label = _eqn_label(s[4:], context)
            elif s.lower().startswith("pretext="):
                # Inter-equation connector ("to", "and", …); no marker
                # support yet — drop and revisit if the renderer ever
                # needs it.
                continue
            else:
                positional.append(s)
        content = "|".join(positional).strip()
    elif name == "mathform1":
        # label-first, content-second (joined back for multi-pipe
        # content).
        if len(parts) >= 2:
            label = _eqn_label(parts[1], context)
        content = "|".join(parts[2:]).strip()
    elif name == "ne":
        # `{{ne|[pretext|]content[|label]}}` — arg0 is a lead-in, EMPTY in 974/983
        # corpus uses; a lone arg is the bare equation.  Earlier this read a non-empty
        # arg0 AS the content and the equation AS the label — swapping the slots on
        # `{{ne|we have|<math>…</math>|(N)}}` (GEOMETRY) / `{{ne|therefore|…|}}` and
        # dropping the number.  Byte-identical for every empty-arg0 / single-arg form.
        args = parts[1:]
        if len(args) == 1:
            content = args[0].strip()
        elif args:
            pretext = args[0].strip()
            content = args[1].strip()
            if pretext:
                content = f"{pretext} {content}"      # lead text rides in the equation row
            if len(args) > 2:
                label = _eqn_label(args[2], context)
    if not content:
        return ""
    # Return the inner (the equation body) through the loop: a `<math>` /
    # `{{sfrac}}` / `{{sub}}` / `{{Greek}}` inside is produced by its own producer,
    # not left raw.  (The old non-leaf classify lifted these to child placeholders;
    # with DOUBLE_BRACE a leaf, the producer recurses its own content.)
    from britannica.pipeline.stages.elements import process_elements
    rendered = process_elements(content, context, _allow_figure=False)
    # `[ \t]+` is ASCII-only and leaves \xa0 alone: a non-breaking space is
    # carried content, not equation layout, so it rides through intact rather
    # than being flattened to a plain (collapsible) space.
    rendered = _INTERNAL_WHITESPACE_RE.sub(" ", rendered)
    # Strip space-before-punctuation — same as body-text's old
    # body-only finishing.  Collapses MOLECULE's literal `+ . . . ` to
    # `+...` (the source spells out the ellipsis with spaces between
    # each period; body-text used to drop those spaces by the same
    # rule, and that's what the snapshot baseline assumes).
    rendered = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", rendered)
    # `«EQN»` is a self-delimiting block marker (like `«TABLE»`): the renderer's block scan
    # peels it in place and it renders as its own `math-system` grid.  No `\n\n` paragraph
    # margins — paragraph structure is carried by `«P»`, never re-inferred from blank lines.
    return f"«EQN:{label}»{rendered}«/EQN»"
