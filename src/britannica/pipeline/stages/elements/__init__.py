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
from dataclasses import dataclass, replace as _dc_replace
from typing import Callable

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
    _is_math_dominant_layout,
    _math_cell_to_latex,
    _math_table_kind,
    _parse_math_layout_cells,
    _process_math_table_layout,
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
    _has_chem_brackets,
    _is_html_illustration_wrapper,
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
    # Extraction order is governed by the registry below (`_EXTRACTOR_REGISTRY`),
    # NOT by this list — `_EXTRACTORS` is the legacy regex table the registry
    # uses to build its regex-based entries.  Per-element ordering, the
    # outline-pass recursion guard, and the wiki-table / chart2 / outline
    # special-case extractors all live in the registry.  Keep this list
    # in regex-grouping order; let the registry decide priority.
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
        html_depth = 0  # tracks nested HTML <table>…</table> blocks
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
            # HTML `<table>` opener / closer — track separately from
            # the wiki `{|` depth so that a `</table>` inside a NESTED
            # HTML table doesn't masquerade as a wiki-table close
            # (INTERPOLATION's outer `{|…|}` contains an HTML
            # `<table>…</table>` whose close would otherwise pop the
            # wiki depth, truncating the outer table early and leaking
            # everything after `</table>` — equation-number cells,
            # `|}`, etc. — as raw wikitext).
            if (i + 7 < len(text) and masked[i:i+6].lower() == "<table"
                    and masked[i+6] in (" ", ">", "\t", "\n")):
                html_depth += 1
                # Step past the `>` of the opening tag.
                j = text.find(">", i)
                i = j + 1 if j >= 0 else i + 6
                continue
            if masked[i:i+8].lower() == "</table>" and html_depth > 0:
                html_depth -= 1
                i += 8
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
                    # Found the balanced close.  Walker always
                    # registers as TABLE — the classifier
                    # (`_classify_table`) decides whether this is a
                    # DJVU_CROP, COMPOUND_TABLE, or other wikitable
                    # sub-kind.
                    table_text = text[idx:i + closer_len]
                    placeholder = registry.add("TABLE", table_text)
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


# ── Extractor registry ────────────────────────────────────────────────
#
# Each entry is `(name, priority, extract_fn, recurse_safe)`.
#
#   * `priority` is the order the walker invokes extractors (low first).
#     The numbering leaves gaps so new extractors can slot in without
#     renumbering — `tens` clusters elements by phase
#     (1x = pre-table, 2x = tables, 3x = pre-outline children of
#     elements, 4x = outline, 5x = post-outline).
#
#   * `extract_fn(text, registry) -> text` consumes its element from
#     `text` (replacing instances with placeholders registered in
#     `registry`) and returns the modified text.  All extractors share
#     this signature — regex-based and bespoke alike.
#
#   * `recurse_safe=False` marks extractors that must be SKIPPED on
#     recursive walks into an element's inner content.  Only OUTLINE
#     today: it inspects line-level indentation across the whole text
#     and would re-trigger inside an OUTLINE's own indented body.
#
# Adding a new element type is one append here — the walker body is
# unchanged.  Order matters: outermost-first so nested elements get
# claimed by their natural parent (a wiki table claims the file refs
# in its cells before the IMAGE extractor sees them bare).
@dataclass(frozen=True)
class _ExtractorEntry:
    name: str
    priority: int
    extract_fn: Callable[[str, ElementRegistry], str]
    recurse_safe: bool = True
    # ``recurse_into_inner`` controls whether the orchestrator
    # recursively walks the inner bytes of an EXTRACTED instance of
    # this element type.  Leaf elements (CHART2 today, others as
    # classification migrates) set this False; the walker leaves
    # their bytes alone and the producer handles them as raw.  Looked
    # up in `_LEAF_ELEMENT_TYPES` by `_process_element`.
    recurse_into_inner: bool = True


def _regex_extractor(name: str, pattern: re.Pattern):
    """Adapt a (name, pattern) pair into the registry's uniform
    `extract_fn` shape: scan `text` for `pattern`, replace each match
    with a placeholder registered under `name`."""
    def _extract(text: str, registry: ElementRegistry) -> str:
        return pattern.sub(
            lambda m: registry.add(name, m.group(0)), text)
    return _extract


def _chart2_extract(text: str, registry: ElementRegistry) -> str:
    return _CHART2_RE.sub(
        lambda m: registry.add("CHART2", m.group(0)), text)


def _build_extractor_registry() -> list[_ExtractorEntry]:
    """Construct the extractor registry from the regex table above
    plus the bespoke wiki-table / chart2 / outline scanners.  Built
    once at import time; the walker iterates this in priority order."""
    by_name = {n: p for n, p, _f in _EXTRACTORS}
    entries: list[_ExtractorEntry] = [
        # Chart2 genealogical trees come before tables: their `{|…|}`
        # would otherwise be eaten by the balanced-table scanner.
        # Leaf: chart2 content is owned end-to-end by `_process_chart2`,
        # never recursed into.
        _ExtractorEntry("CHART2", 10, _chart2_extract,
                         recurse_into_inner=False),
        # Balanced wiki tables — outermost first via brace-counting.
        _ExtractorEntry("WIKITABLE", 20, _extract_balanced_tables),
        # Pre-outline elements: REF, REF_SELF, POEM, MATH must be
        # placeholdered before the outline scanner runs, so its
        # line-level indentation heuristics don't false-trigger on
        # `{{em|N}}` in verse / equation continuations / footnote
        # bodies.
        _ExtractorEntry("REF_SELF", 30,
                        _regex_extractor("REF_SELF", by_name["REF_SELF"])),
        _ExtractorEntry("REF", 31,
                        _regex_extractor("REF", by_name["REF"])),
        _ExtractorEntry("POEM", 32,
                        _regex_extractor("POEM", by_name["POEM"])),
        _ExtractorEntry("MATH", 33,
                        _regex_extractor("MATH", by_name["MATH"])),
        # OUTLINE: taxonomic / genealogical / numbered-caption ladders.
        # Recursion-unsafe — its line-indent scan would loop on its
        # own extracted bytes.
        _ExtractorEntry("OUTLINE", 40, _extract_outlines,
                        recurse_safe=False),
        # Post-outline elements.  Order within this band matches the
        # legacy `_EXTRACTORS` iteration order so spec-by-spec
        # behaviour is preserved (IMAGE_FLOAT before IMAGE, both
        # HIEROGLYPH variants together).
        _ExtractorEntry("HTML_TABLE", 50,
                        _regex_extractor("HTML_TABLE", by_name["HTML_TABLE"])),
        _ExtractorEntry("SCORE", 51,
                        _regex_extractor("SCORE", by_name["SCORE"])),
        _ExtractorEntry("IMAGE_FLOAT", 52,
                        _regex_extractor("IMAGE_FLOAT", by_name["IMAGE_FLOAT"])),
        _ExtractorEntry("IMAGE", 53,
                        _regex_extractor("IMAGE", by_name["IMAGE"])),
    ]
    # HIEROGLYPH appears twice in _EXTRACTORS (template form and
    # `<hiero>` tag form); preserve both as separate registry entries.
    for i, (name, pat, _flags) in enumerate(_EXTRACTORS):
        if name == "HIEROGLYPH":
            entries.append(_ExtractorEntry(
                f"HIEROGLYPH#{i}", 54 + i,
                _regex_extractor("HIEROGLYPH", pat)))
    return entries


_EXTRACTOR_REGISTRY: list[_ExtractorEntry] = sorted(
    _build_extractor_registry(), key=lambda e: e.priority)


# Lookup for the walker-side recursion flag.  An element type appears
# here iff it's in the extractor registry — i.e., the walker itself
# extracted it.  Classifier-emitted labels (`DJVU_CROP`,
# `COMPOUND_TABLE`) are not present here; their leaf-ness is still
# carried on `_HandlerEntry.pre_extract` until classification migrates
# off the WIKITABLE extractor's internal logic into the classifier.
_RECURSE_INTO_INNER_BY_TYPE: dict[str, bool] = {
    e.name: e.recurse_into_inner for e in _EXTRACTOR_REGISTRY
}


def extract(
    text: str, _outline_pass: bool = True,
) -> tuple[str, ElementRegistry]:
    """Extract all embedded elements from text, outermost first.

    Iterates `_EXTRACTOR_REGISTRY` in priority order, calling each
    entry's `extract_fn` to consume that element type from `text`
    (replacing matches with placeholders registered in the returned
    `ElementRegistry`).

    `_outline_pass=False` is passed by recursive calls into an
    OUTLINE element's own inner content; entries marked
    `recurse_safe=False` are skipped in that case so the outline
    scanner doesn't re-trigger on its own bytes.
    """
    registry = ElementRegistry()
    for entry in _EXTRACTOR_REGISTRY:
        if not _outline_pass and not entry.recurse_safe:
            continue
        text = entry.extract_fn(text, registry)
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


# Producer handler signature.  All producers share this shape:
#   (raw, inner, text_transform, context, inner_registry) -> marker
# `raw` is the source bytes of the element (with delimiters); `inner`
# is the delimiter-stripped content with child placeholders.  Most
# producers use `inner` + the child registry; the leaf cases
# (CHART2, DJVU_CROP, COMPOUND_TABLE) use `raw` and ignore the rest.
_ElementHandler = Callable[
    [str, str, Callable, ElementContext, "ElementRegistry | None"], str]


def _passthrough_inner(raw, inner, text_transform, context,
                       inner_registry):
    return inner


# ── Single classifier ─────────────────────────────────────────────────
#
# `classify` is the one and only entry point for "what kind of element
# is this?"  The walker assigns a transport type (WIKITABLE, IMAGE,
# MATH, …) when it extracts; the classifier accepts that type plus the
# element's bytes/registry and returns a LABEL.  For element types
# whose source syntax determines the kind uniquely (MATH, POEM,
# SCORE, …), the label is just the transport type.  For element types
# whose source syntax can carry multiple semantic shapes (WIKITABLE),
# the classifier dispatches internally to a sub-function that examines
# the bytes and returns the discriminated label.
#
# Producers are keyed by LABEL in `_PRODUCER_DISPATCH`.  Adding a new
# sub-kind of an existing transport means: extend the classifier
# branch to emit a new label, add the matching producer to
# `_PRODUCER_DISPATCH`.  The walker is untouched.
def classify(element_type: str, raw: str, inner: str,
              inner_registry: ElementRegistry | None) -> str:
    """Single classifier entry point.  Returns the producer-dispatch
    label for an element extracted by the walker."""
    if element_type == "TABLE":
        return _classify_table(raw, inner, inner_registry)
    # Single-label transports: the element_type IS the kind.
    return element_type


# ── Producer dispatch ─────────────────────────────────────────────────
#
# Flat label → producer table.  Both wikitable sub-kinds (returned by
# `classify` for `element_type=TABLE`) and single-label element types
# (returned trivially) coexist in one dict.  Replaces the previous
# two-level dispatch (`_ELEMENT_HANDLERS` → `_dispatch_table` →
# `_TABLE_KIND_HANDLERS`).
_PRODUCER_DISPATCH: dict[str, _ElementHandler] = {
    # Wikitable sub-kinds.
    "MATH_LAYOUT": lambda raw, inner, tt, ctx, reg:
        _process_math_table_layout(raw, inner, reg, tt),
    "LAYOUT_WRAPPER": lambda raw, inner, tt, ctx, reg:
        _unwrap_layout_table(inner, tt, reg),
    "COMPLEX_HTML": lambda raw, inner, tt, ctx, reg:
        _process_complex_table(inner, tt),
    "CHEMISTRY_LAYOUT": lambda raw, inner, tt, ctx, reg:
        _process_chemistry_layout(inner, tt, reg),
    "DATA_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_table(inner, tt, reg),
    # Single-label kinds — element_type == label.
    "DJVU_CROP": lambda raw, inner, tt, ctx, reg:
        _process_djvu_crop(raw, tt, ctx),
    "CHART2": lambda raw, inner, tt, ctx, reg: _process_chart2(raw, ctx),
    "COMPOUND_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_compound_table(raw, tt),
    "MATH": lambda raw, inner, tt, ctx, reg: _process_math(inner),
    "SCORE": lambda raw, inner, tt, ctx, reg: _process_score(inner),
    "REF_SELF": lambda raw, inner, tt, ctx, reg:
        _process_ref_self(raw, ctx.ref_bodies),
    "REF": lambda raw, inner, tt, ctx, reg:
        _process_ref(raw, inner, tt, ctx.ref_bodies),
    "IMAGE": lambda raw, inner, tt, ctx, reg: _process_image(inner, tt),
    "IMAGE_FLOAT": lambda raw, inner, tt, ctx, reg:
        _process_image_float(inner, tt),
    "POEM": lambda raw, inner, tt, ctx, reg: _process_poem(inner, tt),
    "HIEROGLYPH": lambda raw, inner, tt, ctx, reg:
        f"[hieroglyph: {inner}]",
    "HTML_TABLE": lambda raw, inner, tt, ctx, reg:
        _process_html_table(raw, inner, tt, reg),
    "OUTLINE": lambda raw, inner, tt, ctx, reg: _process_outline(inner, tt),
}


# Element types the walker registers that should NOT be recursed
# into.  Today's only entry is `CHART2`; DJVU_CROP and COMPOUND_TABLE
# used to be here as transitional bridges but are now produced by
# `_classify_table` as labels (the walker emits all wikitables as
# `TABLE`).  Future leaf types declare themselves via
# `recurse_into_inner=False` in the extractor registry.
_LEAF_ELEMENT_TYPES: frozenset[str] = frozenset(
    name for name, recurse
    in _RECURSE_INTO_INNER_BY_TYPE.items() if not recurse
)


# ── Three-pass element processing ─────────────────────────────────────
#
# Walk → Classify → Produce.  Each pass is a complete sweep over the
# registry tree before the next begins.  Earlier the three were
# interleaved in a single recursive `_process_element` call, which
# meant that when a parent's classifier ran, it had to inspect its
# children's RAW bytes to make decisions (e.g. `_is_chemistry_layout`
# scanned for Langle/Rangle file refs).  Under separate passes, the
# classifier can ask "what kind is my child's placeholder?" — a
# registry lookup against the bottom-up classification result —
# instead of re-parsing.

def _walk_recursive(text: str, _outline_pass: bool = True
                     ) -> tuple[str, ElementRegistry]:
    """Walk pass: extract elements at this level, then recurse into
    each non-leaf element's inner bytes.

    Returns (text_with_placeholders, registry) where the registry's
    `elements` is populated by `extract()` and `inners` +
    `inner_registries` are populated for every non-leaf element.
    No classification or production has run yet.
    """
    text, registry = extract(text, _outline_pass)
    for placeholder, (element_type, raw) in registry.elements.items():
        if element_type in _LEAF_ELEMENT_TYPES:
            continue
        inner = _strip_delimiters(element_type, raw)
        inner, inner_registry = _walk_recursive(
            inner, _outline_pass=(element_type != "OUTLINE"))
        registry.inners[placeholder] = inner
        registry.inner_registries[placeholder] = inner_registry
    return text, registry


def _classify_recursive(registry: ElementRegistry) -> None:
    """Classify pass: bottom-up over the tree.

    For each element, recurse into children's classification first,
    then call `classify()` for this element.  The classifier can
    therefore look up its children's labels from `inner_registry.labels`
    when it makes a decision.  Stores the label in THIS registry's
    `labels[placeholder]`.
    """
    for placeholder, (element_type, raw) in registry.elements.items():
        inner_registry = registry.inner_registries.get(placeholder)
        if inner_registry is not None:
            _classify_recursive(inner_registry)
        inner = registry.inners.get(placeholder, "")
        registry.labels[placeholder] = classify(
            element_type, raw, inner, inner_registry)


def _produce_recursive(registry: ElementRegistry,
                        text_transform, context: ElementContext) -> None:
    """Produce pass: bottom-up over the tree.

    Children produced first; the producer is then called for this
    element with the still-placeholder-containing inner (handlers
    that inspect inner_registry expect to see opaque placeholders
    there).  Child markers are substituted into the producer's
    output after it runs.  Multi-pass substitution handles sibling
    cross-references (a child's marker can carry a placeholder for
    another child).  Stores the marker in THIS registry's
    `markers[placeholder]`.
    """
    for placeholder, (element_type, raw) in registry.elements.items():
        inner_registry = registry.inner_registries.get(placeholder)
        if inner_registry is not None:
            _produce_recursive(inner_registry, text_transform, context)
        inner = registry.inners.get(placeholder, "")
        label = registry.labels[placeholder]
        handler = _PRODUCER_DISPATCH.get(label, _passthrough_inner)
        marker = handler(raw, inner, text_transform, context, inner_registry)
        # Substitute child markers into the handler's output.  Cannot
        # be done before the handler runs — table-shaped producers
        # inspect inner_registry to query child placeholder types.
        # Multi-pass because a substituted child marker can itself
        # contain placeholders for other children (cross-references).
        if inner_registry is not None and inner_registry.markers:
            for _ in range(5):
                changed = False
                for child_ph, child_marker in inner_registry.markers.items():
                    if child_ph in marker:
                        marker = marker.replace(child_ph, child_marker)
                        changed = True
                if not changed:
                    break
        # Post-substitution cleanup: a child wiki-table's
        # `{{TABLE:…}TABLE}` marker embedded inside an HTMLTABLE cell
        # leaks as cell text unless converted to inline `<table>` HTML
        # (ORNITHOLOGY taxonomic alignments, EOCENE etymology glossary
        # inside a `<ref>`).
        if "«HTMLTABLE:" in marker and "{{TABLE:" in marker:
            from britannica.pipeline.stages.elements._tables import (
                _inline_nested_table_markers_in_htmltable_blocks,
            )
            marker = _inline_nested_table_markers_in_htmltable_blocks(marker)
        registry.markers[placeholder] = marker



def _classify_table(raw: str, inner: str, inner_registry: ElementRegistry | None) -> str:
    """Classify a wiki table into its processing type.

    Returns one of:
        DJVU_CROP        — table wrapper around a `{{Css image crop}}`
                          template; producer parses the crop directly
                          from raw bytes.
        COMPOUND_TABLE  — data table with nested sub-tables; producer
                          parses the nested structure from raw bytes.
        MATH_LAYOUT     — math/equation layout in any of three encodings
                          (raw tokens, ``<math>`` blocks, or HTML-table
                          wrapper).  Unified detector / dispatcher in
                          ``_math_layout.py``.
        LAYOUT_WRAPPER  — image+caption wrapper or nested table wrapper
        PLATE_LAYOUT    — `summary="Illustration"` multi-image grid (plate)
        COMPLEX_HTML    — tables with rowspan that need HTML passthrough
        CHEMISTRY_LAYOUT — 2-D chemical-reaction / structural-formula diagram
        DATA_TABLE      — regular data tables (default)
    """
    # DJVU_CROP and COMPOUND_TABLE: raw-only classifications — their
    # producers parse the original wikitext directly and don't need
    # the recursed inner registry.  Checked first so we short-circuit
    # before any content-level inspection.
    if re.search(r"\{\{Css image crop", raw, re.IGNORECASE):
        return "DJVU_CROP"
    if _is_compound_table(raw):
        return "COMPOUND_TABLE"
    # Chemistry-reaction / structural-formula layout — atom-label cells,
    # ⟨/⟩ valence-bracket images, `||` bond-lines, ⟶ arrows, `rowspan`
    # brackets.  Priority over every other classification (the
    # `[[File:Langle/Rangle]]` ref is chemistry-exclusive).  Detected
    # by querying the bottom-up-classified registry tree for IMAGE
    # elements whose filename matches the bracket pattern — these
    # exist as registered children by the time this classifier runs,
    # eliminating the need to regex-scan the table's raw bytes.
    if _has_chem_brackets(inner_registry):
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
            return "MATH_LAYOUT"
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
    if _math_table_kind(raw, inner, inner_registry) is not None:
        return "MATH_LAYOUT"
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
    # Pass 1 — walk: build a complete registry tree from the article.
    # Top-down recursion populates `elements`, `inners`, and
    # `inner_registries` for every element in the article and its
    # descendants.  No classification or production yet.
    text, registry = _walk_recursive(text)

    # Body text transform — operates on the prose between top-level
    # placeholders only; each element's inner content is transformed
    # by its own producer when it runs.
    text = text_transform(text)

    # Article-wide ref-body resolution.  `<ref name=X/>` self-closing
    # reuses (and `<ref name=X>body…` definitions whose body comes
    # via a later `<ref follow=X>…` continuation) resolve to the
    # merged body.  Required by MOLECULE p684 where
    # `Atom<ref name=654f1/>` is the anchor and the body arrives via
    # `<ref follow=654f1>…</ref>` two paragraphs later.  Threaded
    # into `context` so the REF producer can read it.  Copy the
    # caller's context so we don't mutate it.
    context = _dc_replace(context)
    context.ref_bodies = _resolve_ref_bodies(registry, text_transform)

    # Pass 2 — classify: bottom-up over the tree.  Each element's
    # classifier sees its already-classified children's labels via
    # the inner_registry.
    _classify_recursive(registry)

    # Pass 3 — produce: bottom-up over the tree.  Each element's
    # producer runs after its children's markers exist; child markers
    # are substituted into the producer's output afterward.
    _produce_recursive(registry, text_transform, context)

    # Reassemble: substitute top-level markers into the article body.
    # Multi-pass handles sibling cross-references (one top-level
    # element's marker can carry a placeholder for another).
    for _ in range(3):
        changed = False
        for key, marker in registry.markers.items():
            if key in text:
                text = text.replace(key, marker)
                changed = True
            for other_key in registry.markers:
                if other_key != key and key in registry.markers[other_key]:
                    registry.markers[other_key] = (
                        registry.markers[other_key].replace(key, marker))
        if not changed:
            break

    return text
