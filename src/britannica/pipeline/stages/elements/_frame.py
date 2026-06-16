"""Layout-frame / reference-list / missing-asset producers.

Three small families the generic `{{…}}` walker now delivers as DOUBLE_BRACE
elements (each was previously leaked raw or shredded):

  * FRAME  — multi-column / indent layout frames (`{{multicol}}`, `{{div col}}`,
    `{{outdent|…}}`, `{{hanging indent|…}}`, `{{familytree|…}}`, …).  The frame is
    pure presentation scaffolding with no faithful render in our medium, so we
    DROP the frame and keep its content: the LAST positional arg is the content
    (recursed through the dispatch); a content-less control marker
    (`{{multicol-break}}`, `{{col-begin}}`) renders to nothing.
  * REFS   — footnote-list emitters (`{{smallrefs}}`, `{{reflist}}`, `{{ref}}`,
    `{{blockref}}`).  Footnotes render inline in this edition, so the list
    emitter renders to EMPTY.
  * MISSING — missing-asset placeholders (`{{missing table}}`, `{{missing image}}`,
    `{{missing math}}`, `{{formula missing}}`).  A VISIBLE `[missing …]` stub so
    the reader knows an asset is absent at this point.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes


def process_frame(raw: str, context) -> str:
    """FRAME producer: drop the layout frame, keep + recurse its content.

    The content is the last POSITIONAL arg (named ``key=value`` args are frame
    parameters — `border=0`, `colwidth=18em`, `align=center` — and are dropped).
    A frame with no positional content (a bare control marker) renders to nothing.
    """
    from britannica.pipeline.stages.elements import process_elements
    # Peel `{{name` … `}}`; split the arg string on top-level pipes.
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    bar = inner.find("|")
    if bar < 0:                                   # bare control marker → nothing
        return ""
    parts = _split_top_pipes(inner[bar:])
    positional = [
        p for p in parts
        if p != "" and not re.match(r"^\s*[A-Za-z_][\w\- ]*\s*=", p)
    ]
    if not positional:
        return ""
    # The faithful content is the longest positional slot (familytree label
    # cells are short glyph tokens like `ALD`; the prose content — when present —
    # is the substantial one).  For the common single-content frames
    # (`{{outdent|text}}`, `{{hanging indent|text}}`) that is just the one slot.
    content = max(positional, key=len)
    return process_elements(content, context, _allow_figure=False).strip()


def process_main_other(raw: str, context) -> str:
    """`{{main other|main-NS|other-NS}}` — a namespace switch.  We assemble the
    MAIN-namespace article, so keep param 1 (recursed) and drop the rest.  It
    places a page-straddling clause on exactly one side of the break without
    duplicating it — the "Needlepoint lace…" split (vol 16) is the lone case."""
    from britannica.pipeline.stages.elements import process_elements
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    parts = _split_top_pipes(inner)
    p1 = parts[1] if len(parts) > 1 else ""
    return (process_elements(p1, context, _allow_figure=False).strip()
            if p1.strip() else "")


def process_refs(raw: str, context) -> str:
    """REFS producer: a footnote-list emitter renders to EMPTY (footnotes render
    inline in this edition)."""
    return ""


_MISSING_LABEL = {
    "missing table": "missing table",
    "missing image": "missing image",
    "missing math": "missing math",
    "formula missing": "missing formula",
}


def process_missing(raw: str, context) -> str:
    """MISSING producer: a visible `[missing …]` stub for an absent asset."""
    m = re.match(r"\{\{\s*([^|{}]+?)\s*[|}]", raw)
    name = (m.group(1).lower() if m else "asset").strip()
    label = _MISSING_LABEL.get(name, name)
    return f"[{label}]"
