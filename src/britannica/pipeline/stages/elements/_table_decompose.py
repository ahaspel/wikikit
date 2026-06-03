"""Canonical recursive table decomposition: table → rows → cells → body-text.

The architectural principle (per
[[feedback_table_decomposes_recursively]]):

  * **Table** extracts ROWS.  Peels `{|…|}` (wiki) or `<table>…</table>`
    (HTML) plus the opener-attribute slot.  Returns the canonical row
    list.
  * **Row** extracts CELLS.  Peels `|-…` / `<tr …>` plus its attribute
    slot.  Returns the canonical cell list.
  * **Cell** produces its own CONTENT via `text_transform` (i.e.
    body-text, the same `_apply_markup` that processes article prose).
    Its attribute slot feeds `_cell_styles` and is consumed entirely.

Cell content reaches body-text in a context indistinguishable from a
paragraph in prose — same handlers, same totality argument.  There is no
"table context" or "cell context" mode; the inner template handlers
(`_convert_sc`, `_convert_sub_sup`, `_convert_foreign_script`, …) apply
uniformly.

This module provides shape-agnostic infrastructure.  Producers (single
data-table producer, HTML-table producer, special-shape producers in
the layout / chemistry / math families) pick the appropriate ROW
EXTRACTOR for their source flavor, then dispatch through the uniform
cell-produce + assemble pipeline below.

Status: Step 1 — additive infrastructure, no existing producers
migrated yet (they continue using their inline logic).  Step 2 migrates
`_process_html_table` to the canonical path; subsequent steps the wiki
and special-shape producers.
"""
from __future__ import annotations

import re
from typing import Callable

TextTransform = Callable[[str], str]

# A nested-table producer: raw ``{|…|}`` bytes -> produced marker string.
# Passed into :func:`produce_cell` by a caller that owns recursion; the
# cell extractor hands it each raw nested table it finds in cell content.
# See the module note on producer-owned recursion below.
NestedTableProducer = Callable[[str], str]


# ── Canonical data shapes ───────────────────────────────────────────────
#
# A `Cell` is `(sep, attr_part, content)`:
#   * `sep` — `'|'` for data cells (wiki `|`/`||`, HTML `<td>`) or `'!'`
#     for header cells (wiki `!`/`!!`, HTML `<th>`).
#   * `attr_part` — raw attribute prefix (wiki `colspan=2 {{Ts|ac}}` or
#     HTML `colspan="2" style="text-align:center"`).  Consumed entirely
#     by `_cell_styles`; nothing here reaches `text_transform`.
#   * `content` — raw cell body, un-transformed.  Fed to `text_transform`
#     by `produce_cell` below.
#
# A `Row` is `(row_attr_part, list[Cell])`:
#   * `row_attr_part` — wiki `|-<attrs>` tail or HTML `<tr <attrs>>`
#     attribute string.  Today the canonical marker format carries no
#     row-level style slot; we extract it so the data is available for
#     future marker-format extension without re-touching the extractors.

Cell = tuple[str, str, str]
Row = tuple[str, list[Cell]]


# ── Nested-table recognition (producer-owned recursion) ─────────────────
#
# The flat-walker contract: the walker bounds only the OUTERMOST shapes
# and never descends into element bodies; each producer owns its own
# recursion privately.  For tables that means the cell extractor must
# recognize a raw nested ``{|…|}`` sitting in cell content and recurse on
# it, rather than relying on the walker to have lifted it into a
# placeholder + ``inner_registry`` first.
#
# THIS PATH IS DORMANT TODAY.  The walker still placeholderizes nested
# tables during classification, so in production a cell's content carries
# a ``\x03ELEM:N\x03`` placeholder, never raw ``{|`` bytes — the masking
# below matches nothing and :func:`produce_cell`'s recursion (opt-in via
# ``recurse``) is never wired by a caller.  It wakes up only when the
# walker stops descending into ``SHAPE_BRACE_PIPE`` (the eventual flip),
# at which point a nested table reaches the cell raw and the producer
# recurses.  Until then it is exercised only by direct-feed unit tests.

# Nested-table mask token: ``\x03``-delimited so ``split_wiki_row``'s
# existing placeholder protection treats it as opaque content (it carries
# no ``|`` / newline, so the row/cell splitters can't fragment it).  The
# ``NT`` infix keeps it distinct from the walker's ``ELEM:`` placeholders.
_NESTED_SENTINEL = "\x03"


def _nested_token(i: int) -> str:
    return f"{_NESTED_SENTINEL}NT{i}{_NESTED_SENTINEL}"


def find_nested_table_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` for each top-level balanced ``{|…|}`` span.

    Depth-counted over ``{|`` / ``|}`` token pairs, so a table nested
    inside a nested table is wholly contained within the OUTER span (the
    outer span alone is returned; whatever produces that span finds the
    inner one on its own next descent — recursion at the right layer).
    A degenerate unclosed ``{|`` runs the span to end-of-string.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("{|", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("{|", j):
                    depth += 1
                    j += 2
                elif text.startswith("|}", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


def _mask_nested_tables(text: str) -> tuple[str, list[str]]:
    """Replace each top-level balanced ``{|…|}`` with an opaque one-line
    token.  Returns ``(masked_text, raw_spans)`` where ``raw_spans[i]`` is
    the original bytes for ``_nested_token(i)``.  No-op (returns the input
    and an empty list) when there is no nested table — the production case.
    """
    spans = find_nested_table_spans(text)
    if not spans:
        return text, []
    raw_spans: list[str] = []
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(_nested_token(len(raw_spans)))
        raw_spans.append(text[start:end])
        last = end
    out.append(text[last:])
    return "".join(out), raw_spans


def _restore_nested(text: str, raw_spans: list[str]) -> str:
    for i, raw in enumerate(raw_spans):
        text = text.replace(_nested_token(i), raw)
    return text


# ── Wiki-side row extractor ─────────────────────────────────────────────

_WIKI_ROW_SEP_RE = re.compile(r"(?:^|\n)\s*\|-([^\n]*)")
_WIKI_CAPTION_RE = re.compile(r"(?:^|\n)\s*\|\+\s*([^\n]+)")


def split_wiki_rows_raw(inner: str) -> list[tuple[str, str]]:
    r"""Split a wikitable's inner text into ``[(row_attr, raw_row_body)]`` on
    the canonical ``|-`` row separator — the SINGLE row-splitter the wiki
    producers share.

    The separator is line-anchored AND indent-tolerant (``(?:^|\n)\s*\|-``):
    it splits a ``|-`` (or ``  |-``) that begins a line and captures that
    line's tail as the row attribute, but does NOT split on a ``|-`` that
    appears mid-content.  This replaces the divergent per-producer regexes
    (``parse_wiki_table``'s line-anchored-no-indent form, ``_process_table``'s
    unanchored over-splitting one) with one correct shared form.

    The pre-first-``|-`` segment is the first entry, with empty ``row_attr``.
    Caption (``|+``) lines are NOT dropped here — each caller applies its own
    ``|+`` policy.  Nested ``{|…|}`` are NOT masked here (in production cells
    carry placeholders, never raw ``{|`` — see the module note); callers that
    need masking (``extract_wiki_rows``) mask before calling.
    """
    parts = _WIKI_ROW_SEP_RE.split(inner)  # [pre, attr1, body1, attr2, body2…]
    rows: list[tuple[str, str]] = [("", parts[0])]
    for k in range(1, len(parts), 2):
        body = parts[k + 1] if k + 1 < len(parts) else ""
        rows.append((parts[k], body))
    return rows


# Rows and cells have two/three interchangeable spellings: a ROW is `|-` or
# `<tr>`; a CELL is `|` (data), `!` (header), or `<td>`/`<th>`.  Source mixes
# them inside one `{|` table (CEMENT: an HTML `<td>` header above wiki `|`
# rows).  Rather than flavor-route (single-syntax) and lose one spelling, we
# canonicalise the HTML spellings to their wiki equivalents up front, so the
# ONE wiki decomposer sees a uniform table.  No-op when no HTML tokens are
# present (pure-wiki path stays byte-identical).
_HTML_CELL_OPEN_RE = re.compile(r"<(t[dh])\b([^>]*)>", re.IGNORECASE)


def _canonicalize_html_cells_to_wiki(inner: str) -> str:
    """`<tr>`→`|-`, `<td attr>`→`|attr|`, `<th attr>`→`!attr|`, closers dropped.
    Wiki cells self-close at the next cell/row token, so `</td>`/`</tr>` carry
    no information once the openers are wiki."""
    if "<t" not in inner.lower():
        return inner
    inner = re.sub(r"</t[dh]>|</tr>|</table>|<table\b[^>]*>", "",
                   inner, flags=re.IGNORECASE)
    inner = re.sub(r"<tr\b[^>]*>", "\n|-\n", inner, flags=re.IGNORECASE)

    def _cell(m: "re.Match") -> str:
        mark = "!" if m.group(1).lower() == "th" else "|"
        attr = m.group(2).strip()
        return f"\n{mark}{attr}|" if attr else f"\n{mark}"
    return _HTML_CELL_OPEN_RE.sub(_cell, inner)


def extract_wiki_rows(inner: str) -> tuple[str, list[Row]]:
    """Decompose a wikitable's inner text into `(caption, rows)`.

    `inner` is the source between the outer `{|<attrs>` and `|}`
    delimiters (the walker has already bounded these).  Caption is the
    `|+<text>` line if present (returned un-transformed; the caller runs
    it through `text_transform`).  Rows are produced by splitting on
    `|-<attrs>` separators; each row's cells come from
    :func:`split_wiki_row`.

    HTML cell/row spellings (`<td>`/`<th>`/`<tr>`) inside the wikitable are
    canonicalised to their wiki equivalents first (see
    :func:`_canonicalize_html_cells_to_wiki`) so a mixed table decomposes
    through this one path.

    The pre-`|-` segment is included as a row only when it contains
    cell content (`|`/`!`-anchored lines).  A bare preamble (caption +
    blank space) becomes the empty rows list with the caption surfaced.
    """
    from britannica.pipeline.stages.elements._tables import split_wiki_row

    inner = _canonicalize_html_cells_to_wiki(inner)

    # Protect balanced nested ``{|…|}`` spans before row-splitting: the
    # outer ``|-`` row key and ``|+`` caption key would otherwise match a
    # nested table's own rows and fragment it.  Each span becomes one
    # opaque token, restored into the owning cell after splitting.  In
    # production ``masked == inner`` (no raw ``{|`` — see module note), so
    # this is byte-identical to the un-masked path.
    masked, raw_spans = _mask_nested_tables(inner)

    cap_m = _WIKI_CAPTION_RE.search(masked)
    caption = cap_m.group(1).strip() if cap_m else ""

    pieces = _WIKI_ROW_SEP_RE.split(masked)
    rows: list[Row] = []

    if len(pieces) == 1:
        cells = split_wiki_row(_drop_caption_lines(pieces[0]))
        if cells:
            rows.append(("", cells))
    else:
        preamble = _drop_caption_lines(pieces[0])
        if _has_cell_lines(preamble):
            cells = split_wiki_row(preamble)
            if cells:
                rows.append(("", cells))
        for i in range(1, len(pieces), 2):
            row_attrs = pieces[i].strip()
            body = pieces[i + 1] if i + 1 < len(pieces) else ""
            cells = split_wiki_row(_drop_caption_lines(body))
            if cells:
                rows.append((row_attrs, cells))

    if raw_spans:
        caption = _restore_nested(caption, raw_spans)
        rows = [
            (attrs, [
                (sep, attr, _restore_nested(content, raw_spans))
                for sep, attr, content in cells
            ])
            for attrs, cells in rows
        ]
    return caption, rows


def _drop_caption_lines(text: str) -> str:
    return re.sub(r"(?:^|\n)\s*\|\+[^\n]*", "", text)


def _has_cell_lines(text: str) -> bool:
    for ln in text.split("\n"):
        s = ln.lstrip()
        if s.startswith(("|", "!")) and s.strip() not in ("|", "!", "{|"):
            return True
    return False


# ── HTML-side row extractor ────────────────────────────────────────────

_HTML_TR_RE = re.compile(r"<tr([^>]*)>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_HTML_CELL_RE = re.compile(
    r"<(t[dh])([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_HTML_CAPTION_RE = re.compile(
    r"<caption[^>]*>(.*?)</caption>", re.DOTALL | re.IGNORECASE)


def extract_html_rows(inner: str) -> tuple[str, list[Row]]:
    """Decompose an HTML table's inner content into `(caption, rows)`.

    `inner` is the source between `<table>` and `</table>` (walker-
    bounded).  Caption is the `<caption>…</caption>` element if present.
    Rows come from `<tr>` matches; cells from `<td>`/`<th>` inside each
    row.  Tables without explicit `<tr>` wrappers (HYDRAULICS-style
    `<table><td>…</td></table>`) become a single synthetic row.
    """
    cap_m = _HTML_CAPTION_RE.search(inner)
    caption = cap_m.group(1).strip() if cap_m else ""

    rows: list[Row] = []
    if _HTML_TR_RE.search(inner):
        for m in _HTML_TR_RE.finditer(inner):
            row_attrs = m.group(1).strip()
            cells = _html_cells(m.group(2))
            if cells:
                rows.append((row_attrs, cells))
    else:
        cells = _html_cells(inner)
        if cells:
            rows.append(("", cells))
    return caption, rows


def _html_cells(body: str) -> list[Cell]:
    cells: list[Cell] = []
    for m in _HTML_CELL_RE.finditer(body):
        tag = m.group(1).lower()
        attrs = m.group(2).strip()
        content = m.group(3)
        sep = "!" if tag == "th" else "|"
        cells.append((sep, attrs, content))
    return cells


# ── Cell producer ──────────────────────────────────────────────────────

def produce_cell(
    attr_part: str, content: str, text_transform: TextTransform,
    recurse: NestedTableProducer | None = None,
) -> tuple[list[str], str]:
    """Produce one cell: extract its styles, run its content through
    body-text.  Returns `(styles, body)`.

    The cell's `attr_part` is consumed entirely by `_cell_styles` —
    `{{Ts|…}}` codes, inline `style="…"`, `align="…"`/`valign="…"` all
    become CSS declarations.  Nothing from `attr_part` reaches
    `text_transform`.

    `content` is the cell body, possibly containing inline templates
    (`{{sc|…}}`, `{{sup|…}}`, foreign-script wrappers, …) and inline
    HTML (`<span>`, `<i>`, etc.).  These are body-text's concern;
    `text_transform` handles them uniformly with article prose.

    ``recurse`` enables producer-owned recursion: when supplied AND the
    content carries a raw nested ``{|…|}`` table, each nested table is
    masked out, the surrounding content is run through body-text, then
    each nested table's marker — produced by ``recurse(raw)`` — is
    substituted back in.  Masking keeps the marker clear of body-text's
    template/markup handling.  ``recurse=None`` (the default, and every
    production caller today) leaves content untouched; combined with the
    fact that production cell content never carries raw ``{|`` (it's a
    placeholder — see module note), this path is dormant until the flip.
    """
    from britannica.pipeline.stages.elements._tables import _cell_styles
    styles = _cell_styles(attr_part, content)
    if not content:
        return styles, ""
    raw_spans: list[str] = []
    if recurse is not None and "{|" in content:
        content, raw_spans = _mask_nested_tables(content)
    body = text_transform(content).strip(" \t")
    for i, raw in enumerate(raw_spans):
        body = body.replace(_nested_token(i), recurse(raw))
    return styles, body


# ── Assemblers ─────────────────────────────────────────────────────────

def assemble_wiki_marker(
    produced_rows: list[tuple[str, list[tuple[list[str], str]]]],
    caption: str,
    header: bool,
    table_styles: list[str],
) -> str:
    """Compose canonical `{{TABLE…:…}TABLE}` (or `{{TABLEH…:…}TABLE}`)
    marker output from the per-row produced cells.

    `produced_rows`: list of `(row_attr_part, list[(cell_styles,
    cell_body)])` — `row_attr_part` is preserved for future row-style
    carriage in the marker (today's format has no row-style slot, so it
    is currently dropped).  Each cell becomes one
    :func:`build_table_cell` token in the row string.

    `caption` is transformed-ready text or empty.  `header=True` selects
    the `TABLEH` variant.  `table_styles` populate the `[style:…]` slot.

    Style → alignment narrowing: the wiki marker format encodes only
    `align` (no full CSS per cell).  We extract the alignment from the
    cell-style list and pass it as `align=`; the rest of the styles
    list is currently dropped at the marker boundary (the renderer can
    only honour what the format carries).  HTML emission uses the full
    styles list via :func:`assemble_html_rows`.
    """
    from britannica.markers import build_table_cell
    from britannica.pipeline.stages.elements._tables import _emit_table_marker

    text_rows: list[str] = []
    if caption:
        text_rows.append("⟦+⟧" + caption)
    for _row_attrs, cells in produced_rows:
        text_rows.append(" | ".join(
            build_table_cell(body, align=_align_of(styles))
            for styles, body in cells
        ))
    return _emit_table_marker(text_rows, header=header, styles=table_styles)


def _align_of(styles: list[str]) -> str | None:
    for r in styles:
        if r.startswith("text-align:"):
            v = r.split(":", 1)[1].strip()
            return v if v in ("right", "center", "left") else None
    return None


# ── Shared leaf: decompose → produce → assemble ─────────────────────────
#
# These three functions are the single recursive-decomposition leaf that
# every table producer collapses onto (the ICL-family model: many labels,
# one engine).  They are lifted verbatim from the canonical
# ``_process_html_table`` spine so a producer that delegates here is
# byte-identical to that spine for the same logical input.
#
# A `ParsedCell` is `(tag, rowspan, colspan, body, styles)`:
#   * `tag` — `'td'`/`'th'` (header-ness already resolved from `sep`).
#   * `rowspan`/`colspan` — integer span counts (1 when absent).
#   * `body` — the produced cell content (`text_transform` already run).
#   * `styles` — CSS declarations from `_cell_styles` (full per-cell list).
# A `ParsedRow` is `list[ParsedCell]`.
#
# Status: ADDITIVE / INERT.  No producer dispatches here yet; exercised
# only by the byte-identity unit test against ``_process_html_table``.
# Producers migrate one per phase (see the table-collapse plan).

ParsedCell = tuple[str, int, int, str, list[str]]
ParsedRow = list[ParsedCell]

_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*"?(\d+)"?', re.IGNORECASE)
_COLSPAN_RE = re.compile(r'colspan\s*=\s*"?(\d+)"?', re.IGNORECASE)
_HTML_FLAVOR_RE = re.compile(r"<table\b|<tr\b|<t[dh]\b", re.IGNORECASE)


# A producer-supplied cell-body strategy: `(attr_part, content,
# text_transform) -> (styles, body)`.  Lets a producer keep a genuinely
# irreducible per-cell branch (e.g. `_process_complex_table`'s image cell,
# which becomes `{{IMG:…}}` and must skip both `text_transform` AND the
# leftover-`{{}}` strip) while still sharing this one decomposition loop.
# Default `None` uses the canonical `produce_cell` (= `_cell_styles` +
# body-text), the uniform leaf.  A `cell_body` strategy is TRANSITIONAL:
# the end-state moves its template handling into body-text so the branch
# dissolves and the producer reverts to the default.
CellBody = Callable[[str, str, TextTransform], "tuple[list[str], str]"]

ProducedRow = tuple[str, ParsedRow]  # (row_attr_part, cells)


def produce_table_rows(
    inner: str,
    text_transform: TextTransform,
    *,
    flavor: str | None = None,
    cell_preclean: TextTransform | None = None,
    recurse: NestedTableProducer | None = None,
    cell_body: CellBody | None = None,
) -> tuple[str, list[ProducedRow], bool, bool]:
    """Decompose a table's inner source into produced rows — the single
    row/cell split + span/header detection loop every table producer
    shares (lifted from `_process_html_table:1963-1990`).

    Returns `(caption, rows, has_header, has_span)` where `rows` is a
    list of `(row_attr_part, ParsedRow)` — the row attribute slot is
    carried so a producer can emit `<tr style=…>` (the spine-facing
    assemblers ignore it and emit a bare `<tr>`).

    `flavor` selects the row extractor: `"wiki"` (`{|…|}`) or `"html"`
    (`<table>…</table>`).  `None` auto-detects from HTML table tags.

    `cell_preclean`, when given, runs on each cell's raw content BEFORE
    `text_transform` (the HTML spine uses `_html_cell_clean` — the
    lossless-`<br>` + tag-strip step).  Ignored when `cell_body` is
    supplied (that strategy owns its own cleaning).

    `recurse` is forwarded to `produce_cell` for producer-owned
    nested-table recursion (dormant in production — see module note).

    `cell_body`, when given, computes `(styles, body)` for each cell
    instead of the default `produce_cell` path — see `CellBody`.
    """
    if flavor is None:
        flavor = "html" if _HTML_FLAVOR_RE.search(inner) else "wiki"
    caption, rows = (
        extract_html_rows(inner) if flavor == "html"
        else extract_wiki_rows(inner))

    has_header = False
    has_span = False
    produced: list[ProducedRow] = []
    for row_attr, cells in rows:
        parsed: ParsedRow = []
        for sep, cell_attrs, cell_content in cells:
            tag = "th" if sep == "!" else "td"
            if sep == "!":
                has_header = True
            rs = _ROWSPAN_RE.search(cell_attrs)
            cs = _COLSPAN_RE.search(cell_attrs)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            if rowspan > 1 or colspan > 1:
                has_span = True
            if cell_body is not None:
                styles, body = cell_body(cell_attrs, cell_content, text_transform)
            else:
                cleaned = (cell_preclean(cell_content) if cell_preclean
                           else cell_content)
                styles, body = produce_cell(
                    cell_attrs, cleaned, text_transform, recurse=recurse)
            parsed.append((tag, rowspan, colspan, body, styles))
        if parsed:
            produced.append((row_attr, parsed))
    return caption, produced, has_header, has_span


def assemble_html_rows(
    parsed_rows: list[ProducedRow],
    inner_registry=None,
) -> str:
    """Compose `«HTMLTABLE:<table>…</table>«/HTMLTABLE»` from parsed rows.

    Used when any cell carries a `rowspan`/`colspan` — the wiki
    `{{TABLE:}TABLE}` marker can carry only align + colspan, so spanned
    tables emit literal HTML with full per-cell `style="…"` preserved.

    `inner_registry`, when given, pre-substitutes DATA_TABLE child
    markers as inline `<table>` HTML so a nested wiki table inside an
    HTML cell renders rather than leaking its `{{TABLE:…}TABLE}` text.

    The per-row attribute slot is IGNORED here (bare `<tr>`), matching the
    spine; producers that carry row styling assemble their own rows.
    Lifted verbatim from `_process_html_table:1992-2024`.
    """
    from britannica.pipeline.stages.elements._tables import (
        _inline_table_marker_as_html, emit_html_cell,
    )
    html_rows: list[str] = []
    for _row_attr, parsed in parsed_rows:
        cells_html = [
            emit_html_cell(tag, content,
                           rowspan=rowspan, colspan=colspan, styles=styles)
            for tag, rowspan, colspan, content, styles in parsed
        ]
        html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
    output = ("«HTMLTABLE:<table>" +
              "".join(html_rows) +
              "</table>«/HTMLTABLE»")
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


def assemble_table_marker(
    caption: str,
    parsed_rows: list[ProducedRow],
    has_header: bool,
    has_span: bool,
    *,
    inner_registry=None,
    table_styles: list[str] | None = None,
) -> str:
    """Form chooser: pick the marker form from the parsed-row shape.

    * `has_span` → `assemble_html_rows` (`«HTMLTABLE»`, full per-cell
      style).
    * otherwise → `assemble_wiki_marker` (`{{TABLE:}TABLE}` /
      `{{TABLEH:}TABLE}`, align-only).

    Empty input (no rows, or all-empty rows after the body filter)
    returns `""`.  Mirrors `_process_html_table:1992-2065`; `caption` is
    passed through to the wiki marker (the HTML spine passes `""`).  The
    per-row attribute slot is dropped at the marker boundary (today's
    format carries no row-style slot).
    """
    if not parsed_rows:
        return ""
    if has_span:
        return assemble_html_rows(parsed_rows, inner_registry)
    produced_rows: list[tuple[str, list[tuple[list[str], str]]]] = [
        ("", [(styles, body) for _, _, _, body, styles in parsed if body])
        for _row_attr, parsed in parsed_rows
    ]
    produced_rows = [(a, c) for a, c in produced_rows if c]
    if not produced_rows:
        return ""
    return assemble_wiki_marker(
        produced_rows, caption=caption, header=has_header,
        table_styles=table_styles or [],
    )
