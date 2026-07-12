"""Reference-list / missing-asset / namespace-switch producers.

Small families the generic `{{…}}` walker delivers as DOUBLE_BRACE elements:

  * REFS   — footnote-list emitters (`{{smallrefs}}`, `{{reflist}}`, `{{ref}}`,
    `{{blockref}}`).  Footnotes render inline in this edition, so the list
    emitter renders to EMPTY.
  * MISSING — missing-asset placeholders (`{{missing table}}`, `{{missing image}}`,
    `{{missing math}}`, `{{formula missing}}`).  A VISIBLE `[missing …]` stub so
    the reader knows an asset is absent at this point.
  * MAIN_OTHER — `{{main other|main-NS|other-NS}}` namespace switch (keep param 1).

(The old FRAME layout-frame producer is DISSOLVED — its shapes route to TABLE /
PARAM / HANGING_INDENT / CHART2 / CONTENT_EXTRACT instead of a catch-all.)
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes


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
