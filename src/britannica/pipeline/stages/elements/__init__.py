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

from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._ref import (
    _process_ref,
    _process_ref_self,
    _ref_attrs,
)
from britannica.pipeline.stages.elements._image import (
    _process_chart2,
    _process_image_from_raw,
)
from britannica.pipeline.stages.elements._dual_line import _process_dual_line
from britannica.pipeline.stages.elements._link import (
    process_eb1911_article_link, process_target_first_link,
    process_eb1911_selfref_link)
from britannica.pipeline.stages.elements._spacer import process_spacer
from britannica.pipeline.stages.elements._content import process_content_extract
from britannica.pipeline.stages.elements._ordered_list import _process_ordered_list
from britannica.pipeline.stages.elements._chem import _process_chem_dual_line
from britannica.pipeline.stages.elements._math import (
    _process_math_dual_line,
    _process_math_equation,
)
from britannica.pipeline.stages.elements._leaf import (
    _format_structural_formula,
    _is_structural_formula,
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
    _process_outline,
)
from britannica.pipeline.stages.elements._section import (
    _process_section,
)
from britannica.pipeline.stages.elements._tables import (
    _CHEM_BRACKET_IMG_RE,
    _has_chem_equation_content,
    _has_chem_reaction_content,
    _is_single_column_table,
    _is_verse_table,
    _process_chemistry_layout,
    _process_inline_glyph_wrapper,
    _process_table_unified,
    _table_grid,
    split_wiki_row,
)


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
# (CHART2, DJVU_CROP, COMPOUND_TABLE) use `raw` and ignore the rest.
_ElementHandler = Callable[
    [str, str, ElementContext, "ElementRegistry | None"], str]


def _passthrough_inner(raw, inner, context,
                       inner_registry):
    return inner


def _produce_body(raw, inner, context, inner_registry):
    """BODY producer: emits the residual prose run as-is — no markup pass
    (whitespace collapse, ``\\xa0`` → space, paragraph-break normalization,
    line-leading ``:``/``;`` sigil strip, body-only ``<br>`` → space rule).

    The walker emits SHAPE_BODY for it.  The run is NOT a pure leaf — it
    carries inline element markers («SC», «LN», …) and child placeholders
    the walker produced, which ``produce_tree`` substitutes; an across-the-
    board pass here would reach into content the body doesn't own.  So the
    body transforms nothing and article assembly is pure ordered
    concatenation of element markers.
    """
    return raw


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
    """CENTER paired-wrapper (`{{c/s}}…{{c/e}}` etc.) → «CTR» around the
    transformed inner.  Child placeholders (a figure/table inside the span)
    are preserved for the framework's marker substitution."""
    return _center_wrap(inner)


def _plain_image_disentangle(raw, context):
    """`{{plain image with caption|image=X|align=|width=|caption=Y}}` IS a
    figtable in template spelling — ONE column, TWO cells: the image on top, the
    caption beneath, the whole box floated.  Reconstruct the canonical
    `{|`-figtable and recurse it through the unified table producer, so the image
    cell becomes an IMAGE leaf and the caption cell rides the cell-collapse — held
    WITH the image by the table, not floated away as a loose sibling.  (The first
    figtable routed through `_process_table_unified`.)"""
    from britannica.pipeline.stages.elements._link import (
        _split_top_pipes)
    inner = re.sub(r"\}\}\s*$", "", re.sub(
        r"^\s*\{\{\s*plain image with caption\s*\|", "", raw, flags=re.IGNORECASE))
    params: dict[str, str] = {}
    for part in _split_top_pipes(inner):
        k, eq, v = part.partition("=")
        if eq:
            params[k.strip().lower()] = v.strip()
    fn = re.sub(r"^\s*(?:File|Image):\s*", "", params.get("image", ""),
                flags=re.IGNORECASE)
    wm = re.match(r"(\d+)", params.get("width", ""))
    w = wm.group(1) if wm else None
    align = params.get("align", "")
    box = ("margin-right:auto;margin-left:auto" if align == "center"
           else f"float:{align}" if align else "")
    style = ";".join(x for x in (box, f"width:{w}px" if w else "") if x)
    opener = f'{{|style="{style}"' if style else "{|"
    img_src = f"[[File:{fn}|{w}px]]" if w else f"[[File:{fn}]]"
    cap = params.get("caption", "")
    body = f"|{img_src}\n|-\n|{cap}" if cap else f"|{img_src}"
    return process_elements(f"{opener}\n{body}\n|}}",
                            context, _allow_figure=False)


def _img_float_disentangle(raw, context):
    """`{{img float|file=…|cap=…}}` / `{{figure|…}}` / `{{FI|…}}` — the floated-
    figure template.  A pure image LEAF when captionless; with a caption it's the
    same floated `{|`-figtable the source's own `{|`-figures use (image cell on
    top, caption cell beneath), now routed through `_process_table_unified` so the
    caption cell recurses through the WALKER.  Replaces render_markers'
    `_rows_to_htmltable`, whose caption cell went through body-text
    — the path that blocked the inline sweep.  Same float/width as before."""
    from britannica.parsers import img_float as _imgf
    from britannica.pipeline.stages.elements._image import build_img_marker
    s = raw.strip()
    inner = s[2:-2] if s.startswith("{{") and s.endswith("}}") else s
    parsed = _imgf.parse(inner)
    if parsed is None:
        return ""
    if not parsed.caption:
        return build_img_marker(parsed.filename, None,
                                align=parsed.align, width=parsed.width)
    align = parsed.align or "left"
    box = ("margin-right:auto;margin-left:auto" if align == "center"
           else f"float:{align}")
    style = ";".join(x for x in (box, f"width:{parsed.width}px"
                                 if parsed.width else "") if x)
    img_src = (f"[[File:{parsed.filename}|{parsed.width}px]]"
               if parsed.width else f"[[File:{parsed.filename}]]")
    return process_elements(
        f'{{|style="{style}"\n|{img_src}\n|-\n|{parsed.caption}\n|}}',
        context, _allow_figure=False)


def _image_leaf(raw):
    """Leaf-image TEMPLATE spellings → the `{{IMG:…}}` marker: `{{Css image crop|…}}`
    (a DjVu crop), `{{raw image|…}}` (a DjVu page-ref / filename), and the bare
    `[[File:…]]` / `[[Image:…]]` bracket.  The walker already bounds the template;
    this just maps the three spellings to a filename and emits the leaf.  Any
    caption that follows is its OWN sibling block, recursed in place by the
    dispatch — not folded in here (there is no figure unit to fold into).  The
    bracket's in-`[[…]]` trailing text is ALT and is dropped ([[feedback_no_caption_concept]])."""
    from britannica.pipeline.stages.elements._image import (
        build_img_marker, djvu_crop_filename, _parse_crop_param,
        _RAW_IMAGE_ARG_RE, _RAW_DJVU_REF_RE, _img_marker)
    tmpl = raw.strip()
    if tmpl.startswith("[["):
        return _img_marker(tmpl)
    if re.match(r"\{\{\s*Css\s+image\s+crop", tmpl, re.IGNORECASE):
        fn = djvu_crop_filename(tmpl)
        if fn is None:
            img = _parse_crop_param(tmpl, "Image")
            fn = img.replace(" ", "_") if img else ""
        return build_img_marker(fn) if fn else ""
    m = _RAW_IMAGE_ARG_RE.match(tmpl)
    if not m:
        return ""
    arg = m.group(1).strip()
    dref = _RAW_DJVU_REF_RE.match(arg)
    filename = (f"djvu_vol{int(dref.group(1)):02d}_page{int(dref.group(2)):04d}.jpg"
                if dref else arg)
    return build_img_marker(filename)


def _process_lb(raw):
    """`{{lb-|N}}` → `N lb`, bare `{{lb-}}` → `lb` (pound-weight glyph leaf).
    Relocated verbatim from body-text's `_convert_lb_dash` so it carries in EVERY
    context, not just content that body-text used to process."""
    m = re.match(r"\{\{\s*lb-\s*\|\s*([^{}|]+?)\s*\}\}", raw, re.IGNORECASE)
    return f"{m.group(1).strip()} lb" if m else "lb"


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
    «BR» (matching the prior faithful-figure rendering).  Only TOP-LEVEL `<br>`
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


def _process_styled(raw, inner, context, inner_registry):
    """The ONE styled-wrapper producer: a `<div>`/`<p>`/`<span>` carrying style
    (`{{Ts}}` / `style=` / `align=`).

    Style is orthogonal to structure.  This producer (1) derives the wrapper's
    CSS via the EXISTING `_cell_styles` (the same `{{Ts}}` / inline-`style` /
    `align`/`valign` translator the table cell/opener uses — no second style
    parser), (2) RECURSES the wrapper's inner content through the MAIN dispatch
    (`process_elements(..., _allow_figure=False)`) so a table / MATH / CHEM /
    nested styled-wrapper inside is handled by its OWN producer (not leaked or
    re-classified by a partial second classifier), and (3) wraps the recursed
    content in the marker the viewer decodes via `style_block` — `«CTR»` for a
    pure-centred block, `«DIV[style:CSS]»` / `«SPAN[style:CSS]»` otherwise.

    Subsumes body-text's `_p_ts` / `_div_ts` / `_span_ts` (the `<p>`/`<div>`/
    `<span>` `{{Ts}}`-opener rewriters) and the styled-`<div>`→faithful gate.
    The `_p_ts` image-drop defect is fixed for free: an `<p {{Ts}}>[[Image]]…</p>`
    nested where figure-recognition is off now recurses its inner through
    `process_elements`, so the image is produced rather than dropped."""
    from britannica.pipeline.stages.elements._tables import (
        _cell_styles, style_block, _TEMPLATE_STYLE_RE, _TEMPLATE_STYLE_WRAPPERS,
        _TEMPLATE_PARAM_STYLE_RE, _SHOULDER_HEADING_RE)
    from britannica.pipeline.stages.elements._walker import (
        _SPAN_TITLE_OPEN_RE, _TRANSLIT_CONTENT_RE)
    # `<span title="T">X</span>` — transliteration TOOLTIP when X is Greek/Hebrew (carry T
    # as «SPAN[title:T]», the HTML twin of {{tooltip}}) vs editorial provenance (drop the
    # wrapper, keep X).  Checked FIRST so a styled-AND-titled amendment span is dropped,
    # not carried.  Re-promotes the gutted body-text `_handle_title_spans`.
    sp = _SPAN_TITLE_OPEN_RE.match(raw)
    if sp:
        title = (sp.group("q") or sp.group("uq") or "").strip().replace(
            "]", "").replace("»", "")
        inner_raw = _styled_br_to_marker(
            re.sub(r"</span\s*>\s*$", "", raw[sp.end():], flags=re.IGNORECASE))
        content = process_elements(
            inner_raw, context, _allow_figure=False).strip()
        if title and _TRANSLIT_CONTENT_RE.search(content):
            return f"«SPAN[title:{title}]»{content}«/SPAN»"
        return content  # editorial title → drop the wrapper, keep the content
    # Shoulder heading — `{{EB1911 Shoulder Heading|[width=N|]LABEL}}` / the
    # `…HeadingSmall` / `{{EB9 Margin Note}}` synonyms.  A marginal SECTION label:
    # emit «SH»…«/SH» (what `detect_sections` keys on for the TOC), recursing the
    # LABEL (after the last top-level pipe, dropping `width=N`) so its inner
    # `{{Fs}}` carries as the styler it is.  Replaces the flat
    # `_convert_shoulder_headings`.
    sh = _SHOULDER_HEADING_RE.match(raw)
    if sh:
        from britannica.pipeline.stages.elements._link import (
            _split_top_pipes)
        rest = re.sub(r"\}\}\s*$", "", raw[sh.end():])
        label = _styled_br_to_marker(_split_top_pipes(rest)[-1])
        content = process_elements(
            label, context, _allow_figure=False).strip()
        return f"«SH»{content}«/SH»"
    # Param font-size wrapper — `{{Fs|108%|X}}` / `{{font size|N%|X}}`.  Same
    # styler family, but the size is arg-1.  Split value | content on the first
    # pipe (content keeps its own pipes), recurse the content, carry as an INLINE
    # `«SPAN[style:font-size:…]»` (font-size is inline).  A bare integer arg is a
    # percent.
    pm = _TEMPLATE_PARAM_STYLE_RE.match(raw)
    if pm:
        rest = re.sub(r"\}\}\s*$", "", raw[pm.end():])
        bar = rest.find("|")
        value = (rest[:bar] if bar >= 0 else "").strip()
        inner_raw = _styled_br_to_marker(rest[bar + 1:] if bar >= 0 else rest)
        content = process_elements(
            inner_raw, context, _allow_figure=False).strip()
        size = value + "%" if value.isdigit() else value
        # FS is just a styler — one uniform inline `«SPAN[style:font-size:…]»`,
        # same as every other styler, NOT a special per-context marker.  Structure
        # (a section heading) is carried by «SH», never inferred from this size.
        return style_block(content, css=f"font-size:{size}" if size else "",
                           tag="SPAN")
    # Template-form wrapper — `{{center|…}}` / `{{csc|…}}` / `{{left|…}}` / …
    # The SAME producer as the HTML form: a `{{center|X}}` is handled identically
    # whether X is text, an image, or a table (style ⊥ content).  Peel `{{name|`
    # + the matching trailing `}}` (the walker already balanced the construct),
    # recurse, wrap per the registry spec.
    tm = _TEMPLATE_STYLE_RE.match(raw)
    if tm:
        spec = _TEMPLATE_STYLE_WRAPPERS[tm.group(1).lower()]
        inner_raw = re.sub(r"\}\}\s*$", "", raw[tm.end():])
        inner_raw = _styled_br_to_marker(inner_raw)
        content = process_elements(
            inner_raw, context, _allow_figure=False).strip()
        return style_block(content, css=spec.get("css", ""),
                           tag=spec.get("tag", "DIV"),
                           ctr=spec.get("ctr", False), sc=spec.get("sc", False))
    m = _STYLED_OPEN_RE.match(raw)
    if not m:
        return raw
    tag = m.group(1).lower()
    attrs = m.group(2)
    # Peel the matching close tag off the tail (the walker already balanced the
    # span, so the last `</tag>` is ours).
    inner_raw = raw[m.end():]
    inner_raw = re.sub(rf"</{tag}\s*>\s*$", "", inner_raw, flags=re.IGNORECASE)
    # Carry the wrapper's own (top-level) `<br>` as «BR» before recursing — a
    # styled block's line breaks are meaningful (see `_styled_br_to_marker`).
    inner_raw = _styled_br_to_marker(inner_raw)
    content = process_elements(
        inner_raw, context, _allow_figure=False).strip()
    css = ";".join(_cell_styles(attrs, ""))
    marker_tag = "SPAN" if tag in ("span", "ins") else "DIV"
    return style_block(content, css=css, tag=marker_tag)


def _process_fraction(inner):
    """FRACTION producer: a `{{sfrac|n|d}}`-family styler (sfrac / mfrac / frac /
    over / EB1911 tfrac / …) lifted as a bounded element so its slots RECURSE.
    The `<math>` numerator/denominator are MATH children (the framework
    substitutes their markers); text slots pass through unchanged; then
    ``_render_fraction`` assembles num-over-den.  Replaces body-text's
    ``_expand_fractions`` flatten, which leaked the `{{sfrac|` the moment a slot
    held an extracted element or the template spanned a body-run `\\n\\n`."""
    from britannica.pipeline.stages.elements._fraction import (
        _render_fraction)
    bar = inner.find("|")                       # strip the `name` token
    if bar < 0:
        return inner
    rendered = _render_fraction(inner[bar:])
    if inner[:bar].strip().lower() == "binom":
        # Binomial coefficient — a GROUPED pair (parens), not a bare bar-fraction.
        return f"({rendered})"
    return rendered


# ── Producer dispatch ─────────────────────────────────────────────────
#
# Flat label → producer table.  Both wikitable sub-kinds (returned by
# `classify` for `element_type=TABLE`) and single-label element types
# (returned trivially) coexist in one dict.  Replaces the previous
# two-level dispatch (`_ELEMENT_HANDLERS` → `_dispatch_table` →
# `_TABLE_KIND_HANDLERS`).
_PRODUCER_DISPATCH: dict[str, _ElementHandler] = {
    # Paired-wrapper span (walker SHAPE_CENTER) → «CTR».
    "CENTER": _process_center,
    # STYLED — `<div>`/`<p>`/`<span>` carrying style (`{{Ts}}`/`style=`/`align=`).
    # ONE producer derives the CSS via the shared `_cell_styles`, recurses the
    # inner through the main dispatch, and wraps via `style_block` (`«CTR»` /
    # `«DIV[style]»` / `«SPAN[style]»`).  Subsumes body-text `_p_ts`/`_div_ts`/
    # `_span_ts` and the styled-`<div>`→figure gate.
    "STYLED": _process_styled,
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
        _process_inline_glyph_wrapper(inner, reg),
    # TABLE — every wikitable (`{|`) and HTML `<table>`, whatever its
    # source sub-shape (data grid, single-column text box, verse-quotation,
    # compound/nested, rowspan-complex), decomposes through the ONE
    # recursive table engine.  The classifier's table predicates gate only
    # table-vs-(math/chem/figure/glyph); among themselves they make no
    # distinction the producer cares about, so there is one label and one
    # dispatch entry.
    "TABLE": lambda raw, inner, ctx, reg:
        _process_table_unified(raw, inner, reg, ctx),
    "CHEMISTRY_LAYOUT": lambda raw, inner, ctx, reg:
        _process_chemistry_layout(raw, inner, reg),
    # Single-label kinds — element_type == label.
    # DJVU_CROP — a `{{Css image crop}}` is just another image; the faithful
    # figure producer recognizes it as an image leaf (stateless filename) and,
    # for the `{|`-wrapped form, recurses the caption/attribution cells.
    "DJVU_CROP": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    "CHART2": lambda raw, inner, ctx, reg: _process_chart2(raw, ctx),
    "MATH": lambda raw, inner, ctx, reg: _process_math(inner),
    "SCORE": lambda raw, inner, ctx, reg: _process_score(inner),
    "REF_SELF": lambda raw, inner, ctx, reg:
        _process_ref_self(raw, ctx.ref_bodies),
    "REF": lambda raw, inner, ctx, reg:
        _process_ref(raw, inner, ctx.ref_bodies),
    # IMAGE is just a figure whose raw the old `_process_image` folded (caption
    # into the marker) and refused to recurse (dropping sibling blocks like a
    # following {{center|…}} — SUNDEW Figs 2/4).  Route it through the ONE
    # faithful recursive producer: image → leaf, caption → its own «CTR» block.
    "IMAGE": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    # INLINE_IMAGE is the one image label faithful can't own: an inline glyph
    # needs `align=inline` (so the viewer renders it at source size in the
    # prose flow), and faithful's leaf is label-blind — it works from raw and
    # can't know the walker classified this one inline.  Keep the inline-aware
    # producer (a retained leaf utility) for it.
    "INLINE_IMAGE": lambda raw, inner, ctx, reg:
        _process_image_from_raw(raw, inline=True),
    "RAW_IMAGE": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    "PLAIN_IMAGE": lambda raw, inner, ctx, reg:
        _plain_image_disentangle(raw, ctx),
    "IMAGE_FLOAT": lambda raw, inner, ctx, reg:
        _img_float_disentangle(raw, ctx),
    # DUAL_LINE — `{{dual line|A|B}}`, a pure layout primitive (two-line
    # stack) with PLAIN content (table headers, hyphenations, figure-
    # caption splits).  Chem-shaped / math-shaped variants reclassify
    # as CHEM_DUAL / MATH_DUAL and route to their family producers.
    "DUAL_LINE": lambda raw, inner, ctx, reg:
        _process_dual_line(inner),
    # CHEM_DUAL — a dual_line whose content is element-formula shaped.
    # Routed to chem's inline producer; renders byte-identical with the
    # layout DUAL_LINE today, but the home is right for future chem-
    # specific work (formula validation, structural-formula layout).
    "CHEM_DUAL": lambda raw, inner, ctx, reg:
        _process_chem_dual_line(inner),
    # MATH_DUAL — a dual_line whose content has math signature (italic
    # variables, sub/sup on non-element content).  Routed to math's
    # inline producer; same byte-output as DUAL_LINE today, but lives
    # in math's home for future math-specific rendering.
    "MATH_DUAL": lambda raw, inner, ctx, reg:
        _process_math_dual_line(inner),
    # FRACTION — the `{{sfrac|n|d}}` family, a STYLER lifted as an element so
    # its slots recurse (the dual-line model for a two-slot wrapper).
    "FRACTION": lambda raw, inner, ctx, reg:
        _process_fraction(inner),
    # LB — `{{lb-|N}}` pound-weight glyph leaf (out of body-text's flat re.sub).
    "LB": lambda raw, inner, ctx, reg: _process_lb(raw),
    # SUBSUP — `{{sub|x}}`/`{{sup|x}}` typography (out of `_convert_sub_sup`).
    "SUBSUP": lambda raw, inner, ctx, reg: _process_subsup(raw, ctx),
    # MATH family — walker lifts labeled-display-equation templates
    # because they're declared as math by their template name and
    # have their own paragraph context.  One producer covers all
    # three labels:
    #   * `_process_math_equation` — labeled display equations
    #     (equation / MathForm1 / ne all share the `«EQN:…»` contract;
    #     producer selects per-template arg parsing keyed on name)
    # Inline `{{sfrac|...}}` fractions and `{{sub|x}}` / `{{sup|x}}`
    # stay in body-text — they're typography whose output flows back
    # into prose, and body-text's `_convert_sfrac` / `_convert_sub_sup`
    # own them.
    "MATH_EQUATION": lambda raw, inner, ctx, reg:
        _process_math_equation(inner),
    "MATH_FORMULA_LABELED": lambda raw, inner, ctx, reg:
        _process_math_equation(inner),
    "MATH_NE": lambda raw, inner, ctx, reg:
        _process_math_equation(inner),
    # (CONTRIBUTOR_FOOTER deleted: the footer is a FIELD, not rendered output, so it's
    # cut upstream by `strip_attributions` before the walker — we don't route a
    # never-rendered field through the renderer just to emit "".)
    # EB1911_ARTICLE_LINK — a cross-reference link recursed at the walker: the producer
    # recurses its display so a nested `{{sc|…}}` is carried as «SC», not flat-stripped
    # by body-text (whose `[^{}]*` regex couldn't bound the nested braces).
    "EB1911_ARTICLE_LINK": lambda raw, inner, ctx, reg:
        process_eb1911_article_link(inner),
    # Target-first link siblings — lkpl / 1911link / EB1911 link.  Same recurse-the-
    # display producer, target-first convention.
    "TARGET_FIRST_LINK": lambda raw, inner, ctx, reg:
        process_target_first_link(inner),
    # EB1911_SELFREF — `[[1911 Encyclopædia Britannica/Article#Sec|Disp]]`, the internal
    # cross-link in raw bracket form; same «LN» family as the template links above.
    "EB1911_SELFREF": lambda raw, inner, ctx, reg:
        process_eb1911_selfref_link(inner),
    # Spacer leaves — em/gap/clear/anchor/ditto/dhr/rule → atomic char/marker.
    "SPACER": lambda raw, inner, ctx, reg: process_spacer(raw),
    # Content extractors — tooltip/abbr carry the hint as «SPAN[title:…]»;
    # lang/sic/dropinitial/fqm unwrap to the display arg.
    "CONTENT_EXTRACT": lambda raw, inner, ctx, reg:
        process_content_extract(inner),
    "POEM": lambda raw, inner, ctx, reg: _process_poem(inner),
    "PPOEM": lambda raw, inner, ctx, reg: _process_ppoem(inner),
    "ORDERED_LIST": lambda raw, inner, ctx, reg: _process_ordered_list(raw),
    "HIEROGLYPH": lambda raw, inner, ctx, reg:
        f"[hieroglyph: {inner}]",
    "OUTLINE": lambda raw, inner, ctx, reg: _process_outline(inner),
    "PAGE": lambda raw, inner, ctx, reg: raw,  # leaf — re-emit the page marker
    # SECTION — `<section begin/end/>` transclusion marker; renders nothing
    # (boundary signal, not content).  Owned element instead of a catch-all
    # HTML strip; the catcher for the honest super-walker (B3).
    "SECTION": lambda raw, inner, ctx, reg: _process_section(raw),
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


# ── ONE figure/image producer ─────────────────────────────────────────────
# Route the ENTIRE figure/image family through the single faithful recursive
# producer.  With one producer, the classifier's figure-label distinctions
# (LAYOUT_WRAPPER vs LEGENDED vs CAPTIONED_FIGURE_INLINE vs IMAGE vs …) no longer
# change the output — every one decomposes to the ground identically — so a
# mis-classification (e.g. CHESS's diagram blocks falling into LAYOUT_WRAPPER)
# is harmless: the catch-all can't botch anything when it's wired where
# everything else is.  The old per-label producers (_unwrap_layout_table,
# _process_legended_*, _process_unpaired_figure_group,
# _process_captioned_figure_inline, _process_image*, _process_raw_image,
# _process_plain_image, _process_image_float, _produce_figure) are now dead.


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


_DATA_TABLE_HEADER_RE = re.compile(
    r'border\s*=\s*"?[1-9]'
    r'|rules\s*='
    r'|class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
    re.IGNORECASE,
)

# Image namespace link.  The `File:`/`Image:` namespace disambiguates an
# image from an ordinary `[[wikilink]]`, so a match is reliably an image.
# (Same pattern recurs in ~6 places across the package — `_layout`'s
# `_PROSE_FIG_IMG_RE` etc.; consolidating those is its own cleanup.)
_IMAGE_NS_LINK_RE = re.compile(r"\[\[(?:File|Image):[^\]]*\]\]", re.IGNORECASE)


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


def _mask_nested_tables_all(text: str) -> str:
    """Mask BOTH nested-table flavors — wiki ``{|…|}`` AND HTML
    ``<table>…</table>`` — so a predicate's "at my own level" scan can't
    pick up content that belongs to a nested table.  Masking only the wiki
    form let a nested ``<table><poem>…</poem></table>`` leak its ``<poem>``
    into the poem-wrapper gate (INTERPOLATION, vol 14 — the bug this
    prevents).  Every this-level predicate scan must use this, not the
    wiki-only ``_mask_nested_tables``."""
    from britannica.pipeline.stages.elements._table_decompose import (
        _mask_nested_tables,
    )
    masked, _ = _mask_nested_tables(text)                        # nested {|…|}
    return re.sub(r"<table\b[\s\S]*?</table>", "", masked,
                  flags=re.IGNORECASE)                            # nested <table>


def _top_level_image_present(raw: str) -> bool:
    """True iff an image namespace link (``[[File:…]]`` / ``[[Image:…]]``)
    sits at the table's OWN level — not inside a nested table.

    Reproduces the inner_registry's ``has_image`` (a direct IMAGE /
    INLINE_IMAGE child) from raw alone: peel the outer, mask nested tables
    (so a nested figure's image doesn't count as ours), then look for the
    image namespace.  ``[[File:`` / ``[[Image:`` is unambiguous — no
    wikilink collision — so a surviving top-level match is a direct image
    child.  This is what lets the ICL gate's single-figure signal stop
    reading the registry (the locality invariant for the predicate)."""
    return bool(_IMAGE_NS_LINK_RE.search(
        _mask_nested_tables_all(_raw_inner(raw))))


def _is_brace_table(raw: str, inner: str,
                     registry: ElementRegistry | None) -> bool:
    """Table contains a `{{brace}}` / `{{brace|…}}` template — the
    poem-with-translation layout pattern.  Always rendered as
    DATA_TABLE even when carrying rowspan."""
    return bool(re.search(r"\{\{brace(?:\s*\||\s*\})", raw, re.IGNORECASE))


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


def _has_data_signal_and_ts(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Header carries a data-table signal AND any cell uses `{{Ts}}`
    styling templates.  The combination breaks `_process_table`'s
    cell parsing (Ts adds phantom pipes); needs HTML rendering.

    NOTE (#29): #28 made `_process_table` handle Ts cells, so re-routing the
    PLAIN no-span tables here to DATA_TABLE is the intended knock-out — but the
    attempt surfaced a pre-existing `_process_table` bug (it DROPS a sub-header
    row, e.g. AGRICULTURE's "Average Acreage … Whole Farm | Proportion" table).
    Re-route is parked until that DATA_TABLE row-drop is fixed."""
    header = raw.split("\n", 1)[0]
    has_data_signal = (
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"?[^"\s]*(?:wikitable|tablecolhd|border)',
                  header, re.IGNORECASE))
    return bool(has_data_signal) and bool(
        re.search(r'\{\{[Tt]s\|', raw))


def _has_rowspan_or_colspan(raw: str, inner: str,
                             registry: ElementRegistry | None) -> bool:
    """Cell-spanning attributes — needs HTML passthrough to render
    properly.  Last-resort COMPLEX_HTML route for tables that aren't
    caught by more-specific predicates above (chemistry, layout
    wrapper, math-dominant, brace)."""
    return bool(
        re.search(r"rowspan\s*=", raw, re.IGNORECASE)
        or re.search(r"colspan\s*=", raw, re.IGNORECASE))


def _is_poem_wrapper_pred(raw: str, inner: str,
                           registry: ElementRegistry | None) -> bool:
    """A table whose content is just `<poem>` child(ren) — a centred quotation
    (BELL/BOAT), verse not a table.  STRUCTURAL: ≥1 POEM child, no IMAGE, no
    data-table header (`!` / `<th>` / `<caption>`).  Placed BEFORE
    `_is_layout_wrapper_pred` so poem-wrappers route to VERSE_TABLE rather than
    being swept into the LAYOUT_WRAPPER catch-all.

    Reads RAW, not the registry/placeholders: peel the outer, mask nested
    tables (so only THIS table's own cells are inspected), then look for a
    `<poem>` at this level and require no image and no data-table header.
    Registry-free per the locality invariant, and flip-ready (works the
    same whether `inner` arrives placeholderized or raw)."""
    masked_inner = _mask_nested_tables_all(_raw_inner(raw))
    if "<poem" not in masked_inner.lower():
        return False
    if _top_level_image_present(raw):
        return False
    if (re.search(r"^\s*!", masked_inner, re.MULTILINE)
            or re.search(r"<(?:th|caption)\b", masked_inner, re.IGNORECASE)):
        return False
    # Every cell must be JUST a `<poem>` (+ layout noise) — NO substantive
    # non-poem text.  A caption/legend cell ("Figs. 1-11.—…", BRACHIOPODA)
    # means this is a figure-legend table, not a verse wrapper; routing it
    # here would drop that caption.
    for cells in _table_grid(masked_inner):
        for content in cells:
            content = re.sub(r"<poem\b[^>]*>[\s\S]*?</poem>", "", content,
                             flags=re.IGNORECASE)               # poem body
            content = re.sub(r"\{\{[^{}]*\}\}", "", content)    # Ts / sc / etc.
            content = re.sub(r"«/?[A-Z]+»|&[a-zA-Z]+;|&#\d+;", "", content)
            if re.search(r"[A-Za-z0-9]", content):
                return False
    return True


def _is_verse_table_pred(raw: str, inner: str,
                          registry: ElementRegistry | None) -> bool:
    """A 2-column quotation layout (hanging-quote col1 + verse lines).
    Content-recognition last resort — no structural signal separates verse
    from a 2-column data table.  Placed near the end (before single-col and
    the DATA_TABLE catch-all): only re-labels tables that would otherwise be
    DATA_TABLE."""
    return _is_verse_table(inner)


def _is_single_column_table_pred(raw: str, inner: str,
                                  registry: ElementRegistry | None) -> bool:
    """A `{|…|}` whose every non-empty row holds exactly one content
    cell — a boxed/centred run of text, not a data grid.  Its producer
    renders it as a `«PRE:` block.

    Placed last (just before the DATA_TABLE catch-all) so it only ever
    re-labels tables that would otherwise fall through to DATA_TABLE; it
    never steals a table a more-specific predicate (layout/math/complex)
    already claimed.  This is the first shape lifted out of
    `_process_table`'s hidden dispatch into the classifier."""
    return _is_single_column_table(inner)


def _always_true(raw: str, inner: str,
                  registry: ElementRegistry | None) -> bool:
    """Catch-all — anything reaching here is a regular DATA_TABLE."""
    return True


# Two priority lists, tried in order.  (The old ICL family dispatch
# that sat between them — `_is_icl_family` gate + `_classify_icl_shape`
# sub-labels — is deleted along with the whole shadow recognizer.  A
# figure `{|`-table is no longer a recognized family: it falls through
# to TABLE and the one producer recurses its cells to image + prose,
# exactly like any other table.)
#
#   1. PRE_ICL — more-specific shapes that take precedence because their
#      producer parses raw bytes or needs its own label (DJVU crops,
#      compound tables, chemistry layouts).
#   2. POST_ICL — every remaining table shape, all labelled TABLE.
_PRE_ICL_PREDS: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    (_is_inline_glyph_wrapper,   "INLINE_GLYPHS"),
    (_is_compound_table_pred,    "TABLE"),
    (_is_chemistry_layout_pred,  "CHEMISTRY_LAYOUT"),
]


_POST_ICL_PREDS: list[tuple[Callable[
    [str, str, "ElementRegistry | None"], bool], str]] = [
    # The table-family predicates (poem-wrapper / brace-table / data-
    # signal+ts / rowspan / verse / single-column / catch-all) ALL emit
    # the one "TABLE" label — the producer (`_process_table_unified`)
    # decomposes every table shape identically, so the sub-distinction is
    # meaningless downstream.  Math equation layouts are NOT a special
    # case any more: `<math>` is the raw's own self-labeling leaf, so a
    # table of math cells is just a TABLE whose cells recurse to `<math>`
    # — rendered with its original table structure (the fraction bars the
    # old flattening `_process_equation_layout` silently dropped) intact.
    # Every shape emits TABLE — the one producer decomposes them identically
    # (LAYOUT_WRAPPER collapsed too; the legend/figure shadow recognizer that
    # distinguished these shapes is deleted).
    (_is_poem_wrapper_pred,          "TABLE"),
    (_is_brace_table,                "TABLE"),
    (_has_data_signal_and_ts,        "TABLE"),
    (_has_rowspan_or_colspan,        "TABLE"),
    (_is_verse_table_pred,           "TABLE"),
    (_is_single_column_table_pred,   "TABLE"),
    (_always_true,                   "TABLE"),
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
    for predicate, label in _PRE_ICL_PREDS:
        if predicate(raw, inner, inner_registry):
            return label
    # (ICL sort removed — figure `{|`-tables are de-recognized; they fall through
    # to the post-ICL predicates / TABLE, which the caller treats identically.)
    for predicate, label in _POST_ICL_PREDS:
        if predicate(raw, inner, inner_registry):
            return label
    # Unreachable — `_always_true` catches everything not matched above.
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

    # Article-wide ref-body resolution, threaded into a COPY of context so the
    # REF producer reads it without mutating the caller's context.
    context = _dc_replace(context)
    context.ref_bodies = resolve_ref_bodies(tree, context)

    # Produce: bottom-up over the tree; child markers substituted into each
    # producer's output by the framework.
    produce_tree(tree, context)

    # Reassemble: substitute markers into the placeholder-only text — ordered
    # concatenation of element markers in walker source order.
    body = substitute_top_level_markers(placeholderized_text, tree)
    return body, tree
