"""Wiki-table renderers.

Each handler takes the table's inner content (delimiters stripped,
child elements placeholdered) and returns a marker-form string for the
viewer to render.

Dispatch lives in ``elements/__init__.py``; classification logic in
``_classify_table`` chooses which renderer to call.
"""

from __future__ import annotations

import re

from britannica.pipeline.stages.elements._image import _process_image_from_raw
from britannica.pipeline.stages.elements._leaf import (
    _format_structural_formula,
    _is_structural_formula,
)
from britannica.pipeline.stages.elements._registry import ElementRegistry, _PH
from britannica.pipeline.stages.elements._text import _strip_br

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
            # Regular row (no nested tables) — process normally
            cells_html = []
            for line in row_text.split("\n"):
                line = line.strip()
                if not line or line.startswith("|+"):
                    continue
                if not (line.startswith("|") or line.startswith("!")):
                    continue
                tag = "th" if line.startswith("!") else "td"

                # Strip {{Ts}} from cell lines
                cell_text = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "",
                                   line[1:])
                # Protect pipes inside remaining {{...}} templates (e.g.
                # {{brace2|4|l}}, {{sub|1}}) so the attr/content rpartition
                # below doesn't mis-split on a template-internal pipe.
                cell_text = re.sub(
                    r"\{\{[^}]*\}\}",
                    lambda m: m.group(0).replace("|", "\x04"),
                    cell_text,
                )
                # Split on || for multi-cell lines
                sep = "!!" if tag == "th" else "||"
                for cell in cell_text.split(sep):
                    # Strip attributes
                    if "|" in cell:
                        _, _, content = cell.rpartition("|")
                    else:
                        content = cell
                    content = content.replace("\x04", "|")
                    content = re.sub(r"\{\{[^{}]*\}\}", "", content)
                    content = re.sub(r"&nbsp;", " ", content)
                    content = _strip_br(content)
                    content = re.sub(r"<td[^>]*>", "", content,
                                     flags=re.IGNORECASE)
                    content = content.strip()
                    if content and text_transform:
                        content = text_transform(content)
                    cells_html.append(f"<{tag}>{content}</{tag}>")

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

    # Extract the wikitext table caption (`|+ TEXT` on its own line)
    # so we can render it inside the HTML table's <caption> element.
    # Without this the caption silently disappears (AFRICA's "BANTU
    # NEGROIDS", EXCHEQUER's "Revenue"/"Expenditure" / "Surplus", etc.).
    caption_html = ""
    cap_match = re.search(r"^\|\+\s*(.+?)$", inner, re.MULTILINE)
    if cap_match:
        cap_raw = cap_match.group(1).strip()
        cap_text = text_transform(cap_raw) if cap_raw else ""
        if cap_text:
            caption_html = f"<caption>{cap_text}</caption>"

    # Row split on `|-` wiki row separators — but only when `|-` is at
    # the start of a line. A `|-` in the middle of a line always means
    # something else (template arg like ``{{rotate|-90|…}}``, math
    # inline, etc.) and must not terminate a row. The old pattern
    # `\|-[^\n]*` ate templates that happened to contain a `|-`.
    rows = re.split(r"(?:^|\n)\|-[^\n]*", inner)
    html_rows = []

    for row in rows:
        # Merge continuation lines into the preceding cell: a wiki cell
        # can have content that spills onto subsequent lines (e.g.
        # `|attr|\n content\n more content`). Any non-empty line that
        # doesn't start with `|`, `!`, or `{|` belongs to the previous
        # cell.
        merged: list[str] = []
        for ln in row.split("\n"):
            stripped = ln.strip()
            if not stripped:
                continue
            if stripped.startswith(("|", "!")) or stripped == "{|":
                merged.append(ln)
            elif merged:
                merged[-1] = merged[-1].rstrip() + " " + stripped
            else:
                # orphan text before any cell — keep as-is
                merged.append(ln)

        cells_html = []
        for line in merged:
            line = line.strip()
            if not line or line.startswith("|+"):
                continue
            if not (line.startswith("|") or line.startswith("!")):
                continue
            tag = "th" if line.startswith("!") else "td"
            sep = "!!" if tag == "th" else "||"

            # Protect pipes inside {{...}} and [[...]] before any splitting
            prot = re.sub(r"\{\{[^}]*\}\}",
                          lambda m: m.group(0).replace("|", "\x04"), line[1:])
            prot = re.sub(r"\[\[[^\]]*\]\]",
                          lambda m: m.group(0).replace("|", "\x04"), prot)

            for cell in prot.split(sep):
                # Split attributes from content on the last real |
                if "|" in cell:
                    attr_part, _, content = cell.rpartition("|")
                    attr_part = attr_part.replace("\x04", "|")
                else:
                    attr_part = ""
                    content = cell
                content = content.replace("\x04", "|")

                # Extract structural attributes
                attrs = ""
                rs = re.search(r'rowspan\s*=\s*"?(\d+)"?', attr_part, re.IGNORECASE)
                cs = re.search(r'colspan\s*=\s*"?(\d+)"?', attr_part, re.IGNORECASE)
                if rs:
                    attrs += f' rowspan="{rs.group(1)}"'
                if cs:
                    attrs += f' colspan="{cs.group(1)}"'
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
                if styles:
                    attrs += f' style="{";".join(styles)}"'

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
                cells_html.append(f"<{tag}{attrs}>{content}</{tag}>")

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

    # Cell attribute pattern
    _ATTR = re.compile(
        r"^(?:colspan|rowspan|width|style|align|valign|class|"
        r"cellpadding|nowrap|border|bgcolor|height)[\s=|]",
        re.IGNORECASE,
    )

    def _extract_cells(row_text):
        """Extract data cells from a row, stripping attributes, processing each."""
        # Protect {{...}} and placeholders from pipe-splitting (this
        # also protects pipes inside {{Ts|...}} cell-styling templates,
        # so the template's internal `|` doesn't get interpreted as a
        # cell boundary).
        protected = re.sub(r"\{\{[^}]*\}\}",
                           lambda m: m.group(0).replace("|", "\x04"),
                           row_text)
        protected = re.sub(re.escape(_PH) + r"[^" + re.escape(_PH) + r"]+"
                           + re.escape(_PH),
                           lambda m: m.group(0).replace("|", "\x04"),
                           protected)

        # Normalize `||` (MediaWiki same-line cell shorthand) to `\n|`
        # so it's treated as a single cell separator, not two.
        protected = protected.replace("||", "\n|")

        # Split cells on `|` at LINE STARTS only — a mid-line `|` is
        # the cell's internal attribute/content separator (`|attrs|
        # content`), NOT a cell boundary.  The older ``\|([^|\n]*)``
        # regex split on every `|` and produced phantom empty cells in
        # rows like ``| ||{{Ts|ar}} | text`` (BRITISH EMPIRE Eastern
        # Colonies summation rows): the source intent was one empty
        # cell + one right-aligned cell, not three cells.
        raw_cells = re.findall(r"(?:^|\n)\|([^\n]*)", protected)

        cells = []
        for c in raw_cells:
            # Operate on the PROTECTED form (with `\x04` standing in
            # for pipes inside `{{...}}` templates) so the rpartition
            # below splits only on real cell attr/content separators,
            # never on a pipe that lives inside ``{{em|3}}`` etc.
            s_prot = c.strip()
            if s_prot.replace("\x04", "|") in ("}", "{|"):
                continue
            # Within-cell attribute/content split: the LAST `|` in a
            # cell (outside templates) separates attribute prefix from
            # content (``attrs|content``).  Treat the prefix as
            # attributes only if it actually looks like one (Ts-styling
            # template or standard table-attribute keyword).
            if "|" in s_prot:
                attr_part, _, content = s_prot.rpartition("|")
                attr_check = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "",
                                    attr_part.replace("\x04", "|")).strip()
                if not attr_check or _ATTR.match(attr_check):
                    s_prot = content
            s = s_prot.replace("\x04", "|").strip()
            if s and _ATTR.match(s):
                continue
            # Drop any remaining {{Ts|...}} cell-styling templates.
            s = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", s).strip()
            # Process cell content through text_transform (empty → " ")
            cells.append(text_transform(s) if s else " ")
        return cells

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
                etype, eraw = inner_registry.elements.get(ph_id, ("", ""))
                if etype == "IMAGE":
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
                            return f"{{{{IMG:{filename}|{caption}}}}}"
                        return f"{{{{IMG:{filename}}}}}"

    # Single-column tables (1 cell per row) are text blocks, not data tables.
    # Render as preformatted text with line breaks preserved.
    if "|-" in inner:
        raw_rows = re.split(r"\|-[^\n]*", inner)
        all_single = True
        text_lines = []
        for raw_row in raw_rows:
            cells = _extract_cells(raw_row)
            content = [c for c in cells if c.strip()]
            if len(content) == 0:
                continue
            if len(content) > 1:
                all_single = False
                break
            text_lines.append(content[0])
        if all_single and text_lines:
            joined = "\n".join(text_lines)
            return f"\u00abPRE:{joined}\u00ab/PRE\u00bb"

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

    # Check for brace layout (poem + translation side by side)
    if "brace" in inner.lower() and "rowspan" in inner.lower():
        result = _process_brace_table(inner, text_transform)
        if result:
            return result

    # Check for verse-layout table: 2 columns where col 1 is just punctuation
    # (quote marks) and col 2 has the actual verse text.
    raw_rows_v = re.split(r"\|-[^\n]*", inner)
    verse_rows_v = []
    is_verse_layout = len(raw_rows_v) >= 1
    for rv in raw_rows_v:
        cells_v = _extract_cells(rv)
        if not cells_v:
            continue
        if len(cells_v) == 2:
            col1 = cells_v[0].strip()
            col2 = cells_v[1].strip()
            # Col 1 must be only punctuation/quotes (or empty)
            if col1 and not re.match(r'^[\s"\'\u201c\u201d\u2018\u2019,.\-;:—]+$', col1):
                is_verse_layout = False
                break
            if col2:
                verse_rows_v.append((col1, col2))
        elif len(cells_v) == 1 and not cells_v[0].strip():
            continue  # empty row
        else:
            is_verse_layout = False
            break
    if is_verse_layout and verse_rows_v:
        # Combine leading/trailing punctuation with verse lines
        lines = []
        for col1, col2 in verse_rows_v:
            line = f"{col1}{col2}" if col1 else col2
            if text_transform:
                line = text_transform(line)
            lines.append(line)
        return "\n\n{{VERSE:" + "\n".join(lines) + "}VERSE}\n\n"

    # Check for structural formula (monospaced, spatial layout)
    if _is_structural_formula(inner):
        return _format_structural_formula(inner)

    # Check for table caption (|+ ...)
    caption = ""
    cap_match = re.search(r"\|\+\s*(.*?)(?:\n|$)", inner)
    if cap_match:
        caption = re.sub(r"\{\{[^{}]*\}\}", "", cap_match.group(1)).strip()

    # Split into rows on |- separators
    raw_rows = re.split(r"\|-[^\n]*", inner)

    text_rows = []
    image_parts = []

    for raw_row in raw_rows:
        # Skip caption rows
        if "|+" in raw_row:
            continue

        # Preserve any child element placeholders outside cells
        for line in raw_row.split("\n"):
            stripped_line = line.strip()
            if _PH in stripped_line and not stripped_line.startswith("|"):
                image_parts.append(stripped_line)

        # Extract and process cells
        cells = _extract_cells(raw_row)

        # Separate image-only cells from data cells — but only when
        # the entire row is images (plate layout).  If a row mixes
        # images and text (e.g. score + description), keep them together.
        img_cells = [c for c in cells if re.match(r"^\s*\{\{IMG:[^}]+\}\}\s*$", c)]
        non_img_cells = [c for c in cells if not re.match(r"^\s*\{\{IMG:[^}]+\}\}\s*$", c)]

        if img_cells and not non_img_cells:
            # All-image row — separate for plate layout
            image_parts.extend(c.strip() for c in img_cells)
            continue

        data_cells = cells  # keep images and text together

        # Handle <br> in cells
        br_cells = [i for i, c in enumerate(data_cells)
                    if re.search(r"<br\s*/?>", c, re.IGNORECASE)]
        if len(br_cells) >= 2:
            # Multi-row data: expand
            split = [re.split(r"<br\s*/?>", c, flags=re.IGNORECASE)
                     for c in data_cells]
            max_sub = max(len(s) for s in split)
            for s in split:
                while len(s) < max_sub:
                    s.append("")
            for i in range(max_sub):
                sub = [s[i].strip() for s in split]
                if any(sub):
                    text_rows.append(" | ".join(sub))
        elif br_cells:
            # Single-cell br: collapse to space (strip soft-hyphen breaks)
            data_cells = [_strip_br(c).strip() for c in data_cells]
            text_rows.append(" | ".join(data_cells))
        else:
            text_rows.append(" | ".join(data_cells))

    # Strip spacer columns from colspan tables only.  These tables have
    # group headers spanning multiple data columns, with empty separator
    # columns between groups.  The colspan attribute in the raw markup
    # is the reliable signal — no single-header table uses colspan.
    if "colspan" in inner and len(text_rows) >= 4:
        split_rows = [r.split(" | ") for r in text_rows]
        ncols = max(len(r) for r in split_rows)
        # Identify data rows vs section-divider rows.  Data rows repeat
        # 3+ times (e.g. 1897, 1901, 1906) at a consistent column count.
        # Section dividers (group labels) repeat fewer times.
        from collections import Counter
        col_counts = Counter(len(r) for r in split_rows)
        # Column counts with 3+ occurrences are data row groups.
        # Counts with exactly 2 are sub-header + section-divider pairs.
        # Use 3+ as the threshold to separate data from labels.
        data_col_groups = {n for n, cnt in col_counts.items() if cnt >= 3}
        if not data_col_groups:
            # Fallback: use the most common count with 2+
            data_col_groups = {col_counts.most_common(1)[0][0]}
            if col_counts.most_common(1)[0][1] < 2:
                data_col_groups = set()

        # For each group, find columns empty in ALL rows of that group
        empty_by_group: dict[int, set[int]] = {}
        for ncols_g in data_col_groups:
            group_rows = [r for r in split_rows if len(r) == ncols_g]
            empty = set()
            for j in range(ncols_g):
                if all(not r[j].strip() for r in group_rows):
                    empty.add(j)
            if empty:
                empty_by_group[ncols_g] = empty

        if empty_by_group:
            new_rows = []
            for cells in split_rows:
                nc = len(cells)
                if nc in empty_by_group:
                    new_rows.append(" | ".join(
                        cells[j] for j in range(nc)
                        if j not in empty_by_group[nc]
                    ))
                else:
                    # Section-divider row (group labels from colspan).
                    # Strip empty cells, then pad to match the stripped
                    # data column count so labels align over their groups.
                    content_cells = [c for c in cells if c.strip()]
                    if content_cells and empty_by_group:
                        # Find the stripped data column count
                        target = max(empty_by_group)
                        stripped_ncols = target - len(empty_by_group[target])
                        if stripped_ncols < 2:
                            new_rows.append(" | ".join(
                                c for c in cells if c.strip()))
                            continue
                        # Check if first raw cell is empty (no row label)
                        has_label = bool(cells[0].strip()) if cells else True
                        if has_label:
                            # First content cell is the row label;
                            # remaining cells are group headers
                            n_groups = len(content_cells) - 1
                            data_cols = stripped_ncols - 1
                            span = data_cols // n_groups if n_groups > 0 else 1
                            padded = [""] * stripped_ncols
                            padded[0] = content_cells[0]
                            for k, lbl in enumerate(content_cells[1:]):
                                pos = 1 + k * span
                                if pos < stripped_ncols:
                                    padded[pos] = lbl
                        else:
                            # No row label on this row — all content cells are
                            # group headers.  But a label column may still exist
                            # (populated in the sub-header row, e.g. "Year.").
                            n_groups = len(content_cells)
                            data_cols = stripped_ncols - 1  # assume label column
                            if n_groups > 0 and data_cols >= n_groups:
                                span = data_cols // n_groups
                                padded = [""] * stripped_ncols
                                for k, lbl in enumerate(content_cells):
                                    pos = 1 + k * span
                                    if pos < stripped_ncols:
                                        padded[pos] = lbl
                            else:
                                # No label column — groups fill all columns
                                span = stripped_ncols // n_groups if n_groups > 0 else 1
                                padded = [""] * stripped_ncols
                                for k, lbl in enumerate(content_cells):
                                    pos = k * span
                                    if pos < stripped_ncols:
                                        padded[pos] = lbl
                        new_rows.append(" | ".join(padded))
                    else:
                        new_rows.append(" | ".join(
                            c for c in cells if c.strip()
                        ))
            text_rows = new_rows

    # Assemble output
    parts = []
    if image_parts:
        parts.extend(image_parts)
    if text_rows:
        header = "H" if caption else ""
        parts.append("{{TABLE" + header + ":" + "\n".join(text_rows) + "}TABLE}")

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
    child_types = [t for t, _ in inner_registry.elements.values()]
    n_images = sum(1 for t in child_types if t == "IMAGE")
    if n_images < 1:
        return False
    # No other block-level children (nested tables etc.)
    if any(t not in ("IMAGE", "REF", "MATH") for t in child_types):
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
            if key in inner_registry.elements:
                img_type, img_raw = inner_registry.elements[key]
                if img_type == "IMAGE":
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
                    if m and m.group(0) in inner_registry.elements:
                        img_type, img_raw = inner_registry.elements[m.group(0)]
                        if img_type == "IMAGE":
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
            c = _strip_br(cell)
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
            cells_html = []
            for tag, rowspan, colspan, content in parsed:
                attrs = ""
                if rowspan > 1:
                    attrs += f' rowspan="{rowspan}"'
                if colspan > 1:
                    attrs += f' colspan="{colspan}"'
                cells_html.append(f"<{tag}{attrs}>{content}</{tag}>")
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
            k for k, (t, _) in inner_registry.elements.items()
            if t == "POEM"
        }
        if only_cell in poem_phs:
            return "\n\n" + only_cell + "\n\n"

    text_rows = []
    for parsed in parsed_rows:
        cells = [content for _, _, _, content in parsed if content]
        if cells:
            text_rows.append(" | ".join(cells))
    if text_rows:
        header = "H" if has_header else ""
        return "{{TABLE" + header + ":" + "\n".join(text_rows) + "}TABLE}"
    return ""


def _process_brace_table(inner: str, text_transform) -> str | None:
    """Handle tables with {{brace}} + rowspan used for poem/translation layout.

    Pattern: left column has verse lines (one per row), middle has a brace,
    right column has a merged translation cell.  Output as verse + translation.
    """
    rows = re.split(r"\|-[^\n]*", inner)

    left_lines = []
    right_text = ""

    for row in rows:
        # Strip templates and attributes
        cleaned = re.sub(r"\{\{[^{}]*\}\}", "", row)
        cells = re.findall(r"\|([^|\n]+)", cleaned)
        cells = [c.strip() for c in cells
                 if c.strip()
                 and not re.match(
                     r"^(?:colspan|rowspan|width|style|align|valign|class|"
                     r"cellpadding|nowrap|border|bgcolor|height)[\s=|]",
                     c.strip(), re.IGNORECASE)
                 and c.strip() not in ("}", "{|")]

        for cell in cells:
            # Skip brace artifacts and empty cells
            if not cell or len(cell) < 2:
                continue
            # The right-side translation tends to be the longest cell
            if len(cell) > 40 and not right_text:
                right_text = text_transform(cell)
            elif len(cell) > 2:
                left_lines.append(text_transform(cell))

    if not left_lines:
        return None

    # Build verse block with translation
    verse = "{{VERSE:" + "\n".join(left_lines) + "}VERSE}"
    if right_text:
        return verse + "\n\n" + right_text
    return verse


