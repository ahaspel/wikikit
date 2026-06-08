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
SHAPE_INLINE_IMAGE      = "INLINE_IMAGE"      # [[File:...]] in inline-prose context
SHAPE_DOUBLE_BRACE      = "DOUBLE_BRACE"      # {{...}}
SHAPE_OUTLINE           = "OUTLINE"           # indented-list ladder (text-shaped)
SHAPE_ORDERED_LIST      = "ORDERED_LIST"      # nested {{ordered list|…}} classification
SHAPE_CHART2            = "CHART2"            # {{chart2/start}}…{{chart2/end}} region
SHAPE_FIGURE            = "FIGURE"            # image + structural caption run
SHAPE_SECTION           = "SECTION"           # <section begin="X"/> / <section end/>
SHAPE_BODY              = "BODY"               # article-level prose run between other elements
SHAPE_MIRROR_GLYPH      = "MIRROR_GLYPH"       # <span style="{{mirrorH}}…">content</span>
SHAPE_CENTER            = "CENTER"             # {{NAME/s}}…{{NAME/e}} paired-wrapper span
SHAPE_STYLED            = "STYLED"             # <div>/<p>/<span> carrying {{Ts}}/style=/align=
SHAPE_PAGE              = "PAGE"               # page-break bookkeeping marker (\x01PAGE:N\x01)
SHAPE_TITLE             = "TITLE"              # «TITLE»…«/TITLE» stamp (preprocess_article)


SHAPES: frozenset[str] = frozenset({
    SHAPE_BRACE_PIPE,
    SHAPE_HTML_TAG,
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_DOUBLE_BRACKET,
    SHAPE_INLINE_IMAGE,
    SHAPE_DOUBLE_BRACE,
    SHAPE_OUTLINE,
    SHAPE_ORDERED_LIST,
    SHAPE_CHART2,
    SHAPE_FIGURE,
    SHAPE_SECTION,
    SHAPE_BODY,
    SHAPE_MIRROR_GLYPH,
    SHAPE_CENTER,
    SHAPE_STYLED,
    SHAPE_PAGE,
    SHAPE_TITLE,
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
    # ORDERED_LIST — a nested `{{ordered list|…}}` classification.  The producer
    # owns the recursion (parses the whole nested template into ONE depth-encoded
    # OUTLINE marker); the classifier must NOT extract nested same-shape children,
    # else each level would become a separate marker.
    SHAPE_ORDERED_LIST,
    # FIGURE — the producer owns the whole image+caption span: it re-processes
    # a copy with figure-recognition off and assembles, so the main walk must
    # NOT recurse into the raw here.
    SHAPE_FIGURE,
    # STYLED — a `<div>`/`<p>`/`<span>` carrying style (`{{Ts}}`/`style=`/
    # `align=`).  The producer owns the whole span: it peels the wrapper, derives
    # the CSS, and re-processes the INNER through the main dispatch
    # (`process_elements(..., _allow_figure=False)`) so a table / MATH / CHEM /
    # nested-wrapper inside is handled by its own producer, not leaked.  The main
    # walk must NOT recurse into the raw here (the producer does it itself).
    SHAPE_STYLED,
    # SECTION — a `<section begin/end/>` transclusion marker; no inner content,
    # the producer reads the raw tag (its name is boundary metadata).
    SHAPE_SECTION,
    # BODY — article-level prose run between other elements.  The body
    # producer owns it end-to-end (markup conversion + body finishing);
    # the walker does NOT recurse into it (any extractable shape would
    # already have been pulled out as its own element before the BODY
    # wrapper ran).
    SHAPE_BODY,
    # PAGE — the `\x01PAGE:N\x01` page-break marker; a leaf, the producer
    # re-emits the raw.  Folding it in retires the page-marker strip the
    # outline recognizer used to need.
    SHAPE_PAGE,
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
    if shape == SHAPE_PAGE:
        # Leaf — the whole `\x01PAGE:N\x01` token is the marker; the
        # producer re-emits `raw`.
        return ""
    if shape == SHAPE_DOUBLE_BRACKET or shape == SHAPE_INLINE_IMAGE:
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
    if shape == SHAPE_ORDERED_LIST:
        # Leaf — the producer reads `raw` and recursively parses the nested
        # `{{ordered list|…}}` itself; no inner content to hand back.
        return ""
    if shape == SHAPE_CHART2:
        # CHART2's bytes are chart-grammar templates; we never walk
        # inside.  Return empty so a downstream `walker.walk("")`
        # trivially yields no extracts.
        return ""
    if shape == SHAPE_FIGURE:
        # Leaf — the producer reads `raw` (re-processes + assembles it); the
        # main walk needs no inner content here.
        return ""
    if shape == SHAPE_STYLED:
        # Leaf — the producer reads `raw`, peels the `<div>`/`<p>`/`<span>`
        # wrapper itself (it needs the opener attrs for the CSS) and recurses
        # the inner through the main dispatch.  No inner content to hand back.
        return ""
    if shape == SHAPE_BODY:
        # No delimiters — the raw bytes ARE the body prose; the producer
        # transforms them end-to-end.
        return raw
    if shape == SHAPE_MIRROR_GLYPH:
        # Strip the wrapping `<span style="…{{mirrorH}}…">…</span>`; the
        # mirror semantic is now carried by the shape's label.  Inner is
        # the glyph(s) to mirror, possibly with content-template markup
        # (`{{larger|𐌔}}`) carried through as a walker-extracted child.
        s = re.sub(r"^<span\s+style\s*=\s*\"[^\"]*\"\s*>", "", raw,
                   flags=re.IGNORECASE)
        s = re.sub(r"</span>\s*$", "", s, flags=re.IGNORECASE)
        return s
    if shape == SHAPE_CENTER:
        # Peel the paired `{{NAME/s}}` opener and `{{NAME/e}}` closer.
        # Name-agnostic (the label-deriver reads the name for the family);
        # `[^{}]*?` spans a multi-word name (`EB1911 fine print`) but not
        # braces, and the closer is anchored at end so a NESTED same-name
        # pair inside survives into the inner for the recursive walk.
        s = re.sub(r"^\{\{\s*[^{}]*?/s\s*\}\}", "", raw, flags=re.IGNORECASE)
        s = re.sub(r"\{\{\s*[^{}]*?/e\s*\}\}\s*$", "", s, flags=re.IGNORECASE)
        return s
    if shape == SHAPE_TITLE:
        # Peel the «TITLE»…«/TITLE» stamp; the inner is the carved title span
        # (markers + raw <ref>/{{sc}}), handed back to the walker so the title node
        # is produced recursively into the same string as today's `title_display`.
        s = re.sub(r"^«TITLE»", "", raw)
        s = re.sub(r"«/TITLE»\s*$", "", s)
        return s
    raise ValueError(f"Unknown shape: {shape!r}")
