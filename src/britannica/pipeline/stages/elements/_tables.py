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


# Wiki cell-attribute keywords — used in two places (here, _layout) to
# identify the `attr=value | content` prefix on a cell.  Centralised so
# every caller agrees on what counts as an
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

# Internal sentinel delimiting a masked newline-significant block (`<poem>`
# / `<pre>`) while `split_wiki_row` re-merges a cell's spilled physical
# lines.  The merge joins continuation lines with a SPACE — right for prose
# that merely wrapped, but it flattens a `<poem>` legend's one-entry-per-
# line structure into a run-on (St Gall's Fig. 3 ground-plan key).  Masking
# to a newline-/pipe-free token survives the merge; restored verbatim into
# the cell content before return.  Distinct from `_PH` (\x03) / `_PIPE_ESCAPE`
# (\x04).
_NLBLOCK = "\x02"
_NLBLOCK_RE = re.compile(r"<(poem|pre)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


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
    # 0. Mask newline-significant blocks (`<poem>`/`<pre>`) so step 1's
    # space-join doesn't flatten their internal line breaks.  In the
    # production data-table path cells carry child elements as `\x03`
    # placeholders (never raw `<poem>`), so this matches nothing there —
    # byte-identical; it bites only the faithful-figure path, which feeds
    # raw source through here and owns a `<poem>` legend's per-entry lines.
    nlblocks: list[str] = []

    def _mask_nlblock(m: "re.Match[str]") -> str:
        nlblocks.append(m.group(0))
        return f"{_NLBLOCK}{len(nlblocks) - 1}{_NLBLOCK}"

    row_text = _NLBLOCK_RE.sub(_mask_nlblock, row_text)

    def _restore_nlblocks(s: str) -> str:
        for i, blk in enumerate(nlblocks):
            s = s.replace(f"{_NLBLOCK}{i}{_NLBLOCK}", blk)
        return s

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
            _restore_nlblocks(content).replace(_PIPE_ESCAPE, "|").strip(),
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

    The `{|` path uses the shared `split_wiki_rows_raw` row split (the same
    line-anchored, indent-tolerant separator the producers use) + the shared
    `split_wiki_row` cells, so this recognition primitive sees exactly the
    rows/cells the producers will; the `<table>` path delegates to
    `_html_table_grid`."""
    from britannica.pipeline.stages.elements._table_decompose import (
        split_wiki_rows_raw,
    )
    if _HTML_TABLE_TAG_RE.search(inner):
        # Content-only view: drop sep+attr at point of use, exactly as the
        # wiki branch below drops them from `split_wiki_row`.
        return [[content for _sep, _attr, content in row]
                for row in _html_table_grid(inner)]
    grid: list[list[str]] = []
    for _attr, row in split_wiki_rows_raw(inner):
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


# A chemical-reaction table that typesets operators / brackets with
# `<big>+</big>` / `<math>\Big[` instead of the Langle/Rangle SVG bracket
# images.  Signal: a `<big>` arithmetic operator AND a
# chemical `<sub>` formula in the same table — e.g. ACCUMULATOR's discharge
# (`«I»x«/I». PbO<sub>2</sub> … <big>+</big>`) and energy
# (`PbO<sub>2</sub><big>+</big>2H<sub>2</sub>SO<sub>4</sub> ＝ …`) reactions, and
# the acetone synthesis (vol 15).  Tight by audit: 5 such tables corpus-wide,
# zero math-layout / taxonomy false-positives.
_CHEM_BIG_OP_RE = re.compile(r"<big>\s*[-+−±=＋＝]")
_CHEM_FORMULA_RE = re.compile(r"[A-Z][a-z]?<sub>\s*\d")


def _has_chem_equation_content(raw: str) -> bool:
    """True for a chemical-reaction table that uses `<big>` operators + `<sub>`
    formulae rather than Langle/Rangle bracket images (`_CHEM_BRACKET_IMG_RE`)."""
    return bool(_CHEM_BIG_OP_RE.search(raw) and _CHEM_FORMULA_RE.search(raw))


# Element-aware chemical-reaction recognizer.  Recognizes a reaction by its
# CONTENT -- real molecular formulae joined by a reaction operator -- rather
# than by surface markup, so reactions typeset with plain =/+/-> operators and
# {{sub}}/<sub>/unicode formulae are caught even when they carry none of the
# Langle/Rangle SVG brackets (`_CHEM_BRACKET_IMG_RE`) or <big>-operator + <sub>
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
    or the shared ``extract_wiki_rows`` decomposition.  Going through the
    general data-table path introduced too many side effects for spatial
    diagrams:

      * the general path's ``^\\|\\+`` caption regex misfires on
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
def styled_marker(tag: str, css: str, body: str) -> str:
    """The ONE styled-wrapper marker, shared by the block `<div>` producer and
    the inline `<span>` producer: wrap `body` in `«{TAG}[style:CSS]»…«/{TAG}»`.
    `tag` is `DIV` (block) or `SPAN` (inline) — the viewer decodes both
    identically, rendering `<div>` vs `<span>`.  A `<div>`/`<span>` differ ONLY
    in display level; there is no producer reason to treat them differently, so
    they don't.  Empty `css` (nothing of the wrapper's own survived) returns
    `body` bare; empty `body` returns ""."""
    if not body:
        return ""
    if not css:
        return body
    return f"«{tag}[style:{css}]»{body}«/{tag}»"


def style_block(content: str, *, css: str = "", tag: str = "DIV",
                ctr: bool = False, sc: bool = False) -> str:
    """The ONE style-marker emitter: an (already-recursed) `content` + a style
    spec → the marker the viewer decodes.  Consolidates `_ts_block`,
    `_style_marker`, the `<div>`/`<p>` block carry, and the `<span style>` carry
    into one place (the style layer's emit side).

    * `sc` → wrap in «SC» (small-caps), innermost.
    * pure centre — the `ctr` flag OR `css == "text-align:center"` — → «CTR», the
      canonical centred block (keeps the viewer's `.centered`).  BLOCK ONLY: a
      centred *span* keeps `«SPAN[style:text-align:center]»` (centring is a block
      concept; the `«CTR»` shortcut is for `tag == "DIV"`).
    * other `css` → «{tag}[style:CSS]» via `styled_marker`.
    * nothing → `content` bare (the wrapper carried nothing of its own).

    Byte-identical to the emitters it replaces (proven in
    `test_style_block_byte_identity`)."""
    if not content:
        return ""
    if sc:
        content = f"«SC»{content}«/SC»"
    if (ctr or css == "text-align:center") and tag == "DIV":
        return f"«CTR»{content}«/CTR»"
    return styled_marker(tag, css, content)


# Template-form BLOCK style wrappers: `{{name|content}}` carrying block layout
# (centring / alignment / float).  ONE source-grounded set (audited from the raw
# corpus, NOT decompose's partial 5-entry list) recognized by the walker as
# SHAPE_STYLED and produced by `_process_styled` — so `{{center|X}}` is handled
# identically whether X is text, an image, or a table (style ⊥ content).  INLINE
# typography (`{{sc}}`/`{{smaller}}`/…) stays in body-text FOR NOW: it cannot move
# here until render_markers' figure captions recurse through the walker — today
# those captions go through body-text's `_apply_markup`, so a handler removed from
# body-text vanishes from every figure caption.  A `"tag": "SPAN"` spec on
# `_process_styled` is ready for when they do move.  (`sc` also overlaps the figure
# recognizer — `{{sc|[[Image]]}}` is a figure — so it needs the collapse first.)
_TEMPLATE_STYLE_WRAPPERS: dict[str, dict] = {
    "center":       {"ctr": True},
    "c":            {"ctr": True},
    "block center": {"ctr": True},
    "center block": {"ctr": True},
    "csc":          {"ctr": True, "sc": True},
    "left":         {"css": "text-align:left"},
    "right":        {"css": "text-align:right"},
    "float right":  {"css": "float:right"},
    # Fine-print family — a BLOCK of reduced-size type (EB1911's register for
    # notes / derivations / citations; the scans render it smaller).  We CARRY
    # the size (`«DIV[style:font-size:83%]»`) — the value the TS `smaller`/`sm`/
    # `Fine` codes already resolve to — instead of body-text's old
    # `_unwrap_layout_templates`, which dropped the styling to bare content.
    "fine block":        {"css": "font-size:83%"},
    "eb1911 fine print": {"css": "font-size:83%"},
    "smaller block":     {"css": "font-size:83%"},
    # ── Inline stylers — folded in now that the style producer is SOLE owner.
    # `_apply_markup` is gone, so there's no flat handler left to collide with (that
    # collision is what blocked `sc` before).  ANY styler routes here, period.
    # Small-caps family → «SC» (the viewer's one special-cased style marker).
    "sc":           {"sc": True},
    "asc":          {"sc": True},
    "smallcaps":    {"sc": True},
    "small caps":   {"sc": True},
    "small-caps":   {"sc": True},
    # Script wrappers → content bare (the script IS the glyphs; no style of ours to
    # carry).  Recognized so they route here and never leak — the producer's
    # "unmatched" case: a styler we own but render as plain content.
    "greek":        {},
    "polytonic":    {},
    "hebrew":       {},
    "uc":           {},
    # Inline no-wrap / font / decoration stylers → CSS the viewer decodes.
    "nowrap":       {"css": "white-space:nowrap", "tag": "SPAN"},
    "sans-serif":   {"css": "font-family:sans-serif", "tag": "SPAN"},
    "serif":        {"css": "font-family:serif", "tag": "SPAN"},
    "overline":     {"css": "text-decoration:overline", "tag": "SPAN"},
    # Graduated font-size family (relative scale) + nowrap variant → CSS.
    "larger":       {"css": "font-size:120%", "tag": "SPAN"},
    "x-larger":     {"css": "font-size:144%", "tag": "SPAN"},
    "xx-larger":    {"css": "font-size:173%", "tag": "SPAN"},
    "smaller":      {"css": "font-size:83%", "tag": "SPAN"},
    "x-smaller":    {"css": "font-size:69%", "tag": "SPAN"},
    "xx-smaller":   {"css": "font-size:58%", "tag": "SPAN"},
    "nw":           {"css": "white-space:nowrap", "tag": "SPAN"},
}
# Longest names first so `block center` wins over `center`/`c`.
_TEMPLATE_STYLE_RE = re.compile(
    r"\{\{\s*(" + "|".join(re.escape(n) for n in sorted(
        _TEMPLATE_STYLE_WRAPPERS, key=len, reverse=True)) + r")\s*\|",
    re.IGNORECASE)
# Param-bearing style wrappers — `{{Fs|108%|X}}` / `{{font size|N%|X}}` — same
# styler family, but the CSS value is the FIRST arg, not a fixed string.  Folded
# in so an element nested inside one (a contributor footer, math) recurses
# instead of being pulled out mid-template and splitting it (the `{{Fs|…{{EB1911
# footer initials}}}}` holdover).  `_process_styled` reads arg-1 as the size.
_TEMPLATE_PARAM_STYLE_RE = re.compile(
    r"\{\{\s*(fs|font\s+size|font-size)\s*\|", re.IGNORECASE)
# Shoulder heading — `{{EB1911 Shoulder Heading|[width=N|]LABEL}}` (+ the
# `…HeadingSmall` and `{{EB9 Margin Note}}` synonyms): a marginal SECTION label
# (`detect_sections` keys on the «SH» marker it produces).  Recognized at the
# walker so its inner `{{Fs}}` recurses as the styler it is instead of being
# pulled out and splitting the heading; producer emits «SH»…«/SH».  Replaces the
# flat `_convert_shoulder_headings` (a never-read-flat reader that broke once
# `{{Fs}}` became an element).
# `heading\w*` matches every suffix the old prefix-match caught — bare `Heading`,
# `HeadingSmall`, `HeadingFine`, … — so none fall through to the catch-all sweeper.
_SHOULDER_HEADING_RE = re.compile(
    r"\{\{\s*(?:EB1911\s+shoulder\s+heading\w*|EB9\s+margin\s+note)\s*\|",
    re.IGNORECASE)


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
    # Split on the `|` arg separator ONLY.  Shorthand codes may be space-
    # separated WITHIN an arg (`ma sm92`), but an inline-CSS pass-through
    # carries a value that can contain spaces (`text-indent: -2em`) — splitting
    # on whitespace too would shear the value off (`text-indent:` + `-2em`).
    for arg in codes_str.strip().split("|"):
        arg = arg.strip()
        if not arg:
            continue
        # Inline CSS passed through as-is (`width:50px`, `text-indent: -2em;`).
        # Keep WHOLE; drop a trailing `;`.
        if ":" in arg:
            rules.append(arg.rstrip(";").strip())
            continue
        for code in arg.split():
            c = code.lower()
            # Alias → canonical (wikisource resolves these first).
            c = TS_ALIASES.get(c, c)
            # Direct Module lookup (covers ~262 canonical codes).
            style = TS_STYLES.get(c)
            if style is None:
                # Missing-period decoding: `pl15` is wikitext shorthand for
                # `pl1.5` (the period was dropped editing).  Restricted to
                # known prefixes so we don't synthesise spurious codes.
                if m := re.match(r"^(p[lrtb]|plr|m[lrtb])(\d)(\d+)$", c):
                    guess = f"{m.group(1)}{m.group(2)}.{m.group(3)}"
                    style = TS_STYLES.get(guess)
            if style:
                # Split semicolon-joined Module entries (`'ma'` ↔
                # `'margin-right:auto; margin-left:auto'`) into individual rules.
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
    was previously discarded — the row decomposition strips the opener
    line wholesale.  We extract via the same ``_cell_styles`` shape as
    cells, so e.g. a ``{{Ts|ma|sm92}}`` opener emits
    ``['margin:0 auto', 'font-size:92%']`` for the ``<table>`` element's
    ``style="…"`` attr.

    Returns ``[]`` if the text isn't a wikitable opener or carries no
    extractable styling."""
    m = re.match(r"^\{\|([^\n]*)", text)
    if not m:
        return []
    return _cell_styles(m.group(1).strip(), "", table_level=True)


def _cell_styles(attr_part: str, content: str,
                 table_level: bool = False) -> list[str]:
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
    # HTML align="right" → text-align:right for a CELL; for a TABLE OPENER
    # (`table_level`), MediaWiki treats the opener's align as the whole table's
    # FLOAT (left/right) or centring (center) — render_markers was right to emit
    # `float:left` where the generic cell path emitted `text-align:left`.
    m = _CELL_ALIGN_ATTR_RE.search(blob)
    if m:
        a = m.group(1).lower()
        a = "center" if a.startswith("cent") else a
        if a in ("right", "center", "left"):
            if table_level:
                rules.append("margin-right:auto;margin-left:auto" if a == "center"
                             else f"float:{a}")
            else:
                rules.append(f"text-align:{a}")
    # A TABLE opener's width="N" is the table's own width (the cell path drops it).
    if table_level:
        wm = re.search(r"(?<![-\w])width\s*=\s*\"?\s*(\d+)", blob, re.IGNORECASE)
        if wm:
            rules.append(f"width:{wm.group(1)}px")
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


def _process_inline_glyph_wrapper(
        inner: str, text_transform,
        inner_registry: ElementRegistry | None = None) -> str:
    """Render an inline-glyph wrapper as the inline prose it actually is.

    EB1911 transcribers wrapped runs of `<hiero>` glyphs (and the odd
    glyph-IMAGE — e.g. EGYPT's Neith sign, which has no WikiHiero code, so an
    image stands in for it) in a `{|{{Ts|ma}}…|}` table purely to centre/flow
    them inside a sentence.  That is not a table: rendering it as one shatters
    the sentence and sprays the cell pipes into the prose.  Selected by
    `_is_inline_glyph_wrapper` (0 `|-` rows + a `<hiero>`); genuine multi-row
    hieroglyph reference grids never reach here (they keep ≥1 `|-` row).

    The cells are joined back into one run — cell separators and `{{Ts|…}}`
    styling dropped (layout, never content) — then run through body-text, so
    `{{nowrap}}` / `«I»` / `&nbsp;` resolve.  Each `<hiero>` child renders
    inline (`[hieroglyph: …]`); a glyph IMAGE child is re-emitted in INLINE
    form (the block `{{IMG:…}}` produce_tree would otherwise substitute renders
    as a figure and breaks the flow).  No table marker → no pipe leak.
    """
    parts = [content for _sep, _attr, content in split_wiki_row(inner)]
    prose = text_transform("".join(parts)).strip()
    if inner_registry is not None:
        for ph, label in list(inner_registry.labels.items()):
            if label in IMAGE_LABELS and ph in prose:
                eraw = inner_registry.elements[ph][1]
                prose = prose.replace(
                    ph, _process_image_from_raw(eraw, text_transform,
                                                inline=True))
    return prose


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
    from britannica.pipeline.stages.elements._table_decompose import (
        split_wiki_rows_raw,
    )
    if _single_cell_verse_cell(inner) is not None:
        return True
    saw_punct_col1 = False
    saw_verse_row = False
    for _attr, rv in split_wiki_rows_raw(inner):
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


def _process_table_unified(
    raw: str,
    inner: str,
    text_transform,
    inner_registry: "ElementRegistry | None" = None,
    context=None,
) -> str:
    """The ONE table producer: `table → row → cell → body-text`, emitting
    full-style ``«HTMLTABLE»`` for every grid label (DATA_TABLE / COMPLEX_HTML /
    HTML_TABLE — and, deliberately, the layout-`{|` shapes SINGLE_COLUMN /
    VERSE / COMPOUND, which are rendered as the tables they literally are rather
    than re-interpreted as PRE/verse/zip).

    Decompose via the shared `produce_table_rows` (auto-detects `{|` vs
    `<table>`); assemble via the shared `assemble_html_rows` (carries the FULL
    per-cell `_cell_styles`, so every `{{Ts|…}}` code rides through — the EUROPE
    cell styling that the align-only `«TABLE»` marker used to drop).  The bare
    `<table>` it emits is then stamped with the source `class=` (or `data-table`)
    and the opener's whole-table styles, and any `|+` / `<caption>` is recursed
    back in.  Replaces the six bespoke `_process_*table` producers; cell images /
    refs / nested tables ride through as placeholdered children (`produce_tree`
    substitutes their markers), so this path never calls the image producers."""
    from britannica.pipeline.stages.elements._table_decompose import (
        assemble_html_rows, produce_table_rows,
    )
    # Flavor is decided by the OPENER, not by the presence of HTML tags in the
    # body: a `{|` table that mixes in `<td>`/`<tr>` cells (CEMENT) is still a
    # wikitable — the wiki decomposer canonicalises those HTML spellings.  Only
    # a table that OPENS with `<table>` is the HTML path.  (Was
    # `_HTML_FLAVOR_RE.search(inner)`, which mis-routed mixed `{|` tables to the
    # single-syntax HTML extractor — it saw only the `<td>` cells and dropped
    # the wiki `|` rows, spilling their `{{Ts}}` to body-text.)
    flavor = "html" if raw.lstrip().startswith("<table") else "wiki"
    if flavor == "html":
        inner = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    # THE collapse: each cell's body recurses through `process_elements` (not a
    # second body-text pass), so a styled wrapper / fraction / nested table /
    # math in a cell is handled as the element it is.  `_allow_figure=False`: a
    # bare `[[File:]]` in a cell is an inline image leaf, not a re-recognized
    # figure.  Only when `context` is threaded (so far the article TABLE path);
    # legacy callers without it keep the body-text cell path.
    cell_recurse = None
    if context is not None:
        from britannica.pipeline.stages.elements import process_elements
        cell_recurse = (lambda c: process_elements(
            c, text_transform, context, _allow_figure=False))
    # No `_html_cell_clean` preclean: an HTML cell recurses RAW through
    # `process_elements` exactly like a wiki cell — its `<sub>`/`<br>`/styler/
    # nested-table content is handled as the element it is, not flattened in
    # place.  The flattener was the source of the cell-context style leaks.
    caption_raw, rows, _has_header, _has_span = produce_table_rows(
        inner, text_transform, flavor=flavor,
        cell_preclean=None,
        cell_recurse=cell_recurse)
    if not rows:
        return ""
    body = assemble_html_rows(rows, inner_registry)  # «HTMLTABLE:<table>…»
    # Stamp class + whole-table styling onto the (bare) outer <table>.
    # Class tracks BORDERS from the source (the scans show wikitable/ruled
    # tables bordered, class-less ones borderless — AUSTRIA 5/5): a real grid
    # (`class=wikitable` / `border=N` / `rules=`) → bordered `data-table`;
    # everything else (layout `{|`, verse / single-column quotes) → borderless
    # `figtable`.  We carry the source's verdict, not a default — same rule
    # faithful's own table branch uses.
    if flavor == "html":
        om = re.match(r"\s*<table\b([^>]*)>", raw, re.I)
    else:
        om = re.match(r"\s*\{\|([^\n]*)", raw)
    opener_attrs = om.group(1) if om else ""
    src_cls_m = re.search(r'class\s*=\s*"?([^"\s>|{}]+)', opener_attrs)
    src_cls = src_cls_m.group(1) if src_cls_m else ""
    bordered = re.search(
        r"wikitable|border\s*=\s*[\"']?[1-9]|rules\s*=", opener_attrs, re.I)
    cls = (("data-table " + src_cls).strip() if (bordered or src_cls)
           else "figtable")
    styles = _table_opener_styles(raw)
    attrs = f' class="{cls}"' + (f' style="{";".join(styles)}"' if styles else "")
    caption_html = ""
    if caption_raw:
        ct = strip_cell_attrs(text_transform(caption_raw))
        if ct:
            caption_html = f"<caption>{ct}</caption>"
    return body.replace(
        "«HTMLTABLE:<table>",
        f"«HTMLTABLE:<table{attrs}>{caption_html}", 1)
