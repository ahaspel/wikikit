"""Transform raw wikitext article bodies into internal marker format.

This stage runs after boundary detection.  Each article's body contains
raw Wikisource wikitext at this point.  We convert it to the internal
marker format (``«B»``, ``«FN:``, ``{{IMG:``, etc.) by running the same
26 fetch stages and clean_pages transformations — but per-article instead
of per-page, and skipping stage 3 (section-tag conversion) since
boundaries have already been determined.

Articles are processed one at a time and committed individually so that
only one article body is in memory at any point.
"""
from __future__ import annotations

import re

from britannica.db.models import Article, ArticleSegment, SourcePage
from britannica.db.session import SessionLocal

from britannica.cleaners.hyphenation import fix_hyphenation
from britannica.cleaners.reflow import reflow_paragraphs
from britannica.cleaners.unicode import normalize_unicode, replace_print_artifacts
from britannica.pipeline.stages.clean_pages import _replace_score_tags
from britannica.pipeline.stages.transform_articles.body_text import (
    _FMT,
    _FRAKTUR_MAP,
    _LNK,
    _SH,
    _convert_bold_italic,
    _convert_hieroglyphs,
    _convert_links,
    _convert_shoulder_headings,
    _convert_small_caps,
    _convert_sub_sup,
    _decode_entities,
    _finalize_markers,
    _strip_html,
    _strip_templates,
    _to_fraktur,
    _transform_body_text,
    _unwrap_content_templates,
    _unwrap_layout_templates,
)
from britannica.pipeline.stages.transform_articles.plate_legacy import (
    _transform_plate,
)
from britannica.pipeline.stages.transform_articles.legend_promote import (
    _ATTRIBUTION_START_RE,
    _BLOCK_END_RE,
    _BLOCK_MARKER_RE,
    _FIGURE_BOUNDARY_MARKERS,
    _INLINE_SECTION_HEADING_RE,
    _LEGEND_CELL_RE,
    _LEGEND_LABEL_ALONE_RE,
    _PARA_LEGEND_LABEL_ONE,
    _PARA_LEGEND_PLAIN_RE,
    _PARA_LEGEND_STRICT_RE,
    _append_attr_to_img,
    _build_legend_line_re,
    _bundle_raw_image_with_caption,
    _classify_figure_paragraph,
    _clean_loose_caption,
    _find_matching_double_braces,
    _fold_image_attribution,
    _is_attribution_paragraph,
    _legend_entries_from_paragraph,
    _match_legend_line,
    _paragraphs_starting_at,
    _parse_legend_lines,
    _parse_table_as_legend,
    _parse_verse_as_legend,
    _process_figures,
    _promote_legend_tables,
    _promote_legend_verses,
    _promote_paragraph_legends,
    _split_multi_entry_line,
    _strip_inline_italic,
    _try_convert_verse_simple,
    _try_convert_with_attr,
)
from britannica.pipeline.stages.transform_articles.djvu_refs import (
    _DJVU_PAGE_REF_RE,
    _normalize_djvu_page_refs,
)


# ── Body text processing stages ──────────────────────────────────────
#
# Each function handles one kind of wiki markup.  They run on body text
# AFTER embedded elements have been extracted, so they never see tables,
# images, footnotes, poems, math, or scores.

# Control characters for intermediate markers.
# \x03 is used by elements.py for placeholders, so we avoid it.

# _FRAKTUR_MAP initialization moved to body_text.py





# Plain-ASCII label shape after italic markers have been stripped.
# Caps label at 4 chars so the regex doesn't greedily eat real words
# (HEXAPODA Fig. 58 `H, Air compressing cylinder` → label `H`, not
# `H, Air`).  Multi-label chains (`K, L. Round-nose tools.`) require
# a period terminator to distinguish them from single-label + comma
# + prose.
#
# Two variants: PERMISSIVE allows a single-label with NO separator
# (used inside VERSE/TABLE/POEM container content where `A text.` is
# legitimate — TOOL Fig. 65).  STRICT requires `,` or `.` after
# single-label (used for body paragraphs where `a drilling…` is an
# English article, not a label — TOOL Fig. 47).

# EB1911 inline section-heading pattern: ``LABEL. ''italic title.''—prose``
# (HARMONY vol 13: ``III. ''Modern Harmony and Tonality.''—In the harmonic
# system of Palestrina…``).  Label + italic-wrapped title + em-dash is
# the distinguishing shape — legend captions with Roman-numeral labels
# (CENTIPEDE "I. Mandibles", HYDRAULICS "VI. STEADY FLOW…") do NOT
# italicize their text or use em-dashes, so this regex misses them.

# ── Figure walker (unified IMG+attribution+legend handler) ────────────
#
# For each `{{IMG:…}}` marker, walk forward paragraph-by-paragraph
# collecting "figure material" (attribution, legend-shaped content in
# any wrapper) until we hit a figure boundary, then emit a cleanly
# formatted IMG + optional LEGEND.  This replaces the earlier zoo of
# container-specific promoters (`_promote_legend_tables/verses/
# paragraphs`) with one pipeline: (1) classify pattern, (2) locate
# boundary, (3) format content.



# Bare label cell (label without text in same cell): `a,` / `At,` /
# `br.s,` / `br f,` / `g.s.`.  Up to ~12 chars; may contain dots,
# hyphens, and a single internal space (TUNICATA Fig. 25 uses `br f,`
# `i l,` for biological abbreviations).  Uses \w (Unicode-aware) so
# Latin ligatures (œ, æ) and accented letters survive.  Requires a
# trailing `,` or `.` so the label is unambiguously a legend label
# and not just a short word in a data table cell.


def _transform_text_v2(raw_wikitext: str, volume: int, page_number: int) -> str:
    """New architecture: extract-process-reassemble.

    1. Minimal preprocessing (strip section tags, noinclude, normalize)
    2. process_elements does everything:
       - Extracts embedded elements
       - Transforms body text (bold, italic, links, etc.)
       - Processes each element recursively
       - Reassembles
    3. Done.
    """
    from britannica.pipeline.stages.elements import process_elements
    from britannica.pipeline.stages.fold_unfold import unfold_folded_rows

    # Source-text corrections (transcription typos in wikisource) are
    # applied once during clean_pages, mutating `wikitext` so all
    # downstream stages — including this one — operate on already-
    # corrected text. No repeat application needed here.

    # Rewrite folded wikitable rows — single physical rows holding N
    # logical rows via <br>-stacking — as N real rows, so downstream
    # table processing sees a well-formed N-row table instead of one
    # giant row with concatenated values.
    raw_wikitext = unfold_folded_rows(raw_wikitext)

    # Convert STANDALONE {{Css image crop|Image=EB1911 - Volume N.djvu|
    # Page=P|…}} templates (not inside a wikitable) to File links.
    # Css image crops INSIDE wikitables are part of image-layout
    # tables and must be left for the table extractor to classify as
    # DJVU_CROP — converting them here would break ORCHIDS et al.
    #
    # Detection: scan {|…|} table bodies, mark their byte ranges, and
    # only apply the replacement to matches whose start is OUTSIDE any
    # such range. Nested-template-aware table boundary tracking (walk
    # {|/|} depth, skipping {{…}} content) mirrors the extractor in
    # elements.py so ``{{Ts|vmi|}}``-style pipes don't mistrigger.
    def _table_ranges(text: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        i = 0
        n = len(text)
        while i < n - 1:
            if text[i:i+2] == "{|":
                start = i
                depth = 1
                j = i + 2
                while j < n - 1 and depth > 0:
                    if text[j:j+2] == "{{":
                        # skip balanced {{…}} block
                        td = 1; j += 2
                        while j < n - 1 and td > 0:
                            if text[j:j+2] == "{{": td += 1; j += 2
                            elif text[j:j+2] == "}}": td -= 1; j += 2
                            else: j += 1
                    elif text[j:j+2] == "{|": depth += 1; j += 2
                    elif text[j:j+2] == "|}": depth -= 1; j += 2
                    else: j += 1
                ranges.append((start, j))
                i = j
            else:
                i += 1
        return ranges

    _table_spans = _table_ranges(raw_wikitext)
    _crop_pat = re.compile(
        r"(\{\{\s*Css\s+image\s+crop\s*\|[^}]*\}\})"
        r"(?:\s*(?:\{\{\s*center\s*\|([^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*)\}\}"
        r"|\{\{\s*csc\s*\|([^{}]*)\}\}))?",
        re.IGNORECASE,
    )

    # Pre-compute crop indices.  ``download_djvu_crops.py`` numbers
    # every ``{{Css image crop|Image=…djvu|Page=P|…}}`` on a page as
    # ``_crop0``, ``_crop1``, …  in wikitext order across BOTH
    # standalone and in-table occurrences.  We must mirror that
    # indexing so the ``{{IMG:djvu_volNN_pagePPPP_cropI.jpg}}`` marker
    # references the file that actually got produced.  (Previously
    # ``_css_crop_replace`` dropped standalone crops into the no-crop
    # ``djvu_volNN_pagePPPP.jpg`` full-page slot, so SHIPBUILDING
    # Figs 3, 6, 9, 10 et al pointed at the whole page instead of the
    # cropped figure.)
    _CSS_CROP_ALL_RE = re.compile(
        r"\{\{\s*Css\s+image\s+crop\s*\n?(.*?)\}\}",
        re.DOTALL | re.IGNORECASE,
    )
    _crop_index_at: dict[int, int] = {}   # match-start offset → crop index
    _page_counts: dict[tuple[int, int], int] = {}
    for _m in _CSS_CROP_ALL_RE.finditer(raw_wikitext):
        _body = _m.group(1)
        _img_m = re.search(
            r"Image\s*=\s*(EB1911\s+-\s+Volume\s+(\d+)\.djvu)",
            _body, re.IGNORECASE)
        _page_m = re.search(r"Page\s*=\s*(\d+)", _body, re.IGNORECASE)
        if not (_img_m and _page_m):
            continue
        _key = (int(_img_m.group(2)), int(_page_m.group(1)))
        _crop_index_at[_m.start()] = _page_counts.get(_key, 0)
        _page_counts[_key] = _page_counts.get(_key, 0) + 1

    def _css_crop_replace(m: re.Match) -> str:
        body = m.group(1)
        caption = ((m.group(2) if len(m.groups()) >= 2 else None) or
                   (m.group(3) if len(m.groups()) >= 3 else None) or "").strip()
        if caption:
            for _ in range(3):
                new = re.sub(r"\{\{[A-Za-z][A-Za-z0-9_]*\s*\|([^{}|]*)\}\}",
                             r"\1", caption)
                if new == caption:
                    break
                caption = new
            caption = caption.strip()
        img_m = re.search(
            r"Image\s*=\s*(EB1911\s+-\s+Volume\s+(\d+)\.djvu)",
            body, re.IGNORECASE)
        page_m = re.search(r"Page\s*=\s*(\d+)", body, re.IGNORECASE)
        if img_m and page_m:
            vol = int(img_m.group(2))
            page = int(page_m.group(1))
            idx = _crop_index_at.get(m.start(), 0)
            filename = f"djvu_vol{vol:02d}_page{page:04d}_crop{idx}.jpg"
            if caption:
                return f"[[File:{filename}|{caption}]]"
            return f"[[File:{filename}]]"
        return m.group(0)

    def _maybe_replace(m: re.Match) -> str:
        for s, e in _table_spans:
            if s <= m.start() < e:
                return m.group(0)  # inside a table — leave alone
        return _css_crop_replace(m)
    raw_wikitext = _crop_pat.sub(_maybe_replace, raw_wikitext)

    # Strip section tags — boundaries already determined
    text = re.sub(r'<section\s+(?:begin|end)="[^"]*"\s*/?>', "",
                  raw_wikitext, flags=re.IGNORECASE)

    # Strip <noinclude> blocks (page headers, quality tags), but preserve
    # any `{|` opener or `|}` closer lines inside them — EB1911 pages
    # often put the table wrapper `{|...` in the header noinclude and
    # `|}` in the footer noinclude so the page displays standalone.
    # Stripping the whole block leaves the rows orphaned and the
    # balanced-table extractor later pairs a `{|` on one page with a
    # `|}` many pages later, swallowing all intermediate prose (this
    # was silently eating Climate / Fauna / Population sections of
    # UNITED STATES, THE). detect_boundaries applies the same logic at
    # its own preprocess step; this is defence in depth.
    def _strip_noinclude(m: re.Match) -> str:
        block = m.group(0)
        kept: list[str] = []
        for om in re.finditer(r"(?:^|\n)\s*\{\|[^\n<]*", block):
            kept.append(om.group(0).strip())
        if re.search(r"(?:^|\n)\s*\|\}(?!\})", block):
            kept.append("|}")
        return ("\n" + "\n".join(kept) + "\n") if kept else ""
    text = re.sub(r"<noinclude>.*?</noinclude>", _strip_noinclude, text,
                  flags=re.DOTALL | re.IGNORECASE)

    # Unclosed `{{nowrap|…` (malformed wikitext in sources like EGYPT
    # vol 9 p76) confuses cell parsing because its inner `|` leaks as
    # a cell separator. Scan for each opener; if it has no matching
    # `}}`, strip just the opener. Balanced cases (including nested
    # templates) are left alone for _unwrap_balanced to handle.
    #
    # Generalized to also handle `ppoem`, `right`, `float right`,
    # `fine block`, `anchor` — quality-report sweep 2026-05-08
    # surfaced unclosed openers of these in HOOD, TANCRED, THEODORE
    # OF MOPSUESTIA, SARAVIA, ST LOUIS articles.  Adding each to the
    # opener list here strips the orphan markup so it doesn't render
    # as raw wikitext in prose.
    _UNCLOSED_TEMPLATES = (
        "nowrap", "ppoem", "right", "float right", "fine block",
        "anchor",
    )

    def _strip_unclosed_templates(text):
        out = []
        i = 0
        low = text.lower()
        n = len(text)
        while i < n:
            # Try each opener.
            matched_opener = None
            for name in _UNCLOSED_TEMPLATES:
                opener_with_pipe = "{{" + name + "|"
                opener_with_space = "{{" + name + " "
                if low[i:i + len(opener_with_pipe)] == opener_with_pipe:
                    matched_opener = opener_with_pipe
                    break
                # Allow whitespace between name and pipe ("{{right |..."),
                # but only if a pipe follows shortly.
                if low[i:i + len(opener_with_space)] == opener_with_space:
                    pipe_idx = text.find("|", i + len(opener_with_space))
                    if 0 <= pipe_idx <= i + len(opener_with_space) + 20:
                        matched_opener = text[i:pipe_idx]
                        break
            if matched_opener is None:
                out.append(text[i])
                i += 1
                continue
            # Find matching }} by depth counting.
            depth = 1
            j = i + 2
            matched_close = False
            while j < n - 1:
                if text[j:j+2] == "{{":
                    depth += 1
                    j += 2
                elif text[j:j+2] == "}}":
                    depth -= 1
                    j += 2
                    if depth == 0:
                        matched_close = True
                        break
                else:
                    j += 1
            if matched_close:
                out.append(text[i:j])
                i = j
            else:
                pipe_idx = text.find("|", i)
                if pipe_idx >= 0:
                    i = pipe_idx + 1
                else:
                    i += 2
        return "".join(out)
    text = _strip_unclosed_templates(text)

    # Replace <score> tags (static lookup, must happen before extraction)
    text = _replace_score_tags(text, volume, page_number)

    # Normalize `EB1911 - Volume N.djvu/PPP` (and the typo variant
    # `…djvu-PPP.png`) to local filenames `djvu_volNN_pagePPPP.jpg`
    # BEFORE image extraction.  `download_djvu_crops.py` provisions
    # these files from the volume's DjVu renders — otherwise the
    # viewer would try (and fail) to resolve them on Commons.
    text = _normalize_djvu_page_refs(text)

    # {{raw image|filename}} is an alternate EB1911 image syntax used
    # for figures whose caption sits on a following line as
    # `{{c|{{sc|Fig. 10.}}}}` or in a separate wikitable. Bundle the
    # image and its caption block into one `[[File:filename|caption]]`
    # so downstream extraction renders them as a single figure (avoids
    # the figure showing both a figcaption and a duplicate caption
    # paragraph below — see WEIGHING MACHINES / SEWING MACHINES).
    text = _bundle_raw_image_with_caption(text)

    # Unwrap center `{{c|…}}` templates — they only control alignment.
    # Done before element extraction so an image caption like
    # `{{c|{{sc|Fig. 10.}}}}` simplifies to `{{sc|Fig. 10.}}` and the
    # IMAGE extractor's caption-pairing regex can recognize it.
    for _ in range(3):
        new = re.sub(
            r"\{\{\s*c\s*\|((?:[^{}]|\{\{[^{}]*\}\})*)\}\}",
            r"\1", text, flags=re.IGNORECASE,
        )
        if new == text:
            break
        text = new

    # Strip {{missing table}} markers that precede chart2 blocks (the chart
    # image replaces the table; the marker is redundant)
    text = re.sub(
        r"\{\{missing table\}\}\s*(?:\x01PAGE:\d+\x01)?\s*(?=\{\{center\|.*?GENEALOGICAL|\{\{chart2/start)",
        "", text, flags=re.IGNORECASE | re.DOTALL,
    )

    # Normalize
    text = normalize_unicode(text)
    text = replace_print_artifacts(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Strip HTML comments (replace with space to avoid creating false paragraph breaks)
    text = re.sub(r"\n?<!--.*?-->\n?", " ", text, flags=re.DOTALL)

    # Unwrap poem wrappers: {{block center|<poem>...</poem>}} → <poem>...</poem>
    text = re.sub(
        r"\{\{block center\|(<poem>.*?</poem>)\}\}",
        r"\1", text, flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"\{\{center\|(<poem>.*?</poem>)\}\}",
        r"\1", text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Unwrap `{{center|[[File:…]]<br>caption}}` so the image and its
    # caption become a normal image+caption sequence the IMAGE
    # extractor already knows how to parse (WEAVING Fig. 1 & 2).
    # Caption may contain one level of nested `{{…}}` templates.
    text = re.sub(
        r"\{\{center\|(\[\[(?:File|Image):[^\]]+\]\])\s*<br\s*/?>\s*"
        r"((?:[^{}]|\{\{[^{}]*\}\})*)\}\}",
        r"\1\n\2",
        text, flags=re.IGNORECASE,
    )

    # Unwrap fine print markers
    text = re.sub(
        r"\{\{EB1911 fine print/s\}\}(.*?)\{\{EB1911 fine print/e\}\}",
        r"\1", text, flags=re.DOTALL | re.IGNORECASE,
    )

    # Unwrap {{fine block|...}} with balanced brace matching
    def _unwrap_balanced(text, template_name):
        """Unwrap a template by finding the balanced closing }}.

        Skips over <math>...</math> regions so that LaTeX braces
        (e.g. \\Delta_{b}}) don't confuse the brace counter.
        """
        prefix = "{{" + template_name + "|"
        # Pre-compute math regions to skip
        math_spans = [(m.start(), m.end()) for m in
                      re.finditer(r"<math\b[^>]*>.*?</math>", text,
                                  re.DOTALL | re.IGNORECASE)]

        def _in_math(pos):
            for s, e in math_spans:
                if s <= pos < e:
                    return e  # return end position to skip to
            return 0

        while True:
            idx = text.lower().find(prefix.lower())
            if idx < 0:
                break
            # Find balanced close
            depth = 0
            i = idx
            while i < len(text) - 1:
                skip_to = _in_math(i)
                if skip_to:
                    i = skip_to
                    continue
                if text[i:i+2] == "{{":
                    depth += 1
                    i += 2
                elif text[i:i+2] == "}}":
                    depth -= 1
                    if depth == 0:
                        # Replace: strip outer {{ and }}
                        content = text[idx + len(prefix):i]
                        # Strip a leading MediaWiki positional-parameter
                        # name (e.g. "1="). Wikitext allows the explicit
                        # form {{center|1=payload}}; without this, the
                        # "1=" leaks into the rendered text.
                        content = re.sub(r"^\d+=", "", content)
                        text = text[:idx] + content + text[i+2:]
                        # Recompute math spans since offsets shifted
                        math_spans = [(m.start(), m.end()) for m in
                                      re.finditer(r"<math\b[^>]*>.*?</math>",
                                                  text, re.DOTALL | re.IGNORECASE)]
                        break
                    i += 2
                else:
                    i += 1
            else:
                break  # unbalanced — give up
        return text

    # Normalize {{center|{{sc|...}}}} (and {{center|1={{sc|...}}}}) to
    # {{csc|...}} so it gets paragraph breaks. The explicit 1= form
    # is MediaWiki's positional-parameter syntax and appears in some
    # hand-edited articles (e.g. JEWS, MALAYS, AMERICAN WAR OF
    # INDEPENDENCE).
    text = re.sub(
        r"\{\{center\|(?:1=)?\{\{sc\|([^{}]*)\}\}\}\}",
        r"{{csc|\1}}", text, flags=re.IGNORECASE,
    )

    # Note: ``hi`` intentionally NOT in this list. ``{{hi|Nem|content}}``
    # has a two-arg form with a size prefix that the generic balanced
    # unwrap would leak into visible text (``3em|content``). The
    # dedicated handlers in ``_unwrap_content_templates`` (called per
    # text-transform pass) handle both ``{{hi|content}}`` and
    # ``{{hi|Nem|content}}`` correctly.
    for tmpl in ["block center", "fine block", "center", "c", "larger", "smaller",
                  "EB1911 Fine Print", "nowrap", "Fine", "sm"]:
        text = _unwrap_balanced(text, tmpl)
    # Note: {{ts|...}} templates are stripped inside table processors,
    # not globally — global stripping corrupts cell boundaries in complex tables.
    # Convert spacing templates to a space ({{gap}}, {{em|N}}, {{rule}})
    text = re.sub(r"\{\{gap(?:\|[^{}]*)?\}\}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{em\s*\|[^{}]*\}\}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{rule\}\}", "———", text, flags=re.IGNORECASE)

    # No need for orphan table wrapping — articles are joined before
    # transform, so all tables have their {| and |} in the same text.

    # Extract, process, reassemble — this does all the work
    from britannica.pipeline.stages.elements import ElementContext
    context = ElementContext(volume=volume, page_number=page_number)
    text = process_elements(text, _transform_body_text, context)

    # Inject chart images for pages where chart2 markup was lost during import
    from britannica.image_assets import CHART2_IMAGES
    for (v, p), filename in CHART2_IMAGES.items():
        if v == volume and f"IMG:{filename}" not in text:
            marker = f"\x01PAGE:{p}\x01"
            if marker in text:
                text = text.replace(marker, f"{marker}\n\n{{{{IMG:{filename}|Genealogical table}}}}\n\n", 1)

    # Single-pass figure walker: for each `{{IMG:…}}`, collect
    # attribution lines + legend-shaped content (in any wrapper —
    # VERSE, TABLE, paragraphs) up to the figure boundary, then emit
    # one clean `{{IMG:…|caption}}` + optional `{{LEGEND:…}LEGEND}`.
    # Replaces the previous zoo of container-specific promoters.
    text = _process_figures(text)

    # Rejoin words split by line-break hyphenation (`trans- \nlation` →
    # `translation`).  Must run before reflow_paragraphs, which would
    # otherwise convert the line break to a space and freeze the broken
    # form in place.
    text, _ = fix_hyphenation(text)

    # Reflow paragraphs — join lines that were hard-wrapped in the source
    text = reflow_paragraphs(text)


    # Strip leading comma/space left after title+descriptor stripping
    # (e.g. "'''BISMARCK,''' {{sc|Prince}}, duke..." → ", duke..." after transform)
    text = re.sub(r"^[\s,]+", "", text)

    # Defensive cleanup for orphan punctuation left when a template
    # gets stripped without its display text (e.g. a malformed
    # `{{1911link|X|Y}}` previously dropped, leaving `…, , Y…`):
    #   ", , , "  → ", "
    #   ", ;"     → ";"
    #   ", ."     → "."
    # Preserve `,,` adjacent (ditto marks in tables).
    text = re.sub(r",(\s+,)+", ",", text)
    text = re.sub(r",\s*([;.])", r"\1", text)

    return text




def _wrap_orphaned_table_rows(text: str) -> str:
    """Wrap orphaned wiki table rows (|- and | lines) that lack a {| opener.

    Multi-page wiki tables have {| in <noinclude> on continuation pages.
    After noinclude stripping, the rows are left bare.  Wrap them in
    {|...|} so the table converter can process them.

    Also detects runs of |lines without |- separators (two-column tables
    spanning page boundaries).
    """
    # Quick check: any lines starting with |?
    has_pipe_rows = any(
        line.strip().startswith("|") and len(line.strip()) > 3
        for line in text.split("\n")
    )
    if not has_pipe_rows:
        return text

    # Count opens and closes
    opens = len(re.findall(r"\{\|", text))
    closes = len(re.findall(r"\|\}", text))

    if "{|" in text:
        if opens > closes:
            # Unclosed table — add |} at end so balanced extractor can find it
            text = text + "\n|}"
        elif opens < closes:
            # Orphaned |} — wrap preceding rows in {|
            first_close = text.find("|}")
            prefix = text[:first_close]
            rest = text[first_close + 2:]
            text = "{|\n" + prefix + "\n|}" + rest
        # Also handle orphaned rows before the first {|
        first_table = text.find("{|")
        prefix = text[:first_table]
        rest = text[first_table:]
        if prefix.strip() and ("\n|-" in prefix or prefix.strip().startswith("|-")):
            wrapped_prefix = _wrap_orphaned_table_rows(prefix)
            return wrapped_prefix + rest
        return text

    # Find runs of |lines and wrap them
    lines = text.split("\n")
    first_row = None
    last_row = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_table_line = (
            (stripped.startswith("|-") or stripped.startswith("|"))
            and len(stripped) > 3
            and not stripped.startswith("|}")
        )
        if is_table_line:
            if first_row is None:
                first_row = i
            last_row = i

    if first_row is None:
        return text

    # Wrap the table rows
    before = "\n".join(lines[:first_row])
    table = "\n".join(lines[first_row:last_row + 1])
    after = "\n".join(lines[last_row + 1:])
    parts = []
    if before.strip():
        parts.append(before)
    parts.append("{|\n" + table + "\n|}")
    if after.strip():
        parts.append(after)
    return "\n".join(parts)




def transform_articles(volume: int) -> int:
    """Transform raw wikitext to internal marker format for all articles in a volume.

    Transforms each segment (page-sized) individually, then joins them
    into article.body with \\x01PAGE:N\\x01 markers at page boundaries.
    The markers are injected after transformation so they survive the
    control-character stripping in clean_pages.

    Processes one article at a time with per-article commits.
    """
    session = SessionLocal()
    try:
        article_ids = [
            aid for (aid,) in session.query(Article.id)
            .filter(Article.volume == volume)
            .all()
        ]

        for aid in article_ids:
            article = session.get(Article, aid)
            segments = (
                session.query(ArticleSegment)
                .join(SourcePage, ArticleSegment.source_page_id == SourcePage.id)
                .filter(ArticleSegment.article_id == aid)
                .order_by(ArticleSegment.sequence_in_article)
                .add_columns(SourcePage.page_number)
                .all()
            )

            is_plate = article.article_type == "plate"

            if is_plate:
                # Plates are single pages — process directly
                raw = segments[0][0].segment_text if segments else ""
                from britannica.parsers.plate import parse_plate
                article.body = parse_plate(raw) if raw else ""
            else:
                # Join raw segments with page markers, then transform once.
                raw_parts = []
                for seg, page_number in segments:
                    raw = seg.segment_text or ""
                    # Always emit the page marker, even for empty/untranscribed pages
                    raw_parts.append(f"\x01PAGE:{page_number}\x01{raw}")
                joined_raw = "\n".join(raw_parts)

                # Fix cross-page hyphenation: con-\n\x01PAGE:N\x01tinuation
                joined_raw = re.sub(
                    r"(\w)-\n(\x01PAGE:\d+\x01)(\w)",
                    r"\1\2\3", joined_raw,
                )
                article.body = _transform_text_v2(
                    joined_raw, volume,
                    segments[0][1] if segments else 0,
                ) if joined_raw else ""
                # Strip redundant title qualifier from body start.
                # e.g. title "YORK, HOUSE OF" → body starts "(House of),"
                if article.body and ", " in article.title:
                    qualifier = article.title.split(", ", 1)[1]
                    # Strip formatting markers for matching
                    body_clean = re.sub(
                        r"[\u00ab\u00bb](?:SC|/SC|I|/I|B|/B)[\u00ab\u00bb]",
                        "", article.body[:200],
                    )
                    paren_q = f"({qualifier})"
                    if body_clean.lstrip("\x01PAGE:0123456789").lstrip().lower().startswith(paren_q.lower()):
                        # Strip the parenthetical qualifier from actual body
                        article.body = re.sub(
                            r"^(\x01PAGE:\d+\x01)?\s*\([^)]*\)[,;\s]*",
                            r"\1", article.body,
                        )
            session.commit()
            session.expire_all()

        return len(article_ids)
    finally:
        session.close()
