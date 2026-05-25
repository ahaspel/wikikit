"""Shape constants — the walker/classifier interface.

A *shape* is the structural form the walker recognises in raw
wikitext.  There are six shapes; nothing else is balanced by the
walker.  Each shape names a delimiter family, nothing more — the
walker emits ``(shape, raw_bytes)`` and the classifier turns that
into a label (``MATH``, ``IMAGE``, ``LAYOUT_WRAPPER``, ``LEGEND``,
…) by inspecting the bytes' opening identifier and, for composite
shapes, the labels of the recursively-classified children.

Shape is the walker's vocabulary; label is the classifier's.  The
two never overlap.

``strip_outer(shape, raw)`` peels the shape's delimiters and returns
the inner content with no further walking applied.  The classifier
hands that inner content back to the walker to find the next-level
shapes — that mutual recursion is how the classified tree is built.
"""

from __future__ import annotations

import re


SHAPE_BRACE_PIPE        = "BRACE_PIPE"        # {|...|}
SHAPE_HTML_TAG          = "HTML_TAG"          # <NAME ...>...</NAME>
SHAPE_HTML_SELF_CLOSING = "HTML_SELF_CLOSING" # <NAME ... />
SHAPE_DOUBLE_BRACKET    = "DOUBLE_BRACKET"    # [[...]]
SHAPE_DOUBLE_BRACE      = "DOUBLE_BRACE"      # {{...}}
SHAPE_OUTLINE           = "OUTLINE"           # indented-list ladder (text-shaped)
SHAPE_CHART2            = "CHART2"            # {{chart2/start}}…{{chart2/end}} region
SHAPE_FIGURE            = "FIGURE"            # image + structural caption run
SHAPE_SECTION           = "SECTION"           # <section begin="X"/> / <section end/>
SHAPE_NOINCLUDE         = "NOINCLUDE"          # <noinclude>…</noinclude> page container


SHAPES: frozenset[str] = frozenset({
    SHAPE_BRACE_PIPE,
    SHAPE_HTML_TAG,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_DOUBLE_BRACE,
    SHAPE_OUTLINE,
    SHAPE_CHART2,
    SHAPE_FIGURE,
    SHAPE_SECTION,
    SHAPE_NOINCLUDE,
})


# Shapes whose inner content is not walked — the producer owns the
# entire payload between (or under) the markers and does whatever
# internal parsing it needs.
#
# * HTML_SELF_CLOSING — no inner content.
# * CHART2 — non-wikitext template-pair region; chart-grammar `{{…}}`
#   tokens inside aren't extractable wikitext.
# * OUTLINE — line-pattern (indented `;head:desc` ladder); the producer
#   walks it line-by-line itself.  Balanced shapes inside an outline
#   body have already been placeholdered by the linear scanner before
#   the OUTLINE phase runs, so there's nothing for the classifier to
#   recurse into anyway.
LEAF_SHAPES: frozenset[str] = frozenset({
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_CHART2,
    SHAPE_OUTLINE,
    # FIGURE — the producer owns the whole image+caption span: it re-processes
    # a copy with figure-recognition off and assembles, so the main walk must
    # NOT recurse into the raw here.
    SHAPE_FIGURE,
    # SECTION — a `<section begin/end/>` transclusion marker; no inner content,
    # the producer reads the raw tag (its name is boundary metadata).
    SHAPE_SECTION,
    # NOINCLUDE — a `<noinclude>…</noinclude>` page container.  Heterogeneous
    # (page-chrome vs. a cross-page `{|` table marker), so disposition is a
    # CONTENT decision the producer makes from `raw` — not walked here.
    SHAPE_NOINCLUDE,
})


def strip_outer(shape: str, raw: str) -> str:
    """Peel `shape`'s delimiters off `raw`, return the inner content.

    No further walking is applied — the returned string may itself
    contain balanced shapes which the walker will find when the
    classifier hands the inner back for next-level extraction.

    Per-label specifics (e.g. IMAGE's optional ``\\n\\nEXTCAP:`` tail
    after ``]]``) do NOT live here — strip_outer is shape-uniform.
    The label-deriver for IMAGE handles that during classification.
    """
    if shape == SHAPE_BRACE_PIPE:
        s = re.sub(r"^\{\|[^\n]*\n?", "", raw)
        s = re.sub(r"\n?\|\}\s*$", "", s)
        return s
    if shape == SHAPE_HTML_TAG:
        s = re.sub(r"^<[A-Za-z][A-Za-z0-9]*\b[^>]*>", "", raw,
                   flags=re.IGNORECASE)
        s = re.sub(r"</[A-Za-z][A-Za-z0-9]*>\s*$", "", s,
                   flags=re.IGNORECASE)
        # Today's legacy strip for `<poem>` / `<ref>` / `<math>` calls
        # `.strip()` after removing the tags, so producers see no
        # leading/trailing whitespace inside.  Preserve that contract
        # at the shape level — wikitext tag content carries no
        # significant whitespace at the boundary.
        return s.strip()
    if shape == SHAPE_HTML_SELF_CLOSING:
        return ""
    if shape == SHAPE_SECTION:
        # No inner content — the whole tag is the marker; the producer
        # reads `raw`.
        return ""
    if shape == SHAPE_NOINCLUDE:
        # Not walked — the producer reads `raw` and makes the chrome-vs-
        # content (keep `{|`/`|}`) decision itself.
        return ""
    if shape == SHAPE_DOUBLE_BRACKET:
        s = re.sub(r"^\[\[", "", raw)
        s = re.sub(r"\]\]\s*$", "", s)
        return s
    if shape == SHAPE_DOUBLE_BRACE:
        s = re.sub(r"^\{\{", "", raw)
        s = re.sub(r"\}\}\s*$", "", s)
        return s
    if shape == SHAPE_OUTLINE:
        # No delimiters — the raw bytes ARE the indented-line ladder.
        return raw
    if shape == SHAPE_CHART2:
        # CHART2's bytes are chart-grammar templates; we never walk
        # inside.  Return empty so a downstream `walker.walk("")`
        # trivially yields no extracts.
        return ""
    if shape == SHAPE_FIGURE:
        # Leaf — the producer reads `raw` (re-processes + assembles it); the
        # main walk needs no inner content here.
        return ""
    raise ValueError(f"Unknown shape: {shape!r}")
