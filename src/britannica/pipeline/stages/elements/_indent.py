"""Indented paragraphs — `:`-marked lines, carried as the indent the source states.

WHAT THIS REPLACES.  A `:`-block used to be recognized as an OUTLINE: a bounds
scan found the block, a profile scan inferred a hierarchy out of the indent
marks, and the producer emitted `«OUTLINE»«OLI:depth»` which the render turned
into a nested `<ul>` — whose CSS then set `list-style-type: none` and 1.6em of
padding per level.  The taxonomy was INVISIBLE: what reached the page was an
indent, which is all the source ever said
([[feedback_imposed_taxonomy_negative_value]]).  If the raw marks a list we
treat it as a list (`#`, `{{ordered list}}`, `<ol>`); `:` marks an indent.

Everything the inference cost goes with it: the block bounds that cut ALKALOID's
item at its wrap and fractured BIRD's sub-orders at a `{{nop}}`, the
depth-summing rule that mixed colons with `{{em|N}}`, the bare-emphasis "label"
items, the range-header items, and the sparse→dense depth remap.  What is left
states only what the source states.

  * DEPTH is the colon count, carried literally — MediaWiki renders `:::` as
    three nested indents, so do we.  No densifying.
  * A `{{nop}}` may sit in FRONT of the colons where a page broke inside the
    list (PATAGONIA v20 p961: `{{nop}}:2. Patagonian Molasse.`), so the mark is
    read past it.  The token stays in the content, where the SPACER producer
    renders it to nothing — read past, never stripped.
  * A following line carrying no mark of its own CONTINUES the paragraph: a
    newline inside a paragraph is a WRAP, not a new item (ALKALOID's `quinine,
    &c.` belongs to the item above it, and used to leak out as body text between
    two «OUTLINE»s).  A blank line ends the paragraph, and so does a line of
    pure markup furniture — a `{{EB1911 fine print/e}}` closing its wrapper is
    not a continuation of anyone's sentence.

Sibling of `_hanging.py`: the same shape (a source-stated indent + recursed
content) carried the same way (`«DIV[style:padding-left:…]»`).
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, substitute_children)

# Ems per `:` level.  What `ul.outline`'s CSS already rendered, so a well-formed
# block lands where it always did.
INDENT_EM = 1.6

# What `{{left margin/s}}` means when it states no width (the Wikisource default).
BLOCK_INDENT_DEFAULT = "2em"

# The mark, read past a page-break `{{nop}}` and leading blanks.
_INDENT_OPEN_RE = re.compile(r"^(?:\{\{\s*nop\s*\}\}|[ \t])*(:+)")
# A line of nothing but templates — furniture (`{{nop}}`, a wrapper's `/e`).
_FURNITURE_ONLY_RE = re.compile(r"^(?:\{\{[^{}]*\}\}|\s)+$")


def is_indent_line(line: str) -> bool:
    return _INDENT_OPEN_RE.match(line) is not None


def indent_peel(raw: str) -> tuple[int, str]:
    """`(depth, content)` — the colon RUN removed, every other byte kept.

    The `{{nop}}` that may precede the colons is content, not mark: it is left
    where the source put it ([[feedback_when_in_doubt_carry]]).
    """
    m = _INDENT_OPEN_RE.match(raw)
    if not m:
        return 0, raw
    return len(m.group(1)), raw[:m.start(1)] + raw[m.end(1):]


def _logical_line_end(text: str, i: int) -> int:
    """Index of the newline ending the logical line at ``i`` (or ``len(text)``).

    A newline INSIDE a construct doesn't count — `_skip` steps over
    `{{…}}`/`[[…]]`/`<math>`/`{|…|}` whole, so a multi-line math expression on an
    indented line stays ONE line rather than shattering into several.
    """
    from britannica.pipeline.stages.elements._table_fold import _skip
    n = len(text)
    while i < n:
        j = _skip(text, i)
        if j > i:
            i = j
            continue
        if text[i] == "\n":
            return i
        i += 1
    return n


def _paragraph_end(text: str, first_line_end: int) -> int:
    """End index of the indented paragraph whose first line ends at
    ``first_line_end`` — its unmarked continuation lines included."""
    n = len(text)
    end = first_line_end
    pos = end + 1 if end < n else n
    while pos < n:
        le = _logical_line_end(text, pos)
        line = text[pos:le]
        if (not line.strip() or is_indent_line(line)
                or _FURNITURE_ONLY_RE.match(line)):
            break
        end = le
        pos = le + 1 if le < n else n
    return end


def extract_indents(text: str, registry: ElementRegistry,
                    starts_at_line: bool = True) -> str:
    """Replace each `:`-marked paragraph with an INDENT placeholder, copying
    every other byte of ``text`` VERBATIM.

    ``starts_at_line`` says whether offset 0 of ``text`` is a LINE start.  Every
    later candidate is one by construction (the scan advances a whole line at a
    time), so this is the only position in doubt — and it is in doubt because a
    wikitable CELL body begins after a `||`, not after a newline.  MediaWiki
    applies no indent there, and in EB1911's chemistry tables that leading `:` is
    a BOND glyph (PURIN's `N:CH`, ARITHMETIC's vertical-ellipsis cells).

    A `:` on a LATER line of the same cell is still an indent, which is why this
    is a position rule and not "no indents inside tables": FLAGELLATA keeps its
    68-line taxonomy ladder inside a layout table, and switching the scanner off
    for cells erased all of it.
    """
    out: list[str] = []
    n = len(text)
    pos = 0
    while pos < n:
        le = _logical_line_end(text, pos)
        if (pos > 0 or starts_at_line) and is_indent_line(text[pos:le]):
            end = _paragraph_end(text, le)
            out.append(registry.add("INDENT", text[pos:end]))
            pos = end
            continue
        seg_end = le + 1 if le < n else n      # this line and its newline, verbatim
        out.append(text[pos:seg_end])
        pos = seg_end
    return "".join(out)


def indent_block(content: str, width: str, *, hanging: bool = False) -> str:
    """THE indent carry: ``content`` indented by ``width``, with the first line
    outdented by the same width when ``hanging``.

    ONE emitter for every way the source states an indent — a `:`-marked
    paragraph, `{{hi|W|text}}`, `{{left margin/s|W}}` — because they are one
    sentence, "indent this by W", differing only in where W comes from and
    whether the first line hangs.  Three producers were spelling the marker
    themselves, and two of them bypassed `style_block`, which is supposed to be
    the sole style-marker emitter ([[feedback_tune_dont_fork]]).
    """
    if not content:
        return ""
    from britannica.pipeline.stages.elements._tables import style_block
    css = f"padding-left:{width}"
    if hanging:
        css += f"; text-indent:-{width}"
    return style_block(content, css=css)


def process_indent_block(raw, inner, context, inner_registry) -> str:
    """INDENT_BLOCK producer — `{{left margin/s|W}}…{{left margin/e}}`.

    The source indents a whole BLOCK by a width stated on the opening half.
    Same sentence as a `:`-line and a `{{hi|W}}`, so it is the same carry; only
    where W comes from differs.  A COMPOSITE like its CENTER sibling: the
    classifier recursed the inner and the framework substitutes the child
    markers into our output afterward, so we only wrap.
    """
    from britannica.pipeline.stages.elements._tables import _TEMPLATE_STYLE_WRAPPERS
    from britannica.wikitext import parse_paired_half
    half = parse_paired_half(raw)
    spec = (_TEMPLATE_STYLE_WRAPPERS.get(half[0]) if half else None) or {}
    # The width comes off the OPENING half; each wrapper states it its own way.
    # `{{left margin/s|3.2em}}` gives a full CSS length, `{{outdent/s|2}}` a bare
    # NUMBER meaning em — so the spec names the unit rather than every caller
    # remembering which wrapper spells it which way.
    width = (half[2] if half else "") or spec.get("arg_default") or BLOCK_INDENT_DEFAULT
    unit = spec.get("arg_unit", "")
    if unit and not width.rstrip().endswith(unit):
        width = width.strip() + unit
    return indent_block(inner.strip(), width, hanging=bool(spec.get("hanging")))


def process_indent(raw, inner, context, inner_registry) -> str:
    """INDENT producer — one `:`-marked paragraph → the indent it states.

    A COMPOSITE: the classifier decomposed the content into child nodes, so a
    link / math / footnote inside an indented paragraph is a real child; we
    substitute their markers and wrap.  Empty content renders to nothing.
    """
    depth, _content = indent_peel(raw)
    body = substitute_children(inner, inner_registry).strip()
    return indent_block(body, f"{depth * INDENT_EM:g}em")
