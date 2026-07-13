"""Content-extractor producers — templates whose VISIBLE content is one of their args.

Folded into the peel/recurse/wrap mechanism: the DISPLAY is the one recursed slot
(`_content_parse` picks it per name — the PEEL side), and `_wrap_content_extract` (the WRAP
side, a `_PR_WRAP` row) turns the substituted body into its marker.  No bespoke producer.

For `tooltip`/`abbr` the OTHER arg is a hover hint (a pronunciation / an abbreviation
expansion).  We CARRY it as the same `«SPAN[title:…]»` tooltip marker that
`_handle_title_spans` emits for the HTML `<span title="…">` form — ONE marker, one viewer
decoder, two source shapes (the EB1911 transliteration tooltip mechanism).

For `lang`/`sic`/`dropinitial`/`fqm` the metadata is genuinely droppable: unwrap to the
display arg.  A nested styler in the display rides through as a classified child node that
`produce_tree` substitutes, so it survives.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._link import _split_top_pipes


def _content_parse(raw: str) -> "tuple[str, str, str]":
    """`(name, display_slot, tip)` — a content-extractor's name, its one recursed DISPLAY slot,
    and the tooltip hint carried raw as a `title=` attr.  WHICH arg is the display is per-name
    (tooltip's is arg-1, lang's is arg-2, …); parsed ONCE here, called by both the peel
    (`_recurse_slot_content`) and the wrap.  A bare `{{fqm}}` defaults its display to a curly
    opening quote (recursed inertly to the same char)."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw.strip()))
    parts = [p.strip() for p in _split_top_pipes(inner)]
    name = parts[0].lower().replace(" ", "")
    args = parts[1:]
    if name in ("tooltip", "abbr"):        # display | hover-hint
        return name, (args[0] if args else ""), (args[1] if len(args) > 1 else "")
    if name in ("lang", "wdl"):            # code | text (the code / Qid is metadata)
        return name, (args[1] if len(args) > 1 else (args[0] if args else "")), ""
    if name == "fqm":                      # floating quote mark; bare → a curly opening quote
        return name, (args[0] if args else "“"), ""
    # sic / dropinitial / di / vrl / phn / definition / nsl / suspect / nodent → the content
    # (drop-cap: the letter; a size arg like `4em` in `{{di|{{serif|J}}|4em}}` is metadata).
    return name, (args[0] if args else ""), ""


def _wrap_content_extract(raw, body, ctx):
    """CONTENT_EXTRACT wrap (a `_PR_WRAP` row): the DISPLAY is the recursed `body`.  A
    tooltip/abbr hint rides as a `«SPAN[title:…]»`; everything else unwraps to the display."""
    name, _display, tip = _content_parse(raw)
    body = body.strip()
    if name in ("tooltip", "abbr"):
        return f"«SPAN[title:{tip}]»{body}«/SPAN»" if tip else body
    return body
