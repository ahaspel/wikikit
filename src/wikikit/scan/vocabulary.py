"""The markup a transcribed page may use: standard Wikisource markup the
engine's producers already read, and nothing else.

ONE list serves three readers, so they cannot drift apart:
- the transcriber's prompt states it (`prompt_text`);
- the proofreading desk renders it from a table the engine measures by running
  each construct through its own chain — preprocess, element walk, inline
  render — not from a second renderer's guesses (`render_table`);
- the parity gate checks the desk's rendering against the engine's.

A construct the engine does not know is a transcription error to correct, never
a reason to grow a producer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Construct:
    name: str
    open: str
    close: str
    says: str            # what it is for, as the prompt states it


VOCABULARY: tuple[Construct, ...] = (
    Construct("bold", "'''", "'''", "bold"),
    Construct("italic", "''", "''", "italic"),
    Construct("sc", "{{sc|", "}}", "small capitals"),
    Construct("sup", "<sup>", "</sup>", "superscript (a homograph number: bating<sup>1</sup>)"),
    Construct("smaller", "{{smaller|", "}}", "text in the smaller type, inside a paragraph"),
    Construct("smaller block", "{{smaller block|", "}}",
              "a quotation or note set off in smaller type on its own lines"),
    Construct("right", "{{right|", "}}",
              "right-aligned on its own line, e.g. a quotation's source inside its block"),
    Construct("c", "{{c|", "}}", "centred, e.g. a figure's caption"),
    Construct("sub", "<sub>", "</sub>", "subscript, in running text that is not a formula"),
    Construct("block center", "{{block center|", "}}",
              "a display formula set apart on its own line, centred: one display, one block"),
    Construct("math", "<math>", "</math>",
              "a mathematical formula or symbol, in LaTeX, inline or inside a display"),
    Construct("poem", "<poem>", "</poem>",
              "verse, or any run of lines the book keeps as lines (a list set line by line, a "
              "recipe, entries joined by leaders): one printed line to a line, a blank line "
              "between stanzas; centred, inside {{block center|…}}"),
)

#: Constructs the engine's chain lifts out whole for the reader to typeset
#: (KaTeX in the viewer and on the desk): their content never reaches the
#: inline render, which shows a stand-in.
TYPESET = frozenset({"math"})
#: Constructs whose LINES the engine keeps: not an open/close pair around text
#: but a block of lines, measured as one — its opening, the break between two
#: lines, the break between two stanzas, its close.  The desk lays it out
#: (desk_render.js) from those measurements.
LINES = frozenset({"poem"})
#: The HTML among the constructs: the only HTML the prompt allows.
_HTML = [c.open for c in VOCABULARY if c.open.startswith("<")]

#: A blank line between paragraphs; the only structure besides the constructs
#: and the table.
PARAGRAPH = "\n\n"

#: TABLES, in the two wikitable forms allowed, borderless, their cells spanning
#: rows or columns and holding inline constructs:
#: - a table printed as one: centred, set as the site sets its tables;
#: - a FORMULA set out in rows of type (a chemical constitution: TROPINE,
#:   FULMINIC ACID) as a compact grid at its own width, the site's existing
#:   "chem-grid", so an upright bond stands under the atom it joins.
#: Not open/close pairs around text: the desk lays them out (desk_render.js)
#: from their measured openings (`render_table()["table"]`), and the parity
#: gate holds the result to the engine's.  An upright bond is a box-drawing
#: line, never "|", which a wikitable reads as its own syntax.
TABLE_STYLE = 'style="margin:auto"'
FORMULA_CLASS = 'class="chem-grid"'
TABLE = f"{{| {TABLE_STYLE}\n|cell||cell\n|-\n|rowspan=2|cell||cell\n|}}"
FORMULA = f"{{| {FORMULA_CLASS}\n|H<sub>2</sub>C||·||CH||—||CH<sub>2</sub>\n|-\n|│||||│||||║\n|}}"


def prompt_text() -> str:
    """The vocabulary as the transcriber's prompt states it."""
    lines = [f"- {c.says}: {c.open}text{c.close}" for c in VOCABULARY]
    lines.append("- a new paragraph: a blank line before it")
    lines.append("- a table, on lines of its own with a blank line before and after; |- begins each "
                 "new row, || divides the cells, and a cell spanning rows or columns begins rowspan=2| "
                 "or colspan=2|; nothing else on a table, row or cell:\n" + TABLE)
    lines.append("- a formula set out in rows of type (atoms above and below one another, joined by "
                 "upright bonds) is a table of its own form, a compact grid: each atom group in its "
                 "own cell, and each bond printed between groups in a row (—, ·, :) in a cell of its "
                 "own too, so that an upright bond (│, a double one ║, each in a cell of its own) "
                 "stands under the group it joins:\n" + FORMULA)
    lines.append(f"- nothing else: no other templates, no HTML but {', '.join(_HTML)}, no lists")
    return "\n".join(lines)


_PROBE = "QQPROBEQQ"


def _render(wikitext: str) -> str:
    """One snippet through the engine's own chain, as an article body is —
    with the body's block forms decoded (a verse is a blockquote only there)."""
    from wikikit.db.models import SourcePage
    from wikikit.pipeline.stages.elements import ElementContext, process_elements
    from wikikit.pipeline.stages.preprocess import stream_with_keys
    from wikikit.render.inline import decode_inline
    stream, _, _ = stream_with_keys([SourcePage(volume=1, page_number=1, wikitext=wikitext)], 1)
    return decode_inline(process_elements(stream, ElementContext(volume=1)), body_blocks=True)


def _measure_lines(c: Construct) -> dict:
    """A block of lines as the engine renders it, set as a paragraph of its
    own: {"open", "line", "stanza", "close"} — the html before its first line,
    between two lines, between two stanzas, after its last."""
    para = _render(f"A{PARAGRAPH}Z").removeprefix("A").removesuffix("Z")
    html = _render(f"A{PARAGRAPH}{c.open}{_PROBE}1\n{_PROBE}2\n\n{_PROBE}3{c.close}{PARAGRAPH}Z")
    body = html.removeprefix("A" + para).removesuffix(para + "Z")
    parts = re.split(f"{_PROBE}[123]", body)
    if len(parts) != 4:
        raise ValueError(f"the engine no longer keeps the lines of {c.name}: {html!r}")
    return dict(zip(("open", "line", "stanza", "close"), parts))


def render_table() -> dict:
    """What the engine renders for each construct, measured, as
    {name: [html_open, html_close, after_newline]} plus "paragraph": html
    between two paragraphs.  `after_newline` is what a single newline right
    after the construct becomes: a block that ends its paragraph makes it a new
    one ("<p>"), anything else a space.  The desk applies this table and
    nothing else."""
    table = {}
    for c in VOCABULARY:
        html = _render(f"A {c.open}{_PROBE}{c.close} Z")
        if c.name in TYPESET:
            # the reader typesets it; the engine's chain shows its stand-in
            stand_in = html.removeprefix("A").removesuffix("Z").strip()
            if _PROBE in stand_in or not stand_in:
                raise ValueError(f"the engine no longer lifts {c.name} out: {html!r}")
            table[c.name] = [stand_in]
            continue
        if c.name in LINES:
            table[c.name] = _measure_lines(c)
            continue
        head, sep, tail = html.partition(_PROBE)
        if not sep:
            raise ValueError(f"the engine lost the content of {c.name}: {html!r}")
        close = tail.removesuffix("Z").strip()
        after = _render(f"A {c.open}{_PROBE}{c.close}\nZ").partition(_PROBE)[2]
        after = after.removeprefix(close).removesuffix("Z")
        table[c.name] = [head.removeprefix("A").strip(), close, after if after.strip() else " "]
    para = _render(f"A{PARAGRAPH}Z")
    table["paragraph"] = para.removeprefix("A").removesuffix("Z")
    # the tables: each allowed opening as the engine renders it, and the close
    # — measured; rows and cells are the plain <tr>/<td> the parity gate checks.
    # {"open": {opening as written: html}, "close": html}
    openings = {}
    for attrs in ("", TABLE_STYLE, FORMULA_CLASS):
        html = _render(f"A{PARAGRAPH}{{| {attrs}\n|{_PROBE}||rowspan=2|{_PROBE}\n|-\n|{_PROBE}\n|}}{PARAGRAPH}Z")
        body = html.removeprefix("A" + table["paragraph"]).removesuffix(table["paragraph"] + "Z")
        opening, sep, _ = body.partition("<tr>")
        if not sep or not body.endswith("</table>") or 'rowspan="2"' not in body:
            raise ValueError(f"the engine no longer renders the vocabulary's table as a table: {html!r}")
        openings[attrs] = opening
    table["table"] = {"open": openings, "close": "</table>"}
    return table
