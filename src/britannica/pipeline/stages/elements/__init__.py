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

from britannica.parsers import img_float as _img_float_parser
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.elements._text import (
    _clean_text,
    _convert_inline_sub_sup,
    _strip_br,
)
from britannica.pipeline.stages.elements._ref import (
    _process_ref,
    _process_ref_self,
    _ref_attrs,
    _resolve_ref_bodies,
)
from britannica.pipeline.stages.elements._image import (
    _CSS_CROP_RE,
    _parse_crop_param,
    _process_chart2,
    _process_djvu_crop,
    _process_image,
    _process_image_float,
    _process_image_from_raw,
)
from britannica.pipeline.stages.elements._leaf import (
    _format_structural_formula,
    _is_structural_formula,
    _process_math,
    _process_poem,
    _process_score,
)
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry,
    _PH,
    _next_placeholder_id,
)
from britannica.pipeline.stages.elements._math_layout import (
    _MATH_CELL_RE,
    _is_equation_layout,
    _is_math_dominant_layout,
    _is_math_layout,
    _math_cell_to_latex,
    _parse_math_layout_cells,
    _process_equation_layout,
    _process_math_layout_table,
)
from britannica.pipeline.stages.elements._outline import (
    _OUTLINE_RANGE_HEADER_RE,
    _PAGE_MARKER_PREFIX_RE,
    _extract_outlines,
    _outline_indent_depth,
    _outline_is_bare_emphasis,
    _outline_is_list_shaped,
    _process_outline,
    _strip_page_marker_prefix,
)
from britannica.pipeline.stages.elements._layout import (
    _append_attribution,
    _ascii_fold_label,
    _clean_legend_text,
    _collect_attribution_rows,
    _emit_legend_chunk,
    _entries_look_like_legend,
    _extract_caption_from_colspan_row,
    _extract_poem_legend,
    _find_caption_row_idx,
    _format_legend_entries,
    _image_ph_filename,
    _is_layout_wrapper,
    _looks_like_caption,
    _parse_inline_legend_cell,
    _parse_multicol_legend_row,
    _parse_prose_legend_rows,
    _simple_table_text,
    _strip_cell_attributes,
    _try_image_layout_subclass,
    _unwrap_layout_table,
)
from britannica.pipeline.stages.elements._tables import (
    _extract_subtable_values,
    _is_chemistry_layout,
    _is_html_illustration_wrapper,
    _process_brace_table,
    _process_chemistry_layout,
    _process_complex_table,
    _process_compound_table,
    _process_html_table,
    _process_table,
    _unwrap_html_illustration,
)


# ElementRegistry / _PH / _next_placeholder_id moved to ._registry


# ── Extraction ─────────────────────────────────────��──────────────────

# Extraction order: outermost first.
# Tables can contain all other elements.
# Poems can contain footnotes.
# Images can (rarely) contain footnotes in captions.
# Footnotes, math, and scores are leaf-level or near-leaf.

_EXTRACTORS = [
    # Extraction order: outermost first.
    # HTML elements first (they can contain wiki markup elements).
    # Wiki tables handled separately with balanced matching.
    ("REF_SELF", re.compile(r"<ref\s[^>]*/\s*>", re.IGNORECASE), 0),
    ("REF", re.compile(r"<ref(?:\s[^>]*)?>.*?</ref>", re.DOTALL | re.IGNORECASE), 0),
    ("HTML_TABLE", re.compile(r"<table\b[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE), 0),
    ("POEM", re.compile(r"<poem>.*?</poem>", re.DOTALL | re.IGNORECASE), 0),
    ("MATH", re.compile(r"<math[^>]*>.*?</math>", re.DOTALL | re.IGNORECASE), 0),
    ("SCORE", re.compile(r"<score[^>]*>.*?</score>", re.DOTALL), 0),
    # Wiki markup elements
    # 4 levels of brace nesting — CASTLE Fig. 9 `cap={{Fs85|…}}
    # {{center|{{EB1911 Fine Print|{{sc|Fig.}} 9.…}}}}` is 4 deep.
    ("IMAGE_FLOAT", re.compile(
        r"\{\{(?:img float|figure|FI)\s*\|"
        r"(?:[^{}]|\{\{(?:[^{}]|\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\})*\}\})*"
        r"\}\}",
        re.DOTALL | re.IGNORECASE), 0),
    ("IMAGE", re.compile(
        r"\[\[(?:File|Image):([^\]]+)\]\]"
        # Optional caption block on the following line(s).  The line
        # must start with a structurally-unmistakable caption marker
        # (a `<small>`-style HTML tag in front is tolerated):
        #   • `{{sm|…}}` / `{{center|…}}`              — wrapped caption
        #   • `{{sc|Fig…}}` / `{{small-caps|Fig…}}` / `{{Fine|Fig…}}`
        #   • `Fig. N` / `Plate N`                     — caption opener
        #   • `N.—…` / `N.–…`                          — numbered caption
        #     (`N.` then an em-/en-dash — the EB1911 caption shape;
        #     distinct from a numbered body section `N. Title…`)
        # A *second* caption line is allowed so 2-line caption blocks
        # stay together — and an attribution line (`From / After /
        # Photo / Copyright / Modified …`) only counts when it is
        # immediately followed by such a caption line, never alone
        # (WEAVING Fig. 29 `<small>From Roth's…</small><br/>\n{{center|
        # {{sc|Fig}} 29—Loom from Sarawak.}}`).  This is what stops a
        # plain `From …` body paragraph from being eaten as the caption
        # (AFGHANISTAN's run-on caption) — and the dropped
        # `\d+\.\s*[A-Z]` trigger is what stops a numbered body section
        # (`42. Successive Division…`) from being read as a caption.
        r"(?:\s*\n\n?("
        r"(?:<[a-z]+[^>\n]*>\s*)?"
        r"(?:"
        # caption line, optionally followed by a 2nd caption line
        r"(?:\{\{sm\||\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
        r"|Fig[. ]\d|Plate\s|\d+\.\s*[—–])"
        r"[^\n]*"
        r"(?:\n(?:<[a-z]+[^>\n]*>\s*)?"
        r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
        r"|Fig[. ]\d|\d+\.\s*[—–])"
        r"[^\n]*)?"
        r"|"
        # attribution line — only if a caption line follows it
        r"(?:From|After|Photo|Copyright|Modified)\s[^\n]*"
        r"\n(?:<[a-z]+[^>\n]*>\s*)?"
        r"(?:\{\{center\||\{\{(?:sc|small-caps|Fine)\|Fig[.}]"
        r"|Fig[. ]\d|\d+\.\s*[—–])"
        r"[^\n]*"
        r")"
        r"))?",
        re.IGNORECASE), 0),
    ("HIEROGLYPH", re.compile(
        r"\{\{hieroglyph\|([^{}]*)\}\}", re.IGNORECASE), 0),
    ("HIEROGLYPH", re.compile(
        r"<hiero>([^<]*)</hiero>", re.IGNORECASE), 0),
]



# Range-style header: `N–M.—TITLE.<br />` (GEM plate captions, similar
# numbered-section openers).  Acts as a top-level label (depth 0) for
# the indented numbered items that follow.  Required to be SHORT and
# end with `<br />` so prose paragraphs that happen to start with
# `1–5.` don't false-match.

def _is_compound_table(table_text: str) -> bool:
    """Detect data tables with nested sub-tables in cells.

    These are outermost tables with data signals (border/rules/class) that
    contain nested {|...|} blocks.  They need dedicated processing to zip
    parallel sub-table rows together.
    """
    header = table_text.split("\n", 1)[0]
    has_data = bool(
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"[^"]*(?:wikitable|tablecolhd|border)',
                   header, re.IGNORECASE))
    if not has_data:
        return False
    # Check for nested table (depth > 1)
    depth = 0
    for m in re.finditer(r"\{\||\|\}", table_text):
        if m.group() == "{|":
            depth += 1
            if depth > 1:
                return True
        else:
            depth -= 1
    return False


# Spans whose `{|` / `|}` are NOT wiki-table syntax — LaTeX inside
# <math>…</math> (`\frac{|…}`), verbatim <nowiki>…</nowiki>, and
# commented-out <!--…-->.  These get extracted to placeholders just
# after this function runs (see extract()), so masking them here only
# stops the balanced-table scan from reading a stray brace-pipe as a
# table opener/closer — which silently swallowed ~5000 words of
# INFINITESIMAL CALCULUS (vol 14 p577 `\frac{|\partial f}`) and shifted
# table pairings in NUMBER (vol 19 p879 `\frac{R}{|√Δ|}`).  <ref> is
# deliberately NOT masked: footnote bodies legitimately carry tables
# this extractor must find.
_NON_TABLE_BRACE_SPAN_RE = re.compile(
    r"<math\b[^>]*>.*?</math\s*>"
    r"|<nowiki\b[^>]*>.*?</nowiki\s*>"
    r"|<!--.*?-->",
    re.DOTALL | re.IGNORECASE,
)


def _extract_balanced_tables(text: str, registry: ElementRegistry) -> str:
    """Extract wiki tables using balanced {| |} matching.

    Handles nested tables correctly by finding outermost {| first.
    Tables containing {{Css image crop}} are registered as DJVU_CROP
    so they get image processing instead of table processing.

    Brace-pipes inside <math>/<nowiki>/comment spans are masked off
    before the scan (see _NON_TABLE_BRACE_SPAN_RE) so LaTeX like
    `\\frac{|…}` isn't misread as a table boundary.
    """
    while True:
        # Re-derive each iteration: extracting a table mutates `text`.
        # Same length as `text` (spans replaced by equal-length spaces),
        # so offsets are interchangeable — scan on `masked`, slice the
        # extracted table out of the original `text`.
        masked = _NON_TABLE_BRACE_SPAN_RE.sub(
            lambda m: " " * len(m.group(0)), text)

        # Find the first {| that isn't already inside a placeholder
        idx = masked.find("{|")
        if idx < 0:
            break

        # Find the balanced |} by tracking depth. Skip over
        # ``{{…}}`` template blocks entirely — e.g. ``{{Ts|vmi|}}``
        # contains ``|}}`` which would otherwise be read as a table
        # closer, truncating the table mid-row (ATMOSPHERIC
        # ELECTRICITY Table IV).
        #
        # Also accept ``</table>`` as an alternative closer.  A handful
        # of Wikisource pages mix syntaxes — open with ``{|`` but close
        # with ``</table>`` (POST, vol22 ws 208).  MediaWiki tolerates
        # this; we have to too, otherwise the table leaks as raw
        # wikitable markup.
        depth = 0
        i = idx
        found = False
        while i < len(text) - 1:
            if masked[i:i+2] == "{{":
                # Skip to matching }} (nested-brace aware).
                tdepth = 1
                j = i + 2
                while j < len(text) - 1 and tdepth > 0:
                    if masked[j:j+2] == "{{":
                        tdepth += 1
                        j += 2
                    elif masked[j:j+2] == "}}":
                        tdepth -= 1
                        j += 2
                    else:
                        j += 1
                if tdepth == 0:
                    i = j
                else:
                    # Unbalanced `{{` (malformed source — e.g. POLYZOA
                    # has `{{sm|(After Braem.)` with no matching `}}`).
                    # Walking to end-of-text would abort the outer
                    # table-extraction loop and strand every subsequent
                    # `{|...|}` table in the article. Treat the `{{` as
                    # literal and keep scanning for the table close.
                    i += 2
                continue
            if masked[i:i+2] == "{|":
                depth += 1
                i += 2
            elif masked[i:i+2] == "|}" or (
                masked[i:i+8].lower() == "</table>" and depth > 0
            ):
                depth -= 1
                # Compute length of the closer we matched.
                closer_len = 2 if masked[i:i+2] == "|}" else 8
                if depth == 0:
                    # Found the balanced close
                    table_text = text[idx:i + closer_len]
                    # Classify at extraction time
                    if re.search(r"\{\{Css image crop", table_text, re.IGNORECASE):
                        etype = "DJVU_CROP"
                    elif _is_compound_table(table_text):
                        etype = "COMPOUND_TABLE"
                    else:
                        etype = "TABLE"
                    placeholder = registry.add(etype, table_text)
                    text = text[:idx] + placeholder + text[i + closer_len:]
                    found = True
                    break
                i += closer_len
            else:
                i += 1

        if not found:
            break  # Unbalanced — stop

    return text


_CHART2_RE = re.compile(
    r"(?:\{\{missing table\}\}\s*(?:\x01PAGE:\d+\x01)?\s*)?"  # consume missing-table + page marker
    r"(?:\{\{center\|[^}]*\}\}\s*)?"  # consume preceding centered title
    r"(?:\{\{EB1911 fine print/s\}\}\s*)?"  # consume fine-print wrapper start
    r"\{\{chart2/start[^}]*\}\}.*?\{\{chart2/end\}\}"
    r"(?:\s*<poem>.*?</poem>)?"  # consume garbled OCR text after chart
    r"(?:\s*\{\{EB1911 fine print/e\}\})?",  # consume fine-print wrapper end
    re.DOTALL | re.IGNORECASE,
)


def extract(
    text: str, _outline_pass: bool = True,
) -> tuple[str, ElementRegistry]:
    """Extract all embedded elements from text, outermost first.

    Returns the text with placeholders and a registry of extracted elements.

    `_outline_pass` is set to False on recursive calls (when processing
    an OUTLINE element's own inner content), to prevent the outline
    detector from re-finding itself and recursing forever.
    """
    registry = ElementRegistry()

    # Chart2 genealogical trees — extract before tables
    text = _CHART2_RE.sub(
        lambda m: registry.add("CHART2", m.group(0)), text)

    # Wiki tables first (outermost) — balanced matching handles nesting
    text = _extract_balanced_tables(text, registry)

    # POEM placeholdered before outline detection so verse stanzas
    # with `{{em|N}}` indent (CHANT ROYAL etc.) don't false-trigger
    # as taxonomic outlines.  Also REF and MATH out of the way for
    # the same reason — multi-line equations / footnote bodies can
    # contain indented continuation lines.
    _PRE_OUTLINE_TYPES = {"POEM", "REF", "REF_SELF", "MATH"}
    for element_type, pattern, _flags in _EXTRACTORS:
        if element_type not in _PRE_OUTLINE_TYPES:
            continue
        text = pattern.sub(
            lambda m, et=element_type: registry.add(et, m.group(0)),
            text,
        )

    # Hierarchical taxonomic / genealogical / classificatory outlines
    # — `:`-indented prose, `{{em|N}}` typesetter indent, etc.
    # ARACHNIDA Tabular Classification, ZOOLOGY taxonomies, GEM plate
    # captions, ~30+ pages corpus-wide.  Run BEFORE the IMAGE
    # extractor so a plate-caption outline (numbered items beneath
    # `[[File:…]]`) isn't consumed as the image's inline caption.
    if _outline_pass:
        text = _extract_outlines(text, registry)

    # Remaining elements (images, html-tables, hieroglyph, image-float)
    for element_type, pattern, _flags in _EXTRACTORS:
        if element_type in _PRE_OUTLINE_TYPES:
            continue
        text = pattern.sub(
            lambda m, et=element_type: registry.add(et, m.group(0)),
            text,
        )

    return text, registry


# ── Processing ────────────────────────────────────────────────────────

def _strip_delimiters(element_type: str, raw: str) -> str:
    """Strip the outer delimiters of an element, returning inner content."""
    if element_type == "TABLE":
        # {| ... |}
        s = re.sub(r"^\{\|[^\n]*\n?", "", raw)
        s = re.sub(r"\n?\|\}\s*$", "", s)
        return s
    elif element_type == "REF_SELF":
        return ""
    elif element_type == "REF":
        return re.sub(r"<ref(?:\s[^>]*)?>|</ref>", "", raw, flags=re.IGNORECASE).strip()
    elif element_type == "POEM":
        return re.sub(r"</?poem>", "", raw, flags=re.IGNORECASE).strip()
    elif element_type == "MATH":
        return re.sub(r"<math[^>]*>|</math>", "", raw, flags=re.IGNORECASE).strip()
    elif element_type == "SCORE":
        return re.sub(r"<score[^>]*>|</score>", "", raw, flags=re.IGNORECASE).strip()
    elif element_type == "IMAGE":
        m = re.match(r"\[\[(?:File|Image):(.+)\]\](?:\s*\n\n?(.+))?$", raw,
                      re.IGNORECASE | re.DOTALL)
        if m:
            inner = m.group(1)
            ext_caption = m.group(2)
            if ext_caption:
                inner = inner + "|EXTCAP:" + ext_caption
            return inner
        return raw
    elif element_type == "HTML_TABLE":
        s = re.sub(r"^<table\b[^>]*>", "", raw, flags=re.IGNORECASE)
        s = re.sub(r"</table>\s*$", "", s, flags=re.IGNORECASE)
        return s
    elif element_type == "OUTLINE":
        # OUTLINE has no outer delimiter — the raw IS the indented
        # lines. Pass through.
        return raw
    elif element_type == "HIEROGLYPH":
        s = re.sub(r"^\{\{hieroglyph\||\}\}$", "", raw, flags=re.IGNORECASE)
        s = re.sub(r"^<hiero>|</hiero>$", "", s, flags=re.IGNORECASE)
        return s
    elif element_type == "IMAGE_FLOAT":
        # Strip outer {{ and }}
        s = re.sub(r"^\{\{", "", raw)
        s = re.sub(r"\}\}$", "", s)
        return s
    return raw


def _process_element(element_type: str, raw: str,
                     text_transform, context: ElementContext) -> str:
    """Process a single element recursively.

    Args:
        element_type: TABLE, IMAGE, REF, POEM, MATH, SCORE, IMAGE_FLOAT
        raw: the raw wikitext of the element
        text_transform: function to transform plain wikitext (bold, italic, etc.)
        context: per-article ElementContext (volume / page / ref_bodies / crop counters)
    """
    # DJVU_CROP, CHART2, SCORE, and COMPOUND_TABLE are self-contained —
    # process from raw, no recursive child extraction.
    if element_type == "DJVU_CROP":
        return _process_djvu_crop(raw, text_transform, context)
    if element_type == "CHART2":
        return _process_chart2(raw, context)
    if element_type == "SCORE":
        return _process_score(raw, context)
    if element_type == "COMPOUND_TABLE":
        return _process_compound_table(raw, text_transform)

    # Strip outer delimiters to get the element's inner content,
    # then recursively extract and process any child elements.  Skip
    # the outline-extraction pass when processing an OUTLINE — its
    # raw content is already the body of one.
    inner = _strip_delimiters(element_type, raw)
    inner, inner_registry = extract(inner, _outline_pass=(element_type != "OUTLINE"))

    # Process children but DON'T reinsert yet — keep placeholders
    # so the parent processor sees opaque markers, not expanded content.
    processed_children = {}
    for key, (child_type, child_raw) in inner_registry.elements.items():
        processed_children[key] = _process_element(
            child_type, child_raw, text_transform, context)

    # Process this element with placeholders still in place
    if element_type == "MATH":
        result = _process_math(inner)
    elif element_type == "REF_SELF":
        result = _process_ref_self(raw, context.ref_bodies)
    elif element_type == "REF":
        result = _process_ref(raw, inner, text_transform,
                              context.ref_bodies)
    elif element_type == "IMAGE":
        result = _process_image(inner, text_transform)
    elif element_type == "IMAGE_FLOAT":
        result = _process_image_float(inner, text_transform)
    elif element_type == "POEM":
        result = _process_poem(inner, text_transform)
    elif element_type == "HIEROGLYPH":
        result = f"[hieroglyph: {inner}]"
    elif element_type == "TABLE":
        table_kind = _classify_table(raw, inner, inner_registry)
        if table_kind == "MATH_LAYOUT":
            result = _process_math_layout_table(raw)
        elif table_kind == "EQUATION_LAYOUT":
            result = _process_equation_layout(inner, text_transform)
        elif table_kind == "LAYOUT_WRAPPER":
            result = _unwrap_layout_table(inner, text_transform, inner_registry)
        elif table_kind == "COMPLEX_HTML":
            result = _process_complex_table(inner, text_transform)
        elif table_kind == "CHEMISTRY_LAYOUT":
            result = _process_chemistry_layout(
                inner, text_transform, inner_registry)
        else:
            result = _process_table(inner, text_transform, inner_registry)
    elif element_type == "HTML_TABLE":
        result = _process_html_table(raw, inner, text_transform, inner_registry)
    elif element_type == "OUTLINE":
        result = _process_outline(inner, text_transform)
    else:
        result = inner

    # Reinsert processed children.  A single pass isn't enough: a
    # processed child may itself contain placeholders for siblings
    # (e.g. ``<poem>`` with a ``<ref>`` inside — the poem processor
    # carries the REF placeholder through opaque, because its inner
    # ``extract()`` can't see past the ``\x03`` wrapper).  If we only
    # substitute in registry order, such a re-introduced placeholder
    # rides out into the parent's result and leaks.  Iterate until
    # the result stops changing.
    for _pass in range(5):
        changed = False
        for key, processed_child in processed_children.items():
            if key in result:
                result = result.replace(key, processed_child)
                changed = True
        if not changed:
            break

    return result



def _classify_table(raw: str, inner: str, inner_registry: ElementRegistry | None) -> str:
    """Classify a wiki table into its processing type.

    Returns one of:
        EQUATION_LAYOUT — math alignment (mostly MATH placeholders or spacer-heavy)
        LAYOUT_WRAPPER  — image+caption wrapper or nested table wrapper
        PLATE_LAYOUT    — `summary="Illustration"` multi-image grid (plate)
        COMPLEX_HTML    — tables with rowspan that need HTML passthrough
        CHEMISTRY_LAYOUT — 2-D chemical-reaction / structural-formula diagram
        DATA_TABLE      — regular data tables (default)
    """
    # Chemistry-reaction / structural-formula layout — atom-label cells,
    # ⟨/⟩ valence-bracket images, `||` bond-lines, ⟶ arrows, `rowspan`
    # brackets.  Priority over every other classification (the
    # `[[File:Langle/Rangle]]` ref is chemistry-exclusive).
    if _is_chemistry_layout(raw):
        return "CHEMISTRY_LAYOUT"
    # Complex HTML: tables with rowspan/colspan need full HTML rendering.
    # Check this BEFORE equation layout — rowspan is a strong signal of a
    # real data table, and {{ts}} stripping can create phantom empty cells
    # that fool the equation layout heuristic.
    has_rowspan = bool(re.search(r"rowspan\s*=", raw, re.IGNORECASE))
    has_colspan = bool(re.search(r"colspan\s*=", raw, re.IGNORECASE))
    # Layout wrappers first — image+caption tables often use colspan for centering
    if _is_layout_wrapper(raw, inner, inner_registry):
        return "LAYOUT_WRAPPER"
    # Complex HTML: tables with rowspan or colspan need full HTML rendering.
    # Check BEFORE equation layout — rowspan is a strong signal of a
    # real data table, and {{ts}} stripping can create phantom empty cells
    # that fool the equation layout heuristic.
    if has_rowspan or has_colspan:
        # Exception: brace tables (poem + translation) handle rowspan
        # themselves. Match `{{brace}}` or `{{brace|...}}` specifically —
        # NOT `{{brace2|...}}` (decorative bracket, not a poem layout).
        if re.search(r"\{\{brace(?:\s*\||\s*\})", raw, re.IGNORECASE):
            return "DATA_TABLE"
        # Exception: math-dominant equation layouts that use rowspan
        # only on layout-only cells (decorative `{{brace2}}` brackets,
        # equation-number labels like `(6)`). Every actual equation row
        # holds a single <math> cell; the spans belong to the brace and
        # the (N) label that visually group the system. METEOROLOGY's
        # Navier-Stokes hydrodynamics block is the canonical case —
        # routing to COMPLEX_HTML renders it as a malformed data table.
        if _is_math_dominant_layout(raw, inner, inner_registry):
            return "EQUATION_LAYOUT"
        return "COMPLEX_HTML"
    # Tables with data signals (border/rules/class) AND {{Ts}} templates
    # need COMPLEX_HTML processing — the template markup creates extra
    # pipes and empty cells that break _process_table's simple cell parsing.
    # {{Ts}} alone is not enough (it's also used for layout table styling).
    # Class value may be quoted or unquoted: class="wikitable" or class=_foo.
    header = raw.split("\n", 1)[0]
    has_data_signal = (
        re.search(r'border\s*=\s*"?[1-9]', header, re.IGNORECASE) or
        re.search(r'rules\s*=', header, re.IGNORECASE) or
        re.search(r'class\s*=\s*"?[^"\s]*(?:wikitable|tablecolhd|border)',
                   header, re.IGNORECASE))
    if has_data_signal and re.search(r'\{\{[Tt]s\|', raw):
        return "COMPLEX_HTML"
    if _is_math_layout(raw, inner):
        return "MATH_LAYOUT"
    if _is_equation_layout(inner, inner_registry):
        return "EQUATION_LAYOUT"
    return "DATA_TABLE"



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

def process_elements(text: str, text_transform, context: ElementContext) -> str:
    """Extract, process, and reassemble all embedded elements.

    Args:
        text: raw wikitext (may contain tables, images, footnotes, etc.)
        text_transform: function that transforms plain wikitext to marker format
        context: per-article ElementContext (volume / page used for score
            and chart-image lookups)

    Returns:
        text with all embedded elements processed to their final form
    """
    extracted, registry = extract(text)

    # Transform the body text (everything between elements)
    extracted = text_transform(extracted)

    # Build an article-wide registry of named-ref bodies so
    # ``<ref name=X/>`` self-closing reuses (and ``<ref name=X>body…``
    # definitions whose body comes via a later ``<ref follow=X>…``
    # continuation) resolve to the merged body. Required by MOLECULE
    # p684 where ``Atom<ref name=654f1/>`` is the anchor and the body
    # arrives only via ``<ref follow=654f1>…</ref>`` two paragraphs
    # later. Threaded into ``context`` so ``_process_element`` can
    # forward it to ``_process_ref`` / ``_process_ref_self``.  We copy
    # the caller's context so we don't mutate it.
    context = _dc_replace(context)
    context.ref_bodies = _resolve_ref_bodies(registry, text_transform)

    # Process each element (recursion handles nesting)
    processed_map = {}
    for key, (element_type, raw) in registry.elements.items():
        processed_map[key] = _process_element(
            element_type, raw, text_transform, context)

    # Substitute all processed elements — repeat until stable
    # (handles cross-element references: table placeholder inside a ref)
    result = extracted
    for _pass in range(3):  # max 3 passes for deeply nested cross-refs
        changed = False
        for key, processed in processed_map.items():
            if key in result:
                result = result.replace(key, processed)
                changed = True
            # Also check if the placeholder appears inside other processed elements
            for other_key in processed_map:
                if key in processed_map[other_key]:
                    processed_map[other_key] = processed_map[other_key].replace(
                        key, processed)
        if not changed:
            break

    return result
