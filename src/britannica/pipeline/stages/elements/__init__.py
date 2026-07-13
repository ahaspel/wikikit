"""Extract, process, and reassemble embedded elements in wikitext.

Embedded elements (tables, images, footnotes, poems, math, scores)
are extracted from the raw wikitext into a registry, the remaining
text is transformed (bold, italic, links, etc.), then each element
is processed recursively and reassembled into the final text.

The key rule: extract outermost first, process innermost first.
Each element processor calls the same extract-process-reassemble
on its own content, so nesting (e.g. footnotes inside tables)
is handled naturally by recursion.
"""
from __future__ import annotations

import re
from dataclasses import replace as _dc_replace
from typing import Callable

from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._ref import (
    _process_ref,
    _process_ref_self,
    _ref_attrs,
)
from britannica.pipeline.stages.elements._image import (
    _process_genealogy,
)
from britannica.pipeline.stages.elements._dual_line import _process_dual_line
from britannica.pipeline.stages.elements._link import (
    _LINK_LABELS, _link_display,
    _wrap_article_link, _wrap_target_first, _wrap_selfref, _wrap_author_link,
    _wrap_fragment_link, _wrap_intra_link, _wrap_wikilink)
from britannica.pipeline.stages.elements._contributor import (
    _process_contributor_footer)
from britannica.pipeline.stages.elements._spacer import process_spacer
from britannica.pipeline.stages.elements._frame import (
    process_refs, process_missing)
from britannica.pipeline.stages.elements._hanging import process_hanging_indent
from britannica.pipeline.stages.elements._brace import process_brace
from britannica.pipeline.stages.elements._coord import process_coord
from britannica.pipeline.stages.elements._toc import process_toc_row
from britannica.pipeline.stages.elements._content import process_content_extract
from britannica.pipeline.stages.elements._math import (
    _process_math_equation,
)
from britannica.pipeline.stages.elements._leaf import (
    _process_math,
    _process_poem,
    _process_ppoem,
    _process_score,
)
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry,
    IMAGE_LABELS,
    TABLE_LABELS,
    _PH,
)
from britannica.pipeline.stages.elements._outline import (
    _OUTLINE_RANGE_HEADER_RE,
    _extract_outlines,
    _outline_indent_depth,
    _outline_is_bare_emphasis,
    _outline_is_list_shaped,
)
from britannica.pipeline.stages.elements._section import (
    _process_section,
)
from britannica.pipeline.stages.elements._anchor import process_anchor
from britannica.pipeline.stages.elements._tables import (
    _CHEM_BRACKET_IMG_RE,
    _has_chem_equation_content,
    _has_chem_reaction_content,
    _process_chemistry_layout,
    _process_inline_glyph_wrapper,
    _process_table_unified,
)
from britannica.pipeline.stages.elements._table_decompose import _tag


# Range-style header: `N–M.—TITLE.<br />` (GEM plate captions, similar
# numbered-section openers).  Acts as a top-level label (depth 0) for
# the indented numbered items that follow.  Required to be SHORT and
# end with `<br />` so prose paragraphs that happen to start with
# `1–5.` don't false-match.

def _is_compound_table(raw: str,
                       inner_registry: ElementRegistry | None) -> bool:
    """Detect data tables with nested sub-tables in cells.

    Two signals, both required:

      * Header carries a data-table indicator (`border=N`, `rules=`,
        or `class="…wikitable|tablecolhd|border…"`) on the `{|…`
        opener line.  Inspected from raw bytes — the walker doesn't
        classify table-opener attributes.

      * The table has nested wikitables as direct children.  Under
        bottom-up walking, any nested `{|…|}` blocks have already
        been extracted as TABLE-typed elements in
        ``inner_registry.elements`` — replacing an earlier raw-byte
        ``\\{\\|`` / ``\\|\\}`` depth scan.

    Returns True when both signals are present — the table is a real
    data table that needs parallel-sub-table-row processing in
    ``_process_compound_table``.
    """
    header = raw.split("\n", 1)[0]
    has_data = bool(
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                   header, re.IGNORECASE))
    if not has_data:
        return False
    if inner_registry is None:
        return False
    return any(t == "TABLE" for t, _ in inner_registry.elements.values())


# ── Processing ────────────────────────────────────────────────────────

# Producer handler signature.  All producers share this shape:
#   (raw, inner, context, inner_registry) -> marker
# `raw` is the source bytes of the element (with delimiters); `inner`
# is the delimiter-stripped content with child placeholders.  Most
# producers use `inner` + the child registry; the leaf cases
# (CHART2, IMAGE, COMPOUND_TABLE) use `raw` and ignore the rest.
_ElementHandler = Callable[
    [str, str, ElementContext, "ElementRegistry | None"], str]


def _passthrough_inner(raw, inner, context,
                       inner_registry):
    return inner


def _cell_markers(inner_registry) -> list:
    """The produced markers of a decompose node's CELL children, IN ORDER, empties kept.

    Iterate `.elements` (every placeholder, source order) — NOT `.markers` (which omits an
    empty marker) — so a decompose producer reassembling its layout keeps every slot in
    position: a blank running-header margin, a missing TOC value, an empty fraction part."""
    if inner_registry is None:
        return []
    return [inner_registry.markers.get(ph, "") for ph in inner_registry.elements]


def _process_cell(tag, raw, inner, reg):
    """A TD / TH cell: substitute the child markers into the cell body, then
    `.strip(' \\t')`, and wrap in the tag with the raw attr-slot folded (`_tag`).

    The strip must run AFTER substitution — the whitespace it trims is often a
    DECODED `{{spaces|N}}` padding (`{{spaces|2}}Year{{spaces|2}}`), literal
    space only once the SPACER producer has run, not at classify time.  This
    reproduces the former `_tag(td, attr, recurse(content).strip(' \\t'))` exactly;
    `produce_tree`'s own child substitution afterwards is then a no-op (every
    placeholder is already resolved here).  Empty child markers substitute to ''
    (matching `produce_tree`), so a cell whose only content dropped strips to ''."""
    body = inner
    if reg is not None:
        for _ in range(5):
            changed = False
            for ph in list(reg.elements):
                if ph in body:
                    body = body.replace(ph, reg.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    return _tag(tag, raw, body.strip(" \t"))


_HYPHEN_MAP = None


def _hyphen_map():
    """Lazy-load the corpus dehyphenation map (built by tools/build_hyphen_map.py).
    Absent / unreadable → empty, so every wrap simply 'leaves'."""
    global _HYPHEN_MAP
    if _HYPHEN_MAP is None:
        import json
        from pathlib import Path
        try:
            _HYPHEN_MAP = json.loads(
                Path("data/hyphen_map.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _HYPHEN_MAP = {}
    return _HYPHEN_MAP


# A hyphenated word reaches the body producer as `X-<sep>Y`, where <sep> is
# anything OR nothing — a space, a collapsed `<br>`, a raw newline, or the split
# written solid in the source (`Differenti-ation`).  The separator is irrelevant:
# the WORD is the only question.  The corpus map votes: drop the hyphen (the
# corpus prefers `XY` solid — a wrap artifact), keep it (real compound), or —
# absent — leave the split alone (suspended hyphen / non-word).
_HYPHEN_RE = re.compile(r"([A-Za-z]{2,})-\s*([A-Za-z]{2,})")


def _dehyphenate(text):
    mp = _hyphen_map()

    def _repl(m):
        x, y = m.group(1), m.group(2)
        v = mp.get(f"{x.lower()}-{y.lower()}")
        if v == "drop":
            # A wrap never breaks between two capitals; a hyphen that does is a
            # proper name or acronym (Ba-Hima, Ba-Rotse, CO-NH) — the hyphen IS
            # part of the name, not an artifact — so keep it.
            if x[:1].isupper() and y[:1].isupper():
                return m.group(0)
            return x + y
        if v == "keep":
            return x + "-" + y
        return m.group(0)                              # absent → leave

    return _HYPHEN_RE.sub(_repl, text)


def _produce_body(raw, inner, context, inner_registry):
    """BODY producer — also the «\\n\\n producer».  The body run is the inert text
    between element markers; this producer turns a blank line into a paragraph-
    OPEN marker «P», then rejoins a word broken across a line/column wrap (the
    corpus dehyphenation map decides drop/keep/leave).

    «P» is open-only on purpose: the viewer maps «P»→`<p>` and the BROWSER
    auto-closes each `<p>` at the next `<p>` or at any block element — so
    paragraph-closing and block-break-out are the browser's native job, computed
    by nobody in this pipeline and needing no look-ahead.  A run with no blank
    line emits no «P» and simply rides inside whichever paragraph is open; the
    inline child placeholders it carries are still substituted by ``produce_tree``.

    Placed here, not in ``preprocess()``: preprocess runs BEFORE detect-boundaries
    and the walker, both of which read raw `\\n\\n`; swapping it there blinds them.
    By the time the body producer runs, every `\\n\\n`-dependent stage is done.
    """
    # Dehyphenate AFTER the «P» split: a paragraph break is now «P» (not
    # whitespace), so a wrap can't rejoin across it — only single line breaks and
    # spaces remain for the wrap regex to see.
    return _dehyphenate(re.sub(r"\n{2,}", "«P»", raw))


_CTR_PURE_PH_RE = re.compile(r"^\s*\x03ELEM:\d+\x03\s*$")


def _center_wrap(text: str) -> str:
    """Wrap each non-placeholder paragraph of centred content in «CTR».
    Pure block-placeholder paragraphs stay unwrapped (the block centres
    itself).  Mirrors the former body_text `_wrap_ctr`."""
    content = text.strip()
    if not content:
        return ""
    out: list[str] = []
    for p in re.split(r"\n\n+", content):
        p = p.strip()
        if not p:
            continue
        out.append(p if _CTR_PURE_PH_RE.match(p) else f"«CTR»{p}«/CTR»")
    return "\n\n".join(out)


def _process_center(raw, inner, context, inner_registry):
    """CENTER paired-wrapper (`{{NAME/s}}…{{NAME/e}}`) → the marker for NAME's
    styler, dispatched on the wrapper name via the shared `_TEMPLATE_STYLE_WRAPPERS`
    registry (the SAME registry the pipe form uses — one styler, two spellings):

      * centring family (`c`/`block center`/`center block` → «CTR») keeps the
        paragraph-wise `_center_wrap` (byte-identical to before);
      * a non-centring block styler (`fine block`/`EB1911 fine print`/`smaller
        block` → font-size) wraps the inner in ONE `style_block` div.

    CENTER is a COMPOSITE now (un-leafed): the classifier already recursed our inner
    into `inner_registry` and produced each child, and `inner` is the placeholderized
    inner_text (block children still bare `\\x03ELEM\\x03` placeholders, so
    `_center_wrap`'s `_CTR_PURE_PH_RE` sees them).  We just wrap; the framework
    substitutes the child markers into our output afterward.  The former produce-time
    re-walk — `classify_article` + `produce_tree` + a hand-rolled
    `substitute_top_level_markers` reaching UNDER the recursion boundary — is
    deleted; `classify_article` no longer re-enters here, so it runs once per article
    and a heading inside a centered block is a real node in the one tree."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_STYLE_WRAPPERS, style_block)
    m = re.match(r"^\s*\{\{\s*([^{}/]*?)\s*/s\s*\}\}", raw, re.IGNORECASE)
    name = re.sub(r"\s+", " ", m.group(1).strip().lower()) if m else ""
    spec = _TEMPLATE_STYLE_WRAPPERS.get(name)
    if spec and spec.get("css") and not spec.get("ctr"):
        return style_block(inner.strip(), css=spec["css"], tag=spec.get("tag", "DIV"))
    return _center_wrap(inner)   # centring family


def _parse_image(raw):
    """Every image spelling → ``(filename, width, align, caption_raw)`` — the ONE parse.
    ``caption_raw`` is "" unless the wrapper glued a caption INSIDE it: a `thumb`/`frame`
    trailing positional, a `cap=`, or a `caption=`.  Filename derivation is per-spelling
    (bracket / crop-hash / raw-djvu-ref / named param); width & align ride the leaf."""
    from britannica.pipeline.stages.elements._image import (
        djvu_crop_filename, _parse_crop_param, _RAW_IMAGE_ARG_RE, _RAW_DJVU_REF_RE,
        _img_bracket_meta, _thumb_caption_raw)
    tmpl = raw.strip()
    if tmpl.startswith("[["):                        # [[File:…]] — caption only if thumb/frame
        fn, width, align = _img_bracket_meta(tmpl)
        return fn, width, align, _thumb_caption_raw(raw)
    if re.match(r"\{\{\s*Css\s+image\s+crop", tmpl, re.IGNORECASE):   # DjVu crop — no caption
        fn = djvu_crop_filename(tmpl)
        if fn is None:
            img = _parse_crop_param(tmpl, "Image")
            fn = img.replace(" ", "_") if img else ""
        return fn, None, None, ""
    if re.match(r"\{\{\s*plain image with caption\s*\|", tmpl, re.IGNORECASE):
        from britannica.pipeline.stages.elements._link import _split_top_pipes
        inner = re.sub(r"\}\}\s*$", "", re.sub(
            r"^\s*\{\{\s*plain image with caption\s*\|", "", raw, flags=re.IGNORECASE))
        params: dict[str, str] = {}
        for part in _split_top_pipes(inner):
            k, eq, v = part.partition("=")
            if eq:
                params[k.strip().lower()] = v.strip()
        fn = re.sub(r"^\s*(?:File|Image):\s*", "", params.get("image", ""), flags=re.IGNORECASE)
        wm = re.match(r"(\d+)", params.get("width", ""))
        return (fn, int(wm.group(1)) if wm else None,
                params.get("align") or None, params.get("caption", ""))
    if re.match(r"\{\{\s*(?:img float|figure|FI)\s*\|", tmpl, re.IGNORECASE):
        from britannica.parsers import img_float as _imgf
        inner = tmpl[2:-2] if tmpl.startswith("{{") and tmpl.endswith("}}") else tmpl
        parsed = _imgf.parse(inner)
        if parsed is None:
            return "", None, None, ""
        return parsed.filename, parsed.width, parsed.align, parsed.caption or ""
    m = _RAW_IMAGE_ARG_RE.match(tmpl)                # {{raw image|…}} — no caption
    if m:
        arg = m.group(1).strip()
        dref = _RAW_DJVU_REF_RE.match(arg)
        fn = (f"djvu_vol{int(dref.group(1)):02d}_page{int(dref.group(2)):04d}.jpg"
              if dref else arg)
        return fn, None, None, ""
    return "", None, None, ""


def _process_image(raw, inner, context, inner_registry):
    """The ONE image producer.  An image is a pure LEAF; a caption — if the wrapper glued
    one on — rides with it as ONE inline-float unit.  The figure floats to the side (prose
    wraps it) at the placement the source chose via `align` — default LEFT, as `img float`
    always did; `align=center` is a centred block instead.  The caption sits centred directly
    below the plate (`text-align:center`).  Never folded into the marker's alt slot (that
    hides it), never a synthesized `<table>` — a figure is not tabular, and the old figtable's
    only real jobs (float + shrink-to-image-width) a floated span does natively.  Every
    spelling (bare `[[File:]]`, `{{Css image crop}}`, `{{raw image}}`, `thumb`/`frame`,
    `{{plain image with caption}}`, `{{img float|figure|FI}}`) funnels through one parse
    (`_parse_image`) and this one emit.

    The CAPTION is a COMPOSITE child: `_classify_image_composite` decomposed `caption_raw`
    into child nodes, so a caption's links / stylers / footnotes are REAL nodes in the one
    tree (not a produce-time re-`process_elements`).  We substitute their markers into `inner`
    for `cap`; the fn / width / align are re-parsed from raw (a pure leaf parse)."""
    from britannica.pipeline.stages.elements._image import build_img_marker
    fn, width, align, caption_raw = _parse_image(raw)
    if not fn:
        return ""
    cap = inner
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in cap:
                    cap = cap.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    cap = cap.strip()
    if not cap:
        return build_img_marker(fn, align=align, width=width)      # bare leaf carries its align
    # Captioned figure: image + caption as ONE inline-float unit.  The span is inline-LEVEL,
    # so it floats INSIDE the paragraph and the prose wraps it — no `<p>`-breaking block.  A
    # floated span shrink-wraps to its content (the plate), so the caption stays snug at the
    # image width even with no explicit width — exactly what the figtable's `<table>` gave.
    leaf = build_img_marker(fn, width=width)
    w = f";width:{width}px" if width else ""
    align = align or "left"                                        # img float's long-standing default
    box = (f"display:block;margin-left:auto;margin-right:auto{w}"
           if align == "center" else f"float:{align}{w}")
    return f"«SPAN[style:{box};text-align:center]»{leaf}«BR»{cap}«/SPAN»"


# LB / CITE producers folded into the peel/recurse/wrap mechanism (`_PR_WRAP` rows
# `_wrap_lb` / `_wrap_italic`); the bespoke functions are gone.


def _process_nowiki(raw):
    """`<nowiki>X</nowiki>` → X verbatim: drop the wiki-escape tags, keep the
    literal inner.  Lifted as a walker element (the walker already treats
    `<nowiki>` opaque) so the inner — which may hold a `|` that nowiki exists to
    protect from cell/template splitting — is placeholdered BEFORE structural
    parsing and never re-exposed.  This is why it is NOT a preprocess unwrap,
    which would re-expose that `|` to the walker."""
    inner = re.sub(r"^\s*<nowiki>", "", raw, flags=re.IGNORECASE)
    return re.sub(r"</nowiki>\s*$", "", inner, flags=re.IGNORECASE)




def _process_coordinates(inner):
    """`{{EB1911 Coordinates|D|M[|S]|H}}` (one lat OR lon) → `D°M′[S″]H`.  Real
    place data the old body-text handler used to format and which then leaked raw.
    The trailing N/S/E/W is the hemisphere; the leading numerics are
    degrees/minutes/seconds (2-arg form `D|M` carries no hemisphere)."""
    parts = [p.strip() for p in inner.split("|")]
    args = [p for p in parts[1:] if p and "=" not in p]  # drop name + named params
    if not args:
        return ""
    hemi = args[-1].upper() if args[-1].upper() in ("N", "S", "E", "W") else ""
    nums = args[:-1] if hemi else args
    syms = ("°", "′", "″")  # ° ′ ″
    body = "".join(f"{n}{syms[i]}" for i, n in enumerate(nums[:3]))
    return body + hemi


_SUB_MAP = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUP_MAP = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
# Spans that `_process_subsup` must pass through untranslated: emitted markers
# (`«…»`) AND walker element placeholders (`\x03ELEM:N\x03`) — translating a
# placeholder's ID digits to sub/superscript breaks the outer marker substitution
# (INTERPOLATION's `δu²` over a fraction placeholder).
_MARKER_TOKEN_RE = re.compile(r"«[^«»]*»|\x03[^\x03]*\x03")


def _process_subsup(raw, context):
    """`{{sub|x}}` / `{{sup|x}}` → Unicode subscript / superscript.  Relocated
    from body-text's `_convert_sub_sup` so it carries in every context (it leaked
    inside math/italic/centred blocks).  The slot RECURSES — it can hold a walker
    element (STEAM_ENGINE's `{{sup|{{sfrac|1|n}}}}`) — and flat runs translate to
    Unicode while element markers (`«…»`) pass through verbatim (their digits would
    otherwise be mangled).  Bold/italic inside is dropped, as the old pass did (it
    doesn't render in super/subscript)."""
    m = re.match(r"\{\{\s*(sub|sup)\s*\|(.*)\}\}\s*$", raw,
                 re.IGNORECASE | re.DOTALL)
    if not m:
        return raw
    table = _SUB_MAP if m.group(1).lower() == "sub" else _SUP_MAP
    inner = process_elements(m.group(2), context,
                             _allow_figure=False)
    inner = (inner.replace("«B»", "").replace("«/B»", "")
                  .replace("«I»", "").replace("«/I»", ""))
    out, last = [], 0
    for mk in _MARKER_TOKEN_RE.finditer(inner):
        out.append(inner[last:mk.start()].translate(table))
        out.append(mk.group(0))
        last = mk.end()
    out.append(inner[last:].translate(table))
    return "".join(out)


# `<div …>` / `<p …>` / `<span …>` opener — captures (tag, attrs).  The matching
# close is found by the walker's one balanced matcher, so the producer peels the
# wrapper without a second balanced scanner.
_STYLED_OPEN_RE = re.compile(
    r"^\s*<(div|p|span|ins)\b([^>]*)>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _styled_br_to_marker(text: str) -> str:
    """A styled wrapper is a display block (centred equations, stacked labels);
    its OWN `<br>` is a meaningful line break, so carry it as the canonical
    «BR» (matching the prior figure producer's rendering).  Only TOP-LEVEL `<br>`
    is converted — a `<br>` inside a nested balanced construct (table cell,
    nested wrapper) belongs to that construct's own producer, so we skip every
    nested span whole via the walker's one `_construct_end` matcher.  Without
    this, the recursion routes the wrapper's prose to the BODY producer, which
    renders `<br>` as a SPACE (its soft-wrap rule) and silently drops the
    break (INTERPOLATION's multi-line centred series)."""
    from britannica.pipeline.stages.elements._walker import _construct_end
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "<":
            mbr = _BR_RE.match(text, i)
            if mbr:
                out.append("«BR»")
                i = mbr.end()
                continue
            j = _construct_end(text, i)        # skip any nested construct whole
            if j is not None and j > i:
                out.append(text[i:j])
                i = j
                continue
        elif ch in "{[":
            j = _construct_end(text, i)        # skip `{{…}}` / `{|…|}` / `[[…]]`
            if j is not None and j > i:
                out.append(text[i:j])
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_FURNITURE_TITLE_RE = re.compile(
    r"\b(?:amended|corrected|changed|altered|emended|spel[lt]|transcribed)\s+from\b"
    r"|\bin\s+(?:the\s+)?original\b|\boriginally\b|\bsic\b|\bmisprint|\bmisspel"
    r"|\bshould\s+(?:be|read)\b"
    r"|\b(?:added|removed|inserted|deleted)\s+(?:the\s+|a\s+|missing\s+)?"
    r"(?:slash|quote|comma|space|hyphen|bracket|stop|point|colon|semicolon|apostrophe)"
    r"|\b(?:quote|comma|space|hyphen|bracket|slash|stop|point|colon)s?\s+"
    r"(?:added|removed|inserted|deleted)\b",
    re.IGNORECASE)


def process_span_title(raw, inner, context, inner_registry):
    """SPAN_TITLE producer (walker SHAPE_SPAN_TITLE): a `<span title="T">X</span>`.

    A `title=` tooltip is a reader-facing gloss (a romanization, a translation, a
    retroactive death year) UNLESS it's transcription furniture — a Wikisource note
    ABOUT the transcription ("amended from 'X'").  Carry T as «SPAN[title:T]» (the
    HTML twin of {{tooltip}}) unless `_FURNITURE_TITLE_RE` matches the title, in
    which case drop the wrapper and keep X.  Re-promotes the gutted body-text
    `_handle_title_spans`; SPAN_TITLE only reaches here when `_SPAN_TITLE_OPEN_RE`
    matched in the walker."""
    from britannica.pipeline.stages.elements._walker import _SPAN_TITLE_OPEN_RE
    sp = _SPAN_TITLE_OPEN_RE.match(raw)
    title = (sp.group("q") or sp.group("uq") or "").strip().replace(
        "]", "").replace("»", "")
    inner_raw = _styled_br_to_marker(
        re.sub(r"</span\s*>\s*$", "", raw[sp.end():], flags=re.IGNORECASE))
    content = process_elements(
        inner_raw, context, _allow_figure=False).strip()
    # Carry the tooltip UNLESS the title is transcription FURNITURE (see
    # `_FURNITURE_TITLE_RE`) — every other title is a gloss the reader wants
    # (romanization, translation, retroactive death year), and the printed text is
    # unchanged either way since it only shows on hover.  [[feedback_when_in_doubt_carry]]
    if title and not _FURNITURE_TITLE_RE.search(title):
        return f"«SPAN[title:{title}]»{content}«/SPAN»"
    return content  # transcription furniture → drop the wrapper, keep the content


def _shoulder_peel(raw):
    """Peel a shoulder heading `{{EB1911 Shoulder Heading|[width=N|][align=…|]LABEL}}` → the
    clean LABEL to recurse.  The LABEL is the last POSITIONAL slot (named `width=`/`align=`
    params may TRAIL it — `{{…|'''Label'''.|align=right}}`); its `<br>`s are margin-column wrap
    typography, dropped to a space (not carried).  Shared so `_classify_shoulder_composite` (the
    label to recurse into child nodes) and the producer agree — the SHOULDER twin of
    `_strip_peel` (the producer needs no value from it, only the slug off the content)."""
    from britannica.pipeline.stages.elements._tables import _SHOULDER_HEADING_RE
    from britannica.pipeline.stages.elements._link import _split_top_pipes
    sh = _SHOULDER_HEADING_RE.match(raw)
    slots = _split_top_pipes(re.sub(r"\}\}\s*$", "", raw[sh.end():]))
    positional = [g for g in slots if not re.match(r"\s*[A-Za-z][\w-]*\s*=", g)]
    label = (positional or slots)[-1]
    label = re.sub(r"\s*<[Bb][Rr]\b[^>]*>\s*", " ", label)
    return _styled_br_to_marker(label)


def process_shoulder(raw, inner, context, inner_registry):
    """SHOULDER producer (walker SHAPE_SHOULDER): a shoulder heading —
    `{{EB1911 Shoulder Heading|[width=N|]LABEL}}` / `…HeadingSmall` / `{{EB9 Margin Note}}`.

    A marginal SECTION label: emit «SH:slug»…«/SH» (what `detect_sections` keys on for the
    TOC).  `_classify_shoulder_composite` decomposed the LABEL into child nodes; we substitute
    their markers into `inner`, `.strip()`, mint the section slug off the assembled content
    (once, by the one slug function, exactly as «SEC» carries its slug), and wrap."""
    from britannica.util.strings import section_slug, strip_markers
    content = inner
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in content:
                    content = content.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    content = content.strip()
    slug = section_slug(strip_markers(content))
    return f"«SH:{slug}»{content}«/SH»"


def _running_header_cells(raw):
    """Peel `{{rh|left|center|right}}` / `{{Running header|…}}` → its 3 cell raws (padded /
    truncated to exactly 3).  Shared so `_classify_running_header_composite` (classify each cell
    into child nodes) and the producer agree on the split — the RUNNING_HEADER twin of the
    other styler peels, but for THREE content regions (margin | centre | margin)."""
    from britannica.pipeline.stages.elements._tables import _RUNNING_HEADER_RE
    from britannica.pipeline.stages.elements._link import _split_top_pipes
    rh = _RUNNING_HEADER_RE.match(raw)
    cells = _split_top_pipes(re.sub(r"\}\}\s*$", "", raw[rh.end():]))
    return (cells + ["", "", ""])[:3]


def process_running_header(raw, inner, context, inner_registry):
    """RUNNING_HEADER producer: `{{rh|left|center|right}}` — a 3-column flex frame.  Body content,
    ~34 articles: displayed-equation layouts (margin connective/number | centred equation |
    equation number) and plate title bars (empty | centred title | plate number).

    `_classify_running_header_composite` DECOMPOSED the row into three CELL nodes; we read the
    three cell markers (each already its recursed content, in order) and REASSEMBLE the flex row
    so the centre stays centred between the margins.  No sentinel split — the cells are nodes."""
    left, center, right = (
        c.strip() for c in (_cell_markers(inner_registry) + ["", "", ""])[:3])
    return (
        "«DIV[style:display:flex;align-items:baseline]»"
        f"«SPAN[style:text-align:left]»{left}«/SPAN»"
        f"«SPAN[style:flex:1;text-align:center]»{center}«/SPAN»"
        f"«SPAN[style:text-align:right]»{right}«/SPAN»«/DIV»")


def _html_style_peel(raw):
    """Peel an HTML-form styler `<div|p|span|ins attrs>content</tag>` → (marker_tag, css,
    clean inner_raw).  Decode the opener's OWN `{{=}}` attr-escape (context-safe — an HTML
    opener has no named args, so `fold_cell_styles` sees `style="…"`, not `style{{=}}"…"`; the
    content `{{=}}` stays SPACER's post-walk job [[feedback_context_sensitive_is_producer]]),
    drop the walker-balanced close tag off the tail, and carry the wrapper's own top-level
    `<br>` as «BR» (a styled block's line breaks are meaningful).  Shared so `process_html_style`
    (marker_tag + css for the style shell) and `_classify_html_style_composite` (the clean inner
    to recurse into child nodes) peel one and the same way — the HTML twin of `_strip_peel`."""
    from britannica.pipeline.stages.elements._table_fold import fold_cell_styles
    m = _STYLED_OPEN_RE.match(raw)
    if not m:
        return None
    tag = m.group(1).lower()
    attrs = re.sub(r"\{\{\s*=\s*\}\}", "=", m.group(2))
    inner_raw = re.sub(rf"</{tag}\s*>\s*$", "",
                       raw[m.end():], flags=re.IGNORECASE)
    marker_tag = "SPAN" if tag in ("span", "ins") else "DIV"
    return marker_tag, ";".join(fold_cell_styles(attrs)), _styled_br_to_marker(inner_raw)


def _wrap_html_style(raw, body, ctx):
    """HTML_STYLE wrap (a `_PR_WRAP` row): a `<div>`/`<p>`/`<span>`/`<ins>` carrying style
    (`{{Ts}}` / `style=` / `align=`).  Style ⊥ structure — the HTML twin of STRIP.  The classified
    inner (a nested table / MATH / verse / footnote → a NODE) arrives substituted as `body`; the
    style shell (tag + CSS) is re-derived from raw via the shared `_html_style_peel`, then wrapped
    by `style_block` — «CTR» for a pure-centred block, «DIV[style:CSS]» / «SPAN[style:CSS]»
    otherwise.  Subsumes body-text's `_p_ts` / `_div_ts` / `_span_ts` and the styled-`<div>`→
    figure gate."""
    from britannica.pipeline.stages.elements._tables import style_block
    peel = _html_style_peel(raw)
    if peel is None:                          # opener didn't match — pass raw through
        return raw
    marker_tag, css, _ = peel
    return style_block(body.strip(), css=css, tag=marker_tag)


_NAMED_PARAM_RE = re.compile(r"^\s*([A-Za-z][\w -]*?)\s*=\s*([^|]*)\|")


def _carry_named_params(inner_raw):
    """Pull leading `name=value|` NAMED params off a styler's content and map them
    to CSS — CARRY them (they're presentation: `{{Hebrew|small=yes|א}}` renders the
    glyph smaller), don't drop.  `small`→`font-size:83%` (the corpus 'smaller'
    value), `style`→verbatim, `width`→`width:VALUE`.  A genuinely-unmappable param
    (rare 1-off) is dropped — there is no CSS to carry it as.  The trailing `|` is
    required, so a single `=`-bearing content segment (`{{nowrap|a=b}}`) stays
    content.  Returns (content_after_params, [css, …])."""
    css: list[str] = []
    while True:
        m = _NAMED_PARAM_RE.match(inner_raw)
        if m is None:
            break
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if key == "small" and val.lower() in ("yes", "y", "1", "true"):
            css.append("font-size:83%")
        elif key == "style" and val:
            css.append(val.rstrip(";"))
        elif key == "width" and val:
            css.append(f"width:{val}")
        # else: unmappable 1-off param — drop (no CSS map to carry it as).
        inner_raw = inner_raw[m.end():]
    return inner_raw, css


def _strip_peel(raw):
    """Peel a PIPE-form styler `{{name|content}}` → (name, clean content, param CSS).
    Drop the matched `{{name|` opener + trailing `}}`, the `1=` explicit-positional
    escape (guards a literal `=`; a real named param never has a bare-number key), and
    leading NAMED params (`{{Hebrew|small=yes|א}}` — presentation, CARRIED as CSS, not
    dropped), then convert the wrapper's own top-level `<br>` to «BR».  Shared so the
    STRIP producer (name + param_css for the style shell) and `_classify_strip_composite`
    (the clean content to recurse into child nodes) peel one and the same way."""
    from britannica.pipeline.stages.elements._tables import _TEMPLATE_STYLE_RE
    tm = _TEMPLATE_STYLE_RE.match(raw)
    inner_raw = re.sub(r"^\s*\d+\s*=\s*", "",
                       re.sub(r"\}\}\s*$", "", raw[tm.end():]))
    inner_raw, param_css = _carry_named_params(inner_raw)
    return tm.group(1).lower(), _styled_br_to_marker(inner_raw), param_css


def _wrap_strip(raw, body, ctx):
    """STRIP wrap (a `_PR_WRAP` row): the template-form styler `{{center|…}}` / `{{block center|…}}`
    / `{{Fine block|…}}` / `{{csc|…}}` / ….  Style ⊥ content.  The peel already gave `body` the
    right content — the PIPE form's recursed inner, or the BARE form's spec-default constant — so
    here we re-derive name + carried-param CSS from raw and wrap via `style_block`."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_STYLE_RE, _TEMPLATE_STYLE_WRAPPERS, style_block)
    if _TEMPLATE_STYLE_RE.match(raw):        # pipe form — carried named-param CSS
        name, _, param_css = _strip_peel(raw)
    else:                                    # bare form — spec default, no carried params
        name = re.match(r"\{\{\s*([^|{}]+?)\s*\}\}", raw).group(1).strip().lower()
        param_css = []
    spec = _TEMPLATE_STYLE_WRAPPERS[name]
    css = ";".join(c for c in [spec.get("css", ""), *param_css] if c)
    # A carried param on a tag-less script wrapper ({{Hebrew}} = {}) decorates an
    # inline glyph → SPAN, not the block DIV default.
    tag = spec.get("tag") or ("SPAN" if param_css else "DIV")
    return style_block(body.strip(), css=css, tag=tag,
                       ctr=spec.get("ctr", False), sc=spec.get("sc", False))


def _process_title(raw, inner, context, inner_registry):
    """TITLE producer (the «TITLE»…«/TITLE» stamp from preprocess_article).

    TITLE is special: it feeds BOTH the DISPLAY heading marker («TITLE:…«/TITLE»)
    and — via that same marker, stripped to text — the PLAIN title field.  The raw
    span carries a trailing JOINT (a comma, or a terminator period) that in print
    joins the headword to the body; `strip_title_joint` removes it (in
    `_classify_title_composite`, before the recurse) so neither output dangles it.

    `_classify_title_composite` decomposed the joint-stripped heading into child
    nodes; we substitute their markers into `inner` — no produce-time re-
    `process_elements`, so a `«FN` in a bracketed title is a REF NODE in the one
    tree (which lets `resolve_ref_bodies` see the title's footnote definition),
    not a re-parse with fresh placeholders that don't match the resolved body."""
    content = inner
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in content:
                    content = content.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    return f"«TITLE:{content}«/TITLE»"


# `{{size|KEYWORD|X}}` (FRAME-dissolution home for `size`) — the keyword font-size
# scale mapped to CSS font-size keywords; PARAM carries it like any arg-1 styler.
_SIZE_KEYWORD_CSS = {"xxl": "xx-large", "xl": "x-large", "l": "large",
                     "m": "medium", "s": "small", "xs": "x-small", "xxs": "xx-small"}


def _param_peel(raw):
    """Peel a param-valued styler `{{name|value|content}}` → (name, value, clean content).
    The CSS value rides in arg-1, the content is arg-2+ (its own pipes kept); the content's
    top-level `<br>` is carried as «BR».  Shared so `process_param` (name + value → CSS) and
    `_classify_param_composite` (the content to recurse into child nodes) peel one and the same
    way — the PARAM twin of `_strip_peel`."""
    from britannica.pipeline.stages.elements._tables import _TEMPLATE_PARAM_STYLE_RE
    pm = _TEMPLATE_PARAM_STYLE_RE.match(raw)
    name = re.sub(r"\s+", " ", pm.group(1).strip().lower())
    rest = re.sub(r"\}\}\s*$", "", raw[pm.end():])
    bar = rest.find("|")
    value = (rest[:bar] if bar >= 0 else "").strip()
    return name, value, _styled_br_to_marker(rest[bar + 1:] if bar >= 0 else rest)


def _substitute_children(inner, inner_registry):
    """Substitute a composite's child markers into its placeholderized `inner` (5-pass, so a
    child marker that itself carries another child's placeholder resolves — cross-references).
    The shared body every composite producer runs when it needs the ASSEMBLED content (a slug,
    a strip, a wrap, a display slot); `produce_tree` also post-substitutes, so this is belt-and-
    braces for producers that read `content` before returning."""
    content = inner
    if inner_registry is not None:
        for _ in range(5):
            changed = False
            for ph in list(inner_registry.elements):
                if ph in content:
                    content = content.replace(
                        ph, inner_registry.markers.get(ph, ""))
                    changed = True
            if not changed:
                break
    return content


def _recurse_slot_content(raw, label):
    """The ONE recursive slot — EXACTLY as the old producer passed it to `process_elements` — for
    a single-slot leaf producer, per label; `_classify_recurse_slot` decomposes it into nodes.
    Returns (slot, allow_figure).  Mirrors each producer's slot parse so the classified slot
    matches what the producer wraps."""
    if label == "LANG":                       # script wrapper → its bare content (glyphs)
        body = raw.strip()
        if body.startswith("{{"):
            body = body[2:]
        if body.endswith("}}"):
            body = body[:-2]
        return body.partition("|")[2], True   # process_lang used the bare (figure-allowing) recurse
    if label in _LINK_LABELS:                 # a link → its DISPLAY slot (both [[…]] and {{…}} forms)
        return _link_display(raw, label), False
    if label == "PARAM":                      # {{Fs|value|X}} param-styler → its content (arg-2+)
        return _param_peel(raw)[2], False
    if label == "HTML_STYLE":                 # <div|span … style>X</> → the clean inner (<br>→«BR»)
        p = _html_style_peel(raw)
        return (p[2] if p else ""), False
    if label == "STRIP":                      # {{name|X}} styler → content; bare {{name}} → spec default
        from britannica.pipeline.stages.elements._tables import (
            _TEMPLATE_STYLE_RE, _TEMPLATE_STYLE_WRAPPERS)
        if _TEMPLATE_STYLE_RE.match(raw):
            return _strip_peel(raw)[1], False
        name = re.match(r"\{\{\s*([^|{}]+?)\s*\}\}", raw).group(1).strip().lower()
        return _styled_br_to_marker(
            _TEMPLATE_STYLE_WRAPPERS[name].get("bare", "")), False
    args = re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    if label == "LB":                         # `{{lb-|N}}` → the quantity N
        return re.sub(r"^\s*lb-?\s*\|?\s*", "", args,
                      flags=re.IGNORECASE).strip(), False
    if label == "CITE":                       # `{{cite|Title}}` → the title
        return args.partition("|")[2].strip(), False
    if label == "MAIN_OTHER":                 # `{{main other|main|other}}` → the main copy (param 1)
        from britannica.pipeline.stages.elements._frame import _main_other_content
        return _main_other_content(raw), False
    if label == "SPLIT_WORD":                 # page-split word → the rejoined word (END → empty)
        from britannica.pipeline.stages.elements._splitword import (
            _split_word_word, _marker_name, _END_NAMES)
        if _marker_name(raw) in _END_NAMES:
            return "", False
        return _split_word_word(raw), False
    return "", False


# ── Strategy: peel → recurse → wrap ──────────────────────────────────────────
# ONE mechanism for every producer that peels an outer, recurses its inner slot(s) to child
# nodes, and wraps the result.  The per-label variation is DATA, not code: the classifier peels
# the slot (`_recurse_slot_content`, the PEEL side) and `_PR_WRAP[label]` turns the substituted
# body into the label's marker.  Adding such a producer is a ROW in `_PR_WRAP`, not a bespoke
# function — the same collapse the figure/image family already got (see the `_process_image`
# note further down).  `body` arrives substituted-but-unstripped; each wrap owns its own strip.
def _wrap_italic(raw, body, ctx):
    b = body.strip()
    return f"«I»{b}«/I»" if b else ""


def _wrap_lb(raw, body, ctx):
    b = body.strip()
    return f"{b} lb" if b else "lb"


def _wrap_bare(raw, body, ctx):
    return body.strip()


def _wrap_param(raw, body, ctx):
    """PARAM wrap (a `_PR_WRAP` row): the param-valued styler `{{Fs|108%|X}}` / `{{font size|N%|X}}`
    (+ ti / margin-left / size).  Same styler family as STRIP, but the CSS value is arg-1; re-
    derived from raw via `_param_peel` (a bare integer arg is a percent; a `size` keyword → a CSS
    font-size keyword).  Wraps the substituted body as an INLINE «SPAN[style:…]»."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_PARAM_STYLE_WRAPPERS, style_block)
    name, value, _content = _param_peel(raw)
    tmpl, pct = _TEMPLATE_PARAM_STYLE_WRAPPERS[name]
    if name == "size":           # keyword size (xl/l/…) → a CSS font-size keyword
        value = _SIZE_KEYWORD_CSS.get(value.lower(), value)
    if pct and value.isdigit():
        value += "%"             # font-size family: bare int is a percent
    elif not value and "letter-spacing" in tmpl:
        value = "0.1em"          # {{lsp||X}}: arg-1 empty → default spacing
    return style_block(body.strip(), css=tmpl.format(v=value) if value else "",
                       tag="SPAN")


# label → how to WRAP the substituted body into its marker.  Grows as each peel/recurse/wrap
# producer folds in (its bespoke producer function deleted, replaced by this one row).  NB: every
# wrap named here must be DEFINED ABOVE this dict (module-load order).
_PR_WRAP = {
    "CITE":       _wrap_italic,   # {{cite|Title}}          → «I»Title«/I»
    "LB":         _wrap_lb,       # {{lb-|N}}               → "N lb" / "lb"
    "LANG":       _wrap_bare,     # {{greek|X}}             → X  (glyphs are the text)
    "SPLIT_WORD": _wrap_bare,     # {{hws|frag|WORD}}       → WORD (END → "")
    "MAIN_OTHER": _wrap_bare,     # {{main other|main|other}} → main copy (param 1)
    # the «LN:target|display» family — one wrap each, all on the shared `_link_display` peel
    "EB1911_ARTICLE_LINK": _wrap_article_link,
    "TARGET_FIRST_LINK":   _wrap_target_first,
    "EB1911_SELFREF":      _wrap_selfref,
    "AUTHOR_LINK":         _wrap_author_link,
    "FRAGMENT_LINK":       _wrap_fragment_link,
    "INTRA_ARTICLE_LINK":  _wrap_intra_link,
    "WIKILINK":            _wrap_wikilink,
    # stylers — the substituted body wrapped by `style_block`; each re-derives its CSS from raw
    "PARAM":      _wrap_param,        # {{Fs|108%|X}}      → «SPAN[style:…]»X
    "HTML_STYLE": _wrap_html_style,   # <div|span … style>X → «DIV/SPAN[style:…]»X / «CTR»
    "STRIP":      _wrap_strip,        # {{center|X}}/{{csc|X}}/… → style_block (pipe or bare form)
}


def _make_peel_recurse(label):
    """Bind one `_PR_WRAP` row into a dispatch handler: substitute the classified children, then
    wrap.  The wrap gets (raw, body, ctx) — most ignore ctx; the rare one needs it (a link's
    contributor-initials check reads `ctx.contributor_initials`).  `produce_tree` post-
    substitutes too, so this is the whole producer."""
    wrap = _PR_WRAP[label]
    return lambda raw, inner, ctx, reg: wrap(raw, _substitute_children(inner, reg), ctx)


def _process_fraction(raw, inner, context, inner_registry):
    """FRACTION producer: a `{{sfrac|n|d}}`-family fraction reassembled from its decomposed
    CELL markers.  `_classify_fraction_composite` chopped the slots (`_fraction_parse`) and
    recursed each — so a `{{Greek}}`/`{{sub}}`/`<math>` in a numerator is a real child node;
    here we read the cell markers and PRODUCE the fraction (vulgar-Unicode where available,
    else `n/d`).  The reassembly FORM re-derives from raw — the producer's own leaf parse,
    like IMAGE re-reading file/width/align while its caption is the decomposed child.
    Replaces body-text's `_expand_fractions` flatten."""
    from britannica.pipeline.stages.elements._fraction import (
        _fraction_parse, _frac, _strip_latex_font)
    form, _slots = _fraction_parse(raw)
    cells = _cell_markers(inner_registry)
    if form == "over":
        # Bar-less LaTeX-ish `num \over den` (`{{1\over 2}}` / `{{\it a \over b}}`) — a leading
        # `\it`/`\rm` font directive has no Unicode form here, dropped.  (Reaches the producer
        # only via the bare standalone form; in real bodies every `\over` rides inside an opaque
        # `<math>`, so this is the harness-exercised / defensive path.)
        num = _strip_latex_font(cells[0].strip()) if cells else ""
        den = _strip_latex_font(cells[1].strip()) if len(cells) > 1 else ""
        return _frac(num, den)
    if form == "bare":                          # a bare `{{sfrac}}` — echo the name (defensive)
        return re.sub(r"\}\}\s*$", "", re.sub(r"^\{\{", "", raw))
    # piped / binom — positional count selects mixed / num-den / 1-n
    if len(cells) >= 3:
        rendered = f"{cells[0].strip()}{_frac(cells[1], cells[2])}"
    elif len(cells) == 2:
        rendered = _frac(cells[0], cells[1])
    elif len(cells) == 1:
        rendered = _frac("1", cells[0])
    else:
        rendered = ""
    # Binomial coefficient — a GROUPED pair (parens), not a bare bar-fraction.
    return f"({rendered})" if form == "binom" else rendered


# ── Producer dispatch ─────────────────────────────────────────────────
#
# Flat label → producer table.  Both wikitable sub-kinds (returned by
# `classify` for `element_type=TABLE`) and single-label element types
# (returned trivially) coexist in one dict.  Replaces the previous
# two-level dispatch (`_ELEMENT_HANDLERS` → `_dispatch_table` →
# `_TABLE_KIND_HANDLERS`).
_PRODUCER_DISPATCH: dict[str, _ElementHandler] = {
    # Paired-wrapper span (walker SHAPE_PAIRED_WRAPPER, classifier routes the
    # `{{NAME/s}}…{{NAME/e}}` centring family → CENTER) → «CTR».
    "CENTER": _process_center,
    # The four structures drained out of the former STYLED shape, each its own
    # single-structure producer (verbatim former `_process_styled` branch).
    # SPAN_TITLE — `<span title="T">X</span>` translit-tooltip / editorial-drop.
    "SPAN_TITLE": process_span_title,
    # SHOULDER — `{{EB1911 Shoulder Heading|…}}` marginal section label → «SH».
    "SHOULDER": process_shoulder,
    # RUNNING_HEADER — `{{rh|l|c|r}}` 3-column flex frame.
    "RUNNING_HEADER": process_running_header,
    # STRIP / HTML_STYLE / PARAM — the styler family, generated from `_PR_WRAP` (peel/recurse/wrap
    # → `_wrap_strip` / `_wrap_html_style` / `_wrap_param`, all wrapping the substituted body with
    # `style_block`).  STRIP's peel branches pipe-form content vs the bare form's spec default.
    # (Figure `{|`-tables are de-recognized: LEGENDED_FIGURE / CAPTIONED_FIGURE(
    # _INLINE) / LEGENDED_FIGURE_BESIDE/_CHILD / FIGURE_GROUP / UNPAIRED_FIGURE_GROUP
    # / LAYOUT_WRAPPER / FIGURE classify as TABLE and dispatch to the same
    # `_process_table_unified` below.  A plate is an article fragment, processed
    # identically — the shadow producer they fed is gone.)
    # INLINE_GLYPHS — a `{|…|}` with no `|-` rows that flows `<hiero>` glyphs
    # (and the odd glyph-image) inline in a sentence; the transcriber's table
    # is layout scaffolding, not data.  Producer joins the cells back into
    # inline prose, glyphs inline, no table marker (kills the pipe leak).
    "INLINE_GLYPHS": lambda raw, inner, ctx, reg:
        _process_inline_glyph_wrapper(inner, ctx),
    # TABLE — every wikitable (`{|`) and HTML `<table>`, whatever its
    # source sub-shape (data grid, single-column text box, verse-quotation,
    # compound/nested, rowspan-complex), decomposes through the ONE
    # recursive table engine.  The classifier's table predicates gate only
    # table-vs-(math/chem/figure/glyph); among themselves they make no
    # distinction the producer cares about, so there is one label and one
    # dispatch entry.
    "TABLE": lambda raw, inner, ctx, reg:
        _process_table_unified(raw, inner, reg, ctx),
    # The table composite's own nodes (built by `_classify_table_composite`): a
    # ROW wraps its ordered cell placeholders in `<tr>`, a TD/TH wraps its
    # (classified) cell content in `<td>`/`<th>`, a CAPTION passes its recursed
    # content through for the TABLE producer to wrap in `<caption>`.  Each folds
    # its raw attr-slot at the wrap via the shared `_tag` — the ONLY table-
    # specific production; everything inside a cell rode `classify_article`.
    "ROW": lambda raw, inner, ctx, reg: _tag("tr", raw, inner),
    "TD": lambda raw, inner, ctx, reg: _process_cell("td", raw, inner, reg),
    "TH": lambda raw, inner, ctx, reg: _process_cell("th", raw, inner, reg),
    "CAPTION": _passthrough_inner,
    # CELL — a decompose cell node (RUNNING_HEADER, and the multi-slot set as it folds in):
    # passthrough, its marker IS its recursed content; the container producer reassembles.
    "CELL": _passthrough_inner,
    "CHEMISTRY_LAYOUT": lambda raw, inner, ctx, reg:
        _process_chemistry_layout(raw, inner, ctx, reg),
    # Single-label kinds — element_type == label.
    "CHART2": lambda raw, inner, ctx, reg:
        _process_genealogy(raw, ctx, lambda s: process_elements(s, ctx)),
    "MATH": lambda raw, inner, ctx, reg: _process_math(raw, inner),
    "SCORE": lambda raw, inner, ctx, reg: _process_score(inner),
    "REF_SELF": lambda raw, inner, ctx, reg:
        _process_ref_self(raw, ctx.ref_bodies),
    "REF": lambda raw, inner, ctx, reg:
        _process_ref(raw, inner, ctx.ref_bodies),
    # IMAGE — every image spelling (bare `[[File:]]`, `{{Css image crop}}`,
    # `{{raw image}}`, `thumb`/`frame`, `{{plain image with caption}}`,
    # `{{img float|figure|FI}}`).  The classifier labels them all IMAGE; the ONE
    # producer parses the spelling to a filename and, if the wrapper glued a caption
    # on, renders it CENTERED DIRECTLY BELOW the plate (a centered `«DIV»` block).  An
    # image is a pure leaf; a free-standing sibling caption still recurses in place.
    "IMAGE": _process_image,
    # DUAL_LINE — `{{dual line|A|B}}`, a pure layout primitive (two-line
    # stack: table headers, hyphenations, figure-caption splits, stacked
    # math/chem notation).  A decompose node of two CELL children, so its
    # chem/math content is produced by its own producer — no chem/math-
    # specific dual_line label or producer (see the classifier note: the
    # old CHEM_DUAL / MATH_DUAL split was speculative specificity).
    "DUAL_LINE": _process_dual_line,
    # FRACTION — the `{{sfrac|n|d}}` family, a decompose node of CELL slots the
    # producer reassembles to a vulgar glyph or `n/d`.
    "FRACTION": _process_fraction,
    # LB — `{{lb-|N}}` pound-weight glyph leaf (out of body-text's flat re.sub).
    # LB — generated from `_PR_WRAP` below (peel/recurse/wrap).
    # NOWIKI — `<nowiki>X</nowiki>` wiki-escape; unwrap to the literal inner.
    "NOWIKI": lambda raw, inner, ctx, reg: _process_nowiki(raw),
    # INCLUDEONLY — `<includeonly>X</includeonly>` transclusion complement of
    # noinclude: its inner IS the transcluded content → unwrap, keep the recursed
    # inner (non-opaque, so `inner` is already walked).
    "INCLUDEONLY": lambda raw, inner, ctx, reg: inner,
    # COORDINATES — `{{EB1911 Coordinates|D|M[|S]|H}}` geographic coordinate → D°M′[S″]H.
    "COORDINATES": lambda raw, inner, ctx, reg: _process_coordinates(inner),
    # CITE — `{{cite|Work Title}}` → «I»title«/I»; the framework already recursed any
    # embedded `[[link]]` in the (placeholdered) inner.
    # CITE — generated from `_PR_WRAP` below (peel/recurse/wrap).
    # SUBSUP — `{{sub|x}}`/`{{sup|x}}` typography (out of `_convert_sub_sup`).
    "SUBSUP": lambda raw, inner, ctx, reg: _process_subsup(raw, ctx),
    # MATH_EQUATION — one label for the labeled-display-equation templates
    # (equation / MathForm1 / ne): a decompose node of a NUMBER cell + a BODY
    # cell the producer reassembles into the `«EQN:…»` block.  (`<math>` tags
    # inside stay opaque leaves — that's the math-qua-math bit.)
    "MATH_EQUATION": _process_math_equation,
    # CONTRIBUTOR_FOOTER — the `{{EB1911 footer …}}` signature.  Renders ONLY the initials
    # signoff a reader sees (right-aligned parenthetical); the byline binding is harvested
    # separately by extract_contributors off those initials, via the vol-29/front-matter index.
    "CONTRIBUTOR_FOOTER": lambda raw, inner, ctx, reg:
        _process_contributor_footer(raw),
    # The «LN:target|display» link family (EB1911_ARTICLE_LINK / TARGET_FIRST_LINK / AUTHOR_LINK /
    # EB1911_SELFREF / FRAGMENT_LINK / INTRA_ARTICLE_LINK / WIKILINK) is generated from `_PR_WRAP`
    # below (peel/recurse/wrap): `_link_display` peels the display, each `_wrap_*` parses the
    # target from raw and emits «LN».
    # Spacer leaves — em/gap/clear/anchor/ditto/dhr/rule → atomic char/marker.
    "SPACER": lambda raw, inner, ctx, reg: process_spacer(raw),
    # HANGING_INDENT — `{{hi|W|text}}` / `{{hanging indent|W|text}}` / `{{outdent|text}}`:
    # render the block at the source's own indent width (default 2em), recurse the text.
    "HANGING_INDENT": process_hanging_indent,
    # BRACE — `{{brace2|N|dir}}`: render a row-spanning curly brace glyph, not the
    # leaked `N|dir` arguments (and not nothing).
    "BRACE": lambda raw, inner, ctx, reg: process_brace(raw),
    # LANG — `{{greek|…}}` / `{{polytonic|…}}` / …: unwrap to content (glyphs are text).
    # LANG — generated from `_PR_WRAP` (peel/recurse/wrap).
    # COORD — `{{11co|DEG|[MIN|]DIR}}`: render the lat/long value.
    "COORD": lambda raw, inner, ctx, reg: process_coord(raw),
    # DOUBLE_BRACE_LEAK — a template we don't handle yet: emit it RAW so it leaks
    # visibly (surfaces in the audit), never crashing the walk.
    "DOUBLE_BRACE_LEAK": lambda raw, inner, ctx, reg: raw,
    # REFS — a footnote-list emitter (smallrefs / reflist / ref / blockref) → empty
    # (footnotes render inline in this edition).
    "REFS": lambda raw, inner, ctx, reg: process_refs(raw, ctx),
    # TOC_ROW — one `{{Dotted TOC line}}` / `{{Dotted TOC page listing}}` /
    # `{{TOC line}}` dotted-leader row: a decompose node of a left-label CELL and a
    # right-value CELL, rendered in place inside whatever fences it ({|, <div>, block-center).
    "TOC_ROW": process_toc_row,
    # SPLIT_WORD — `{{hws}}`/`{{hwe}}`/`{{lps}}`/`{{lpe}}`: a page-split word; the
    # start marker rejoins it, the end marker renders empty.
    # SPLIT_WORD — generated from `_PR_WRAP` (peel/recurse/wrap).
    # MAIN_OTHER — `{{main other|main|other}}`: keep param 1 (main-namespace copy).
    # MAIN_OTHER — generated from `_PR_WRAP` (peel/recurse/wrap).
    # MISSING — a missing-asset placeholder → a visible `[missing …]` stub.
    "MISSING": lambda raw, inner, ctx, reg: process_missing(raw, ctx),
    # Content extractors — tooltip/abbr carry the hint as «SPAN[title:…]»;
    # lang/sic/dropinitial/fqm unwrap to the display arg.
    "CONTENT_EXTRACT": lambda raw, inner, ctx, reg:
        process_content_extract(inner, ctx),
    "POEM": lambda raw, inner, ctx, reg: _process_poem(inner, ctx),
    "PPOEM": _process_ppoem,
    "HIEROGLYPH": lambda raw, inner, ctx, reg:
        f"[hieroglyph: {inner}]",
    # OUTLINE is now a composite: the classifier built nested OUTLINE_ITEM children,
    # so the producers just fold their already-produced markers — `«OUTLINE»…«/OUTLINE»`
    # around the items, `«OLI:depth»…«/OLI»` around each item's content and the deeper
    # items it owns.  The render walks that structure; nothing is flattened.
    "OUTLINE": lambda raw, inner, ctx, reg: f"«OUTLINE»{inner}«/OUTLINE»",
    "OUTLINE_ITEM": lambda raw, inner, ctx, reg: f"«OLI:{raw}»{inner}«/OLI»",
    "PAGE": lambda raw, inner, ctx, reg: raw,  # leaf — re-emit the page marker
    # TITLE — the «TITLE»…«/TITLE» stamp from preprocess_article.  A COMPOSITE:
    # `_classify_title_composite` decomposed the joint-stripped heading into child nodes;
    # the producer substitutes their markers (no produce-time re-walk).  The viewer renders
    # the node in-stream as the H1; `walk_article` decodes it to the plain `title` field.
    "TITLE": _process_title,
    # SECTION — `<section begin/end/>` transclusion marker; renders nothing
    # (boundary signal, not content).  Owned element instead of a catch-all
    # HTML strip; the catcher for the honest super-walker (B3).
    "SECTION": lambda raw, inner, ctx, reg: _process_section(raw),
    # ANCHOR — `{{section|…}}` / `{{anchor|…}}` / `{{anchor+|…}}`: Wikisource link
    # targets carried as «ANCHOR:slug|name» (a «SEC» sibling, but kind="anchor" so the
    # TOC ignores it); anchor+ also renders its display text.
    "ANCHOR": lambda raw, inner, ctx, reg: process_anchor(raw, ctx),
    # PAGEQUALITY — `<pagequality level=N user=X />` Wikisource metadata.
    # Previously inside a `<noinclude>` block claimed by NOINCLUDE.  With
    # noinclude wiped in `_transform_text_v2`, this self-closing tag sits
    # naked in body raw; producer renders nothing.
    "PAGEQUALITY": lambda raw, inner, ctx, reg: "",
    # BODY — article-level prose run between extracted elements; the body
    # producer applies markup conversion + body-only finishing.  With BODY
    # owning its bytes as a normal element, article assembly collapses to
    # ordered concatenation of element markers (no body-substrate, no
    # marker-substitution glue layer).
    "BODY": _produce_body,
    # MIRROR_GLYPH — `<span style="…{{mirrorH}}…">content</span>`:
    # a horizontally mirrored glyph (ALPHABET shows ~18 of these for
    # left-right-flipped letters in Etruscan / Italic / Cleonae's E).
    # Producer emits `«MIRROR:content«/MIRROR»` — preserves the mirror
    # SEMANTIC end-to-end so the viewer can apply `transform: scaleX(-1)`.
    # Without this, the catch-all silently dropped the `{{mirrorH}}` CSS
    # and the wrapper-strip removed the `<span>`, leaving glyphs displayed
    # un-mirrored with no signal that they were ever meant to be.
    "MIRROR_GLYPH": lambda raw, inner, ctx, reg:
        f"«MIRROR:{inner.strip()}«/MIRROR»",
}

# Generate the peel/recurse/wrap producers from `_PR_WRAP` — each label is a DATA ROW, not a
# hand-written function.  As families fold in, this loop grows and the dict above shrinks.
for _pr_label in _PR_WRAP:
    _PRODUCER_DISPATCH[_pr_label] = _make_peel_recurse(_pr_label)


# ── ONE figure/image producer ─────────────────────────────────────────────
# Route the ENTIRE figure/image family through the single unified recursive
# producer.  With one producer, the classifier's figure-label distinctions
# (LAYOUT_WRAPPER vs LEGENDED vs CAPTIONED_FIGURE_INLINE vs IMAGE vs …) no longer
# change the output — every one decomposes to the ground identically — so a
# mis-classification (e.g. CHESS's diagram blocks falling into LAYOUT_WRAPPER)
# is harmless: the catch-all can't botch anything when it's wired where
# everything else is.  The old per-label figure producers (_unwrap_layout_table,
# _process_legended_*, _process_unpaired_figure_group, _process_captioned_figure_inline,
# _process_raw_image, _produce_figure) are dead — and the split IMAGE producers
# (_image_leaf / _captioned_image / _img_float_disentangle / _plain_image_disentangle)
# have since collapsed into the ONE `_process_image` (bare `[[File:]]`/crop/raw → leaf;
# any attached caption → one inline-float `«SPAN»` with the caption centred below the plate).


# ── Independent wikitable predicates ──────────────────────────────────
#
# Each function below is a self-contained "is this MY kind of
# wikitable?" check.  Same signature, same input shape, no knowledge
# of any other predicate.  No predicate is defined as "X but not Y" —
# overlaps are resolved by priority in `_TABLE_PREDICATES`, not by the
# code structure.
#
# Adding a new wikitable kind is one new predicate + one entry in
# the registry.  Removing one is one delete in each.  No edits to
# other predicates.  Entanglement is the source of bugs here, so
# entanglement is what the architecture refuses.
def _is_compound_table_pred(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Data table (header has `border`/`rules`/`class=wikitable`) with
    at least one nested TABLE child.  Compound producer parses the
    parallel-row structure from raw."""
    return _is_compound_table(raw, registry)


def _is_chemistry_layout_pred(raw: str, inner: str,
                               registry: ElementRegistry | None) -> bool:
    """A chemistry-reaction / structural-formula layout, recognized by any of:
    a descendant Langle/Rangle bracket IMAGE; the `<big>`-operator + `<sub>`
    formula signal (ACCUMULATOR discharge/energy, acetone); or — the
    element-aware arm — operator-connected molecular formulae in a cell,
    which catches reactions typeset with plain =/+/-> and {{sub}} formulae
    that otherwise fall through to DATA_TABLE / SINGLE_COLUMN.

    Reads RAW (`_raw_inner`), never the placeholderized `inner`/`registry`:
    classification must be invariant under extraction, so a styled `<span>`
    wrapping a Langle/Rangle bracket image or a reaction formula can't blind it
    (the flagless-recursion invariant — [[feedback_walker_on_raw_source]]).  The
    `[[File:[LR]angle…]]` ref is unambiguous in raw, so the old registry walk
    (chosen to avoid a prose false-match) is unnecessary."""
    raw_inner = _raw_inner(raw)
    return (bool(_CHEM_BRACKET_IMG_RE.search(raw_inner))
            or _has_chem_equation_content(raw)
            or _has_chem_reaction_content(raw_inner))


def _raw_inner(raw: str) -> str:
    """Peel a wikitable / HTML-table's OWN outer delimiters, returning the
    raw inner bytes (un-placeholderized).  Mirrors ``strip_outer`` for the
    two table flavors the ICL gate sees, kept local so the gate needs no
    shape argument.  Used to compute "at my own level" signals from raw."""
    s = raw.strip()
    if s.startswith("{|"):
        s = re.sub(r"^\{\|[^\n]*\n?", "", s)
        return re.sub(r"\n?\|\}\s*$", "", s)
    m = re.match(r"<table\b[^>]*>", s, re.IGNORECASE)
    if m:
        return re.sub(r"</table>\s*$", "", s[m.end():], flags=re.IGNORECASE)
    return s


_INLINE_GLYPH_ROW_RE = re.compile(r"(?:^|\n)\s*\|-")  # shared row separator
_HIERO_TAG_RE = re.compile(r"<hiero\b", re.IGNORECASE)


def _is_inline_glyph_wrapper(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """A `{|…|}` with NO `|-` row separators that carries a `<hiero>` glyph —
    transcriber scaffolding used purely to flow hieroglyphs (and the odd
    glyph-image) inline inside a sentence, not a table.

    STRUCTURAL: the only facts consulted are "has a `<hiero>`" and "has zero
    `|-` rows".  Genuine multi-row hieroglyph reference grids (transliteration,
    acrophony, Hebrew-spelling charts) keep ≥1 `|-` row and are NOT caught.
    Verified corpus-wide 2026-06-02: 93 hiero-bearing tables across EGYPT /
    PHARAOH / SCARAB split cleanly by row count — 88 with 0 rows (all
    inline-flow wrappers), 5 with ≥2 rows (all real grids), nothing with
    exactly 1 (clean gap).  See [[project_inline_glyph_wrapper]]."""
    if not _HIERO_TAG_RE.search(raw):
        return False
    return not _INLINE_GLYPH_ROW_RE.search(inner)


# The only `{|`/`<table>` shapes needing a label OTHER than the plain TABLE
# the one producer gives every table: inline-glyph wrappers and chemistry
# layouts (their producers parse raw bytes / need their own label), with the
# compound-table guard claiming TABLE ahead of chem.  Everything else is the
# TABLE default in `_classify_table`.  (There is no second list any more: the
# post-ICL apparatus that "sub-classified" verse / single-column / poem-wrapper
# / brace / data-signal / rowspan ALL emitted TABLE — pure vestige of the
# pre-collapse per-shape producers — deleted, helpers and all, along with the
# ICL/legend shadow recognizer before it.)
_TABLE_LABEL_PREDS: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    (_is_inline_glyph_wrapper,   "INLINE_GLYPHS"),
    (_is_compound_table_pred,    "TABLE"),
    (_is_chemistry_layout_pred,  "CHEMISTRY_LAYOUT"),
]


def _classify_table(raw: str, inner: str,
                     inner_registry: ElementRegistry | None) -> str:
    """Classify a wiki table to its producer-dispatch label.

    Two-stage dispatch:

      1. Pre-ICL predicates (DJVU crops, compound tables, chemistry
         layouts) get priority — their producers parse raw bytes or
         need their own label.
      2. Post-ICL predicates: every remaining table shape is labelled
         TABLE, which the one producer decomposes identically.  Figure
         `{|`-tables are de-recognized — a figure is just a table whose
         cells recurse to image + prose, no separate family gate.

    Adding a new wikitable kind: add a predicate + entry to the
    appropriate list.
    """
    for predicate, label in _TABLE_LABEL_PREDS:
        if predicate(raw, inner, inner_registry):
            return label
    # Every remaining `{|`/`<table>` is one TABLE: the producer decomposes all
    # shapes identically, so there is no sub-distinction to draw.  (The post-ICL
    # predicate apparatus — verse / single-column / poem-wrapper / brace /
    # data-signal / rowspan — ALL emitted this same TABLE label; pure vestige of
    # the pre-collapse per-shape producers, deleted along with its helpers.)
    return "TABLE"


# ── Image-layout subclassification ────────────────────────────────────
#
# Layout wikitables wrapping a single image fall into a few structural
# subclasses we recognize explicitly. Each subclass has a dedicated
# handler that emits `{{IMG:filename|caption}}` plus, when applicable,
# `{{LEGEND:…}LEGEND}` for the structured legend. Classes:
#
#   IMG_INLINE_LEGEND  — image cell + same-line `||` legend items;
#                        caption in a `|colspan=N|…Fig.` row below.
#                        Example: ABBEY vol 1 p. 43 (Fig. 1, Santa Laura).
#   IMG_MULTICOL_LEGEND — image row + caption row + subsequent rows of
#                         `||`-separated (label, text) pairs that need
#                         re-sorting into alphabetic reading order.
#                         Example: ABBEY vol 1 p. 46 (Fig. 5, Cluny).
#   IMG_POEM_LEGEND    — outer table wraps image + caption + a nested
#                        POEM-only layout table (left/right columns of
#                        `<poem>` legends with `{{csc|…}}` subheadings).
#                        Example: ABBEY vol 1 p. 44 (Fig. 3, St Gall).
#
# Anything that doesn't match falls back to the generic layout unwrap
# logic further down.


# Accepts legend labels like:
#   A              single letter
#   P₁             letter + subscript
#   X₁X₁           repeated letter-subscript pair (Abbey_3 X₁X₁, X₂X₂)
#   c,c            comma-separated repeats (Abbey_3 "c,c. Mills.")
#   k,k,k          same, 3 entries
#   1              single digit (Fig. 9 Kirkstall Abbey)
#   10             multi-digit (Fig. 10 Fountains third column)
#   16-19          inclusive range (Fig. 9 "16-19. Uncertain…")
#   c.c            dotted compound abbreviation (HYDROMEDUSAE Fig. 30
#                    "c.c,  Circular canal.")
#   st.c           similar
# Label must start AND end with an alphanumeric; internal chars may
# include letters, digits, subscript chars, a hyphen (for ranges),
# or a period (for compound abbreviations).
# The trailing `.` is required (legends always use "L. text" form).

# Same shape but with an italic-wrapped label.  ARACHNIDA Fig. 31
# uses ``''d'', Chelicera. || ''ad'', Muscle…`` — every cell is a
# complete (italic-label, text) entry instead of an alternating
# label/text pair.
# Strict validator for a parsed legend label — rejects "Steiner," and
# similar chemist-name false positives while accepting "A", "P₁",
# "X₁X₁", "c,c", "k,k,k", "1", "10", "16-19", "c.c", "st.c", etc.
#
# Also accepts multi-word italicized biological abbreviations
# (HYDROMEDUSAE Fig. 30 "c.c,  Circular canal.", SPONGES Fig. 2
# "cl. osc.,  Closed osculum.", etc.) — up to ~20 chars, made of
# short alphanumeric words separated by periods or spaces.

# Latin ligatures used in biological abbreviations (œsophagus, æ for
# ae in taxonomic names) — fold to ASCII so legend-label validation
# accepts them (TUNICATA Fig. 5: `œ` for Oesophagus, `œa` for
# Oesephageal aperture).


# Matches a `Fig. N.` / `Plate N.` caption anywhere in a string.
# Allows up to 10 chars of punctuation/whitespace between `Fig` and
# the number so variants like `{{sc|Fig}}. 8` (period AFTER the `}}`)
# still match.

# Prose-legend entry: `LABEL, text.` or `LABEL. text.` chunk where
# LABEL is a short alphanumeric token (possibly Roman numeral).

# Static lookup: (volume, page) → chart image filename

# ── Public API ────────────────────────────────────────────────────────

def process_elements(text: str,
                     context: ElementContext,
                     _allow_figure: bool = True) -> str:
    """Extract, process, and reassemble all embedded elements.

    Walker–classifier–producer pipeline.  The classifier drives
    recursion: at each level it strips the shape's outer delimiters,
    asks the walker for one-level extracts of the inner, recursively
    classifies each child, then decides its own label.  The producer
    pass walks the classified tree bottom-up emitting markers.

    With BODY as its own element shape, the walker emits SHAPE_BODY for
    every residual prose run between extracted elements; the BODY
    producer (registered like any other) emits its bytes as-is.
    Article assembly is then pure ordered concatenation of element
    markers — no body-substrate, no marker-substitution glue layer.

    Args:
        text: raw wikitext (may contain tables, images, footnotes, etc.)
        context: per-article ElementContext (volume / page used for
            score and chart-image lookups).

    Returns:
        text with all embedded elements processed to their final form
    """
    return process_elements_tree(text, context, _allow_figure)[0]


def process_elements_tree(
    text: str,
    context: ElementContext,
    _allow_figure: bool = True,
) -> tuple[str, dict]:
    """Walk + classify + produce, returning BOTH the assembled body and the
    element tree.

    `tree` is the top-level registry of ClassifiedElement records — each with
    its children populated and `marker` set by the producer pass, keyed by
    placeholder in walker source order.  Consumers that read elements off the
    structure (the export reading TITLE / PAGE / «LN» off the tree instead of
    reparsing the flattened body) use this; callers that only need the body
    call `process_elements`, which is exactly this minus the tree.
    """
    from britannica.pipeline.stages.elements._classifier import (
        classify_article,
        produce_tree,
        substitute_top_level_markers,
    )
    from britannica.pipeline.stages.elements._ref import resolve_ref_bodies

    # Walk + classify (one mutually-recursive pass): the placeholderized body
    # plus a tree of ClassifiedElement records — each knows its label, raw
    # bytes, inner text, and inner registry of classified children.  With
    # BODY-wrap on, the placeholderized text is placeholders only.
    placeholderized_text, tree = classify_article(
        text, _allow_figure=_allow_figure)

    # Order top-level nodes by DOCUMENT position — their placeholder's spot in the
    # placeholderized text — not walker EXTRACTION order.  The outline pre-pass (and any
    # pre-extraction) registers elements out of reading order, so `tree` as built is NOT
    # document-ordered.  `substitute`/`produce` place each node by its own placeholder and
    # are order-independent (body stays byte-identical); a document-order consumer — the
    # tree render — needs reading order, so make the tree carry it.
    tree = {ph: tree[ph] for ph in sorted(tree, key=placeholderized_text.find)}

    # Gather named / continuation footnotes ONCE per article (resolve_ref_bodies' own
    # contract — "runs once per article").  A definition, its `<ref name=X/>` reuses, and its
    # `<ref follow=X>` continuations can each live ANYWHERE in the article, so the gather is
    # only meaningful over the whole tree.  `ref_bodies is None` marks the top-level article
    # call; every nested process_elements (a FRAME's content, a bare styler, a `main other`)
    # already carries this article-wide map in its threaded context and INHERITS it — it must
    # not re-gather its own fragment (that per-fragment gather WAS the styler-local footnote
    # scope, the flattener-era bug).  Threaded into a COPY so the caller's context is untouched.
    context = _dc_replace(context)
    if context.ref_bodies is None:
        context.ref_bodies = resolve_ref_bodies(tree, context)

    # Produce: bottom-up over the tree; child markers substituted into each
    # producer's output by the framework.
    produce_tree(tree, context)

    # Reassemble: substitute markers into the placeholder-only text — ordered
    # concatenation of element markers in walker source order.
    body = substitute_top_level_markers(placeholderized_text, tree)
    return body, tree
