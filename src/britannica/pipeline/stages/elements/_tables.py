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


def _html_table_grid(inner: str) -> list[list[str]]:
    """Rows × raw cell-content for an HTML `<table>` inner.

    Robust to the unclosed `</td>`/`</tr>` the source sometimes emits
    (the malformed markup that zeroed MAGNETISM / SATURN): cells are the
    text after each `<td>`/`<th>` opener, cut at the next cell/row/table
    boundary whether or not a closing tag is present."""
    rows: list[list[str]] = []
    for row in re.split(r"<tr\b[^>]*>", inner, flags=re.IGNORECASE):
        cells: list[str] = []
        for piece in re.split(r"<t[dh]\b[^>]*>", row, flags=re.IGNORECASE)[1:]:
            content = re.split(
                r"</t[dh]>|</tr>|</table>|<tr\b", piece,
                maxsplit=1, flags=re.IGNORECASE)[0]
            cells.append(content.strip())
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
        return _html_table_grid(inner)
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
) -> tuple[str, list[list[tuple[str, str, str]]]]:
    """Parse a wiki-table's structure into ``(caption, rows)``.

    The whole-table counterpart to :func:`split_wiki_row` (which
    operates on one row at a time).  Steps:

    1. Strip outer ``{|…\\n`` opener and ``\\n?|}`` closer (if present)
       — accepts either form (``raw`` or ``inner``) for caller
       convenience.
    2. Strip ``<br>`` tags (soft-hyphen-aware via :func:`_strip_br`:
       a ``-<br>`` line-break drops both halves so ``Circum-<br>ference``
       renders as ``Circumference``; plain ``<br>`` becomes a space).
    3. Extract the first ``|+ caption`` line as the table caption.
    4. Split on ``|-`` row separators (line-anchored).
    5. Run each row through :func:`split_wiki_row`; drop empty rows.

    Returns:
    * ``caption`` — caption text (empty if no ``|+`` line).
    * ``rows`` — list of rows, each a list of
      ``(sep, attr_part, content)`` cells.

    Limitations: ``|-`` splitting is NOT depth-aware — it splits on
    ``|-`` inside nested ``{|…|}`` blocks too.  Callers that need to
    preserve nested-table structure (currently only
    ``_process_compound_table``) must do their own depth-tracking
    row split and call :func:`split_wiki_row` per row directly."""
    text = re.sub(r"^\{\|[^\n]*\n?", "", text)
    text = re.sub(r"\n?\|\}\s*$", "", text)

    text = _strip_br(text)

    caption = ""
    cap_match = re.search(r"^\|\+\s*(.+?)$", text, re.MULTILINE)
    if cap_match:
        caption = cap_match.group(1).strip()

    raw_rows = re.split(r"(?:^|\n)\|-[^\n]*", text)
    rows: list[list[tuple[str, str, str]]] = []
    for raw_row in raw_rows:
        cells = split_wiki_row(raw_row)
        if cells:
            rows.append(cells)
    return caption, rows


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


def _emit_table_marker(text_rows: list[str], header: bool = False) -> str:
    """Join row strings into a ``{{TABLE:…}TABLE}`` / ``{{TABLEH:…}TABLE}`` marker.

    Data tables (``header=False``): canonical form — one space on each
    side of every ``|`` separator, multi-space runs collapsed (an empty
    cell renders as ``a | | b`` not ``a |   | b``), blank rows removed.
    The renderer emits canonical output directly so no downstream
    pipe-normalisation pass is needed.  (Downstream legend-detection in
    ``legend_promote._table_row_cells`` is whitespace-robust, so the
    collapsed-empty-cell form doesn't change which tables become legends.)

    Header tables (``header=True``): rows are joined raw.  Normalizing
    header tables is a deliberate-change item (burndown) — would change
    shipped output for ``{{TABLEH:`` tables that haven't been touched
    since the historical ``\\{\\{TABLE:`` cleanup regex (which never
    matched the ``H`` suffix) was deleted.
    """
    content = "\n".join(text_rows)
    if not header:
        content = re.sub(r"(?<! )\|", " |", content)
        content = re.sub(r"\|(?! )", "| ", content)
        content = re.sub(r"  +", " ", content)
        content = re.sub(r"\n\s*\n", "\n", content)
    tag = "TABLEH" if header else "TABLE"
    return "{{" + tag + ":" + content + "}TABLE}"


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
                content = content.strip()
                if content and text_transform:
                    content = text_transform(content)
                cells_html.append(emit_html_cell(tag, content))

            if cells_html:
                html_rows.append("<tr>" + "".join(cells_html) + "</tr>")

    if not html_rows:
        return ""

    return ("\n\n\u00abHTMLTABLE:<table>" + "".join(html_rows)
            + "</table>\u00ab/HTMLTABLE\u00bb\n\n")


def _process_complex_table(inner: str, text_transform) -> str:
    """Convert a wiki table with rowspan/colspan to HTML.

    Strategy: each cell in wiki markup has the form
        {{ts|style}} rowspan=N colspan=M {{ts|style}}| content
    Everything before the last | is attributes; everything after is content.
    We keep only rowspan/colspan from the attributes and transform the content.
    Pipes inside {{...}} are protected so they don't confuse the split.

    Receives `inner` (delimiters already stripped, child elements replaced
    with placeholders) so that nested elements like <math> are preserved.
    """

    # Use the shared `parse_wiki_table` for table parsing — gets us
    # caption extraction + row split + cell parsing in one call,
    # consistent with every other wiki-table path.
    caption_raw, parsed_rows = parse_wiki_table(inner)
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

    for parsed_row in parsed_rows:
        cells_html = []
        for sep, attr_part, content in parsed_row:
            tag = "th" if sep == "!" else "td"

            # Extract structural attributes
            rs = re.search(r'rowspan\s*=\s*"?(\d+)"?', attr_part, re.IGNORECASE)
            cs = re.search(r'colspan\s*=\s*"?(\d+)"?', attr_part, re.IGNORECASE)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            # Propagate horizontal/vertical alignment from
            # ``{{Ts|ar}}``/``ac``/``al`` style tokens or HTML
            # ``align=`` / ``valign=`` attributes to inline CSS so
            # the rendered table preserves the source's column
            # alignment.  Without this, ``<td colspan="2">824,000
            # </td>`` summation rows (BRITISH EMPIRE Africa /
            # Australasia / Summary tables) default-left-align and
            # detach from the value column above them.
            ts_tokens: set[str] = set()
            for ts in re.findall(r"\{\{[Tt]s\|([^{}]*)\}\}", attr_part):
                for tok in ts.split("|"):
                    ts_tokens.add(tok.strip().lower())
            styles: list[str] = []
            if ("ar" in ts_tokens
                    or re.search(r'align\s*=\s*"?right"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("text-align:right")
            elif ("ac" in ts_tokens
                    or re.search(r'align\s*=\s*"?center"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("text-align:center")
            elif ("al" in ts_tokens
                    or re.search(r'align\s*=\s*"?left"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("text-align:left")
            if ("vtp" in ts_tokens
                    or re.search(r'valign\s*=\s*"?top"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("vertical-align:top")
            elif ("vtm" in ts_tokens
                    or re.search(r'valign\s*=\s*"?middle"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("vertical-align:middle")
            elif ("vtb" in ts_tokens
                    or re.search(r'valign\s*=\s*"?bottom"?',
                                 attr_part, re.IGNORECASE)):
                styles.append("vertical-align:bottom")

            # Clean content
            # Convert [[Image:...|params]] to {{IMG:filename}}
            img_m = re.match(r"\s*\[\[(?:Image|File):([^|\]]+)[^\]]*\]\]\s*$",
                             content, re.IGNORECASE)
            if img_m:
                content = f"{{{{IMG:{img_m.group(1).strip()}}}}}"
            else:
                content = re.sub(r"\[\[(?:Image|File):[^\]]*\]\]", "", content, flags=re.IGNORECASE)
                content = re.sub(r"\{\{ditto(?:\|[^{}]*)?\}\}", "\u2033",
                                 content, flags=re.IGNORECASE)
                content = re.sub(r"\{\{\.\.\.\}\}", "...", content)
                content = _strip_br(content)
                content = content.strip()
                # Run text_transform FIRST so it can convert
                # templates it knows about (``{{sfrac|…}}``,
                # ``{{hi|…}}``, ``{{sc|…}}``, etc.) into their
                # marker form.  Previously the catch-all
                # ``\{\{[^{}]*\}\}`` strip ran first and ate every
                # unlabelled template, dropping SHIPBUILDING's
                # ``{{sfrac|…|Volume of Displacement|Length × …}}``
                # entirely from the "Block coefficients or" cell.
                if content:
                    content = text_transform(content)
                # Strip any templates text_transform didn't handle.
                content = re.sub(r"\{\{[^{}]*\}\}", "", content)
            cells_html.append(emit_html_cell(
                tag, content,
                rowspan=rowspan, colspan=colspan, styles=styles,
            ))

        if cells_html:
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")

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
        parts.append("\n\n" + p + "\n\n")
    if html_rows:
        parts.append("\n\n\u00abHTMLTABLE:<table>" +
                     caption_html +
                     "".join(html_rows) + "</table>\u00ab/HTMLTABLE\u00bb\n\n")
    return "".join(parts)


def _inline_nested_table_markers_in_htmltable_blocks(text: str) -> str:
    """Convert `{{TABLE:row|row|\u2026}TABLE}` markers nested INSIDE
    `\u00abHTMLTABLE:\u2026\u00ab/HTMLTABLE\u00bb` blocks to inline `<table>` HTML.

    A nested wiki table (ORNITHOLOGY taxonomic alignments, EOCENE
    etymology glossary inside a `<ref>`) gets processed by the inner
    element handler and emitted as a `{{TABLE:\u2026}TABLE}` marker.  When
    the outer table is HTMLTABLE-formatted, the substituted child
    marker leaks as literal text in the rendered cell.  Scoped to
    inside HTMLTABLE blocks so top-level paragraph-level
    `{{TABLE:\u2026}TABLE}` markers stay untouched.
    """
    def _convert_marker(m: re.Match) -> str:
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
        return (
            '<table class="nested-data-table">'
            + "".join(rows_out)
            + "</table>"
        )
    # Walk HTMLTABLE blocks (depth-aware so nested HTMLTABLE markers
    # don't shadow the outer's TABLE-marker conversion).
    out: list[str] = []
    i = 0
    HT_OPEN = "\u00abHTMLTABLE:"
    HT_CLOSE = "\u00ab/HTMLTABLE\u00bb"
    while i < len(text):
        opener = text.find(HT_OPEN, i)
        if opener < 0:
            out.append(text[i:])
            break
        out.append(text[i:opener])
        depth = 1
        j = opener + len(HT_OPEN)
        block_start = j
        while j < len(text) and depth > 0:
            n_open = text.find(HT_OPEN, j)
            n_close = text.find(HT_CLOSE, j)
            if n_close < 0:
                # Unbalanced \u2014 preserve original; don't risk
                # mangling.
                out.append(text[opener:])
                return "".join(out)
            if 0 <= n_open < n_close:
                depth += 1
                j = n_open + len(HT_OPEN)
            else:
                depth -= 1
                if depth == 0:
                    inner_block = text[block_start:n_close]
                    inner_block = re.sub(
                        r"\{\{TABLE:(.*?)\}TABLE\}",
                        _convert_marker, inner_block,
                        flags=re.DOTALL,
                    )
                    out.append(HT_OPEN + inner_block + HT_CLOSE)
                    j = n_close + len(HT_CLOSE)
                else:
                    j = n_close + len(HT_CLOSE)
        i = j
    return "".join(out)


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


def _process_chemistry_layout(inner: str, text_transform,
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
            content = re.sub(
                r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content)
            content = content.strip()
            if content:
                content = text_transform(content)
            # Anything text_transform didn't expand is unrecognised
            # template noise (e.g. stray {{larger|...}} that the
            # template was supposed to neutralise).
            content = re.sub(r"\{\{[^{}]*\}\}", "", content)
            content = (content.replace("&vert;", "|")
                              .replace("&nbsp;", " "))
            for sentinel, glyph in _CHEM_RESOLVE.items():
                if sentinel in content:
                    content = content.replace(sentinel, glyph)
            cells_html.append(emit_html_cell(
                tag, content, rowspan=rowspan, colspan=colspan,
            ))
        if cells_html:
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
    if not html_rows:
        return ""
    return ("\n\n\u00abCHEM:<table>" + "".join(html_rows)
            + "</table>\u00ab/CHEM\u00bb\n\n")



# Per-cell alignment the producer must carry so the viewer renders it instead
# of defaulting to left.  Source encodes it two ways: an HTML/CSS attr
# (``align="right"`` / ``text-align:center``) or an EB1911 ``{{Ts|…}}`` style
# code (``ar`` right, ``ac`` center, ``al`` left).  Both live in the cell's
# attr-part (and sometimes leak into content), so scan both.
_CELL_ALIGN_ATTR_RE = re.compile(
    r"(?:text-)?align\s*[:=]\s*\"?\s*(right|centre|center|left)", re.IGNORECASE)
_CELL_TS_RE = re.compile(r"\{\{[Tt]s\|([^{}]*)\}\}")


def _cell_align(attr_part: str, content: str) -> str | None:
    """Resolved alignment for one cell: ``"right"``/``"center"``/``"left"`` or
    ``None`` (left default)."""
    blob = (attr_part or "") + " " + (content or "")
    m = _CELL_ALIGN_ATTR_RE.search(blob)
    if m:
        a = m.group(1).lower()
        return "center" if a.startswith("cent") else a
    for tm in _CELL_TS_RE.finditer(blob):
        codes = set(re.split(r"[|\s]+", tm.group(1).strip().lower()))
        if "ar" in codes:
            return "right"
        if "ac" in codes:
            return "center"
        if "al" in codes:
            return "left"
    return None


def _extract_table_cells(row_text, text_transform, with_attrs=False):
    """Extract data cells from a row via the shared `split_wiki_row`
    helper, then drop any remaining `{{Ts|…}}` cell-styling templates
    and run each cell's content through `text_transform`.

    ``with_attrs=True`` returns ``(content, align)`` tuples — the per-cell
    alignment the producer must CARRY (resolved before the `{{Ts}}` style
    codes are stripped) so the viewer renders it instead of guessing.
    Default returns plain content strings (the other callers' contract).

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
            cells.append((" ", None) if with_attrs else " ")
            continue
        # Content that is itself a bare attribute keyword (`colspan=2`
        # with no trailing-pipe boundary in source) is a malformed
        # cell, not real content — drop it.
        if _CELL_ATTR_RE.match(content):
            continue
        # Resolve alignment BEFORE stripping the {{Ts|…}} style codes that
        # carry it (`{{Ts|ar}}` = right) — the TABLE marker now CARRIES
        # alignment so the viewer renders it rather than guessing.
        align = _cell_align(attr_part, content) if with_attrs else None
        cleaned = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content).strip()
        val = text_transform(cleaned) if cleaned else " "
        cells.append((val, align) if with_attrs else val)
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


def _process_single_column_table(inner: str, text_transform) -> str:
    """Render a single-column wikitable as a `«PRE:` text block.

    Carved out of `_process_table`'s hidden dispatch: a `{|…|}` used to
    box/centre a run of text (one cell per row) is a text block, not a
    grid.  Selected upstream by the `SINGLE_COLUMN_TABLE` label
    (`_is_single_column_table`); this producer only ever sees that shape.
    """
    inner = _strip_br(inner)
    text_lines = []
    if _HTML_TABLE_TAG_RE.search(inner):
        for cells in _html_table_grid(inner):
            content = [c for c in (text_transform(x) for x in cells)
                       if c.strip()]
            if content:
                text_lines.append(content[0])
    else:
        for raw_row in re.split(r"\|-[^\n]*", inner):
            content = [c for c in _extract_table_cells(raw_row, text_transform)
                       if c.strip()]
            if content:
                text_lines.append(content[0])
    return "«PRE:" + "\n".join(text_lines) + "«/PRE»"


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


def _process_verse_table(inner: str, text_transform,
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
                return "\n\n" + "\n\n".join(parts) + "\n\n"
    # Single-cell quoted poem: split the cell on `<br>` into verse lines
    # (joining soft-hyphen `-<br>` breaks first), BEFORE `_strip_br` would
    # flatten them.
    sc = _single_cell_verse_cell(inner)
    if sc is not None:
        sc = re.sub(r"(\w)-<br\s*/?>\s*", r"\1", sc, flags=re.IGNORECASE)
        lines = [text_transform(ln.strip())
                 for ln in _VERSE_BR_RE.split(sc) if ln.strip()]
        return "\n\n{{VERSE:" + "\n".join(lines) + "}VERSE}\n\n"

    inner = _strip_br(inner)
    lines = []
    for rv in re.split(r"\|-[^\n]*", inner):
        cells = _extract_table_cells(rv, text_transform)
        if len(cells) == 2:
            col1, col2 = cells[0].strip(), cells[1].strip()
            if col2:
                line = f"{col1}{col2}" if col1 else col2
                lines.append(text_transform(line))
    return "\n\n{{VERSE:" + "\n".join(lines) + "}VERSE}\n\n"


def _process_table(inner: str, text_transform,
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
        parts.append(_emit_table_marker(text_rows, header=is_header_table))

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


def _process_html_table(
    raw: str,
    inner: str,
    text_transform,
    inner_registry: ElementRegistry | None,
) -> str:
    """Convert HTML table content to either an unwrapped illustration,
    a {{TABLE:...}TABLE} data table, or an HTMLTABLE marker when
    rowspan/colspan need to be preserved."""
    # Strip HTML comments before any row/cell parsing — matches what the
    # bypassed Layer-A ``html_comments`` pass did; without this, comments
    # between ``<tr>``s leak as bogus content (cf. the chem producer).
    inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    if "Oriental Railways" in inner and "Mustafa-Pasha" in inner:
        import sys as _sys
        _sys.stderr.write("DEBUG: _process_html_table called for TURKEY\n")
    # Illustration wrapper — unwrap to IMG + caption
    if _is_html_illustration_wrapper(raw, inner_registry):
        return _unwrap_html_illustration(inner, text_transform, inner_registry)

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", inner, re.DOTALL | re.IGNORECASE)
    if not rows:
        # No <tr> wrappers (e.g. HYDRAULICS: `<table><td>...</td></table>`
        # with td directly under table). Try to pull out <td> cells; if
        # none, strip and run the plain content through text_transform so
        # italic/bold/entities get converted.
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>",
            inner, re.DOTALL | re.IGNORECASE)
        if cells:
            parts = []
            for c in cells:
                c = _strip_wiki_cell_attr_in_html(c)
                c = _strip_br(c)
                c = _convert_inline_sub_sup(c)
                c = re.sub(r"<[^>]+>", " ", c)
                c = re.sub(r"\s+", " ", c).strip()
                if c:
                    parts.append(text_transform(c))
            return " | ".join(parts)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            text = text_transform(text)
        return text

    parsed_rows = []
    has_header = False
    has_span = False
    for row in rows:
        if "<th" in row.lower():
            has_header = True
        matches = re.findall(
            r"<(t[dh])([^>]*)>(.*?)</\1>",
            row, re.DOTALL | re.IGNORECASE)
        if not matches:
            continue
        parsed = []
        for tag, attrs, cell in matches:
            rs = re.search(r'rowspan\s*=\s*"?(\d+)"?', attrs, re.IGNORECASE)
            cs = re.search(r'colspan\s*=\s*"?(\d+)"?', attrs, re.IGNORECASE)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            if rowspan > 1 or colspan > 1:
                has_span = True
            c = _strip_wiki_cell_attr_in_html(cell)
            c = _strip_br(c)
            c = _convert_inline_sub_sup(c)
            c = re.sub(r"<[^>]+>", " ", c)
            c = re.sub(r"\s+", " ", c).strip()
            if c:
                c = text_transform(c)
            parsed.append((tag.lower(), rowspan, colspan, c))
        if parsed:
            parsed_rows.append(parsed)

    if not parsed_rows:
        return ""

    if has_span:
        html_rows = []
        for parsed in parsed_rows:
            cells_html = [
                emit_html_cell(tag, content,
                               rowspan=rowspan, colspan=colspan)
                for tag, rowspan, colspan, content in parsed
            ]
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
        return ("\n\n\u00abHTMLTABLE:<table>" +
                "".join(html_rows) +
                "</table>\u00ab/HTMLTABLE\u00bb\n\n")

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
            return "\n\n" + only_cell + "\n\n"

    text_rows = []
    for parsed in parsed_rows:
        cells = [content for _, _, _, content in parsed if content]
        if cells:
            text_rows.append(" | ".join(cells))
    if text_rows:
        return _emit_table_marker(text_rows, header=bool(has_header))
    return ""


