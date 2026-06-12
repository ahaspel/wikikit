"""MediaWiki bold/italic quote-run → internal ``«B»``/``«I»`` marker conversion.

Extracted verbatim from the deleted per-page ``prepare_wikitext`` stage; now a
helper that ``preprocess`` (the single source-prep step) calls on the joined
stream.  Line-scoped (see ``_convert_quote_runs``), so the conversion is
leaf-local — identical result whether run per-page or on the concatenation.
"""
from __future__ import annotations

import re

import mwparserfromhell as _mwp


_QUOTE_RUN_HINT = re.compile(
    r"''|<[bi][>\s/]|\{\{bold\|", re.IGNORECASE)


def _convert_quote_runs(text: str) -> str:
    """Convert MediaWiki bold/italic quote-run markup to internal markers.

    Line-scoped, matching MediaWiki's ``doQuotes`` algorithm: the PHP
    source literally splits its input on ``\\n`` and runs the bold /
    italic pair-matcher per line.  We do the same — split on ``\\n``,
    feed each line to ``mwparserfromhell`` independently, replace
    italic / bold ``Tag`` nodes with ``«I»…«/I»`` / ``«B»…«/B»`` in
    place, join.

    Line-scoping prevents the cascade bug where one unbalanced
    ``'''`` (typically a transcription typo like ``''The Fishmongers'''``
    on vol 28 p403) pairs with the opening ``'''`` of a later heading
    and inverts every quote-run between them — eating articles like
    WATER-SCORPION, WATERSHED, WATERSPOUT, WHEAT.  In MediaWiki those
    typos render the affected line ugly but don't spill onto adjacent
    lines; we now match that behavior.

    Within a single line we use mwparserfromhell's AST in place: each
    italic / bold ``Tag`` node is replaced with marker text.
    Templates, wikilinks, wiki-tables, refs, math, HTML tags etc. are
    left untouched.  In-place mutation (vs. recursive node
    reconstruction) preserves wiki-table syntax — the previous
    walker-based version round-tripped ``{|…|}`` to
    ``<table>…</table>``, killing the ``\\n\\n`` boundary that
    detect_boundaries needs before each subsequent bold heading.

    Runs once over the joined stream in ``preprocess`` so every downstream
    stage sees ``«B»``/``«I»`` instead of ``'''``/``''``.

    Replaces the previous regex-based ``_convert_bold_italic`` in
    ``body_text.py`` which couldn't handle:
      * 4-quote / 5-quote runs per MediaWiki's algorithm
      * Quote-runs inside templates / wikilinks / refs
      * The ``'''X''. (''Y''). ''Z'''`` mismatch pattern (ARACHNIDA
        Sub-Class II)
      * HTML ``<b>``/``<i>`` tags (which never had ``'''`` to match against)
    """
    # Mask <ref>...</ref> blocks before line-splitting.  Some refs
    # contain multi-line disambiguation notes embedded INSIDE a bold
    # heading (e.g. vol 20 ODO OF BAYEUX:
    #   '''ODO<ref>{multi-line note}</ref> OF BAYEUX'''
    # ).  Without masking, the line-splitter chops the bold span
    # across newlines inside the ref, leaving each line with an
    # unbalanced `'''` so the conversion gives up.  Masking collapses
    # the ref to a single token, line-scoping operates on a ref-free
    # version, then we restore.
    _refs: list[str] = []

    def _mask(m: re.Match) -> str:
        # Convert quote-runs INSIDE the ref before masking, so italics
        # like `''Ency. Bib.''` in citations become `«I»Ency. Bib.«/I»`
        # at the same time as italics in the main prose.  Without this,
        # ref-internal italics never see the converter (they sit
        # behind the mask) and emerge as stray `''…''` in the final
        # body — visible as ~2,000 stray_wiki_italic flags.
        ref_text = m.group(0)
        converted_lines = []
        for line in ref_text.split("\n"):
            if _QUOTE_RUN_HINT.search(line):
                converted_lines.append(_convert_quote_runs_line(line))
            else:
                converted_lines.append(line)
        _refs.append("\n".join(converted_lines))
        return f"«REFMASK:{len(_refs)-1}»"

    masked = re.sub(r"<ref[^/>]*>[\s\S]*?</ref>", _mask, text)

    out = []
    for line in masked.split("\n"):
        # Cheap optimization: most lines have no quote-runs.  Skip
        # the parse + filter cycle for those.
        if not _QUOTE_RUN_HINT.search(line):
            out.append(line)
            continue
        out.append(_convert_quote_runs_line(line))
    converted = "\n".join(out)

    # Restore the masked refs.
    if _refs:
        converted = re.sub(
            r"«REFMASK:(\d+)»",
            lambda m: _refs[int(m.group(1))],
            converted,
        )
    return converted


def _convert_quote_runs_line(line: str) -> str:
    """Convert italic/bold tags in a SINGLE line of wikitext.

    Quote-run pairing is therefore line-scoped: a stray ``'`` at the
    end of one line cannot pair with markup on a later line.  Also
    converts ``{{bold|X}}`` template (used by ~7 articles in EB1911
    including SPARKS, JARED vol 25 p629) to the same ``«B»…«/B»``
    marker as wikitext / HTML bold.
    """
    parsed = _mwp.parse(line)
    # First pass: convert any `{{bold|X}}` templates to the same
    # `«B»X«/B»` form so the rest of the rule treats them uniformly
    # with `'''X'''` and `<b>X</b>`.
    for tpl in parsed.filter_templates(recursive=True):
        if str(tpl.name).strip().lower() == "bold" and tpl.params:
            inner = str(tpl.params[0].value)
            try:
                parsed.replace(tpl, f"«B»{inner}«/B»")
            except ValueError:
                continue
    # Iterate to fixed-point: replacing a tag changes the tree, so a
    # newly-exposed inner tag (e.g. italic inside what was a bold
    # span) needs another pass.  In practice MediaWiki bold/italic
    # don't nest, so one pass is almost always enough — but the
    # loop is cheap insurance.
    while True:
        tags = [t for t in parsed.filter_tags(recursive=True)
                if str(t.tag).lower() in ("i", "b")]
        if not tags:
            break
        for tag in tags:
            tag_str = str(tag.tag).lower()
            # ``tag.contents`` is a Wikicode whose ``str()`` emits
            # the original wikitext slice intact — including any
            # nested templates, wikilinks, etc.
            inner = str(tag.contents)
            if tag_str == "i":
                replacement = f"«I»{inner}«/I»"
            else:
                replacement = f"«B»{inner}«/B»"
            try:
                parsed.replace(tag, replacement)
            except ValueError:
                # Tag may have already been removed by a previous
                # replacement in this iteration (rare; happens when
                # the same Tag object appears under multiple parents
                # via aliasing).  Skip and continue.
                continue
    return str(parsed)
