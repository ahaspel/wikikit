"""Producer for the DUAL_LINE element.

`{{dual line|A|B}}` is a pure layout primitive — stacks A and B as
two lines (emitted as `A«BR»B`).  Content-agnostic: A and B can carry
any inline content (chem `C{{sub|6}}H{{sub|5}}`, layout `{{gap}}`,
refs `<ref>…</ref>`, math vars).  Used by math (60% of 611 corpus
instances — ALGEBRAIC FORMS, COMBINATORIAL ANALYSIS, HYDRAULICS,
MECHANICS, MENSURATION, CIRCLE), chemistry (9% — COUMARONES, ALCOHOLS,
INDIGO, PYRIDINE, AMINES, ISOMERISM, OXIMES), and pure layout (~22% —
table headers in POST / RUSSIA / IRON AND STEEL, figure-caption splits
in PHOTOGRAPHY, hyphenation wraps `Janu-|ary.`).

Walker recognizes the bracket pair (`_DUAL_LINE_RE` in `_walker.py`),
classifier labels it DUAL_LINE (structure-only).  ONE producer recurses
both lines, so chem/math content inside is produced by its own producer —
there is no chem/math-specific dual_line label (the old CHEM_DUAL /
MATH_DUAL split was speculative specificity, since removed).

Variants:
  * `{{dual line|A|B}}` — bare two-arg.
  * `{{dual line|style=…|A|B}}` / `{{dual line|A|B|style=…}}` — a `style=…`
    decoration param (leading in POST table headers, trailing in ALGEBRAIC
    FORMS); it is EB1911 cell presentation, CARRIED onto a wrapping
    `«SPAN[style:…]»` — never dropped, never leaked as text.
"""
from __future__ import annotations

import re


_DUAL_LINE_NAME_RE = re.compile(
    r"^\s*dual\s+line\s*\|", re.IGNORECASE)

# A cell-decoration param slot (`style=…`, `align=…`, …) — NOT one of the two
# content lines.  It may sit LEADING (POST headers) or TRAILING (ALGEBRAIC
# FORMS' `{{dual line|A|B|style=…}}`); either way it is dropped, never rendered.
_DECORATION_RE = re.compile(
    r"^\s*(?:style|align|valign|class|width|height)\s*=", re.IGNORECASE)


_MATH_OPEN_RE = re.compile(r"<math\b", re.IGNORECASE)
_MATH_CLOSE = "</math>"


def _split_top_level_pipe(s: str) -> list[str]:
    """Split ``s`` on `|`, but only at depth-0 brace nesting — pipes INSIDE a
    nested ``{{…|…}}`` aren't separators.  A ``<math>…</math>`` block is opaque
    too: a LaTeX pipe (``\\left|…\\right|``, an absolute value, a matrix column)
    is content, never an arg boundary.  Pure utility; no caller-specific
    knowledge."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        if depth == 0 and s[i] == "<" and _MATH_OPEN_RE.match(s, i):
            end = s.lower().find(_MATH_CLOSE, i)
            if end != -1:
                end += len(_MATH_CLOSE)
                buf.append(s[i:end])     # the whole <math>…</math> rides through
                i = end
                continue
        if i + 1 < n and s[i] == "{" and s[i + 1] == "{":
            depth += 1
            buf.append("{{")
            i += 2
        elif i + 1 < n and s[i] == "}" and s[i + 1] == "}":
            depth = max(0, depth - 1)
            buf.append("}}")
            i += 2
        elif s[i] == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(s[i])
            i += 1
    parts.append("".join(buf))
    return parts


def _dual_line_cells(raw: str) -> list[str]:
    """Chop ``{{dual line|A|B}}`` into its cell slots — the strings the old producer
    handed to ``recurse``.  Two-arg form → ``[A, B]`` (``<br>``-stacked by the producer);
    a ``style=…`` decoration (leading OR trailing) is separated out here and CARRIED by
    the producer onto a wrapping span (not a content line); a degenerate <2-arg form → its lone slot
    (space-joined + outer-stripped by the producer).  Each slot is ``.strip()``-ed HERE —
    while its entities are still ENCODED, so the strip can't eat a decoded U+2007 figure
    space (ORDNANCE's `{{dual line|1·75|1·6&numsp;}}`, whose trailing figure space is
    display content); ``classify_article`` then decodes it like the rest of the pipeline.

    A dual_line is a decompose node (one row of two stacked cells), not a leaf: each cell
    is article markup carrying inline stylers / sub-sup / refs / math vars ({{Polytonic}},
    {{sub}}, `<ref>`, …).  The classifier recurses each slot to a CELL node; the producer
    reassembles the stack from the two cell markers."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    m = _DUAL_LINE_NAME_RE.match(inner)
    body = inner[m.end():] if m else inner
    parts = [p for p in _split_top_level_pipe(body) if not _DECORATION_RE.match(p)]
    if len(parts) < 2:
        return [p.strip() for p in parts]
    return [parts[0].strip(), "|".join(parts[1:]).strip()]


def _dual_line_decoration(raw: str) -> str:
    """The dual_line's ``style=…`` param as a CSS string to CARRY onto the stack —
    EB1911 cell presentation (smaller type, alignment), separated from the two
    content lines so it neither leaks as text nor is silently dropped."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    m = _DUAL_LINE_NAME_RE.match(inner)
    body = inner[m.end():] if m else inner
    for p in _split_top_level_pipe(body):
        sm = re.match(r"^\s*style\s*=\s*(.*)$", p, re.IGNORECASE | re.DOTALL)
        if sm:
            return sm.group(1).strip()
    return ""


def _process_dual_line(raw, inner, context, inner_registry) -> str:
    """Render a DUAL_LINE element as ``A<br>B`` from its two decomposed CELL markers,
    wrapped in the carried ``style=…`` decoration if the source gave one.

    ``_classify_dual_line_composite`` chopped the row into cell slots
    (``_dual_line_cells``) and recursed each to a node; here we read the cell markers
    (each already its recursed content, in order) and stack them.  Two cells → the
    ``A<br>B`` stack; a degenerate single cell → its content, space-joined + stripped.
    A ``style=`` param rides onto a wrapping span (carried, not dropped, not leaked).
    No re-``process_elements`` — the cells ARE the recursion."""
    from britannica.pipeline.stages.elements import _cell_markers
    cells = _cell_markers(inner_registry)
    stack = " ".join(cells).strip() if len(cells) < 2 else f"{cells[0]}«BR»{cells[1]}"
    css = _dual_line_decoration(raw)
    return f"«SPAN[style:{css}]»{stack}«/SPAN»" if css else stack
