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
    process_eb1911_article_link, process_target_first_link,
    process_eb1911_selfref_link, process_author_link,
    process_fragment_link, process_intra_article_link, process_wikilink)
from britannica.pipeline.stages.elements._contributor import (
    _process_contributor_footer)
from britannica.pipeline.stages.elements._spacer import process_spacer
from britannica.pipeline.stages.elements._frame import (
    process_frame, process_refs, process_missing, process_main_other)
from britannica.pipeline.stages.elements._hanging import process_hanging_indent
from britannica.pipeline.stages.elements._brace import process_brace
from britannica.pipeline.stages.elements._lang import process_lang
from britannica.pipeline.stages.elements._coord import process_coord
from britannica.pipeline.stages.elements._splitword import process_split_word
from britannica.pipeline.stages.elements._toc import process_toc_row
from britannica.pipeline.stages.elements._content import process_content_extract
from britannica.pipeline.stages.elements._ordered_list import _process_ordered_list
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
    _process_outline,
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

    PAIRED_WRAPPER is a LEAF (it shares the shape with CHART2, a leaf), so the
    classifier no longer pre-recurses our inner the way it did when CENTER was a
    non-leaf shape.  We therefore recurse the inner OURSELF — but the wrap must run
    on the PLACEHOLDERIZED inner (so `_center_wrap`'s `_CTR_PURE_PH_RE` still sees a
    block child as a bare placeholder, exactly as it did when classify handed the
    placeholderized inner to the producer), THEN the child markers are substituted.
    So we replicate the classify → wrap → produce → substitute order verbatim
    rather than flatten-then-wrap (which would change the pure-block-paragraph and
    body-only cases)."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_STYLE_WRAPPERS, style_block)
    from britannica.pipeline.stages.elements._classifier import (
        classify_article, produce_tree, substitute_top_level_markers)

    # Walk + classify the inner to a placeholderized body + child tree (figures
    # off, exactly like classify's own inner descent did), then produce the tree
    # so each child's marker is populated.
    placeholderized, tree = classify_article(inner, _allow_figure=False)
    produce_tree(tree, context)

    # Wrap on the PLACEHOLDERIZED text (block children are still bare
    # placeholders here), then substitute the produced child markers in.
    m = re.match(r"^\s*\{\{\s*([^{}/]*?)\s*/s\s*\}\}", raw, re.IGNORECASE)
    name = re.sub(r"\s+", " ", m.group(1).strip().lower()) if m else ""
    spec = _TEMPLATE_STYLE_WRAPPERS.get(name)
    if spec and spec.get("css") and not spec.get("ctr"):
        wrapped = style_block(placeholderized.strip(), css=spec["css"],
                              tag=spec.get("tag", "DIV"))
    else:
        wrapped = _center_wrap(placeholderized)   # centring family — unchanged
    return substitute_top_level_markers(wrapped, tree)


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
    """`{{img float|file=…|cap=…}}` / `{{figure|…}}` / `{{FI|…}}` — the floated
    CAPTIONED FIGURE.  The template IS the binding: whatever it holds is the figure,
    so we float exactly that — no guess about which surrounding prose "belongs."
    Captionless → a bare image LEAF (the renderer floats it by carried align).  With
    a caption → image + caption as ONE inline-float unit,
    `«SPAN[style:float:…;width:Npx]»{{IMG}}«BR»<caption>«/SPAN»` (a centred block
    span for align=center).  The span is inline-LEVEL, so it floats INSIDE the
    paragraph and the prose wraps it — there is no `<p>`-breaking block.  NOT a
    synthesized `{|`-table: that table costume was an imposed taxonomy (a figure is
    not tabular), and a `<table>` can't live in a `<p>` — which is exactly what
    guillotined the line above it."""
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
    img = build_img_marker(parsed.filename, None, width=parsed.width)
    # Recurse the WHOLE caption — its own `<br>`s and soft line-wraps ride verbatim
    # (renderable), exactly as the figtable cell carried them.  Splitting on `<br>`
    # and `.strip()`-ing each line eats the inter-line space and any thin-space
    # indent, running words together (CITHARA "(Mus. Pio-Clementino)" → "(Mus.Pio-
    # Clementino)", BRACHIOPODA's numbered key).  The leading «BR» is the only break
    # WE add — the image-to-caption seam; everything below it is the source's.
    cap = process_elements(parsed.caption, context, _allow_figure=False).strip()
    align = parsed.align or "left"
    w = f";width:{parsed.width}px" if parsed.width else ""
    box = (f"display:block;margin-left:auto;margin-right:auto{w}"
           if align == "center" else f"float:{align}{w}")
    return f"«SPAN[style:{box}]»{img}«BR»{cap}«/SPAN»"


def _image_leaf(raw):
    """Leaf-image TEMPLATE spellings → the `{{IMG:…}}` marker: `{{Css image crop|…}}`
    (a DjVu crop), `{{raw image|…}}` (a DjVu page-ref / filename), and the bare
    `[[File:…]]` / `[[Image:…]]` bracket.  The walker already bounds the template;
    this just maps the three spellings to a filename and emits a TRUE leaf.  A
    captioned image (`thumb`/`frame` + caption) is NOT a leaf — it's a wrapper the
    classifier routes to CAPTIONED_IMAGE (`_captioned_image`).  The bracket's
    in-`[[…]]` trailing text is ALT and dropped ([[feedback_no_caption_concept]])."""
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


def _captioned_image(raw, context):
    """A captioned image — MediaWiki `thumb`/`frame`.  The image is a LEAF, the
    CAPTION is the INNER.  On the table model: split the raw bracket into its two
    parts BEFORE recursing — image via `_img_marker`, caption via
    `_thumb_caption_raw` — recurse the WHOLE caption (its own `<br>`s and whitespace
    ride through verbatim), and reassemble `{{IMG:…}}«BR»<caption>`.  The single
    «BR» is the image-to-caption seam; every break below it is the source's own.
    Splitting the caption per-`<br>` and `.strip()`-ing each shard ate inter-line
    whitespace (soft-wrap space, thin-space indents) — a lossy re-flatten of the
    very caption this producer exists to keep intact."""
    from britannica.pipeline.stages.elements._image import _img_marker, _thumb_caption_raw
    cap = process_elements(_thumb_caption_raw(raw), context, _allow_figure=False).strip()
    return _img_marker(raw) + "«BR»" + cap


def _process_lb(inner, context):
    """`{{lb-|N}}` → `N lb`, bare `{{lb-}}` → `lb` (pound-weight glyph ℔ unwrapped
    to literal "lb").  Peel the `lb-` name + optional pipe, then RETURN the quantity
    through the loop (a nested `{{tfrac}}`/`{{sub}}` in N is produced by its own
    producer) — DOUBLE_BRACE is a leaf, so the producer recurses its own content."""
    rest = re.sub(r"^\s*lb-?\s*\|?\s*", "", inner, flags=re.IGNORECASE).strip()
    rest = process_elements(rest, context, _allow_figure=False).strip()
    return f"{rest} lb" if rest else "lb"


def _process_nowiki(raw):
    """`<nowiki>X</nowiki>` → X verbatim: drop the wiki-escape tags, keep the
    literal inner.  Lifted as a walker element (the walker already treats
    `<nowiki>` opaque) so the inner — which may hold a `|` that nowiki exists to
    protect from cell/template splitting — is placeholdered BEFORE structural
    parsing and never re-exposed.  This is why it is NOT a preprocess unwrap,
    which would re-expose that `|` to the walker."""
    inner = re.sub(r"^\s*<nowiki>", "", raw, flags=re.IGNORECASE)
    return re.sub(r"</nowiki>\s*$", "", inner, flags=re.IGNORECASE)


def _process_cite(inner, context):
    """`{{cite|Title}}` → «I»Title«/I» (the work-title italic the template supplies;
    the title is not pre-italicized in source).  Strip the name, then RETURN the title
    through the loop (an embedded `[[link]]`/`{{sc}}` is produced by its own producer),
    italic-wrap — DOUBLE_BRACE is a leaf, so the producer recurses its own content."""
    _name, _bar, title = inner.partition("|")
    title = process_elements(title.strip(), context, _allow_figure=False).strip()
    return f"«I»{title}«/I»" if title else ""


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


def process_span_title(raw, inner, context, inner_registry):
    """SPAN_TITLE producer (walker SHAPE_SPAN_TITLE): a `<span title="T">X</span>`.

    Transliteration TOOLTIP when X is Greek/Hebrew (carry T as «SPAN[title:T]»,
    the HTML twin of {{tooltip}}) vs editorial provenance (drop the wrapper, keep
    X).  Re-promotes the gutted body-text `_handle_title_spans`.  Body is the
    verbatim former `sp = …` branch of `_process_styled`, now unconditional
    (SPAN_TITLE only reaches here when `_SPAN_TITLE_OPEN_RE` matched in the
    walker)."""
    from britannica.pipeline.stages.elements._walker import (
        _SPAN_TITLE_OPEN_RE, _TRANSLIT_CONTENT_RE)
    sp = _SPAN_TITLE_OPEN_RE.match(raw)
    title = (sp.group("q") or sp.group("uq") or "").strip().replace(
        "]", "").replace("»", "")
    inner_raw = _styled_br_to_marker(
        re.sub(r"</span\s*>\s*$", "", raw[sp.end():], flags=re.IGNORECASE))
    content = process_elements(
        inner_raw, context, _allow_figure=False).strip()
    if title and _TRANSLIT_CONTENT_RE.search(content):
        return f"«SPAN[title:{title}]»{content}«/SPAN»"
    return content  # editorial title → drop the wrapper, keep the content


def process_shoulder(raw, inner, context, inner_registry):
    """SHOULDER producer (walker SHAPE_SHOULDER): a shoulder heading —
    `{{EB1911 Shoulder Heading|[width=N|]LABEL}}` / the `…HeadingSmall` /
    `{{EB9 Margin Note}}` synonyms.

    A marginal SECTION label: emit «SH:slug»…«/SH» (what `detect_sections` keys on for
    the TOC), recursing the LABEL (after the last top-level pipe, dropping
    `width=N`) so its inner `{{Fs}}` carries as the styler it is.  Replaces the
    flat `_convert_shoulder_headings`.  Body is the verbatim former `sh = …`
    branch of `_process_styled`, now unconditional (SHOULDER only reaches here
    when `_SHOULDER_HEADING_RE` matched in the walker)."""
    from britannica.pipeline.stages.elements._tables import _SHOULDER_HEADING_RE
    from britannica.pipeline.stages.elements._link import _split_top_pipes
    from britannica.util.strings import section_slug, strip_markers
    sh = _SHOULDER_HEADING_RE.match(raw)
    rest = re.sub(r"\}\}\s*$", "", raw[sh.end():])
    label = _split_top_pipes(rest)[-1]
    # A shoulder heading's `<br>`s are margin-column wrap typography, not
    # content — drop them to a space.  Hyphenation across the wrap is NOT
    # healed here: that is one leaf concern, not scattered into this producer.
    label = re.sub(r"\s*<[Bb][Rr]\b[^>]*>\s*", " ", label)
    label = _styled_br_to_marker(label)
    content = process_elements(
        label, context, _allow_figure=False).strip()
    # Carry the section slug in the marker — minted here once by the one slug
    # function, exactly as «SEC» carries its slug — so the viewer and export
    # read it and nothing downstream recomputes it.
    slug = section_slug(strip_markers(content))
    return f"«SH:{slug}»{content}«/SH»"


def process_running_header(raw, inner, context, inner_registry):
    """RUNNING_HEADER producer (walker SHAPE_RUNNING_HEADER): a running header —
    `{{rh|left|center|right}}` / `{{Running header|…}}` alias.

    A 3-COLUMN frame: plate title bars, captioned figures, and displayed-
    equation layouts (margin label/number | centred equation | number).  Split
    the top-level pipes (a nested `{{…|…}}` / «MATH» stays intact), recurse each
    cell, and emit a flex row so the centre stays centred between the margins.
    Body is the verbatim former `rh = …` branch of `_process_styled`, now
    unconditional (RUNNING_HEADER only reaches here when `_RUNNING_HEADER_RE`
    matched in the walker)."""
    from britannica.pipeline.stages.elements._tables import _RUNNING_HEADER_RE
    from britannica.pipeline.stages.elements._link import _split_top_pipes
    rh = _RUNNING_HEADER_RE.match(raw)
    cells = _split_top_pipes(re.sub(r"\}\}\s*$", "", raw[rh.end():]))
    cells = (cells + ["", "", ""])[:3]
    left, center, right = (
        process_elements(c, context, _allow_figure=False).strip()
        for c in cells)
    return (
        "«DIV[style:display:flex;align-items:baseline]»"
        f"«SPAN[style:text-align:left]»{left}«/SPAN»"
        f"«SPAN[style:flex:1;text-align:center]»{center}«/SPAN»"
        f"«SPAN[style:text-align:right]»{right}«/SPAN»«/DIV»")


def process_html_style(raw, inner, context, inner_registry):
    """HTML_STYLE producer (walker SHAPE_HTML_STYLE): a `<div>`/`<p>`/`<span>`/
    `<ins>` carrying style (`{{Ts}}` / `style=` / `align=`).

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
    `<span>` `{{Ts}}`-opener rewriters) and the styled-`<div>`→figure gate.
    The `_p_ts` image-drop defect is fixed for free: an `<p {{Ts}}>[[Image]]…</p>`
    nested where figure-recognition is off now recurses its inner through
    `process_elements`, so the image is produced rather than dropped.

    Body is the verbatim former `m = _STYLED_OPEN_RE.match(raw)` fallthrough of
    `_process_styled`, keeping its `if not m: return raw` guard."""
    from britannica.pipeline.stages.elements._tables import style_block
    from britannica.pipeline.stages.elements._table_fold import fold_cell_styles
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
    css = ";".join(fold_cell_styles(attrs))
    marker_tag = "SPAN" if tag in ("span", "ins") else "DIV"
    return style_block(content, css=css, tag=marker_tag)


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


def process_strip(raw, inner, context, inner_registry):
    """STRIP producer (walker SHAPE_STRIP): the template-form styler
    `{{center|…}}` / `{{csc|…}}` / `{{left|…}}` / ….

    The SAME producer as the HTML form: a `{{center|X}}` is handled identically
    whether X is text, an image, or a table (style ⊥ content).  Peel `{{name|`
    + the matching trailing `}}` (the walker already balanced the construct),
    recurse, wrap per the registry spec.  Body is the verbatim TEMPLATE branch
    formerly in `_process_styled`, now unconditional (STRIP only reaches here
    when `_TEMPLATE_STYLE_RE` matched in the walker)."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_STYLE_RE, _TEMPLATE_STYLE_WRAPPERS, style_block)
    param_css: list[str] = []
    tm = _TEMPLATE_STYLE_RE.match(raw)
    if tm is not None:                       # `{{name|content}}` — the pipe form
        name = tm.group(1).lower()
        inner_raw = re.sub(r"\}\}\s*$", "", raw[tm.end():])
        # Leading NAMED params (`{{Hebrew|small=yes|א}}`) are presentation, not
        # content — CARRY them as CSS (see `_carry_named_params`), don't drop.
        inner_raw, param_css = _carry_named_params(inner_raw)
    else:                                    # `{{name}}` — bare form: the classifier
        # routes a registered styler with OR without content (see _classifier
        # line ~390), so supply the styler's default content — e.g. `{{0}}` is
        # one invisible width-reserving zero.
        name = re.match(r"\{\{\s*([^|{}]+?)\s*\}\}", raw).group(1).strip().lower()
        inner_raw = _TEMPLATE_STYLE_WRAPPERS[name].get("bare", "")
    spec = _TEMPLATE_STYLE_WRAPPERS[name]
    inner_raw = _styled_br_to_marker(inner_raw)
    content = process_elements(
        inner_raw, context, _allow_figure=False).strip()
    css = ";".join(c for c in [spec.get("css", ""), *param_css] if c)
    # A carried param on a tag-less script wrapper ({{Hebrew}} = {}) decorates an
    # inline glyph → SPAN, not the block DIV default.
    tag = spec.get("tag") or ("SPAN" if param_css else "DIV")
    return style_block(content, css=css, tag=tag,
                       ctr=spec.get("ctr", False), sc=spec.get("sc", False))


def _process_title(raw, context):
    """TITLE producer (the «TITLE»…«/TITLE» stamp from preprocess_article).

    Strip the trailing joint comma off the RAW span BEFORE recursing it
    (transform-then-recurse, like any producer) — so the comma never enters the
    walk and so reaches neither the rendered marker nor the decoded plain field.
    One removal on the shared input; PLAIN and DISPLAY both descend from it clean."""
    from britannica.pipeline.stages.elements._title import strip_title_joint
    inner_raw = re.sub(r"^«TITLE»", "", raw)
    inner_raw = re.sub(r"«/TITLE»\s*$", "", inner_raw)
    inner = process_elements(
        strip_title_joint(inner_raw), context, _allow_figure=False)
    return f"«TITLE:{inner}«/TITLE»"


def process_param(raw, inner, context, inner_registry):
    """PARAM producer (walker SHAPE_PARAM): the param-valued font-size styler
    `{{Fs|108%|X}}` / `{{font size|N%|X}}`.

    Same styler family as STRIP, but the size is arg-1.  Split value | content
    on the first pipe (content keeps its own pipes), recurse the content, carry
    as an INLINE `«SPAN[style:font-size:…]»` (font-size is inline).  A bare
    integer arg is a percent.  Body is the verbatim PARAM branch formerly in
    `_process_styled`, now unconditional (PARAM only reaches here when
    `_TEMPLATE_PARAM_STYLE_RE` matched in the walker)."""
    from britannica.pipeline.stages.elements._tables import (
        _TEMPLATE_PARAM_STYLE_RE, _TEMPLATE_PARAM_STYLE_WRAPPERS, style_block)
    pm = _TEMPLATE_PARAM_STYLE_RE.match(raw)
    name = re.sub(r"\s+", " ", pm.group(1).strip().lower())
    tmpl, pct = _TEMPLATE_PARAM_STYLE_WRAPPERS[name]
    rest = re.sub(r"\}\}\s*$", "", raw[pm.end():])
    bar = rest.find("|")
    value = (rest[:bar] if bar >= 0 else "").strip()
    inner_raw = _styled_br_to_marker(rest[bar + 1:] if bar >= 0 else rest)
    content = process_elements(
        inner_raw, context, _allow_figure=False).strip()
    if pct and value.isdigit():
        value += "%"             # font-size family: bare int is a percent
    elif not value and "letter-spacing" in tmpl:
        value = "0.1em"          # {{lsp||X}}: arg-1 empty → default spacing
    # The styler is one uniform inline `«SPAN[style:…]»`, same as every other
    # styler — the CSS property comes from the registry, the value from arg-1.
    return style_block(content, css=tmpl.format(v=value) if value else "",
                       tag="SPAN")


def _process_fraction(inner, context):
    """FRACTION producer: a `{{sfrac|n|d}}`-family styler (sfrac / mfrac / frac /
    over / EB1911 tfrac / …) lifted as a bounded element so its slots RECURSE.
    Each numerator/denominator slot is RETURNED through the loop (the producer
    transforms its outer — the `name` token + the bar-fraction scaffold — and hands
    each slot to ``process_elements``), so a `{{Greek}}`/`{{sub}}`/`<math>` inside is
    produced by its own producer.  Replaces body-text's ``_expand_fractions`` flatten."""
    from britannica.pipeline.stages.elements._fraction import (
        _render_fraction, render_over_fraction)
    recurse = (lambda s: process_elements(s, context, _allow_figure=False))
    bar = inner.find("|")                       # strip the `name` token
    if bar < 0:
        # Bar-less LaTeX-ish `num \over den` form (`{{1\over 2}}` /
        # `{{\it a \over b}}` / `{{\kappa\over\kappa'}}`) — no `name|` token, the
        # whole inner is the fraction.  (Reaches the producer only via the bare
        # standalone form; in real bodies every `\over` rides inside an opaque
        # `<math>`, so this is the harness-exercised / defensive path.)
        if r"\over" in inner:
            return render_over_fraction(inner, recurse)
        return inner
    rendered = _render_fraction(inner[bar:], recurse)
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
    # HTML_STYLE — `<div>`/`<p>`/`<span>`/`<ins>` carrying style (`{{Ts}}`/`style=`/
    # `align=`).  Derives the CSS via the shared `_cell_styles`, recurses the
    # inner through the main dispatch, and wraps via `style_block` (`«CTR»` /
    # `«DIV[style]»` / `«SPAN[style]»`).  Subsumes body-text `_p_ts`/`_div_ts`/
    # `_span_ts` and the styled-`<div>`→figure gate.
    "HTML_STYLE": process_html_style,
    # STRIP — the template-form styler `{{center|…}}`/`{{csc|…}}`/… (walker
    # SHAPE_STRIP).  Split out of STYLED: its own single-structure producer
    # (verbatim former TEMPLATE branch) peels `{{name|`…`}}`, recurses, wraps.
    "STRIP": process_strip,
    # PARAM — the param-valued styler `{{Fs|N%|X}}`/`{{font size|N%|X}}`/…
    # (walker SHAPE_PARAM).  Split out of STYLED: its own single-structure
    # producer (verbatim former PARAM branch) splits value|content, recurses,
    # wraps inline.
    "PARAM": process_param,
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
    "CHEMISTRY_LAYOUT": lambda raw, inner, ctx, reg:
        _process_chemistry_layout(raw, inner, ctx, reg),
    # Single-label kinds — element_type == label.
    # DJVU_CROP — a `{{Css image crop}}` is just another image; the unified
    # figure producer recognizes it as an image leaf (stateless filename) and,
    # for the `{|`-wrapped form, recurses the caption/attribution cells.
    "DJVU_CROP": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    "CHART2": lambda raw, inner, ctx, reg:
        _process_genealogy(raw, ctx, lambda s: process_elements(s, ctx)),
    "MATH": lambda raw, inner, ctx, reg: _process_math(raw, inner),
    "SCORE": lambda raw, inner, ctx, reg: _process_score(inner),
    "REF_SELF": lambda raw, inner, ctx, reg:
        _process_ref_self(raw, ctx.ref_bodies),
    "REF": lambda raw, inner, ctx, reg:
        _process_ref(raw, inner, ctx.ref_bodies),
    # IMAGE is a pure leaf (`_image_leaf` → an `{{IMG:…}}` marker): the image
    # carries only the raw's own params, and any caption that follows is its OWN
    # sibling block — a `{{center|…}}`, a table cell — recursed in place by the
    # dispatch, never folded into the image (SUNDEW Figs 2/4).
    "IMAGE": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    # CAPTIONED_IMAGE is the WRAPPER form (MediaWiki `thumb`/`frame`): the image is
    # the leaf, the in-bracket caption is the INNER — recursed by `_captioned_image`.
    "CAPTIONED_IMAGE": lambda raw, inner, ctx, reg:
        _captioned_image(raw, ctx),
    "RAW_IMAGE": lambda raw, inner, ctx, reg:
        _image_leaf(raw),
    "PLAIN_IMAGE": lambda raw, inner, ctx, reg:
        _plain_image_disentangle(raw, ctx),
    "IMAGE_FLOAT": lambda raw, inner, ctx, reg:
        _img_float_disentangle(raw, ctx),
    # DUAL_LINE — `{{dual line|A|B}}`, a pure layout primitive (two-line
    # stack: table headers, hyphenations, figure-caption splits, stacked
    # math/chem notation).  ONE producer that recurses each line, so its
    # chem/math content is produced by its own producer — no chem/math-
    # specific dual_line label or producer (see the classifier note: the
    # old CHEM_DUAL / MATH_DUAL split was speculative specificity).
    "DUAL_LINE": lambda raw, inner, ctx, reg:
        _process_dual_line(inner, ctx),
    # FRACTION — the `{{sfrac|n|d}}` family, a STYLER lifted as an element so
    # its slots recurse (the dual-line model for a two-slot wrapper).
    "FRACTION": lambda raw, inner, ctx, reg:
        _process_fraction(inner, ctx),
    # LB — `{{lb-|N}}` pound-weight glyph leaf (out of body-text's flat re.sub).
    "LB": lambda raw, inner, ctx, reg: _process_lb(inner, ctx),
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
    "CITE": lambda raw, inner, ctx, reg: _process_cite(inner, ctx),
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
        _process_math_equation(inner, ctx),
    "MATH_FORMULA_LABELED": lambda raw, inner, ctx, reg:
        _process_math_equation(inner, ctx),
    "MATH_NE": lambda raw, inner, ctx, reg:
        _process_math_equation(inner, ctx),
    # CONTRIBUTOR_FOOTER — the `{{EB1911 footer …}}` signature.  Renders ONLY the initials
    # signoff a reader sees (right-aligned parenthetical); the byline binding is harvested
    # separately by extract_contributors off those initials, via the vol-29/front-matter index.
    "CONTRIBUTOR_FOOTER": lambda raw, inner, ctx, reg:
        _process_contributor_footer(raw),
    # EB1911_ARTICLE_LINK — a cross-reference link recursed at the walker: the producer
    # recurses its display so a nested `{{sc|…}}` is carried as «SC», not flat-stripped
    # by body-text (whose `[^{}]*` regex couldn't bound the nested braces).
    "EB1911_ARTICLE_LINK": lambda raw, inner, ctx, reg:
        process_eb1911_article_link(inner, ctx),
    # Target-first link siblings — lkpl / 1911link / EB1911 link.  Same recurse-the-
    # display producer, target-first convention.
    "TARGET_FIRST_LINK": lambda raw, inner, ctx, reg:
        process_target_first_link(inner, ctx),
    # AUTHOR_LINK — `[[Author:Name|Display]]`.  Routed on the display: a contributor's
    # initials (in ctx.contributor_initials) → render the initials; else → «LN» xref.
    "AUTHOR_LINK": lambda raw, inner, ctx, reg:
        process_author_link(raw, inner, ctx),
    # EB1911_SELFREF — `[[1911 Encyclopædia Britannica/Article#Sec|Disp]]`, the internal
    # cross-link in raw bracket form; same «LN» family as the template links above.
    "EB1911_SELFREF": lambda raw, inner, ctx, reg:
        process_eb1911_selfref_link(raw, ctx),
    # FRAGMENT_LINK — `[[#Section]]`, a bare same-article anchor link → «LN:#Section».
    "FRAGMENT_LINK": lambda raw, inner, ctx, reg:
        process_fragment_link(raw, ctx),
    # INTRA_ARTICLE_LINK — `{{EB1911 intra-article link|Section}}`, its template twin.
    "INTRA_ARTICLE_LINK": lambda raw, inner, ctx, reg:
        process_intra_article_link(inner, ctx),
    # WIKILINK — generic `[[Target]]` cross-reference → «LN», resolved by the ladder.
    "WIKILINK": lambda raw, inner, ctx, reg: process_wikilink(raw, ctx),
    # Spacer leaves — em/gap/clear/anchor/ditto/dhr/rule → atomic char/marker.
    "SPACER": lambda raw, inner, ctx, reg: process_spacer(raw),
    # FRAME — a layout frame (multicol / div-col / outdent / hanging indent /
    # familytree / …).  Drop the presentation scaffolding, recurse + keep content.
    "FRAME": lambda raw, inner, ctx, reg: process_frame(raw, ctx),
    # HANGING_INDENT — `{{hi|W|text}}` / `{{hanging indent|W|text}}`: render the
    # block at the source's own indent width (default 2em), recurse the text.
    "HANGING_INDENT": lambda raw, inner, ctx, reg: process_hanging_indent(raw, ctx),
    # BRACE — `{{brace2|N|dir}}`: render a row-spanning curly brace glyph, not the
    # leaked `N|dir` arguments (and not nothing).
    "BRACE": lambda raw, inner, ctx, reg: process_brace(raw),
    # LANG — `{{greek|…}}` / `{{polytonic|…}}` / …: unwrap to content (glyphs are text).
    "LANG": lambda raw, inner, ctx, reg: process_lang(raw, ctx),
    # COORD — `{{11co|DEG|[MIN|]DIR}}`: render the lat/long value.
    "COORD": lambda raw, inner, ctx, reg: process_coord(raw),
    # DOUBLE_BRACE_LEAK — a template we don't handle yet: emit it RAW so it leaks
    # visibly (surfaces in the audit), never crashing the walk.
    "DOUBLE_BRACE_LEAK": lambda raw, inner, ctx, reg: raw,
    # REFS — a footnote-list emitter (smallrefs / reflist / ref / blockref) → empty
    # (footnotes render inline in this edition).
    "REFS": lambda raw, inner, ctx, reg: process_refs(raw, ctx),
    # TOC_ROW — one `{{Dotted TOC line}}` / `{{Dotted TOC page listing}}` /
    # `{{TOC line}}` dotted-leader row → its content (cells from template params),
    # rendered in place inside whatever fences it ({|, <div>, block-center).
    "TOC_ROW": lambda raw, inner, ctx, reg: process_toc_row(raw, ctx),
    # SPLIT_WORD — `{{hws}}`/`{{hwe}}`/`{{lps}}`/`{{lpe}}`: a page-split word; the
    # start marker rejoins it, the end marker renders empty.
    "SPLIT_WORD": lambda raw, inner, ctx, reg: process_split_word(raw, ctx),
    # MAIN_OTHER — `{{main other|main|other}}`: keep param 1 (main-namespace copy).
    "MAIN_OTHER": lambda raw, inner, ctx, reg: process_main_other(raw, ctx),
    # MISSING — a missing-asset placeholder → a visible `[missing …]` stub.
    "MISSING": lambda raw, inner, ctx, reg: process_missing(raw, ctx),
    # Content extractors — tooltip/abbr carry the hint as «SPAN[title:…]»;
    # lang/sic/dropinitial/fqm unwrap to the display arg.
    "CONTENT_EXTRACT": lambda raw, inner, ctx, reg:
        process_content_extract(inner, ctx),
    "POEM": lambda raw, inner, ctx, reg: _process_poem(inner, ctx),
    "PPOEM": lambda raw, inner, ctx, reg: _process_ppoem(inner, ctx),
    "ORDERED_LIST": lambda raw, inner, ctx, reg: _process_ordered_list(raw),
    "HIEROGLYPH": lambda raw, inner, ctx, reg:
        f"[hieroglyph: {inner}]",
    "OUTLINE": lambda raw, inner, ctx, reg: _process_outline(inner),
    "PAGE": lambda raw, inner, ctx, reg: raw,  # leaf — re-emit the page marker
    # TITLE — the «TITLE»…«/TITLE» stamp from preprocess_article.  Recurses its inner
    # (the carved title span) like any wrapper; produce_tree substitutes the children,
    # so the node carries the fully-walked title.  The viewer renders the node
    # in-stream as the H1; `walk_article` decodes it to the plain `title` field.
    "TITLE": lambda raw, inner, ctx, reg: _process_title(raw, ctx),
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


# ── ONE figure/image producer ─────────────────────────────────────────────
# Route the ENTIRE figure/image family through the single unified recursive
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
