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
SHAPE_BODY              = "BODY"               # article-level prose run between other elements
SHAPE_PAIRED_WRAPPER    = "PAIRED_WRAPPER"     # {{NAME/s}}…{{NAME/e}} paired open/close span
                                               # (the former CENTER + CHART2 — one structure;
                                               #  the classifier routes by name to CENTER / CHART2)
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
    SHAPE_BODY,
    SHAPE_PAIRED_WRAPPER,
    SHAPE_PAGE,
    SHAPE_TITLE,
})


# Shapes whose inner content is not walked — the producer owns the
# entire payload between (or under) the markers and does whatever
# internal parsing it needs.
#
# * HTML_SELF_CLOSING — no inner content.
# * PAIRED_WRAPPER — the `{{NAME/s}}…{{NAME/e}}` paired open/close span
#   (former CENTER + CHART2).  A LEAF: the producer owns the whole span.
#   The CHART2 family is a non-wikitext template-pair region (chart-grammar
#   `{{…}}` tokens inside aren't extractable wikitext, so its producer reads
#   raw); the CENTER family's producer recurses its OWN inner through the main
#   dispatch (figure-recognition off) exactly as the classifier used to.
# * OUTLINE — line-pattern (indented `;head:desc` ladder); the producer
#   walks it line-by-line itself.  Balanced shapes inside an outline
#   body have already been placeholdered by the linear scanner before
#   the OUTLINE phase runs, so there's nothing for the classifier to
#   recurse into anyway.
LEAF_SHAPES: frozenset[str] = frozenset({
    SHAPE_HTML_SELF_CLOSING,
    SHAPE_PAIRED_WRAPPER,
    SHAPE_OUTLINE,
    # The six STYLED-derived structures (STRIP / PARAM / SHOULDER / RUNNING_HEADER
    # = `{{…}}` template-form stylers/headings; SPAN_TITLE / HTML_STYLE = `<tag>`
    # styled wrappers) no longer have their own shapes: they ride the generic
    # SHAPE_DOUBLE_BRACE / SHAPE_HTML_TAG shapes, with the type carve done by the
    # classifier's two label-derivers.  Their leaf-ness is inherited from those
    # generic shapes (both already leaves below); the producers (`process_strip` /
    # `process_param` / `process_shoulder` / `process_running_header` /
    # `process_span_title` / `process_html_style`) are unchanged — they read `raw`,
    # peel their own wrapper, and recurse the inner through the main dispatch.
    # BODY — a prose run between other elements, at any depth.  The body
    # producer owns it end-to-end; it is a leaf (no inner — that's what makes
    # it body text), so the walker never recurses into it.
    SHAPE_BODY,
    # BRACE_PIPE — a `{|…|}` table.  Its inner is a GRID (`|-`, `|` row/cell
    # delimiters) — the table's own structure, NOT body text.  The producer
    # (`_process_table_unified`) decomposes the grid from raw and `cell_recurse`s
    # each cell's content through `process_elements` (so cell prose → BODY →
    # «P»).  A leaf here so the generic walk doesn't grab the grid as body text
    # (table→row→cell→body lives in the producer, not the classifier).
    SHAPE_BRACE_PIPE,
    # DOUBLE_BRACE — a `{{name|arg|arg}}` template (link, fraction, image, footer,
    # coordinate, …).  Its inner is PIPE-SEPARATED ARGS — the template's own
    # structure, NOT body text.  The producer chews the whole arg string (splits
    # the pipes, pulls target/display/params) and stops; a content slot that needs
    # markup recursion is the producer's own `process_elements` call.  A leaf so
    # the generic walk doesn't wrap the args into one BODY (which left link
    # producers a placeholder with no pipes → empty link).  The label derives from
    # `raw`, so no child registry is needed.
    SHAPE_DOUBLE_BRACE,
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
    if shape == SHAPE_BODY:
        # No delimiters — the raw bytes ARE the body prose; the producer
        # transforms them end-to-end.
        return raw
    if shape == SHAPE_PAIRED_WRAPPER:
        # The `{{NAME/s}}…{{NAME/e}}` paired open/close span (former CENTER +
        # CHART2 — one structure, two families distinguished by name).
        #
        #   * CHART2 family (`{{chart2/start}}…{{chart2/end}}`): the bytes are
        #     chart-grammar templates we never walk inside.  Return empty (the
        #     old CHART2 `strip_outer`), so a downstream `walker.walk("")`
        #     yields no extracts and the producer reads `raw`.
        #   * CENTER family (every other paired wrapper): peel the paired
        #     `{{NAME/s}}` opener and `{{NAME/e}}` closer (the old CENTER
        #     `strip_outer`).  Name-agnostic; `[^{}]*?` spans a multi-word name
        #     (`EB1911 fine print`) but not braces, and the closer is anchored
        #     at end so a NESTED same-name pair inside survives into the inner
        #     for the producer's own recursive walk.
        if re.match(r"\{\{\s*chart2\s*/\s*start", raw, flags=re.IGNORECASE):
            return ""
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
