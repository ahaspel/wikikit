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
from britannica.pipeline.stages.elements._registry import (
    ElementRegistry, IMAGE_LABELS, _PH)
from britannica.pipeline.stages.elements._text import (
    _strip_br,
)
from britannica.pipeline.stages.elements._table_fold import (
    fold_cell_attrs, fold_cell_styles, format_html_attrs)


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

# Internal sentinel standing in for a cell-body NEWLINE while the row text is
# split into cell-lines on `\n` (step 4).  A wiki cell can spill its body onto
# subsequent physical lines (`|`⏎`content`⏎`more`); the old merge joined those
# with a SPACE, flattening a multi-line cell body into a run-on so the content
# recursion never saw the line structure the source carried (per-entry legend
# lists, stacked formula rows, paragraph breaks).  Joining with this sentinel
# preserves the break across the `\n`-split, then restores it to `\n` in the
# returned content so `process_elements` recurses the body intact — the same
# `\n` an article-prose paragraph would carry.  Distinct from `_NLBLOCK`
# (\x02) / `_PH` (\x03) / `_PIPE_ESCAPE` (\x04).
_CELL_NL = "\x05"


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
    # byte-identical; it bites only the unified producer's figure path, which feeds
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
            # A blank line inside a cell body is a PARAGRAPH break — carry it
            # into the body (as the newline sentinel) instead of dropping it,
            # so the content recursion sees the break.  Only when we're
            # already accumulating a cell-line (else it's inter-row blank
            # space, which carries nothing).
            if merged and not merged[-1].rstrip().endswith(_CELL_NL):
                merged[-1] = merged[-1].rstrip() + _CELL_NL + _CELL_NL
            continue
        if stripped.startswith("|+"):
            continue
        if stripped.startswith(("|", "!")) or stripped == "{|":
            merged.append(ln)
        elif merged:
            # Continuation line — preserve the line break (sentinel → `\n`
            # later) rather than flattening to a space, so a multi-line cell
            # body reaches the content recursion with its structure intact.
            merged[-1] = merged[-1].rstrip() + _CELL_NL + stripped
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
                r"\{\{[Tt]s(?:\|[^{}]*)?\}\}\s*", "",
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
            # Attrs are single-line; a stray cell-newline sentinel there
            # (malformed source) collapses to a space, never a CSS-shearing \n.
            attr_part.replace(_PIPE_ESCAPE, "|").replace(_CELL_NL, " ").strip(),
            # Restore the cell-body newline sentinel to a real `\n` so the
            # content recursion sees the line structure the source carried.
            _restore_nlblocks(content)
            .replace(_PIPE_ESCAPE, "|")
            .replace(_CELL_NL, "\n")
            .strip(),
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
# returned raw/untransformed — no flat transform, nothing flows
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


_OPENER_ATTR_RE = re.compile(
    r"\s*(?:<table\b([^>]*)>|\{\|([^\n]*))", re.IGNORECASE)


def _opener_attr_slot(text: str) -> str:
    """A table's opener attribute slot — ``<table <attrs>>`` or ``{|<attrs>``,
    both syntaxes one regex — for the emit to fold (`fold_cell_attrs`)."""
    m = _OPENER_ATTR_RE.match(text)
    return (m.group(1) or m.group(2) or "").strip() if m else ""


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
                r"\{\{[Tt]s(?:\|[^{}]*)?\}\}\s*", "",
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


def _process_chemistry_layout(raw: str, inner: str,
                              inner_registry=None) -> str:
    """Render a 2-D chemical-reaction / structural-formula layout.

    These are NOT data tables \u2014 they're spatial diagrams (no gridlines,
    no cell padding; the wiki-table syntax is just a positioning
    crutch).  They get their own marker, ``\u00abCHEM:\u2026\u00ab/CHEM\u00bb``, distinct
    from ``\u00abHTMLTABLE:\u2026\u00bb``, so the viewer can lay them out without
    table chrome.

    Self-contained parser \u2014 does NOT delegate to ``_process_complex_table``
    or the shared ``produce_table_rows`` fold.  Going through the
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

    Atom-bracket and ``<br>`` substitution is two-phase (it once had to
    survive body-text's HTML-tag strip): each placeholder/markup is
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
    # Preserve in-cell <br> as a sentinel (body-text's HTML-tag strip
    # once ate the line breaks that stack atom-bond-atom).
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

    from britannica.pipeline.stages.elements._table_decompose import _tag
    html_rows: list[str] = []
    for raw_row in raw_rows:
        if not raw_row.strip():
            continue
        cells_html: list[str] = []
        for sep, attr_part, content in _row_cells(raw_row):
            tag = "th" if sep == "!" else "td"
            # Chem-specific content clean (Ts strip, glyph sentinels); the cell
            # attr-slot rides raw to `_tag`, which folds it like any cell.
            content = re.sub(r"\{\{[Tt]s\|[^{}]*\}\}\s*", "", content)
            content = content.strip()
            content = (content.replace("&vert;", "|")
                              .replace("&nbsp;", " "))
            for sentinel, glyph in _CHEM_RESOLVE.items():
                if sentinel in content:
                    content = content.replace(sentinel, glyph)
            cells_html.append(_tag(tag, attr_part, content))
        if cells_html:
            html_rows.append("<tr>" + "".join(cells_html) + "</tr>")
    if not html_rows:
        return ""
    # Whole-table styling from the opener, folded at the emit like any tag.
    css, opener_html = fold_cell_attrs(_opener_attr_slot(raw), table_level=True)
    table_attrs = (format_html_attrs(opener_html)
                   + (f' style="{";".join(css)}"' if css else ""))
    return ("\u00abCHEM:<table" + table_attrs + ">"
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
# typography (`{{sc}}`/`{{smaller}}`/…) is folded in below too: with body-text gone,
# `_process_styled` is the SOLE styler owner, so there's no flat handler left for it
# to collide with.  (`{{sc|[[Image]]}}` overlaps the figure recognizer — that case is
# resolved by the figure collapse, which runs first.)
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
    # The flat body-text handler is gone, so there's no collision left (that
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
    # Rare styler tail — same rows, same mechanism.
    "sm":               {"css": "font-size:83%", "tag": "SPAN"},
    "underline":        {"css": "text-decoration:underline", "tag": "SPAN"},
    "double underline": {"css": "text-decoration:underline", "tag": "SPAN"},
    "float left":       {"css": "float:left"},
    "normal":           {},  # font-variant:normal override → just the content
    "brace2":           {},  # grouping brace → content (decoration not rendered)
    "11co":             {},  # column wrapper → content
    "u":                {"css": "text-decoration:underline", "tag": "SPAN"},
    # ── BROKEN-leak backlog stylers (2026-06-09).  Each was a styler the walk
    # leaked raw; same rows, same mechanism.  CSS grounded in the
    # Module:Table_style mirror (_ts_codes.py) where the name maps.
    "hi":               {"css": "padding-left:2em; text-indent:-2em"},  # hanging indent (block); hi→it
    "fine":             {"css": "font-size:92%", "tag": "SPAN"},  # fine→fs092 (inline)
    "strikethrough":    {"css": "text-decoration:line-through", "tag": "SPAN"},  # strike→tds
    "sp":               {"css": "letter-spacing:0.25em", "tag": "SPAN"},  # spaced-out lettering
    "zfloat right":     {"css": "float:right"},  # z-prefixed float variant of float right
    # Language/script wrappers → content bare (the script IS the glyphs), like greek/hebrew.
    "arabic":           {},
    "he":               {},
    "latin":            {},
    "coptic":           {},  # Coptic script run → glyphs bare
    "grc":              {},  # Ancient-Greek (ISO grc) script run → glyphs bare
    "linktext":         {},  # {{linktext|X}} — display text of a dictionary link → X bare
    # ── Batch-2 simple wrappers (forms confirmed in walked context).
    "smb":              {"sc": True},  # small-caps era markers (B.C./A.D.)
    "bc":               {"ctr": True},  # block-centre (centred display equations)
    "float center":     {"ctr": True},  # centred block
    "fs70":             {"css": "font-size:70%", "tag": "SPAN"},  # small fractions
    "0":                {"css": "visibility:hidden", "tag": "SPAN"},  # {{0|x}} reserves x's width, invisible
    "di":               {},  # drop initial → the letter (decorative drop-cap deferred to render)
    "blackletter":      {"css": "font-family:'UnifrakturCook',serif", "tag": "SPAN"},
    "bl":               {"css": "font-family:'UnifrakturCook',serif", "tag": "SPAN"},  # blackletter math variables
    # ── Generic-flip backlog stylers (2026-06-11) — each a font-weight / family /
    # variant styler the old walker leaked; same rows, same mechanism.
    "bold":               {"css": "font-weight:bold", "tag": "SPAN"},
    "nobold":             {"css": "font-weight:normal", "tag": "SPAN"},
    "mono":               {"css": "font-family:monospace", "tag": "SPAN"},
    "sans":               {"css": "font-family:sans-serif", "tag": "SPAN"},
    "bbsc":               {"sc": True},  # bold-blackletter-smallcaps display → smallcaps
    "font-variant normal": {},  # font-variant:normal override → just the content
    # Graduated-size BLOCK variants (`{{xxx-larger}}`, `{{xx-larger block}}`, …) →
    # CSS, same scale as the inline `larger`/`x-larger`/`xx-larger` family above.
    "xxx-larger":         {"css": "font-size:207%", "tag": "SPAN"},
    "xxxx-larger":        {"css": "font-size:249%", "tag": "SPAN"},
    "xx-larger block":    {"css": "font-size:173%"},
    "x-larger block":     {"css": "font-size:144%"},
    # ── Generic-flip backlog: fixed-size / font-weight stylers (2026-06-11).
    "fwn":                {"css": "font-weight:normal", "tag": "SPAN"},  # full-width normal weight
    "fs90":               {"css": "font-size:90%", "tag": "SPAN"},  # plate-caption fixed size
    "fs85":               {"css": "font-size:85%", "tag": "SPAN"},  # plate-caption fixed size (Fs85)
}
# Longest names first so `block center` wins over `center`/`c`.
_TEMPLATE_STYLE_RE = re.compile(
    r"\{\{\s*(" + "|".join(re.escape(n) for n in sorted(
        _TEMPLATE_STYLE_WRAPPERS, key=len, reverse=True)) + r")\s*\|",
    re.IGNORECASE)
# Param-bearing style wrappers — `{{name|VALUE|content}}`: the CSS value rides in
# arg-1, the content is arg-2+ (unlike the fixed-value registry above).  ONE
# registry (name → (css template with `{v}`, percent flag)); the walker's
# _STYLED_OPEN_RE auto-syncs off the regex built from these names, exactly like
# the fixed-value registry.  `pct=True` means a bare-integer arg-1 is a percentage
# (the font-size family: `{{Fs|108|X}}` → 108%).  Folding the font-size family in
# here keeps a nested element (a contributor footer, math) recursing instead of
# being pulled out mid-template and splitting it (the `{{Fs|…{{EB1911 footer
# initials}}}}` holdover).
_TEMPLATE_PARAM_STYLE_WRAPPERS: dict[str, tuple[str, bool]] = {
    "fs":             ("font-size:{v}", True),
    "fsx":            ("font-size:{v}", True),  # `{{fsx|75%|content}}` — explicit-% size
    "font size":      ("font-size:{v}", True),
    "font-size":      ("font-size:{v}", True),
    "lh":             ("font-size:{v}", True),  # plate caption line — size in arg-1, == {{fs}}
    "rotate":         ("transform:rotate({v}deg);display:inline-block", False),
    "letter-spacing": ("letter-spacing:{v}", False),
    "lsp":            ("letter-spacing:{v}", False),
    "font-stretch":   ("transform:scaleX({v});display:inline-block", False),
    "word-spacing":   ("word-spacing:{v}", False),  # currency-column alignment (was preprocess-stripped)
}
_TEMPLATE_PARAM_STYLE_RE = re.compile(
    r"\{\{\s*(" + "|".join(
        re.escape(n).replace(r"\ ", r"\s+")
        for n in sorted(_TEMPLATE_PARAM_STYLE_WRAPPERS, key=len, reverse=True))
    + r")\s*\|", re.IGNORECASE)
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
# Running header — `{{rh|left|center|right}}` and its `{{Running header|…}}`
# alias: a 3-COLUMN left|center|right frame.  Page-furniture rh is stripped
# upstream, so what survives into the body is CONTENT — plate title bars
# (`Plate II. | PLASTIC ART |`), captioned figures (` | Fig. 1.—… | credit`),
# and displayed-equation layouts in math articles (`1. | H = wL²/8y |`).
# Recognized at the walker so the inner stylers / «MATH» recurse; the producer
# renders the three cells as a flex row.
_RUNNING_HEADER_RE = re.compile(
    r"\{\{\s*(?:rh|running\s*header)\s*\|", re.IGNORECASE)  # rh / Running header / RunningHeader


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
        # A `key=val` form (`width=240px`) — a CSS-ish value the source spelled
        # with `=` not `:`.  Carry it (bare-int width/height get HTML-default px)
        # rather than dropping it as an unknown code — the "carry, don't drop"
        # rule the fold applies to plain attrs.
        if "=" in arg:
            k, _eq, val = arg.partition("=")
            k, val = k.strip().lower(), val.strip()
            if k in ("width", "height") and re.fullmatch(r"\d+", val):
                val += "px"
            rules.append(f"{k}:{val}")
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


def _process_inline_glyph_wrapper(
        inner: str,
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
    prose = "".join(parts).strip()
    if inner_registry is not None:
        for ph, label in list(inner_registry.labels.items()):
            if label in IMAGE_LABELS and ph in prose:
                eraw = inner_registry.elements[ph][1]
                prose = prose.replace(
                    ph, _process_image_from_raw(eraw,
                                                inline=True))
    return prose


def _process_table_unified(
    raw: str,
    inner: str,
    inner_registry: "ElementRegistry | None",
    context,
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
        produce_table,
    )
    # Both `{|` and `<table>` are LEAVES now, so the walker hands us `inner` as
    # the bare grid (outer delimiters already peeled) for either syntax — no
    # flavor branch, no re-derive from `raw`.  Strip HTML comments (the one
    # thing the old `<table>`-shell peel did beyond peeling); `cell_recurse`
    # recurses each cell's content downstream.
    grid = re.sub(r"<!--.*?-->", "", inner, flags=re.DOTALL)
    # THE collapse: each cell's body recurses through `process_elements` (not a
    # second body-text pass), so a styled wrapper / fraction / nested table /
    # math in a cell is handled as the element it is.  `_allow_figure=False`: a
    # bare `[[File:]]` in a cell is an inline image leaf, not a re-recognized
    # figure.  Unconditional: a cell is prose in a box, so it ALWAYS recurses;
    # `context` is a required arg (no None default) — recursion is the floor,
    # never an opt-in, and there is no no-recurse fallback.
    from britannica.pipeline.stages.elements import process_elements
    cell_recurse = (lambda c: process_elements(
        c, context, _allow_figure=False))
    # No `_html_cell_clean` preclean: an HTML cell recurses RAW through
    # `process_elements` exactly like a wiki cell — its `<sub>`/`<br>`/styler/
    # nested-table content is handled as the element it is, not flattened in
    # place.  The flattener was the source of the cell-context style leaks.
    caption_raw, body = produce_table(grid, cell_recurse)
    if not body:
        return ""
    # Stamp class + whole-table styling onto the (bare) outer <table>.
    # Class tracks BORDERS from the source (the scans show wikitable/ruled
    # tables bordered, class-less ones borderless — AUSTRIA 5/5): a real grid
    # (`class=wikitable` / `border=N` / `rules=`) → bordered `data-table`;
    # everything else (layout `{|`, verse / single-column quotes) → borderless
    # `figtable`.  We carry the source's verdict, not a default — same rule
    # `_process_table_unified`'s own table branch uses.
    # Opener attrs from whichever syntax opened the table — one regex, no flavor.
    opener_attrs = _opener_attr_slot(raw)
    src_cls_m = re.search(r'class\s*=\s*"?([^"\s>|{}]+)', opener_attrs)
    src_cls = src_cls_m.group(1) if src_cls_m else ""
    bordered = re.search(
        r"wikitable|border\s*=\s*[\"']?[1-9]|rules\s*=", opener_attrs, re.I)
    cls = (("data-table " + src_cls).strip() if (bordered or src_cls)
           else "figtable")
    # Fold the opener's attr-slot at the emit, same as a cell: style bits → CSS,
    # the rest → real attrs.  `class` rides as the computed `cls` (data-table/
    # figtable + the source class), so drop the folded dupe.
    css, opener_html = fold_cell_attrs(opener_attrs, table_level=True)
    opener_html.pop("class", None)
    attrs = (f' class="{cls}"' + format_html_attrs(opener_html)
             + (f' style="{";".join(css)}"' if css else ""))
    caption_html = ""
    if caption_raw:
        # A caption is prose in a box, same as a cell — recurse the RAW through
        # the loop so a `{{sc}}`/`{{c}}`/footnote is produced.  NOT strip_cell_attrs
        # first: its `[|{}]`→space cleanup shreds a raw `{{sc|…}}` into `sc …`
        # (it only ever saw pre-produced «SC» markers under the old non-leaf classify).
        ct = cell_recurse(caption_raw).strip()
        if ct:
            caption_html = f"<caption>{ct}</caption>"
    return body.replace(
        "«HTMLTABLE:<table>",
        f"«HTMLTABLE:<table{attrs}>{caption_html}", 1)
