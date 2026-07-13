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


def _main_other_content(raw: str) -> str:
    """Peel `{{main other|main-NS|other-NS}}` → its main-namespace copy (param 1).  Shared so
    `_classify_main_other_composite` recurses the SAME copy the producer wraps."""
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    parts = _split_top_pipes(inner)
    return parts[1] if len(parts) > 1 else ""


# `{{main other|main|other}}` folded into the peel/recurse/wrap mechanism: `_main_other_content`
# is its peel (`_recurse_slot_content`), `_wrap_bare` its wrap.  No bespoke producer.


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
