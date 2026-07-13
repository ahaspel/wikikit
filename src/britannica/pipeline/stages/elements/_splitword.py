"""Split-word producer — one home for a word broken across a page break.

EB1911 marks a word split at a page boundary with a START marker that owns the
whole rejoined word and an END marker that renders nothing (the word already
appeared once, whole, at the start).  Two template families express the SAME
thing in two encodings:

  * ``{{hws|frag|WORD}}`` / ``{{hwe|frag|WORD}}`` — the whole word is a
    POSITIONAL slot (``WORD``).  ``hws`` emits it; ``hwe`` emits nothing.
  * ``{{lps|hws=A|hyph=|hwe=B}}`` / ``{{lpe|…}}`` — the word is two NAMED halves
    (``hws=``/``hwe=``).  ``lps`` emits ``A+B``; ``lpe`` emits nothing.

Reconstruct by recognition, never a preprocess sweep: the START marker is the
sole owner of the rejoined word; the END marker renders empty.  One producer
reads the word from whichever encoding the marker uses.
"""
from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes

# END markers render nothing — the whole word already appeared at the START.
_END_NAMES = frozenset({"hwe", "hyphenated word end", "lpe"})
_NAMED_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*=(.*)$", re.DOTALL)


def _marker_name(raw: str) -> str:
    m = re.match(r"\{\{\s*([^|{}]+?)\s*[|}]", raw)
    return m.group(1).lower() if m else ""


def _split_word_word(raw: str) -> str:
    """The rejoined whole word a START split-word marker owns — the longest positional slot
    (hws form) or the `hws=`+`hwe=` halves (lps form).  Shared so `_classify_recurse_slot`
    recurses the SAME word the producer emits."""
    inner = re.sub(r"^\{\{", "", raw)
    inner = re.sub(r"\}\}\s*$", "", inner)
    positional: list[str] = []
    named: dict[str, str] = {}
    for arg in _split_top_pipes(inner)[1:]:        # parts[0] is the template name
        m = _NAMED_RE.match(arg)
        if m:
            named[m.group(1).lower()] = m.group(2)
        elif arg != "":
            positional.append(arg)
    if positional:                                  # hws form — word is a slot
        return max(positional, key=len)
    return named.get("hws", "") + named.get("hwe", "")  # lps form — join the halves


# `{{hws|frag|WORD}}` / `{{lps|…}}` folded into the peel/recurse/wrap mechanism: the peel
# (`_recurse_slot_content`, END → empty) uses `_split_word_word` + `_marker_name`/`_END_NAMES`,
# the wrap is `_wrap_bare`.  No bespoke producer.
