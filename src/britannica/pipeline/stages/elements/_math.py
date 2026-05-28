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

The dual-line family (CHEM_DUAL / MATH_DUAL / DUAL_LINE) is the
content-aware split established in the dual-line element-promotion
work; the classifier inspects content to dispatch.  Predicates and
the math-dual producer live in this module.

Block-level math (``<math>``, ``MATH_LAYOUT_*`` for table-shaped
math) lives in `_leaf.py` and `_math_layout.py` for historical
reasons; this module is the home for inline math going forward.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._dual_line import (
    _process_dual_line, _split_top_level_pipe)


# ── Labeled-equation family (equation / MathForm1 / ne) ──────────────


_PAREN_LABEL_RE = re.compile(r"\(([^()]+)\)")


def _clean_eqn_label(raw_label: str) -> str:
    """Source labels are `(N)` with optional `''italic''` decoration.
    Strip parens and italic markers — the `«EQN:LABEL»` slot is plain
    text, and a stray `''…''` would later collide with the marker's
    own `»` boundary."""
    paren = _PAREN_LABEL_RE.match(raw_label.strip())
    label = (paren.group(1) if paren else raw_label).strip()
    return label.replace("''", "")


_INTERNAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([,.;:!?])")


def _process_math_equation(inner: str, text_transform) -> str:
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
    spaces — mirrors `_transform_body_text`'s `[ \\t]+` collapse that
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
                label = _clean_eqn_label(s[4:])
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
            label = _clean_eqn_label(parts[1])
        content = "|".join(parts[2:]).strip()
    elif name == "ne":
        args = parts[1:]
        # `{{ne||content[|label]}}` — drop the empty label-slot prefix.
        if args and args[0].strip() == "":
            args = args[1:]
        if args:
            content = args[0].strip()
            if len(args) > 1:
                label = _clean_eqn_label(args[1])
    if not content:
        return ""
    rendered = text_transform(content)
    rendered = rendered.replace("\xa0", " ")
    rendered = _INTERNAL_WHITESPACE_RE.sub(" ", rendered)
    # Strip space-before-punctuation — same as `_transform_body_text`'s
    # body-only finishing.  Collapses MOLECULE's literal `+ . . . ` to
    # `+...` (the source spells out the ellipsis with spaces between
    # each period; body-text used to drop those spaces by the same
    # rule, and that's what the snapshot baseline assumes).
    rendered = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", rendered)
    return f"\n\n«EQN:{label}»{rendered}«/EQN»\n\n"


# ── MATH_DUAL (dual-line content-shape sub-classification) ────────────


_DUAL_LINE_NAME_PREFIX_RE = re.compile(
    r"^\s*dual\s+line\s*\|", re.IGNORECASE)

# Italic-variable span: `«I»…«/I»` produced by `prepare_wikitext`'s
# quote-run conversion from `''x''`.  An italic single-word or short run
# inside a dual_line is a strong math signal because plain prose in a
# dual_line is exceedingly rare; if there's italic markup, it's almost
# always a math variable (ALGEBRAIC FORMS / COMBINATORIAL ANALYSIS /
# MECHANICS / MENSURATION / CIRCLE / HYDRAULICS).
_ITALIC_SPAN_RE = re.compile(r"«I»[^«]+«/I»")
# HTML sub/sup wrappers — math exponents/subscripts on variables
# (already ruled out as chem because chem case caught its formula
# operands first).
_SUB_SUP_RE = re.compile(r"<su[pb]\b", re.IGNORECASE)


def is_math_dual_line(inner_text: str) -> bool:
    """True iff the dual_line's content has math signature.

    Caller MUST have ruled out chem first (`is_chem_dual_line`) — the
    discriminator between chem and math is whether sub/sup ride on
    element symbols (chem) or on italic variables (math), and chem
    formulae will themselves contain `<sub>`/`{{sub|N}}` and italic-
    free `=` operators, which would otherwise trip this predicate.
    """
    m = _DUAL_LINE_NAME_PREFIX_RE.match(inner_text)
    body = inner_text[m.end():] if m else inner_text
    if _ITALIC_SPAN_RE.search(body):
        return True
    if _SUB_SUP_RE.search(body):
        return True
    return False


def _process_math_dual_line(inner: str, text_transform) -> str:
    """Render a MATH_DUAL element.

    Initially byte-identical with `_process_dual_line` (the shared
    layout producer).  Lives here so future math-specific rendering
    (variable typography, equation alignment, KaTeX) has its natural
    home — the math family owns its math-shaped dual_lines even when
    the rendering hasn't specialized yet.
    """
    return _process_dual_line(inner, text_transform)
