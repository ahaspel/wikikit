"""Wiki-table renderers.

Each handler takes the table's inner content (delimiters stripped,
child elements placeholdered) and returns a marker-form string for the
viewer to render.

Dispatch lives in ``elements/__init__.py``; classification logic in
``_classify_table`` chooses which renderer to call.
"""

from __future__ import annotations

import re

from britannica.captions import clean_caption, strip_cell_attrs
from britannica.markers import build_table_cell
from britannica.pipeline.stages.elements._image import _process_image_from_raw
from britannica.pipeline.stages.elements._leaf import (
    _format_structural_formula,
    _is_structural_formula,
)
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, IMAGE_LABELS, _PH)
from britannica.pipeline.stages.elements._text import (
    _convert_inline_sub_sup,
    _strip_br,
)
from britannica.pipeline.stages.transform_articles.body_text import (
    strip_known_wrapper_tags,
)


# Wiki cell-attribute keywords — used in three places (here, _layout,
# _emit_table_marker) to identify the `attr=value | content` prefix on
# a cell.  Centralised here so every caller agrees on what counts as an
# attribute vs body content.  The trailing `[\s=|]` is load-bearing —
# bare keywords (no `=`) collide with English words like "Classics",
# "border-line", etc., which would eat real content.
_CELL_ATTR_KEYWORDS = (
    r"colspan|rowspan|width|style|align|valign|class|"
    r"cellpadding|nowrap|border|bgcolor|height"
)
_CELL_ATTR_RE = re.compile(
    r"^(?:" + _CELL_ATTR_KEYWORDS + r")[\s=|]",
    re.IGNORECASE,
)

# Internal sentinel used to mark pipes that are *inside* a protected
# span (template, wikilink, child-element placeholder) so cell-splitting
# regexes don't treat them as cell or attribute separators.  Restored to
# `|` at the end of `split_wiki_row`.
_PIPE_ESCAPE = "\x04"


def split_wiki_row(row_text: str) -> list[tuple[str, str, str]]:
    """Split a wiki-table row into ``(sep, attr_part, content)`` cells.

    * ``sep`` — ``'|'`` (data, from ``|`` or ``||``) or ``'!'``
      (header, from ``!`` or ``!!``).
    * ``attr_part`` — the cell-attribute prefix string
      (``colspan="2" style="text-align:right"`` etc.), or ``''`` when
      the cell has no attributes.
    * ``content`` — the cell's text content with protected pipes
      restored.

    Shared by the data-table renderer (``_extract_cells`` inside
    ``_process_table``), the complex-HTML renderer
    (``_process_complex_table``), and the layout-table unwrapper
    (``_unwrap_layout_table`` in ``_layout``).  Each caller used to
    re-implement this — and the implementations diverged just enough
    that the same leaked-attribute bug surfaced once per path.  Steps:

    1. Merge continuation lines into the preceding cell-line (a wiki
       cell can spill onto subsequent lines when it contains a multi-
       line ``<ref>``, ``{{hi|…}}``, etc.).
    2. Protect pipes inside ``{{…}}``, ``[[…]]`` wikilinks, and
       ``\\x03…\\x03`` child-element placeholders so they don't get
       treated as cell or attribute separators.
    3. Normalise inline ``||`` / ``!!`` separators to line-anchored
       ``\\n|`` / ``\\n!`` so every cell becomes its own line.
    4. Split each cell-line into ``(sep, attr_part, content)`` via
       ``rpartition('|')``: the part before the last ``|`` is the
       attribute prefix iff it matches ``_CELL_ATTR_RE`` (or it's
       empty / pure ``{{Ts|…}}`` styling).  Otherwise the entire body
       is content with empty attrs.
    """
    # 1. Merge continuation lines AND skip `|+` caption lines (which
    # are extracted at the table-processor level before this function
    # is called).  The `|+` filter MUST run before `||` normalisation
    # below — otherwise a cell whose content starts with `+`
    # (ALGEBRAIC FORMS' `+B₁a₀` math operator) gets normalised to
    # `|+B₁a₀` and incorrectly filtered as a caption.
    merged: list[str] = []
    for ln in row_text.split("\n"):
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith("|+"):
            continue
        if stripped.startswith(("|", "!")) or stripped == "{|":
            merged.append(ln)
        elif merged:
            merged[-1] = merged[-1].rstrip() + " " + stripped
        else:
            merged.append(ln)
    text = "\n".join(merged)

    # 2. Protect pipes inside templates / wikilinks / placeholders.
    text = re.sub(r"\{\{[^}]*\}\}",
                  lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text)
    text = re.sub(r"\[\[[^\]]*\]\]",
                  lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text)
    text = re.sub(
        re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+" + re.escape(_PH),
        lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text,
    )

    # 3. Inline cell-separator normalisation.
    text = text.replace("||", "\n|").replace("!!", "\n!")

    # 4. Per-cell attr / content split.
    cells: list[tuple[str, str, str]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line in ("}", "{|"):
            continue
        if not line.startswith(("|", "!")):
            continue
        sep = line[0]
        body = line[1:].strip()
        if "|" in body:
            attr_part, _, content = body.rpartition("|")
            attr_check = re.sub(
                r"\{\{[Tt]s\|[^{}]*\}\}\s*", "",
                attr_part.replace(_PIPE_ESCAPE, "|"),
            ).strip()
            if attr_check and not _CELL_ATTR_RE.match(attr_check):
                # Not a real attribute prefix — keep whole body as
                # content (this is the case for chemistry rows like
                # `«I»d«B»ᵢ«/I» = 1.2 «I»r«/B»ᵢ«/I» | rowspan=3 | …`
                # where the leading text isn't an attribute).
                attr_part, content = "", body
        else:
            attr_part, content = "", body
        cells.append((
            sep,
            attr_part.replace(_PIPE_ESCAPE, "|").strip(),
            content.replace(_PIPE_ESCAPE, "|").strip(),
        ))
    return cells


# ── Syntax-neutral table-structure primitives ─────────────────────────
#
# The shape classifiers (ICL / verse / data) ask structural questions —
# "is the image alone in its row?", "are images in parallel row-0 cells?",
# "is there a header / caption?" — that are identical for `{|…|}` and
# `<table>…</table>`; only the surface markers differ.  These primitives
# answer the row/cell question once, syntax-detected, so a single
# predicate serves both encodings (remove-nontables-from-table-path).
# RECOGNITION-only: row/cell boundaries are parsed and cell CONTENT is
# returned raw/untransformed — no text_transform, nothing flows
# differently to any producer.
_HTML_TABLE_TAG_RE = re.compile(r"<t[rdh]\b", re.IGNORECASE)


def _html_table_grid(inner: str) -> list[list[tuple[str, str, str]]]:
    """Rows × ``(sep, attr_part, content)`` for an HTML `<table>` inner —
    the canonical cell shape `split_wiki_row` returns, so HTML and wiki cells
    are the same triple.  ``sep`` is ``'!'`` for a header cell (`<th>`),
    ``'|'`` for a data cell (`<td>`).

    Robust to the unclosed `</td>`/`</tr>` the source sometimes emits
    (the malformed markup that zeroed MAGNETISM / SATURN): cells are the
    text after each `<td>`/`<th>` opener, cut at the next cell/row/table
    boundary whether or not a closing tag is present.

    Every cell carries its sep AND its attr — they are part of the cell, and
    the surest way to keep them is never to drop them.  A consumer that wants
    content only discards them at the point of use; the extractor never
    pre-empts that choice."""
    rows: list[list[tuple[str, str, str]]] = []
    for row in re.split(r"<tr\b[^>]*>", inner, flags=re.IGNORECASE):
        cells: list[tuple[str, str, str]] = []
        # Capturing split: parts = [pre, tag1, attr1, content1, tag2, …].
        parts = re.split(r"<(t[dh])\b([^>]*)>", row, flags=re.IGNORECASE)
        for i in range(1, len(parts), 3):
            content = re.split(
                r"</t[dh]>|</tr>|</table>|<tr\b", parts[i + 2],
                maxsplit=1, flags=re.IGNORECASE)[0].strip()
            sep = "!" if parts[i].lower() == "th" else "|"
            cells.append((sep, parts[i + 1], content))
        if cells:
            rows.append(cells)
    return rows


def _table_grid(inner: str) -> list[list[str]]:
    """Rows × cell-content-strings for a wiki OR HTML table, syntax-detected.

    The `{|` path uses the same `|-` row split + `split_wiki_row` cells the
    ICL helpers already inline, so converting those helpers to this
    primitive is label-preserving by construction; the `<table>` path
    delegates to `_html_table_grid`."""
    if _HTML_TABLE_TAG_RE.search(inner):
        # Content-only view: drop sep+attr at point of use, exactly as the
        # wiki branch below drops them from `split_wiki_row`.
        return [[content for _sep, _attr, content in row]
                for row in _html_table_grid(inner)]
    grid: list[list[str]] = []
    for row in re.split(r"\|-[^\n]*", inner):
        cells = [content for _sep, _attr, content in split_wiki_row(row)]
        if cells:  # drop empty segments (e.g. a leading `|-` row separator)
            grid.append(cells)
    return grid


def _strip_wiki_cell_attr_in_html(text: str) -> str:
    """Strip wiki cell-attribute syntax (``|attr=val|``) that leaked
    into HTML ``<td>`` cell content.

    Some source authors mix syntaxes inside an HTML ``<table>`` —
    CALENDAR (vol 4 pp. 1028-30) and HYDRAULICS (vol 14 p. 120) both
    write ``<td>content|rowspan=3|content</td>``, expecting the wiki
    ``|rowspan=3|`` to be interpreted as a cell attribute.  Wiki
    syntax doesn't apply inside HTML, so the ``|rowspan=3|`` would
    leak as raw text through ``<td>`` content.  This strip removes
    the wiki-attr token (visual cell-spanning is lost, but the
    surrounding math/text content survives clean)."""
    return re.sub(
        r"\|\s*(?:colspan|rowspan|style|align|valign|width|class|bgcolor|"
        r"cellpadding|cellspacing|border|height)\s*=[^|\n]*\|",
        " ", text, flags=re.IGNORECASE,
    )


def parse_wiki_table(
    text: str,
) -> tuple[str, str, list[list[tuple[str, str, str]]], list[str]]:
    """Parse a wiki-table's structure into ``(opener_attrs, caption, rows)``.

    The whole-table counterpart to :func:`split_wiki_row` (which
    operates on one row at a time).  Steps:

    1. Capture the outer ``{|<attrs>\\n`` opener's attribute string
       (whole-table styling: ``{{Ts|ma|bc|fwb}}``, ``class="…"``,
       ``style="…"``, etc.) — previously dropped on the floor, now
       returned so producers can apply the styling to the rendered
       ``<table>``.  Strip outer ``\\n?|}`` closer.  Accepts either
       form (``raw`` or ``inner``) for caller convenience.
    2. Strip ``<br>`` tags (soft-hyphen-aware via :func:`_strip_br`:
       a ``-<br>`` line-break drops both halves so ``Circum-<br>ference``
       renders as ``Circumference``; plain ``<br>`` becomes a space).
    3. Extract the first ``|+ caption`` line as the table caption.
    4. Split on ``|-`` row separators (line-anchored).
    5. Run each row through :func:`split_wiki_row`; drop empty rows.

    Returns:
    * ``opener_attrs`` — whole-table attribute string (empty if no
      opener present, ``raw``-form callers).
    * ``caption`` — caption text (empty if no ``|+`` line).
    * ``rows`` — list of rows, each a list of
      ``(sep, attr_part, content)`` cells.

    Limitations: ``|-`` splitting is NOT depth-aware — it splits on
    ``|-`` inside nested ``{|…|}`` blocks too.  Callers that need to
    preserve nested-table structure (currently only
    ``_process_compound_table``) must do their own depth-tracking
    row split and call :func:`split_wiki_row` per row directly."""
    opener_attrs = ""
    opener_m = re.match(r"^\{\|([^\n]*)\n?", text)
    if opener_m:
        opener_attrs = opener_m.group(1).strip()
        text = text[opener_m.end():]
    text = re.sub(r"\n?\|\}\s*$", "", text)

    # Lossless `<br>` fidelity: keep the source/print's deliberate line breaks
    # (stacked-list data rows, wrapped multi-line headers) as breaks rather than
    # flattening to a space — the transcriber hand-aligns columns FOR break
    # rendering, so this is what makes them line up.  `-<br>` soft-hyphen joins
    # still apply.  `<br>` is not a row/cell delimiter, so it doesn't disturb the
    # `|+`/`|-`/`|` splitting below.
    text = _strip_br(text, "<br>")

    caption = ""
    cap_match = re.search(r"^\|\+\s*(.+?)$", text, re.MULTILINE)
    if cap_match:
        caption = cap_match.group(1).strip()

    # Capturing split keeps each row's `|-<attr>` styling (e.g. `|-{{Ts|ac}}`)
    # so producers can emit `<tr style=…>` — that row's cells inherit it (the
    # middle rung of the table/row/cell cascade), previously dropped on the
    # floor.  `parts` = [row0, attr1, row1, attr2, row2, …]; row0 precedes the
    # first `|-` and so has no row attr.
    parts = re.split(r"(?:^|\n)\|-([^\n]*)", text)
    pending: list[tuple[str, str]] = [("", parts[0])]
    for k in range(1, len(parts), 2):
        pending.append((parts[k], parts[k + 1]))
    rows: list[list[tuple[str, str, str]]] = []
    row_attrs: list[str] = []
    for attr, raw_row in pending:
        cells = split_wiki_row(raw_row)
        if cells:
            rows.append(cells)
            row_attrs.append(attr.strip())
    return opener_attrs, caption, rows, row_attrs


def emit_html_cell(
    tag: str,
    content: str,
    *,
    rowspan: int = 1,
    colspan: int = 1,
    styles: list[str] | None = None,
) -> str:
    """Build one HTML table cell with the standard attribute
    serialisation.

    * ``tag`` — ``'td'`` or ``'th'``.
    * ``content`` — already-cleaned cell content (templates expanded,
      entities decoded, etc.).
    * ``rowspan`` / ``colspan`` — integer cell-span counts; the
      attribute is emitted only when ``> 1``.
    * ``styles`` — list of CSS declarations (e.g.
      ``['text-align:right', 'vertical-align:top']``); joined with
      ``;`` and emitted as a single ``style="…"`` attribute when
      non-empty.

    Shared by ``_process_complex_table`` (wiki ``{|…|}`` with spans →
    HTML) and ``_process_html_table`` (HTML ``<table>`` → marker).
    Centralising the serialisation here keeps the two output paths
    byte-identical for the same logical cell."""
    attrs = ""
    if rowspan > 1:
        attrs += f' rowspan="{rowspan}"'
    if colspan > 1:
        attrs += f' colspan="{colspan}"'
    if styles:
        attrs += f' style="{";".join(styles)}"'
    return f"<{tag}{attrs}>{content}</{tag}>"


def _emit_table_marker(
    text_rows: list[str], header: bool = False,
    styles: list[str] | None = None,
) -> str:
    """Join row strings into a ``{{TABLE:…}TABLE}`` / ``{{TABLEH:…}TABLE}`` marker.

    Data tables (``header=False``): canonical form — one space on each
    side of every ``|`` separator, multi-space runs collapsed (an empty
    cell renders as ``a | | b`` not ``a |   | b``), blank rows removed.
    The renderer emits canonical output directly so no downstream
    pipe-normalisation pass is needed.

    Header tables (``header=True``): rows are joined raw.  Normalizing
    header tables is a deliberate-change item (burndown) — would change
    shipped output for ``{{TABLEH:`` tables that haven't been touched
    since the historical ``\\{\\{TABLE:`` cleanup regex (which never
    matched the ``H`` suffix) was deleted.

    Whole-table styling (``styles``) — when the source ``{|<attrs>``
    opener carries ``{{Ts|ma|sm92|…}}`` etc., it is extracted by
    :func:`_table_opener_styles` and emitted as
    ``{{TABLE[style:margin:0 auto;font-size:92%]:rows}TABLE}``.  The
    renderer reads the optional ``[style:…]`` slot and applies it to the
    ``<table>`` element.  Absent / empty ``styles`` → unchanged marker.
    """
    content = "\n".join(text_rows)
    if not header:
        content = re.sub(r"(?<! )\|", " |", content)
        content = re.sub(r"\|(?! )", "| ", content)
        content = re.sub(r"  +", " ", content)
        content = re.sub(r"\n\s*\n", "\n", content)
    tag = "TABLEH" if header else "TABLE"
    style_slot = (f"[style:{';'.join(styles)}]" if styles else "")
    return "{{" + tag + style_slot + ":" + content + "}TABLE}"


def _extract_subtable_values(table_text: str) -> list[str]:
    """Extract cell values from a nested sub-table (single-column layout)."""
    values = []
    for line in table_text.split("\n"):
        line = line.strip()
        if line.startswith("|-") or line.startswith("{|") or line == "|}":
            continue
        if line.startswith("|"):
            cell = line[1:].strip()
            # Strip attributes: everything before last |
            if "|" in cell:
                cell = cell.rpartition("|")[2].strip()
            # Strip {{Ts}} and other templates
            cell = re.sub(r"\{\{[^{}]*\}\}\s*", "", cell).strip()
            values.append(cell)
    return values


def _process_compound_table(raw: str, text_transform) -> str:
    """Process a data table with nested sub-tables in cells.

    These tables have parallel sub-tables (one per column) where each
    sub-table lists values vertically.  We zip the sub-table rows together
    to reconstruct the intended grid layout.

    Self-contained: works from raw wikitext, does not use the recursive
    extract/process pipeline, so it cannot affect other table types.
    """
    # Strip outer delimiters
    inner = re.sub(r"^\{\|[^\n]*\n?", "", raw)
    inner = re.sub(r"\n?\|\}\s*$", "", inner)

    # Split into outer rows on top-level |- only (not inside nested tables)
    outer_rows = []
    current = []
    depth = 0
    for line in inner.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{|"):
            depth += 1
        elif stripped == "|}":
            depth -= 1
        if stripped.startswith("|-") and depth == 0:
            outer_rows.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current:
        outer_rows.append("\n".join(current))

    html_rows = []

    for row_text in outer_rows:
        # Check if this row contains nested sub-tables
        subtables = list(re.finditer(
            r"\{\|.*?\|\}", row_text, re.DOTALL))

        if subtables:
            # Extract values from each sub-table
            all_values = [_extract_subtable_values(m.group(0))
                          for m in subtables]
            n_rows_list = [len(v) for v in all_values]

            if n_rows_list and len(set(n_rows_list)) == 1 and n_rows_list[0] > 0:
                # Parallel sub-tables — zip into rows
                for i in range(n_rows_list[0]):
                    cells = []
                    for vs in all_values:
                        content = vs[i]
                        content = re.sub(r"&nbsp;", " ", content)
                        content = _strip_br(content)
                        content = content.strip()
                        if content and text_transform:
                            content = text_transform(content)
                        cells.append(f"<td>{content}</td>")
                    html_rows.append("<tr>" + "".join(cells) + "</tr>")
            else:
                # Unequal sub-tables — flatten each to <br>-joined content
                for m in subtables:
                    values = _extract_subtable_values(m.group(0))
                    content = "<br>".join(
                        text_transform(v) if text_transform and v.strip()
                        else v for v in values)
                    html_rows.append(f"<tr><td>{content}</td></tr>")
        else:
            # Regular row (no nested tables) — share the wiki-cell
            # extraction with the other table paths.  Compound-table
            # specialisation only kicks in for rows containing nested
            # `{|…|}` sub-tables (the branch above); rows without
            # nesting are just normal wiki rows whose cells get joined
            # into a single `<tr>` of `<td>`s.
            cells_html = []
            for sep, _attr, content in split_wiki_row(row_text):
                tag = "th" if sep == "!" else "td"
                content = re.sub(r"\{\{[^{}]*\}\}", "", content)
                content = re.sub(r"&nbsp;", " ", content)
                content = _strip_br(content)
                content = re.sub(r"<td[^>]*>", "", content,
                                 flags=re.IGNORECASE)
                # Cell-content wrapper-strip: enumerated tag set the
                # complex-table cell renders inline content of (e.g.
                # AFRICA's `<span style="border-bottom..." title="…">`
                # transcriber-amendment annotations).  Shared toolkit
                # utility; the producer composes it like any other rule.
                content = strip_known_wrapper_tags(content)
                content = content.strip()
                if content and text_transform:
                    content = text_transform(content)
                cells_html.append(emit_html_cell(tag, content))

            if cells_html:
                html_rows.append("<tr>" + "".join(cells_html) + "</tr>")

    if not html_rows:
        return ""

    return ("\u00abHTMLTABLE:<table>" + "".join(html_rows)
            + "</table>\u00ab/HTMLTABLE\u00bb")


def _complex_cell_body(attr_part: str, content: str,
                       text_transform) -> tuple[list[str], str]:
    """Cell-body strategy for `_process_complex_table` (the `cell_body`
    hook of `produce_table_rows`).

    Reproduces the producer's image-vs-text BRANCH: a cell that is
    wholly an `[[Image:…]]`/`[[File:…]]` becomes a `{{IMG:filename}}`
    marker, skipping `text_transform` AND the leftover-`{{}}` strip that
    would otherwise delete the marker.  Non-image cells run the bespoke
    clean: drop stray image links, `{{ditto}}`→″, `{{…}}`→…, lossless
    `<br>`, body-text, strip leftover templates, strip wrapper tags.

    TRANSITIONAL (see `CellBody` in `_table_decompose`): the end-state
    moves the `{{ditto}}`/`{{…}}`/image template handling into body-text
    so this branch dissolves and the producer reverts to the default
    `produce_cell`.  `_cell_styles` is scoped to `attr_part` only (it
    ignores `content`), so the cell's full styling is independent of the
    body clean above.
    """
    styles = _cell_styles(attr_part, content)
    img_m = re.match(r"\s*\[\[(?:Image|File):([^|\]]+)[^\]]*\]\]\s*$",
                     content, re.IGNORECASE)
    if img_m:
        return styles, f"{{{{IMG:{img_m.group(1).strip()}}}}}"
    c = re.sub(r"\[\[(?:Image|File):[^\]]*\]\]", "", content,
               flags=re.IGNORECASE)
    c = re.sub(r"\{\{ditto(?:\|[^{}]*)?\}\}", "″", c, flags=re.IGNORECASE)
    c = re.sub(r"\{\{\.\.\.\}\}", "...", c)
    # Lossless `<br>` fidelity (see _html_cell_clean): keep the
    # source/print's deliberate line breaks (stacked-list rows, wrapped
    # headers); `-<br>` soft-hyphen joins still apply.  emit_html_cell
    # outputs literal HTML, so the `<br>` renders as a break.
    c = _strip_br(c, "<br>")
    c = c.strip()
    # Run text_transform FIRST so it converts templates it knows about
    # (`{{sfrac|…}}`, `{{hi|…}}`, `{{sc|…}}`) before the catch-all strip
    # below eats every unlabelled template.
    if c:
        c = text_transform(c)
    c = re.sub(r"\{\{[^{}]*\}\}", "", c)
    # Cell-content wrapper-strip: enumerated HTML tags whose inner content
    # the cell renders directly (AFRICA's transcriber-annotation spans).
    c = strip_known_wrapper_tags(c)
    return styles, c


def _process_complex_table(raw: str, inner: str, text_transform) -> str:
    """Convert a wiki table with rowspan/colspan to HTML.

    Strategy: each cell in wiki markup has the form
        {{ts|style}} rowspan=N colspan=M {{ts|style}}| content
    Everything before the last | is attributes; everything after is content.
    We keep only rowspan/colspan from the attributes and transform the content.
    Pipes inside {{...}} are protected so they don't confuse the split.

    Receives both:
    * `raw` — the full wikitable text (``{|<attrs>…|}``), used to extract
      whole-table styling from the opener attrs that ``strip_outer`` would
      otherwise discard.
    * `inner` — delimiters already stripped, child elements replaced with
      placeholders, so nested elements like <math> are preserved.
    """

    # Decompose via the shared `produce_table_rows` (the one row/cell
    # split + span detection loop), supplying the complex producer's own
    # image-vs-text cell strategy.  Replaces the bespoke
    # `parse_wiki_table` + inline cell loop this producer used to carry —
    # the duplicated decomposition now lives in one place.
    from britannica.pipeline.stages.elements._table_decompose import (
        produce_table_rows,
    )
    caption_raw, produced_rows, _has_header, _has_span = produce_table_rows(
        inner, text_transform, flavor="wiki", cell_body=_complex_cell_body)
    # Whole-table styling extracted from `raw` (not `inner`, which has the
    # ``{|<attrs>\n`` opener already stripped by `strip_outer`):
    # `{|{{Ts|ma|bc|fwb}}` → `<table style="margin:0 auto;…">` so the
    # rendered table inherits margin-auto / borders / etc.
    table_styles = _table_opener_styles(raw)
    table_style_attr = (f' style="{";".join(table_styles)}"'
                        if table_styles else "")
    # Preserve the source's `class="…"` (e.g. `wikitable`) — a carried signal
    # the viewer decodes via its OWN `.wikitable` CSS.  The scans show wikitable
    # tables ARE bordered and class-less ones borderless (AUSTRIA: 5/5), so the
    # class faithfully tracks borders; we carry the class, not MediaWiki's CSS.
    cls_m = re.search(r'class\s*=\s*(?:"([^"]*)"|([^\s|{}]+))', raw)
    cls = ((cls_m.group(1) or cls_m.group(2) or "").strip()) if cls_m else ""
    table_class_attr = f' class="{cls}"' if cls else ""
    caption_html = ""
    if caption_raw:
        cap_text = text_transform(caption_raw)
        if cap_text:
            # Strip any wiki cell-attribute prefix that survived
            # `text_transform` — when the caption line itself bears cell
            # styling (``|+ |style="…"|TEXT``, METEOR / PHOTOGRAPHY), the
            # attribute would otherwise leak into the rendered `<caption>`.
            # `strip_cell_attrs`, not `clean_caption`: the HTMLTABLE
            # caption renders its `«SC»` / `«I»` / `«B»` markers as real
            # small-caps / italics / bold, so we must NOT unwrap them
            # here (ACCUMULATOR's «SC»Table I.«/SC», ALPACA's «I»Alpaca«/I»…).
            cap_text = strip_cell_attrs(cap_text)
        if cap_text:
            caption_html = f"<caption>{cap_text}</caption>"

    html_rows = []

    for row_attr, parsed in produced_rows:
        cells_html = [
            emit_html_cell(tag, content,
                           rowspan=rowspan, colspan=colspan, styles=styles)
            for tag, rowspan, colspan, content, styles in parsed
        ]

        if cells_html:
            # Carry the row's `|-{{Ts|…}}` styling onto `<tr>` — that row's
            # cells inherit it (the middle rung of the cascade); cells with
            # their own ts override it.  AUSTRIA's `|-{{Ts|ac}}` header row.
            row_styles = _cell_styles(row_attr, "") if row_attr else None
            row_style_attr = (f' style="{";".join(row_styles)}"'
                              if row_styles else "")
            html_rows.append(f"<tr{row_style_attr}>"
                             + "".join(cells_html) + "</tr>")

    if not html_rows:
        return ""

    # Pull out leading colspan rows that contain images or captions —
    # these are header material that belongs above the table, not in it.
    preamble = []
    while html_rows:
        row = html_rows[0]
        # Check if every cell in this row has colspan (full-width row)
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        has_colspan = 'colspan=' in row
        if has_colspan and len(cells) == 1:
            content = cells[0].strip()
            if content:
                preamble.append(content)
            html_rows.pop(0)
        else:
            break

    parts = []
    for p in preamble:
        parts.append(p)
    if html_rows:
        parts.append("\u00abHTMLTABLE:<table" + table_class_attr +
                     table_style_attr + ">" +
                     caption_html +
                     "".join(html_rows) + "</table>\u00ab/HTMLTABLE\u00bb")
    return "\n\n".join(parts)


def _inline_table_marker_as_html(marker: str) -> str:
    """Convert a ``{{TABLE[style:\u2026]:row\\n\u2026}TABLE}`` marker to inline
    ``<table class="nested-data-table">\u2026</table>`` HTML.

    Used by HTML-emitting parent producers (HTMLTABLE) to render their
    wiki-table CHILDREN inline, so a nested wiki ``{|\u2026|}`` inside an
    HTML ``<table>`` cell (ORNITHOLOGY taxonomic alignments, EOCENE
    etymology glossary inside a ``<ref>``) doesn't leak its
    ``{{TABLE:\u2026}TABLE}`` marker as cell text.

    Lossy by design \u2014 drops ``\u27e6\u2026\u27e7`` cell-layout prefixes and the
    ``[style:\u2026]`` slot.  The full styling path would route nested wiki
    tables through ``_process_complex_table``'s HTML emitter; this
    helper covers the small/simple-table case where that's overkill.
    """
    m = re.match(r"^\{\{TABLE(?:\[style:[^\]]*\])?:(.*)\}TABLE\}$",
                 marker, re.DOTALL)
    if not m:
        return marker
    inner = m.group(1)
    rows_out: list[str] = []
    for row in inner.split("\n"):
        row = row.strip()
        if not row:
            continue
        cells = [c.strip() for c in row.split("|")]
        cells_html = "".join(f"<td>{c}</td>" for c in cells if c)
        if cells_html:
            rows_out.append(f"<tr>{cells_html}</tr>")
    if not rows_out:
        return ""
    return ('<table class="nested-data-table">' + "".join(rows_out)
            + "</table>")


# \u2500\u2500 Chemistry-reaction / structural-formula layouts \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

# ``Langle``/``Rangle`` plus the variant suffixes the corpus uses
# (``LangleBar``, ``LangleIT``, ``LangleIB``, ``RangleBar``, …) — all
# the EB1911 valence-bracket images.  ``[A-Za-z]*`` after ``angle`` is
# wide but the false-positive surface is nil: it only matters for a
# ``{|…|}`` raw, and the only ``[LR]angle*.svg/png`` files in this
# corpus are these brackets.
_CHEM_BRACKET_IMG_RE = re.compile(
    r"\[\[(?:File|Image):\s*[LR]angle[A-Za-z]*\.(?:svg|png)", re.IGNORECASE)


def _has_chem_brackets(registry: ElementRegistry | None) -> bool:
    """Recursively scan a registry tree for chemistry-bracket file refs
    (``[[File:Langle*.svg]]`` / ``[[File:Rangle*.svg]]``).

    A wiki table classifies as CHEMISTRY_LAYOUT when any of its
    descendants is such a bracket \u2014 the EB1911 chemistry-reaction
    diagrams use these images to typeset ``\u3008`` / ``\u3009`` valence
    chevrons.  About 36 such tables corpus-wide, clustered in the
    organic-chemistry runs (FULMINIC ACID, POLYMETHYLENES, PURIN,
    INDAZOLES, \u2026).

    Walks ``registry.elements`` for IMAGE-typed entries whose raw
    bytes match the bracket regex, plus recurses into
    ``inner_registries`` for nested tables.  This replaced an earlier
    ``_is_chemistry_layout(raw)`` that regex-scanned the whole table's
    raw bytes \u2014 that version could in principle false-match
    Langle/Rangle text in prose; this one inspects only properly
    extracted IMAGE elements."""
    if registry is None:
        return False
    for placeholder, label in registry.labels.items():
        if label in IMAGE_LABELS:
            raw = registry.elements[placeholder][1]
            if _CHEM_BRACKET_IMG_RE.search(raw):
                return True
        child_registry = registry.inner_registries.get(placeholder)
        if child_registry is not None and _has_chem_brackets(child_registry):
            return True
    return False


# A chemical-reaction table that typesets operators / brackets with
# `<big>+</big>` / `<math>\Big[` instead of the Langle/Rangle SVG images
# `_has_chem_brackets` keys on.  Signal: a `<big>` arithmetic operator AND a
# chemical `<sub>` formula in the same table — e.g. ACCUMULATOR's discharge
# (`«I»x«/I». PbO<sub>2</sub> … <big>+</big>`) and energy
# (`PbO<sub>2</sub><big>+</big>2H<sub>2</sub>SO<sub>4</sub> ＝ …`) reactions, and
# the acetone synthesis (vol 15).  Tight by audit: 5 such tables corpus-wide,
# zero math-layout / taxonomy false-positives.
_CHEM_BIG_OP_RE = re.compile(r"<big>\s*[-+−±=＋＝]")
_CHEM_FORMULA_RE = re.compile(r"[A-Z][a-z]?<sub>\s*\d")


def _has_chem_equation_content(raw: str) -> bool:
    """True for a chemical-reaction table that uses `<big>` operators + `<sub>`
    formulae rather than Langle/Rangle bracket images (see `_has_chem_brackets`)."""
    return bool(_CHEM_BIG_OP_RE.search(raw) and _CHEM_FORMULA_RE.search(raw))


# Element-aware chemical-reaction recognizer.  Recognizes a reaction by its
# CONTENT -- real molecular formulae joined by a reaction operator -- rather
# than by surface markup, so reactions typeset with plain =/+/-> operators and
# {{sub}}/<sub>/unicode formulae are caught even when they carry none of the
# Langle/Rangle SVG brackets (`_has_chem_brackets`) or <big>-operator + <sub>
# signals (`_has_chem_equation_content`).  This freezes the domain knowledge
# (periodic table + formula grammar) those surface gates miss; without it such
# reactions fall through to DATA_TABLE / SINGLE_COLUMN.  Validated corpus-wide:
# flags 27 {| tables (11 already CHEMISTRY_LAYOUT, 16 misrouted), zero
# math-layout / taxonomy false-positives.  `_chem_row_is_reaction` takes
# already-joined cell text so it serves <table> cell extraction too (see #12).
_CHEM_ELEMENTS = frozenset("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K
Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Ru Rh Pd
Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta
W Os Ir Pt Au Hg Tl Pb Bi Th U""".split())
_CHEM_REACTION_OP = re.compile("[=＝→⟶]")        # = , = , -> , -->
_CHEM_ELEM_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")
_CHEM_SUB_DIGITS = str.maketrans(
    "₀₁₂₃₄₅₆₇₈₉",
    "0123456789")


def _chem_normalize(s: str) -> str:
    """Flatten subscript markup (<sub>, {{sub}}, unicode) to bare digits and
    strip layout templates / markers so formula tokens can be parsed."""
    s = re.sub(r"<sub>\s*(\d+)\s*</sub>", r"\1", s, flags=re.IGNORECASE)
    s = re.sub(r"\{\{\s*sub\s*\|\s*(\d+)\s*\}\}", r"\1", s, flags=re.IGNORECASE)
    s = re.sub("«/?[A-Z]+»", "", s)       # marker runs (italic etc.)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)             # residual templates
    s = (s.replace("&nbsp;", " ").replace("&emsp;", " ").replace("&ensp;", " ")
          .replace("·", "").replace("<big>", "").replace("</big>", ""))
    return s.translate(_CHEM_SUB_DIGITS)


def _is_chem_formula(tok: str) -> bool:
    """A token is a molecular formula iff it parses entirely as element symbols
    (+ counts, parens, R/X/Y organic placeholders) AND contains H/O/N."""
    if not re.sub(r"[()\[\]0-9\s]", "", tok):
        return False
    elems: list[str] = []
    pos = 0
    while pos < len(tok):
        m = _CHEM_ELEM_TOKEN.match(tok, pos)
        if m and m.group(1) in _CHEM_ELEMENTS:
            elems.append(m.group(1))
            pos = m.end()
        elif tok[pos] in "()[]·0123456789 " or tok[pos] in "RXY":
            pos += 1
        else:
            return False                             # a non-element letter
    return len(elems) >= 2 and bool({"H", "O", "N"} & set(elems))


def _chem_row_is_reaction(text: str) -> bool:
    """A reaction = >=2 molecular-formula operands (with H/O/N) joined by a
    reaction operator.  Syntax-agnostic: takes already-joined cell text, so it
    serves both `{|` rows and `<table>` cell extraction."""
    n = _chem_normalize(text)
    if not _CHEM_REACTION_OP.search(n):
        return False
    operands = [o.strip(" .,;:") for o in re.split("[=＝→⟶+]", n)
                if o.strip(" .,;:")]
    return sum(1 for o in operands
               if _is_chem_formula(o.replace(" ", ""))) >= 2


def _has_chem_reaction_content(inner: str) -> bool:
    """True if any row of a table holds an operator-connected molecular
    reaction -- the element-aware arm of the chemistry-layout predicate.
    Syntax-neutral via `_table_grid`, so `{|` and `<table>` reactions both
    route to CHEMISTRY_LAYOUT instead of falling to the table path."""
    for cells in _table_grid(inner):
        nonempty = [c for c in cells if c.strip()]
        if nonempty and _chem_row_is_reaction(" ".join(nonempty)):
            return True
    return False


def _split_chem_row(row_text: str) -> list[tuple[str, str, str]]:
    """Chem-specific row splitter — strict subset of `split_wiki_row`.

    Diverges from the shared splitter in two ways:

      * No ``|+`` caption-line filter: in chem layouts a cell starting
        with ``+`` (post print-artifact-normalisation of ``＋``) is real
        cell content, not a caption marker.
      * No continuation-line merging: chem cells use ``<br>``
        explicitly for multi-line atom stacks, so wrap lines are
        already explicit rather than being part of the cell's text.

    Otherwise the cell-splitting logic mirrors `split_wiki_row`
    (pipe protection inside templates / wikilinks / placeholders;
    ``||`` / ``!!`` normalisation; attribute-prefix detection).
    """
    text = row_text
    text = re.sub(r"\{\{[^}]*\}\}",
                  lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text)
    text = re.sub(r"\[\[[^\]]*\]\]",
                  lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text)
    text = re.sub(
        re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+" + re.escape(_PH),
        lambda m: m.group(0).replace("|", _PIPE_ESCAPE), text,
    )
    text = text.replace("||", "\n|").replace("!!", "\n!")

    cells: list[tuple[str, str, str]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line in ("}", "{|"):
            continue
        if not line.startswith(("|", "!")):
            continue
        sep = line[0]
        body = line[1:].strip()
        if "|" in body:
            attr_part, _, content = body.rpartition("|")
            attr_check = re.sub(
                r"\{\{[Tt]s\|[^{}]*\}\}\s*", "",
                attr_part.replace(_PIPE_ESCAPE, "|"),
            ).strip()
            if attr_check and not _CELL_ATTR_RE.match(attr_check):
                attr_part, content = "", body
        else:
            attr_part, content = "", body
        cells.append((
            sep,
            attr_part.replace(_PIPE_ESCAPE, "|").strip(),
            content.replace(_PIPE_ESCAPE, "|").strip(),
        ))
    return cells


def _split_html_chem_row(row_text: str) -> list[tuple[str, str, str]]:
    """`<table>` analog of `_split_chem_row`: ``(sep, attr, content)`` per
    ``<td>``/``<th>`` cell, so the chem producer's span-aware cell loop (which
    reads rowspan/colspan off ``attr`` and th-vs-td off ``sep``) serves
    `<table>` chem layouts too.  ``sep`` = ``!`` for ``<th>`` else ``|``."""
    cells: list[tuple[str, str, str]] = []
    for m in re.finditer(
            r"<(t[dh])\b([^>]*)>(.*?)(?=</t[dh]>|<t[dh]\b|</tr>|</table>|$)",
            row_text, re.IGNORECASE | re.DOTALL):
        sep = "!" if m.group(1).lower() == "th" else "|"
        content = m.group(3).strip()
        if content:
            cells.append((sep, m.group(2) or "", content))
    return cells


_CHEM_SENTINEL = {
    ("L", ""):   "\ue000",
    ("R", ""):   "\ue001",
    ("L", "IT"): "\ue002",
    ("R", "IT"): "\ue003",
    ("L", "IB"): "\ue004",
    ("R", "IB"): "\ue005",
}
_CHEM_BR_SENTINEL = "\ue006"
_CHEM_RESOLVE = {
    "\ue000": "\u276e",
    "\ue001": "\u276f",
    "\ue002": '<span class="chem-bracket-it">\u276e</span>',
    "\ue003": '<span class="chem-bracket-it">\u276f</span>',
    "\ue004": '<span class="chem-bracket-ib">\u276e</span>',
    "\ue005": '<span class="chem-bracket-ib">\u276f</span>',
    "\ue006": "<br>",
}


def _process_chemistry_layout(raw: str, inner: str, text_transform,
                              inner_registry=None) -> str:
    """Render a 2-D chemical-reaction / structural-formula layout.

    These are NOT data tables \u2014 they're spatial diagrams (no gridlines,
    no cell padding; the wiki-table syntax is just a positioning
    crutch).  They get their own marker, ``\u00abCHEM:\u2026\u00ab/CHEM\u00bb``, distinct
    from ``\u00abHTMLTABLE:\u2026\u00bb``, so the viewer can lay them out without
    table chrome.

    Self-contained parser \u2014 does NOT delegate to ``_process_complex_table``
    or ``parse_wiki_table``.  Going through the general data-table path
    introduced too many side effects for spatial diagrams:

      * ``parse_wiki_table``'s ``^\\|\\+`` caption regex misfires on
        any chem cell whose content starts with ``+`` after the upstream
        ``replace_print_artifacts`` pass converts ``\uff0b`` (FULLWIDTH PLUS)
        \u2192 ``+`` \u2014 e.g. ALDEHYDES' first equation, where the third cell
        ``|\uff0bCl\u00b7CH\u2082\u00b7COOC\u2082H\u2085`` got hoisted to ``<caption>``.
      * ``_strip_br`` flattens stacked atom-bond-atom cells
        (``CRR\u00b9<br>&vert;<br>CH\u00b7COOH``) into one line, killing the
        vertical bond rendering.

    Atom-bracket and ``<br>`` substitution is two-phase to survive
    ``text_transform``'s HTML-tag strip: each placeholder/markup is
    first replaced with a Private-Use-Area sentinel that passes
    through text-transform unmolested; we resolve the sentinels to
    final glyphs / ``<br>`` / spans after each cell is processed.

    Variants of the valence chevron:

      ``Langle.svg`` / ``Rangle.svg``     \u2014 plain heavy chevron \u2192 \u276e / \u276f
      ``LangleIT.svg`` / ``RangleIT.svg`` \u2014 "Internal line Top"
      ``LangleIB.svg`` / ``RangleIB.svg`` \u2014 "Internal line Bottom"

    IT / IB variants have no single-char Unicode match \u2014 they're
    emitted as ``<span class="chem-bracket-it">\u276e</span>`` / ``-ib``,
    with the bar drawn by a CSS ``::before`` / ``::after`` pseudo-element.
    """
    # Strip HTML comments before cell-splitting — chem layouts often carry
    # column-number markers (``<!--2--><!--3-->…``) that the bypassed Layer-A
    # ``html_comments`` pass used to remove; on raw input they leak as bogus
    # cells (ACCUMULATOR fig 22/23 row).
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    # Build placeholder -> sentinel map for Langle/Rangle images.
    angle_sentinel: dict[str, str] = {}
    if inner_registry is not None:
        for ph, label in inner_registry.labels.items():
            if label not in IMAGE_LABELS:
                continue
            eraw = inner_registry.elements[ph][1]
            m = re.match(
                r"\[\[(?:File|Image):([LR])angle(IB|IT)?\.svg",
                eraw, re.IGNORECASE,
            )
            if not m:
                continue
            side = m.group(1).upper()
            suffix = (m.group(2) or "").upper()
            angle_sentinel[ph] = _CHEM_SENTINEL[(side, suffix)]
    for ph, sentinel in angle_sentinel.items():
        inner = inner.replace(ph, sentinel)
    # Preserve in-cell <br> as a sentinel so text_transform's HTML-tag
    # strip doesn't eat the line breaks that stack atom-bond-atom.
    inner = re.sub(r"<br\s*/?>", _CHEM_BR_SENTINEL, inner, flags=re.IGNORECASE)

    # Split rows.  No caption / preamble detection \u2014 chem layouts are
    # spatial diagrams; interpreting a cell as a title strips it out.  Wiki
    # path (``|-`` + `_split_chem_row`) is untouched; a `<table>` splits on
    # `<tr>` and reads `<td>`/`<th>` via `_split_html_chem_row`.
    if _HTML_TABLE_TAG_RE.search(inner):
        raw_rows = re.split(r"<tr\b[^>]*>", inner, flags=re.IGNORECASE)
        _row_cells = _split_html_chem_row
    else:
        raw_rows = re.split(r"(?:^|\n)\|-[^\n]*", inner)
        _row_cells = _split_chem_row

    html_rows: list[str] = []
    for raw_row in raw_rows:
        if not raw_row.strip():
            continue
        cells_html: list[str] = []
        for sep, attr_part, content in _row_cells(raw_row):
            tag = "th" if sep == "!" else "td"
            rs = re.search(r'rowspan\s*=\s*"?(\d+)"?', attr_part,
                           re.IGNORECASE)
            cs = re.search(r'colspan\s*=\s*"?(\d+)"?', attr_part,
                           re.IGNORECASE)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            # Full Ts + align/valign + inline style extraction.  Previously
            # the cell-styling was discarded (the chem cell loop only kept
            # rowspan/colspan structural attrs); the producer emitted
            # bare `<td>` cells in the CHEM marker.
            styles = _cell_styles(attr_part, content)
            content = re.sub(
                r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content)
            content = content.strip()
            if content:
                content = text_transform(content)
            # Anything text_transform didn't expand is unrecognised
            # template noise (e.g. stray {{larger|...}} that the
            # template was supposed to neutralise).
            content = re.sub(r"\{\{[^{}]*\}\}", "", content)
            # Cell-content wrapper-strip: shared toolkit utility,
            # enumerated tag set, same as the BODY producer's call.
            content = strip_known_wrapper_tags(content)
            content = (content.replace("&vert;", "|")
                              .replace("&nbsp;", " "))
            for sentinel, glyph in _CHEM_RESOLVE.items():
                if sentinel in content:
                    content = content.replace(sentinel, glyph)
            cells_html.append(emit_html_cell(
                tag, content, rowspan=rowspan, colspan=colspan,
                styles=styles,
            ))
        if cells_html:
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
    if not html_rows:
        return ""
    # Whole-table styling from the `{|<attrs>` opener (extracted from
    # `raw`, since `inner` has the opener line stripped by `strip_outer`).
    # Chem layouts often carry `{|{{Ts|ma|sm92}}` for centering / font-
    # sizing \u2014 apply as inline `style="\u2026"` on the `<table>`.
    table_styles = _table_opener_styles(raw)
    table_style_attr = (f' style="{";".join(table_styles)}"'
                        if table_styles else "")
    return ("\u00abCHEM:<table" + table_style_attr + ">"
            + "".join(html_rows)
            + "</table>\u00ab/CHEM\u00bb")



# Per-cell alignment the producer must carry so the viewer renders it instead
# of defaulting to left.  Source encodes it two ways: an HTML/CSS attr
# (``align="right"`` / ``text-align:center``) or an EB1911 ``{{Ts|…}}`` style
# code (``ar`` right, ``ac`` center, ``al`` left).  Both live in the cell's
# attr-part (and sometimes leak into content), so scan both.
_CELL_ALIGN_ATTR_RE = re.compile(
    r"(?:text-)?align\s*[:=]\s*\"?\s*(right|centre|center|left)", re.IGNORECASE)
_CELL_TS_RE = re.compile(r"\{\{[Tt]s\|([^{}]*)\}\}")


# `{{Ts|...}}` code → CSS declaration.  Direct mirror of the wikisource
# `Module:Table_style/styles` + `/aliases` tables (see `_ts_codes.py` for
# the converted Python dicts).  Codes resolve identically to the way
# wikisource itself renders them: alias → canonical code → CSS string.
#
# Two fallback rules cover corpus-only patterns the Module's lookup
# table does NOT define:
#
# 1. **Missing-period decoding** (`pl15` → `pl1.5`, `lh11` → `lh110`,
#    `sm92` → `fs092` etc.) — corpus has ~1300 `p[lrtb]NN` codes whose
#    intent is decimal-em (`pl15` = ``1.5em``) but whose period was
#    dropped in the wikitext.  These render as broken CSS on wikisource
#    itself (the Module emits the literal token); we silently recover
#    the intended em-with-period form and look up again.  Without this,
#    1261 cells would carry `padding:15em` (the PADDING_SCALE
#    regression).
#
# 2. **Inline CSS passthrough** (`width:50px`, `border:1px solid red`,
#    etc.) — any `code` containing `:` is a literal CSS declaration
#    the source author wrote in `{{Ts|...}}`'s pass-through slot; emit
#    as-is.
#
# Unknown bare codes (no `:`, no Module entry, no decoding) are
# dropped silently — they're broken on wikisource too.
def _parse_ts_codes(codes_str: str) -> list[str]:
    """Parse `{{Ts|code|code|...}}` arg-string into a list of CSS
    declarations like `['text-align:right', 'padding-left:0.5em']`.
    """
    from britannica.pipeline.stages.elements._ts_codes import (
        TS_STYLES, TS_ALIASES,
    )
    rules: list[str] = []
    if not codes_str:
        return rules
    for code in re.split(r"[|\s]+", codes_str.strip()):
        if not code:
            continue
        # Inline CSS style passed through as-is: `width:50px`,
        # `margin-left:1em`, `text-align:center`, etc.  Drop trailing `;`.
        if ":" in code:
            rules.append(code.rstrip(";"))
            continue
        c = code.lower()
        # Alias → canonical (wikisource resolves these first).
        c = TS_ALIASES.get(c, c)
        # Direct Module lookup (covers ~262 canonical codes).
        style = TS_STYLES.get(c)
        if style is None:
            # Missing-period decoding: `pl15` is wikitext shorthand for
            # `pl1.5` (the period was dropped editing).  Try inserting
            # one and re-looking-up.  Restricted to known prefixes
            # (`p[lrtb]`, `plr`, `m[lrtb]`) so we don't synthesise codes
            # that happen to match the pattern but aren't real.
            if m := re.match(r"^(p[lrtb]|plr|m[lrtb])(\d)(\d+)$", c):
                guess = f"{m.group(1)}{m.group(2)}.{m.group(3)}"
                style = TS_STYLES.get(guess)
        if style:
            # Split semicolon-joined Module entries (`'ma' ↔
            # 'margin-right:auto; margin-left:auto'`) into individual
            # rules so downstream property-dedup works.
            for decl in style.split(";"):
                d = decl.strip()
                if d:
                    rules.append(d)
    return rules


def _cell_align(attr_part: str, content: str) -> str | None:
    """Resolved alignment for one cell: ``"right"``/``"center"``/``"left"`` or
    ``None`` (left default).  Thin wrapper around ``_cell_styles`` for
    callers that only need alignment (the ``{{TABLE:}`` marker can only
    encode alignment via ``⟦c⟧``/``⟦r⟧`` codes, not the full styling
    set)."""
    for rule in _cell_styles(attr_part, content):
        if rule.startswith("text-align:"):
            val = rule.split(":", 1)[1].strip()
            return val if val in ("right", "center", "left") else None
    return None


def _table_opener_styles(text: str) -> list[str]:
    """Extract CSS styling from a wiki table's opener line.

    Source ``{|<attrs>\\n…`` carries whole-table styling (``{|{{Ts|ma|bc|fwb}}``
    centers the table, ``{|class="data-table"`` adds a class, etc.) that
    was previously discarded — ``parse_wiki_table`` strips the opener
    line wholesale.  We extract via the same ``_cell_styles`` shape as
    cells, so e.g. a ``{{Ts|ma|sm92}}`` opener emits
    ``['margin:0 auto', 'font-size:92%']`` for the ``<table>`` element's
    ``style="…"`` attr.

    Returns ``[]`` if the text isn't a wikitable opener or carries no
    extractable styling."""
    m = re.match(r"^\{\|([^\n]*)", text)
    if not m:
        return []
    return _cell_styles(m.group(1).strip(), "")


def _cell_styles(attr_part: str, content: str) -> list[str]:
    """Extract the FULL styling for one cell from its attribute portion.

    Scans:
       * HTML ``align="..."``/``valign="..."`` attributes
       * Inline ``style="..."`` declarations
       * ``{{Ts|...}}`` shorthand code template(s)
    Returns a list of CSS declarations like
    ``['text-align:right', 'vertical-align:top', 'padding-left:0.5em']``.
    Empty list means "no styling beyond defaults".

    SCOPED TO ``attr_part`` ONLY.  Cell ``content`` is inline body text that
    may legitimately contain its own ``<span style="…">``, ``<i>``, ``{{Ts}}``,
    etc. — those belong to the inline rendering of the cell body and MUST
    NOT be hoisted to the cell-level ``style="…"`` attr.  ``content`` is kept
    in the signature so callers that previously scanned both (and the
    ``_cell_align`` helper) don't have to be touched; it's accepted and
    ignored.  Historical bugs from scanning content: AFRICA shipped a
    ``border-bottom:1px dashed red`` from an inner annotation ``<span>``;
    ALDEHYDES duplicated the Toluicaldehyde cell text after a malformed
    inline ``<span style="…>"`` (missing closing quote) made the style
    regex match past the cell boundary.
    """
    del content  # intentionally unused — see docstring.
    rules: list[str] = []
    blob = attr_part or ""
    # HTML align="right" → text-align:right
    m = _CELL_ALIGN_ATTR_RE.search(blob)
    if m:
        a = m.group(1).lower()
        a = "center" if a.startswith("cent") else a
        if a in ("right", "center", "left"):
            rules.append(f"text-align:{a}")
    # HTML valign="top" → vertical-align:top
    vm = re.search(r"valign\s*=\s*\"?(top|middle|bottom)", blob, re.IGNORECASE)
    if vm:
        rules.append(f"vertical-align:{vm.group(1).lower()}")
    # Inline style="..." — pass through as-is.
    sm = re.search(r"style\s*=\s*\"([^\"]*)\"", blob, re.IGNORECASE)
    if sm:
        for decl in sm.group(1).split(";"):
            d = decl.strip()
            if d:
                rules.append(d)
    # `{{Ts|code|code|...}}` shorthand — parse codes.
    for tm in _CELL_TS_RE.finditer(blob):
        rules.extend(_parse_ts_codes(tm.group(1)))
    # Dedupe while preserving order — later occurrences override earlier
    # by removing the earlier match on the same property.
    seen: dict[str, int] = {}
    deduped: list[str] = []
    for r in rules:
        prop = r.split(":", 1)[0]
        if prop in seen:
            deduped[seen[prop]] = r
        else:
            seen[prop] = len(deduped)
            deduped.append(r)
    return deduped


def _extract_table_cells(row_text, text_transform,
                         with_attrs=False, with_styles=False):
    """Extract data cells from a row via the shared `split_wiki_row`
    helper, then drop any remaining `{{Ts|…}}` cell-styling templates
    and run each cell's content through `text_transform`.

    ``with_attrs=True`` returns ``(content, align)`` tuples — the per-cell
    alignment the producer must CARRY (resolved before the `{{Ts}}` style
    codes are stripped) so the viewer renders it instead of guessing.

    ``with_styles=True`` returns ``(content, styles)`` tuples where
    ``styles`` is the full list of CSS declarations (alignment +
    vertical-align + padding + borders + width + size + line-height +
    margin + font-weight, anything `_cell_styles` can extract).  Use
    this when emitting through `emit_html_cell` which accepts the full
    styles list; the wiki ``{{TABLE:}`` marker callers stick with
    ``with_attrs`` since their format only encodes alignment.

    Default (neither flag) returns plain content strings.

    Module-level so the lean data-grid producer (`_process_table`) and
    the carved shape producers (single-column, etc.) share one cell
    parser instead of each re-implementing it.
    """
    cells = []
    for sep, attr_part, content in split_wiki_row(row_text):
        if content in ("}", "{|"):
            continue
        if not content:
            # Preserve as a real empty cell — this is `|` on its
            # own (AETHER / AQUEDUCT column spacers) or `|attr|`
            # with a trailing-pipe and empty content (GRASS AND
            # GRASSLAND's `|width="50%" {{ts|ac|ba}}| ` header-
            # corner: a real cell carrying only sizing/styling
            # attributes).  The trailing pipe is the signal that
            # source intended a real cell.  This is `split_wiki_row`'s
            # contract: anything it returns is a real cell — and the
            # mid-line case where source has `|attr-keyword` with NO
            # trailing pipe gets caught by the `_CELL_ATTR_RE.match`
            # check on `content` below.
            empty = (" ", []) if with_styles else (" ", None) if with_attrs else " "
            cells.append(empty)
            continue
        # Content that is itself a bare attribute keyword (`colspan=2`
        # with no trailing-pipe boundary in source) is a malformed
        # cell, not real content — drop it.
        if _CELL_ATTR_RE.match(content):
            continue
        # Resolve styling BEFORE stripping the {{Ts|…}} codes that carry
        # it.  `with_styles` returns the full CSS list (for HTML cell
        # emission); `with_attrs` returns just alignment (for the wiki
        # `{{TABLE:}` marker, which only encodes ⟦c⟧/⟦r⟧).
        styles = _cell_styles(attr_part, content) if with_styles else None
        align = _cell_align(attr_part, content) if with_attrs else None
        cleaned = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content).strip()
        # text_transform expands layout templates like ``{{gap}}`` /
        # ``{{em|N}}`` to whitespace, which can leave stray leading or
        # trailing whitespace at the cell boundary — render-irrelevant for
        # most consumers but markup-noisy.  Strip after the transform so
        # the cell's emitted bytes are canonical.  Ascii-space-and-tab only
        # so visible-on-render non-breaking entities (``&nbsp;`` → ``\xa0``)
        # the source carried deliberately survive.
        val = text_transform(cleaned).strip(" \t") if cleaned else " "
        if with_styles:
            cells.append((val, styles or []))
        elif with_attrs:
            cells.append((val, align))
        else:
            cells.append(val)
    return cells


def _is_single_column_table(inner: str) -> bool:
    """A wikitable whose every row holds exactly one cell — a boxed run of
    lines, not a data grid.  Its producer renders it as a `«PRE:` block.

    Purely STRUCTURAL.  The only fact consulted is how many cells each row
    has (per `split_wiki_row`); a cell is present when it carries any
    characters after its `{{Ts|…}}` *styling* marker is set aside (styling
    is an attribute, not content — the same separation `split_wiki_row`
    already makes for `attr=val|` prefixes).  Cell *content* — whether it's
    a number, a word, or a `&ensp;` that happens to render as whitespace —
    is never inspected: that is the producer's concern, not the
    classifier's (see [[transform-only-two-places]]).  A table with even
    one 2-cell row is therefore a grid, not single-column.
    """
    # Wiki needs a `|-` row separator to be a multi-row table; `<table>` uses
    # `<tr>` instead (no `|-`).  Cells come from `_table_grid` either way
    # (wiki = the same `re.split(|-)`+`split_wiki_row` content as before, so the
    # wiki result is unchanged).
    is_html = bool(_HTML_TABLE_TAG_RE.search(inner))
    if not is_html and "|-" not in inner:
        return False
    saw_row = False
    for cells_row in _table_grid(inner):
        cells = []
        for content in cells_row:
            if content in ("}", "{|") or not content:
                continue
            if _CELL_ATTR_RE.match(content):
                continue
            if re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content).strip():
                cells.append(content)
        if not cells:
            continue
        if len(cells) > 1:
            return False
        saw_row = True
    return saw_row


def _process_single_column_table(raw: str, inner: str, text_transform) -> str:
    """Render a single-column wikitable as one body paragraph.

    Carved out of `_process_table`'s hidden dispatch: a `{|…|}` used to
    box/centre a run of text (one cell per row) is a text block, not a
    grid.  Selected upstream by the `SINGLE_COLUMN_TABLE` label
    (`_is_single_column_table`); this producer only ever sees that shape.

    Cells are joined with a single `\n` — a soft-wrap inside the body
    paragraph.  The renderer (browser, via `<p>`) treats single
    newlines as whitespace, so the print-column-width line breaks
    naturally reflow at the viewport width.  The producer makes no
    claim about where paragraph breaks belong: that decision is the
    renderer's, based on `\n\n` boundaries between source content.

    Historically wrapped in `«PRE[style:…]:` so a monospace `<pre>` block
    preserved the source's boxed/centred typography.  Monospace
    whitespace alignment doesn't survive a responsive web viewport with
    proportional fonts, and the PRE wrap was a leak source for inline
    markers in cell content.  Marker dropped per
    [[preserved-markup-is-a-contract]].
    """
    inner = _strip_br(inner)
    text_lines = []
    if _HTML_TABLE_TAG_RE.search(inner):
        for cells in _html_table_grid(inner):
            # `_html_table_grid` yields `(sep, attr, content)` triples;
            # transform the content slot only (the same content-only view
            # `_table_grid` takes), not the whole tuple.
            content = [c for c in (text_transform(cell[2]) for cell in cells)
                       if c.strip()]
            if content:
                text_lines.append(content[0])
    else:
        for raw_row in re.split(r"\|-[^\n]*", inner):
            content = [c for c in _extract_table_cells(raw_row, text_transform)
                       if c.strip()]
            if content:
                text_lines.append(content[0])
    if not text_lines:
        return ""
    return "\n".join(text_lines)


# col1 of a verse quotation: only quotation/whitespace punctuation (the
# hanging opening quote). Read on RAW col1 (no transform), so template/label
# cells like `{{em|N}}` / `{{Dotted TOC line|…}}` / `{{nowrap|B.}}` are NOT
# punctuation — which is exactly how the matrix / TOC / taxonomy false
# positives (that branch 5 mis-claimed by transforming col1 to blank) get
# excluded.
_VERSE_COL1_PUNCT_RE = re.compile(
    r'^[\s"\'“”‘’,.\-;:—]+$')

# A single-cell quoted poem: the whole table is ONE content cell that opens with
# a quotation (`{{fqm}}` or a quote mark) and is broken into lines with `<br>`.
# VERSE is a CONTENT-defined shape (see [[transform-only-two-places]]): a quoted
# multi-line passage is a poem — recognise it, don't try to structure it.  The
# quote requirement excludes `<br>`-lined non-verse (ARISTOTLE's logic diagram);
# the `<br>` requirement excludes single-line prose quotes (ARMY's note).
_VERSE_QUOTE_OPEN_RE = re.compile(r"^\s*(?:\{\{\s*fqm\b|[“”‘’\"'])")
_VERSE_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _single_cell_verse_cell(inner: str) -> str | None:
    """Raw cell content if `inner` is a single-cell quoted poem (one content
    cell, quote-opened, `<br>`-lined); else None.  No transform — recognition
    only."""
    cells = [c for _s, _a, c in split_wiki_row(inner)
             if c.strip() and c not in ("}", "{|")]
    if len(cells) != 1:
        return None
    cell = cells[0].strip()
    if _VERSE_QUOTE_OPEN_RE.match(cell) and _VERSE_BR_RE.search(cell):
        return cell
    return None


def _is_verse_table(inner: str) -> bool:
    """A 2-column quotation layout: col1 hangs only quotation punctuation
    (the opening quote of a quoted poem), col2 holds the verse lines.

    Content-recognition — a justified last resort, because no STRUCTURAL
    signal separates a verse quotation from a 2-column data table (see
    [[transform-only-two-places]]).  STRUCTURAL wherever it can be, and it
    does NOT transform.  Requirements:
      * every non-empty row is two cells (or one empty cell), each col1
        punctuation-only or empty;
      * ≥1 row hangs a non-empty (punctuation) col1 — the hanging mark a
        determinant matrix / all-empty-col1 table lacks;
      * ≥1 row carries col2 text.
    Verified against the (static) corpus: all 24 two-column table-verse hang a
    quote in col1; none are unquoted.  See [[source-is-static]].

    Two VERSE shapes: the 2-column hanging-quote layout (below) and the
    single-cell quoted poem (`_single_cell_verse_cell`).  Both are
    content-recognition — VERSE is content-defined.
    """
    if _single_cell_verse_cell(inner) is not None:
        return True
    saw_punct_col1 = False
    saw_verse_row = False
    for rv in re.split(r"\|-[^\n]*", inner):
        cells = [c for _s, _a, c in split_wiki_row(rv) if c not in ("}", "{|")]
        if not cells:
            continue
        if len(cells) == 2:
            col1, col2 = cells[0].strip(), cells[1].strip()
            if col1 and not _VERSE_COL1_PUNCT_RE.match(col1):
                return False
            if col1:
                saw_punct_col1 = True
            if col2:
                saw_verse_row = True
        elif len(cells) == 1 and not cells[0].strip():
            continue
        else:
            return False
    return saw_verse_row and saw_punct_col1


def _process_verse_table(raw: str, inner: str, text_transform,
                         inner_registry: ElementRegistry | None = None) -> str:
    """Render a verse table as `{{VERSE:}VERSE}`.

    Three shapes: a POEM-wrapper (the table just centres `<poem>` child(ren) —
    BELL/BOAT, the dominant `<table>` case), a single-cell quoted poem (split on
    `<br>`), and the 2-column hanging-quote layout (col1 rejoined to col2).
    """
    # Poem-wrapper: emit each `<poem>` child as verse, drop the table chrome.
    # The poem's inner is rendered exactly as a standalone `<poem>` would be
    # (`_process_poem` = `{{VERSE: tt(inner) }VERSE}`).
    if inner_registry is not None:
        labels = inner_registry.labels
        poem_phs = [ph for ph, lbl in labels.items() if lbl == "POEM"]
        if poem_phs and not any(
                lbl in IMAGE_LABELS for lbl in labels.values()):
            parts = []
            for ph in poem_phs:
                body = text_transform(
                    (inner_registry.inners.get(ph) or "").strip())
                if body.strip():
                    parts.append("{{VERSE:" + body + "}VERSE}")
            if parts:
                return "\n\n".join(parts)
    # Single-cell quoted poem: split the cell on `<br>` into verse lines
    # (joining soft-hyphen `-<br>` breaks first), BEFORE `_strip_br` would
    # flatten them.
    sc = _single_cell_verse_cell(inner)
    if sc is not None:
        sc = re.sub(r"(\w)-<br\s*/?>\s*", r"\1", sc, flags=re.IGNORECASE)
        lines = [text_transform(ln.strip())
                 for ln in _VERSE_BR_RE.split(sc) if ln.strip()]
        return "{{VERSE:" + "\n".join(lines) + "}VERSE}"

    inner = _strip_br(inner)
    lines = []
    for rv in re.split(r"\|-[^\n]*", inner):
        cells = _extract_table_cells(rv, text_transform)
        if len(cells) == 2:
            col1, col2 = cells[0].strip(), cells[1].strip()
            if col2:
                line = f"{col1}{col2}" if col1 else col2
                lines.append(text_transform(line))
    # Whole-table styling from the source `{|<attrs>` opener (extracted
    # from `raw`).  Threaded into `{{VERSE[style:…]:` slot for the
    # renderer (verse blocks are commonly `{|{{Ts|ma|sm}}` for centred /
    # smaller-font quoted passages).
    table_styles = _table_opener_styles(raw)
    style_slot = (f"[style:{';'.join(table_styles)}]" if table_styles else "")
    return ("{{VERSE" + style_slot + ":"
            + "\n".join(lines) + "}VERSE}")


def _process_table(raw: str, inner: str, text_transform,
                   inner_registry: ElementRegistry | None = None) -> str:
    """Convert table rows to {{TABLE:...}TABLE} with clean cells.

    The table processor handles STRUCTURE: rows, cell boundaries, attributes.
    Each cell's content is processed through text_transform — cells are
    elements in their own right.
    """
    # Convert <br> to space before cell parsing (and strip soft-hyphen
    # line breaks, e.g. "Circum-<br>ference" → "Circumference").
    inner = _strip_br(inner)

    def _extract_cells(row_text, with_attrs=False):
        return _extract_table_cells(row_text, text_transform, with_attrs)

    # Tiny inline tables (few cells, short content) → unwrap to inline text.
    # Only for single-row tables with no row separators and no block-level
    # child elements (poems produce VERSE blocks that need table wrapping).
    _BLOCK_TYPES = {"POEM", "TABLE", "HTML_TABLE"}
    has_block_child = inner_registry and any(
        ctype in _BLOCK_TYPES for ctype, _ in inner_registry.elements.values())
    if "|-" not in inner and not has_block_child:
        all_cells = _extract_cells(inner)
        content_cells = [c for c in all_cells if c.strip()]
        if len(content_cells) <= 4 and sum(len(c) for c in all_cells) < 120:
            return " ".join(content_cells)

    # Image + caption wikitable: row 1 contains a single image placeholder
    # and row 2+ contains caption / attribution text. Bundle into a single
    # {{IMG:filename|caption}} so the image renders with its caption inside
    # the figure rather than leaving the caption row as a duplicate paragraph
    # below the figure (e.g. SEWING MACHINES Fig. 2). Skips trailing
    # attribution rows (typically beginning "From …" / "After …").
    if "|-" in inner and inner_registry is not None:
        ph_re = re.compile(re.escape(_PH) + r"ELEM:\d+" + re.escape(_PH))
        rows_filtered = [r for r in re.split(r"\|-[^\n]*", inner) if r.strip()]
        if len(rows_filtered) >= 2:
            row1_cells = _extract_cells(rows_filtered[0])
            if (len(row1_cells) == 1
                    and ph_re.fullmatch(row1_cells[0].strip())):
                ph_id = row1_cells[0].strip()
                if inner_registry.labels.get(ph_id) in IMAGE_LABELS:
                    eraw = inner_registry.elements[ph_id][1]
                    fname_m = re.match(
                        r"\[\[(?:File|Image):([^\]|]+)",
                        eraw, re.IGNORECASE)
                    if fname_m:
                        filename = fname_m.group(1).strip()
                        # Take row 2 as the primary caption.
                        row2_cells = _extract_cells(rows_filtered[1])
                        caption = " ".join(c.strip() for c in row2_cells
                                           if c.strip())
                        if caption:
                            return f"{{{{IMG:{filename}|{clean_caption(caption)}}}}}"
                        return f"{{{{IMG:{filename}}}}}"

    # (Single-column text-block tables are now classified upstream as
    # SINGLE_COLUMN_TABLE and rendered by `_process_single_column_table`;
    # they never reach here.)

    # Check for image-layout table (plate pages: grid of images + captions)
    if _PH in inner:
        # Count child placeholders that are images vs text content
        placeholders = re.findall(re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+" + re.escape(_PH), inner)
        non_placeholder = re.sub(re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+" + re.escape(_PH), "", inner)
        # Strip wiki table markup and whitespace, but keep actual cell text
        non_placeholder = re.sub(r"[-|{}\n]", " ", non_placeholder)
        non_placeholder = re.sub(r"\b(?:align|valign|colspan|rowspan|style|width|cellpadding|cellspacing|center|right|left|top|bottom)\b", "", non_placeholder, flags=re.IGNORECASE)
        non_placeholder = re.sub(r'[="]+', "", non_placeholder)
        non_placeholder = re.sub(r"\s+", " ", non_placeholder).strip()
        if len(placeholders) >= 2 and len(non_placeholder) < len(placeholders) * 20:
            # Mostly images — extract placeholders and any caption text
            parts = []
            for ph in placeholders:
                parts.append(ph)
            # Find numbered captions in the remaining text
            for m in re.finditer(r"(\d+)\.\s*([A-Z][A-Z\s,.:;()\-']+)", inner):
                parts.append(f"{m.group(1)}. {m.group(2).strip()}")
            return "\n\n".join(parts)

    # (Verse-layout tables are now classified upstream as VERSE_TABLE and
    # rendered by `_process_verse_table`; they never reach here.)

    # Check for structural formula (monospaced, spatial layout)
    if _is_structural_formula(inner):
        return _format_structural_formula(inner)

    # Table caption (|+ …) — LINE-INITIAL only, so a `||+2.1` cell value is not
    # mistaken for a caption.  CARRIED (transformed) so the viewer renders it as
    # <caption> rather than dropping it.
    caption = ""
    cap_match = re.search(r"(?:^|\n)\s*\|\+\s*(.+)", inner)
    if cap_match:
        caption = text_transform(cap_match.group(1).strip()).strip()

    # Split into rows on |- separators
    raw_rows = re.split(r"\|-[^\n]*", inner)

    # Header table = the FIRST data row uses `!` header cells (the real source
    # signal).  Replaces the old `header=bool(caption)` guess, which both missed
    # `!` rows and false-fired on `||+`-valued cells mis-read as captions.
    _hdr_rows = [r for r in raw_rows if r.strip() and "|+" not in r]
    is_header_table = bool(_hdr_rows) and any(
        sep == "!" for sep, _, _ in split_wiki_row(_hdr_rows[0]))

    image_parts = []
    data_rows: list[list[tuple[str, str | None]]] = []  # (content, align) per cell
    _img_cell_re = re.compile(r"^\s*\{\{IMG:[^}]+\}\}\s*$")

    for raw_row in raw_rows:
        # Drop the `|+` caption LINE (already extracted above) but KEEP the
        # rest of this pre-`|-` segment — it can hold the header row.  The
        # source often puts the caption and the first header row in the same
        # segment before the first `|-` (AGRICULTURE's "Average Acreage" table:
        # `|+ Table XXVIII…` then `| | Whole Farm. | Proportion…`).  Skipping
        # the whole segment on `|+` dropped that header row.
        raw_row = re.sub(r"(?:\A|\n)[ \t]*\|\+[^\n]*", "", raw_row)
        if not raw_row.strip():
            continue

        # Preserve any child element placeholders outside cells
        for line in raw_row.split("\n"):
            stripped_line = line.strip()
            if _PH in stripped_line and not stripped_line.startswith("|"):
                image_parts.append(stripped_line)

        # Extract cells carrying per-cell alignment.
        cells = _extract_cells(raw_row, with_attrs=True)

        # All-image row → separate for plate layout (a row mixing images and
        # text keeps them together as table cells).
        img_cells = [c for c in cells if _img_cell_re.match(c[0])]
        if img_cells and len(img_cells) == len(cells):
            image_parts.extend(c[0].strip() for c in img_cells)
            continue

        # Handle uppercase <br> that `_strip_br` (case-sensitive, run at the
        # top) missed — carrying each cell's alignment to its sub-cells.
        br_idx = [i for i, (cc, _) in enumerate(cells)
                  if re.search(r"<br\s*/?>", cc, re.IGNORECASE)]
        if len(br_idx) >= 2:
            split = [(re.split(r"<br\s*/?>", cc, flags=re.IGNORECASE), al)
                     for (cc, al) in cells]
            max_sub = max(len(s) for s, _ in split)
            for i in range(max_sub):
                sub = [((s[i].strip() if i < len(s) else ""), al)
                       for (s, al) in split]
                if any(cc for cc, _ in sub):
                    data_rows.append(sub)
        elif br_idx:
            data_rows.append([(_strip_br(cc).strip(), al) for (cc, al) in cells])
        else:
            data_rows.append(list(cells))

    # Emit rows.  A true group-header row — content in the FIRST cell only,
    # spanning the full width — becomes one colspan cell (the producer's
    # decision, carried explicitly), replacing the viewer's sparseness guess.
    # Every other cell carries its resolved alignment.
    text_rows = []
    maxcols = max((len(r) for r in data_rows), default=0)
    for row in data_rows:
        nonempty = [i for i, (cc, _) in enumerate(row) if cc.strip()]
        if (maxcols >= 2 and len(row) == maxcols
                and len(nonempty) == 1 and nonempty[0] == 0):
            cc, al = row[0]
            text_rows.append(build_table_cell(cc, align=al, colspan=maxcols))
        else:
            text_rows.append(" | ".join(
                build_table_cell(cc, align=al) for (cc, al) in row))

    # Whole-table styling from the source `{|<attrs>` opener (extracted
    # from `raw`, since `inner` has the opener line stripped by
    # `strip_outer`).  Threaded through the `{{TABLE[style:…]:` marker
    # slot so the renderer can apply it to the `<table>` element.
    table_styles = _table_opener_styles(raw)

    # Assemble output
    parts = []
    if image_parts:
        parts.extend(image_parts)
    if text_rows:
        # Caption rides as a ⟦+⟧-flagged leading row; the viewer pulls it out as
        # <caption> before row-indexing, so `is_header_table` still applies to
        # the first DATA row.
        if caption:
            text_rows.insert(0, "⟦+⟧" + caption)
        parts.append(_emit_table_marker(text_rows, header=is_header_table,
                                        styles=table_styles))

    if parts:
        return "\n\n".join(parts)
    return ""


def _is_html_illustration_wrapper(
    raw: str, inner_registry: ElementRegistry | None
) -> bool:
    """Detect HTML tables that wrap an image+caption for layout.

    EB1911 Wikisource uses HTML tables like:

        <table ... summary="Illustration">
          <tr><td>[[File:...]]</td></tr>
          <tr><td>Fig. 1. caption text.</td></tr>
        </table>

    Signals:
      - summary="Illustration" attribute on the opening <table> tag, OR
      - contains exactly one IMAGE child and the non-image cells are short
        caption text.
    """
    if re.search(r'summary\s*=\s*"?Illustration', raw, re.IGNORECASE):
        return True
    if inner_registry is None:
        return False
    child_labels = list(inner_registry.labels.values())
    n_images = sum(1 for lbl in child_labels if lbl in IMAGE_LABELS)
    if n_images < 1:
        return False
    # No other block-level children (nested tables etc.)
    if any(lbl not in IMAGE_LABELS and lbl not in ("REF", "MATH")
           for lbl in child_labels):
        return False
    return True


def _unwrap_html_illustration(
    inner: str, text_transform, inner_registry: ElementRegistry | None
) -> str:
    """Unwrap an HTML illustration table to a bundled IMG+caption.

    Collects every image child and every caption cell, then emits
    `{{IMG:filename|caption}}` where the caption is the concatenated
    non-image cell text. This lets the viewer render the caption under
    the image instead of as a detached paragraph.

    For single-image illustrations we bypass the placeholder mechanism
    entirely and emit the final IMG tag directly — the placeholder
    substitution step in _process_element won't find the placeholder in
    the output, so it simply does nothing for this child.

    Falls back to emitting placeholders + caption paragraph if there's
    no single image or no inner_registry.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", inner, re.DOTALL | re.IGNORECASE)

    # Per-row breakdown.  Each row's cells are split into image cells
    # (those containing an IMAGE placeholder) and caption cells (text-
    # only).  We keep the row structure so the multi-column case (DOG
    # breeds: row of 3 images, then row of 3 captions, all in one
    # `summary="Illustration"` table) can pair each image with its own
    # column's caption rather than collapsing every caption into one
    # detached paragraph.
    def _process_cell(cell: str) -> str:
        c = re.sub(r"<[^>]+>", " ", cell)
        c = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}", "", c)
        c = re.sub(r"\s+", " ", c).strip()
        if not c:
            return ""
        if _PH in c:
            ph_re = re.compile(
                re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+"
                + re.escape(_PH))
            out = []
            last = 0
            for m in ph_re.finditer(c):
                if m.start() > last:
                    chunk = c[last:m.start()]
                    out.append(text_transform(chunk) if chunk.strip() else chunk)
                out.append(m.group(0))
                last = m.end()
            if last < len(c):
                tail = c[last:]
                out.append(text_transform(tail) if tail.strip() else tail)
            return "".join(out)
        return text_transform(c)

    rows_breakdown: list[tuple[list[str], list[str]]] = []
    image_cells: list[str] = []
    caption_parts: list[str] = []
    for row in rows:
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL | re.IGNORECASE)
        row_images: list[str] = []
        row_captions: list[str] = []
        for cell in cells:
            processed = _process_cell(cell)
            if not processed:
                continue
            if _PH in processed:
                row_images.append(processed)
                image_cells.append(processed)
            else:
                row_captions.append(processed)
                caption_parts.append(processed)
        rows_breakdown.append((row_images, row_captions))

    caption_text = " ".join(p for p in caption_parts if p).strip()

    # Single-image case: emit the IMG tag directly with caption bundled.
    if len(image_cells) == 1 and inner_registry is not None:
        cell = image_cells[0]
        m = re.search(re.escape(_PH) + r"ELEM:\d+" + re.escape(_PH), cell)
        if m:
            key = m.group(0)
            if inner_registry.labels.get(key) in IMAGE_LABELS:
                img_raw = inner_registry.elements[key][1]
                # Inject the caption via EXTCAP so _process_image bundles it.
                if caption_text:
                    new_raw = img_raw + "\n\n" + caption_text
                else:
                    new_raw = img_raw
                return _process_image_from_raw(new_raw, text_transform)

    # Multi-image figure-grid: image-row + caption-row + optional
    # shared-caption row.  BOILER Fig 17 (vol 4 ws 162) is the canonical
    # in-article case: 2 images side-by-side with per-column sub-captions
    # and a colspan="2" row carrying the shared figure caption.  Without
    # this branch the fallback below concatenates every caption into one
    # paragraph, losing the per-image pairing.
    if len(image_cells) > 1 and inner_registry is not None:
        for i in range(len(rows_breakdown) - 1):
            img_row, img_caps = rows_breakdown[i]
            next_imgs, cap_row = rows_breakdown[i + 1]
            if (img_row and not img_caps
                    and cap_row and not next_imgs
                    and len(img_row) == len(cap_row)):
                out: list[str] = []
                for img_cell, cap in zip(img_row, cap_row):
                    m = re.search(
                        re.escape(_PH) + r"ELEM:\d+" + re.escape(_PH),
                        img_cell)
                    if m and inner_registry.labels.get(m.group(0)) in IMAGE_LABELS:
                        img_raw = inner_registry.elements[m.group(0)][1]
                        new_raw = img_raw + "\n\n" + cap if cap else img_raw
                        out.append(
                            _process_image_from_raw(new_raw, text_transform))
                        continue
                    out.append(img_cell + ("\n\n" + cap if cap else ""))
                # Remaining caption rows (typically a colspan shared
                # caption) become a LEGEND block under the figure group.
                shared_caps: list[str] = []
                for j in range(len(rows_breakdown)):
                    if j in (i, i + 1):
                        continue
                    _, more_caps = rows_breakdown[j]
                    shared_caps.extend(c for c in more_caps if c)
                if shared_caps:
                    shared = " ".join(shared_caps)
                    out.append(f"{{{{LEGEND:{shared}}}LEGEND}}")
                return "\n\n".join(out)

    # Otherwise multi-image falls through to the paragraph-style fallback
    # below.  Plate-shaped multi-image illustration tables are detected
    # earlier in the pipeline (clean_pages._extract_illustration_plates,
    # operating on page.wikitext where `summary="Illustration"` is
    # still visible) and converted to `{{PLATE:…}PLATE}` markers
    # before this stage runs — so by the time _unwrap_html_illustration
    # sees them they're already PLATE markers, not multi-image tables.
    # The branch handled below is the residual: HTML-form illustration
    # tables that the wikitext-level detector missed.

    # Fallback: paragraph-style (images as placeholders, captions below)
    parts: list[str] = list(image_cells)
    if caption_text:
        parts.append(caption_text)
    return "\n\n".join(parts) if parts else ""


def _html_cell_clean(content: str) -> str:
    """HTML cell-content normalisation: join soft-hyphen ``<br>`` breaks,
    convert inline ``<sub>``/``<sup>`` to Unicode, strip remaining HTML
    tags, collapse whitespace.

    Producer-specific pre-``text_transform`` step; the canonical
    ``produce_cell`` then runs body-text on the cleaned content while
    ``_cell_styles`` consumes the cell's attribute slot independently.
    """
    c = _strip_wiki_cell_attr_in_html(content)
    # Lossless `<br>` fidelity: a `<br>` in a table cell is the source/print's
    # DELIBERATE line break (data-row delimiters in stacked-list columns, wrapped
    # multi-line headers, legend line breaks) — keep it as a break instead of
    # flattening to a space, so hand-aligned columns line up and headers wrap as
    # printed.  We do NOT classify what each `<br>` "means"; rendering the break
    # is faithful in every structural case (the only cost is the rare incidental
    # narrow-column wrap, which shows a harmless extra line — no misalignment, no
    # content loss).  The `-<br>` soft-hyphen join is still applied (a real print
    # artifact, not a break).
    c = _strip_br(c, "<br>")
    c = _convert_inline_sub_sup(c)
    # Strip remaining HTML tags but PRESERVE `<br>` (else the line break we just
    # kept would be stripped here).
    c = re.sub(r"<(?!br\s*/?>)[^>]+>", " ", c, flags=re.IGNORECASE)
    # Collapse whitespace (incl. source newlines) to single spaces; `<br>` is
    # not whitespace, so the preserved breaks survive.
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _process_html_table(
    raw: str,
    inner: str,
    text_transform,
    inner_registry: ElementRegistry | None,
) -> str:
    """Render an HTML ``<table>`` element via the canonical recursive
    decomposition (``extract_html_rows`` -> ``produce_cell`` ->
    ``assemble_wiki_marker``).

    Three output paths, selected per cell-span / row-structure shape:

    * Illustration wrapper (image + caption inside ``<tr>``/``<td>``
      for layout) -> unwrap to ``{{IMG:...}}`` via
      ``_unwrap_html_illustration``.
    * ``rowspan``/``colspan`` present -> emit
      ``«HTMLTABLE:<table>...</table>«/HTMLTABLE»`` with
      full per-cell ``style="..."`` preserved (the wiki
      ``{{TABLE:}TABLE}`` marker can carry only align + colspan).
    * No spans -> emit canonical ``{{TABLE:}TABLE}`` /
      ``{{TABLEH:}TABLE}`` via the shared assembler.

    HYDRAULICS-shape ``<table><td>...</td></table>`` (no ``<tr>``
    wrapper) renders as plain ``" | "``-joined prose, NOT a table
    marker -- the source's lack of row structure signals flowing-text
    intent.
    """
    from britannica.pipeline.stages.elements._table_decompose import (
        assemble_wiki_marker, extract_html_rows, produce_cell,
    )

    # Strip HTML comments before any row/cell parsing -- matches what the
    # bypassed Layer-A ``html_comments`` pass did; without this, comments
    # between ``<tr>``s leak as bogus content (cf. the chem producer).
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    # Illustration wrapper -- unwrap to IMG + caption
    if _is_html_illustration_wrapper(raw, inner_registry):
        return _unwrap_html_illustration(inner, text_transform, inner_registry)

    # HYDRAULICS shape: ``<table><td>...</td></table>`` with ``<td>``
    # directly under ``<table>`` (no ``<tr>``).  Source intent is
    # flowing prose, not a tabular grid -- render as ``" | "``-joined
    # cells, no marker.
    has_tr = bool(re.search(r"<tr\b", inner, re.IGNORECASE))
    if not has_tr:
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>",
            inner, re.DOTALL | re.IGNORECASE)
        if cells:
            parts = []
            for c in cells:
                cleaned = _html_cell_clean(c)
                if cleaned:
                    parts.append(text_transform(cleaned))
            return " | ".join(parts)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        return text_transform(text) if text else ""

    # Canonical decomposition: rows -> cells.  Row-level attribute
    # styling is extracted but not yet carried in the marker format
    # (the data is available for future marker-format extension).
    _caption, rows = extract_html_rows(inner)

    has_header = False
    has_span = False
    # parsed_rows: list[list[(tag, rowspan, colspan, body, styles)]]
    parsed_rows: list[list[tuple[str, int, int, str, list[str]]]] = []
    for _row_attrs, cells in rows:
        parsed: list[tuple[str, int, int, str, list[str]]] = []
        for sep, cell_attrs, cell_content in cells:
            tag = "th" if sep == "!" else "td"
            if sep == "!":
                has_header = True
            rs = re.search(r'rowspan\s*=\s*"?(\d+)"?',
                           cell_attrs, re.IGNORECASE)
            cs = re.search(r'colspan\s*=\s*"?(\d+)"?',
                           cell_attrs, re.IGNORECASE)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            if rowspan > 1 or colspan > 1:
                has_span = True
            cleaned = _html_cell_clean(cell_content)
            styles, body = produce_cell(cell_attrs, cleaned, text_transform)
            parsed.append((tag, rowspan, colspan, body, styles))
        if parsed:
            parsed_rows.append(parsed)

    if not parsed_rows:
        return ""

    if has_span:
        html_rows = []
        for parsed in parsed_rows:
            cells_html = [
                emit_html_cell(tag, content,
                               rowspan=rowspan, colspan=colspan,
                               styles=styles)
                for tag, rowspan, colspan, content, styles in parsed
            ]
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
        output = ("\u00abHTMLTABLE:<table>" +
                  "".join(html_rows) +
                  "</table>\u00ab/HTMLTABLE\u00bb")
        # Pre-substitute DATA_TABLE child markers as inline `<table>`
        # HTML so they render correctly inside our HTML cells instead
        # of leaking `{{TABLE:\u2026}TABLE}` marker text (the previous
        # ``_inline_nested_table_markers_in_htmltable_blocks``
        # post-pass in `_classifier.py`, now folded into the producer
        # that needs the conversion \u2014 see [[refactoring-is-the-model]]
        # / [[pipeline-is-the-only-clean-place]]).  Other child types
        # are left as placeholders for `produce_tree`'s generic
        # post-substitution.  Child markers are already populated when
        # this producer runs (`produce_tree` recurses children first).
        if inner_registry is not None:
            for ph, label in list(inner_registry.labels.items()):
                if label != "DATA_TABLE":
                    continue
                if ph not in output:
                    continue
                child_marker = inner_registry.markers.get(ph, "")
                output = output.replace(
                    ph, _inline_table_marker_as_html(child_marker))
        return output

    # Verse-only layout: a single-row, single-cell <table> whose
    # entire content is a <poem> placeholder is an editor's centering
    # / sizing wrapper around embedded verse (DONNE's "Sweetest Love"
    # passage).  At this point in the pipeline the cell holds a POEM
    # placeholder — children are re-substituted by the caller after
    # we return — so we check the inner_registry instead of looking
    # for the expanded {{VERSE:…}VERSE} marker.  Emit the placeholder
    # flanked by blank lines so the downstream paragraph splitter
    # treats the resulting {{VERSE:…}VERSE} as its own paragraph; the
    # viewer then renders it as <blockquote class="verse"> (set off
    # from prose like BLANK VERSE) instead of as an inline span.
    if (
        inner_registry is not None
        and len(parsed_rows) == 1 and len(parsed_rows[0]) == 1
    ):
        only_cell = (parsed_rows[0][0][3] or "").strip()
        poem_phs = {
            k for k, label in inner_registry.labels.items()
            if label == "POEM"
        }
        if only_cell in poem_phs:
            return only_cell

    # No spans, no special case -> canonical ``{{TABLE:}TABLE}`` /
    # ``{{TABLEH:}TABLE}`` marker via the shared assembler.  Each cell's
    # styles list is carried so the assembler can encode alignment
    # (``align=`` -> ``text-align:`` style).  Row-styles are passed
    # through (currently dropped at the marker boundary; future-extension
    # slot).
    produced_rows: list[tuple[str, list[tuple[list[str], str]]]] = [
        ("", [(styles, body) for _, _, _, body, styles in parsed if body])
        for parsed in parsed_rows
    ]
    # Drop fully-empty rows after the filter.
    produced_rows = [(a, c) for a, c in produced_rows if c]
    if not produced_rows:
        return ""
    return assemble_wiki_marker(
        produced_rows, caption="", header=has_header, table_styles=[],
    )


