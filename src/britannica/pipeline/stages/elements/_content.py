"""Content-extractor producers — templates whose VISIBLE content is one of their args.

For `tooltip`/`abbr` the OTHER arg is a hover hint (a pronunciation / an abbreviation
expansion).  We CARRY it as the same `«SPAN[title:…]»` tooltip marker that
`_handle_title_spans` emits for the HTML `<span title="…">` form — ONE marker, one viewer
decoder, two source shapes (the EB1911 transliteration tooltip mechanism).

For `lang`/`sic`/`dropinitial`/`fqm` the metadata is genuinely droppable: unwrap to the
display arg.  A nested styler in the display rides through as a walker-extracted
placeholder that `produce_tree` substitutes, so it survives.
"""

from __future__ import annotations

from britannica.pipeline.stages.elements._link import _split_top_pipes


def process_content_extract(inner: str) -> str:
    parts = [p.strip() for p in _split_top_pipes(inner)]
    name = parts[0].lower().replace(" ", "")
    args = parts[1:]
    if name in ("tooltip", "abbr"):  # display | hover-hint → carry the hint as a tooltip
        display = args[0] if args else ""
        tip = args[1] if len(args) > 1 else ""
        return f"«SPAN[title:{tip}]»{display}«/SPAN»" if tip else display
    if name == "lang":  # lang|code|text → the text (the language code is metadata)
        return args[1] if len(args) > 1 else (args[0] if args else "")
    if name == "fqm":  # floating quote mark — bare defaults to a curly opening quote
        return args[0] if args else "“"
    # sic / dropinitial → just the content.
    return args[0] if args else ""
