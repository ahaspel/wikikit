"""Content-extractor producers — templates whose VISIBLE content is one of their args.

Folded into the peel/recurse/wrap mechanism: the DISPLAY is the one recursed slot
(`_content_parse` picks it per name — the PEEL side), and `_wrap_content_extract` (the WRAP
side, a `_PR_WRAP` row) turns the substituted body into its marker.  No bespoke producer.

For `tooltip`/`abbr` the OTHER arg is a hover hint (a pronunciation / an abbreviation
expansion).  We CARRY it as the same `«SPAN[title:…]»` tooltip marker that
`_handle_title_spans` emits for the HTML `<span title="…">` form — ONE marker, one viewer
decoder, two source shapes (the EB1911 transliteration tooltip mechanism).

`sic` is a tooltip too, and was miscategorised as droppable.  `{{SIC|lire|life}}` shows
the page's own reading and carries the EMENDATION as its hint; throwing the second arg
away discards an editor's correction, which is the same loss as discarding an erratum.

For `lang`/`dropinitial`/`fqm` the metadata genuinely is droppable: unwrap to the
display arg.  A nested styler in the display rides through as a classified child node that
`produce_tree` substitutes, so it survives.
"""

from __future__ import annotations

import re

from britannica.wikitext import split_top_pipes


def _content_parse(raw: str) -> "tuple[str, str, str]":
    """`(name, display_slot, tip)` — a content-extractor's name, its one recursed DISPLAY slot,
    and the tooltip hint carried raw as a `title=` attr.  WHICH arg is the display is per-name
    (tooltip's is arg-1, lang's is arg-2, …); parsed ONCE here, called by both the peel
    (`_recurse_slot_content`) and the wrap.  A bare `{{fqm}}` defaults its display to a curly
    opening quote (recursed inertly to the same char)."""
    inner = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw.strip()))
    parts = [p.strip() for p in split_top_pipes(inner)]
    raw_name = parts[0].strip()          # CASE MATTERS for sic — see below
    name = raw_name.lower().replace(" ", "")
    args = parts[1:]
    if name in ("tooltip", "abbr"):        # display | hover-hint
        return name, (args[0] if args else ""), (args[1] if len(args) > 1 else "")
    if name == "sic":
        # TWO DIFFERENT TEMPLATES, told apart only by case.  MediaWiki template
        # names are case-sensitive after the first letter, and Wikisource has
        # both:
        #
        #   {{SIC|as-printed|correction}}  Template:SIC — expands to
        #       {{tooltip|as-printed|[sic] 'correction'}}.  Shows the page's own
        #       reading; the second arg is the EMENDATION, not metadata.
        #
        #   {{sic}} / {{Sic|anything}}     Template:Sic — "THIS TEMPLATE IS
        #       INTENTIONALLY LEFT BLANK".  A proofreader's annotation on the
        #       PRECEDING word, invisible to readers, whatever arguments it is
        #       given.
        #
        # Lowercasing the name collapsed them, so `{{sic|hide=y}}` and
        # `{{sic|lowercase in original}}` printed their arguments as body text —
        # live on the site today: "maufacturedhide=y by fermenting".
        if raw_name != "SIC":
            return name, "", ""          # the blank template renders nothing
        named = {}
        pos = []
        for a in args:
            k, sep, v = a.partition("=")
            (named.__setitem__(k.strip(), v.strip()) if sep else pos.append(a))
        display = named.get("target") or (pos[0] if pos else "")
        corr = named.get("texttip") or (pos[1] if len(pos) > 1 else "")
        # A hint with nothing to hang on is not worth a span; the bare form
        # annotates text we cannot reach from inside this template.
        tip = ("[sic]" + (f" '{corr}'" if corr else "")) if display else ""
        return name, display, tip
    if name in ("lang", "wdl"):            # code | text (the code / Qid is metadata)
        return name, (args[1] if len(args) > 1 else (args[0] if args else "")), ""
    if name == "fqm":                      # floating quote mark; bare → a curly opening quote
        return name, (args[0] if args else "“"), ""
    # dropinitial / di / vrl / phn / definition / nsl / suspect / nodent → the content
    # (drop-cap: the letter; a size arg like `4em` in `{{di|{{serif|J}}|4em}}` is metadata).
    return name, (args[0] if args else ""), ""


def _wrap_content_extract(raw, body, ctx):
    """CONTENT_EXTRACT wrap (a `_PR_WRAP` row): the DISPLAY is the recursed `body`.  A
    tooltip/abbr hint rides as a `«SPAN[title:…]»`; everything else unwraps to the display."""
    name, _display, tip = _content_parse(raw)
    body = body.strip()
    if name in ("tooltip", "abbr", "sic"):
        # No display text means no span: a tooltip on nothing cannot be hovered.
        return f"«SPAN[title:{tip}]»{body}«/SPAN»" if (tip and body) else body
    return body
