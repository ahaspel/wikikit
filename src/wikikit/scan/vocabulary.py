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
    #: page FURNITURE — recorded in the reading where it is printed (a sheet
    #: signature), set by assembly in the page's footer, never page text
    furniture: bool = False
    #: ARGUMENTS — for a template of several arguments, or one whose argument
    #: the engine does not print as written: a sample of each, in order, as a
    #: reading would write it (`open` + the samples joined by | + `close`).
    #: The desk is measured argument by argument: which samples the engine's
    #: html shows, in what order, and the html between them; an argument it
    #: never shows (a ditto's word) is not shown.  Each shown sample must be
    #: unique and come through the engine as written — so a sample looks like
    #: the real thing (a rule's "13.25em"; initials ending in their full stop,
    #: as printed, because the engine adds one where it is missing).
    args: tuple[str, ...] = ()


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
              "between stanzas"),
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
def constructs() -> tuple[Construct, ...]:
    """The vocabulary for the current book's pages: the shared constructs,
    then the book's own (`Corpus.scan_constructs` — EB1911's margin heading).
    Every reader of the vocabulary asks this: the prompt, the desk's measured
    table, the joins across a cut — one set, never two."""
    from wikikit.corpora import current_corpus
    return VOCABULARY + tuple(current_corpus().scan_constructs)


def _allowed_html() -> list[str]:
    """The HTML among the constructs: the only HTML the prompt allows."""
    return [c.open for c in constructs() if c.open.startswith("<")]

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
#: A RULED table, in Wikisource's own classes (en.wikisource's TemplateStyles,
#: styled by the site): a frame, a rule between every column and under the
#: heading cells — the print's usual ruled table (TIDE's schedules); or every
#: cell ruled.  Rules on single rows and cells (`TABLE_CODES`) add to these.
COLUMN_RULED = 'class="_tablecolhdborder"'
GRID_RULED = 'class="_tablegridhdborder"'
#: A table opens and closes at a line's start — the `|}}` of
#: `{{block center|<math>…</math>}}` closes none
TABLE_OPENS = re.compile(r"(?m)^[ \t]*\{\|")
TABLE_CLOSES = re.compile(r"(?m)^[ \t]*\|\}")
#: What a printed table shows besides its cells, in the forms the engine's
#: producers already render (`{{Ts|…}}` → CSS, `!` → <th>, `{{brace2|N|l/r}}`
#: → a brace stretched over N rows) — and no others.  A bare grid lost the
#: print's rules, its header rows, its columns of figures set right, and the
#: braces that gather rows (TIDE's schedules, 2026-10-13): the table read as
#: text run together.
#: - a rule the print draws: on a row (`|-{{Ts|bt}}`) or a cell, above (bt),
#:   below (bb), left (bl), right (br)
#: - a column set as printed: right (ar), centred (ac), left (al)
TABLE_CODES = ("bt", "bb", "bl", "br", "ar", "ac", "al")
#: braces the desk is measured for: over 2 to 12 rows, opening left or right
BRACE_SIZES = range(2, 13)
#: The table as the transcriber writes it.  How a table is SET (centred:
#: TABLE_STYLE) is assembly's decision, not the reading's.
TABLE = (f"{{| {COLUMN_RULED}\n!heading!!heading!!heading\n|-{{{{Ts|bt}}}}\n|rowspan=2|cell||"
         "rowspan=2|{{brace2|2|l}}||{{Ts|ar}}|12·5\n|-\n|{{Ts|ar}}|3·75\n|-{{Ts|bt|bb}}\n|colspan=3|cell\n|}")
FORMULA = f"{{| {FORMULA_CLASS}\n|H<sub>2</sub>C||·||CH||—||CH<sub>2</sub>\n|-\n|│||||│||||║\n|}}"


def prompt_text() -> str:
    """The vocabulary as the transcriber's prompt states it."""
    lines = [f"- {c.says}: {c.open}text{c.close}" for c in constructs()]
    lines.append("- a new paragraph: a blank line before it")
    lines.append("- a table, on lines of its own where it is printed; |- begins each new row, || "
                 "divides the cells, and a cell spanning rows or columns begins rowspan=2| or "
                 "colspan=2|. Set the table as the print sets it: a table the print frames and "
                 f"rules between its columns opens {{| {COLUMN_RULED}; one ruled round every cell "
                 f"opens {{| {GRID_RULED}; an unruled table opens {{| alone. A row of headings "
                 "begins with ! and its cells are divided by !!; any other rule the print draws "
                 "goes on its row, "
                 "|-{{Ts|bt}} (a rule above the row; bb below), or on a cell (bt, bb, bl, br: above, "
                 "below, left, right); a column of figures set right, or centred, gives each of its "
                 "cells {{Ts|ar}} or {{Ts|ac}} (several codes in one: {{Ts|ar|bb}}), after the "
                 "cell's spans and before its content: rowspan=2 {{Ts|ac}}|cell; a brace the print "
                 "sets across rows is a cell of its own spanning them, holding {{brace2|N|l}} "
                 "(opening left, over N rows) or {{brace2|N|r}}; nothing else on a table, row or "
                 "cell:\n" + TABLE)
    lines.append("- a formula set out in rows of type (atoms above and below one another, joined by "
                 "upright bonds) is a table of its own form, a compact grid: each atom group in its "
                 "own cell, and each bond printed between groups in a row (—, ·, :) in a cell of its "
                 "own too, so that an upright bond (│, a double one ║, each in a cell of its own) "
                 "stands under the group it joins:\n" + FORMULA)
    lines.append(f"- nothing else: no other templates, no HTML but {', '.join(_allowed_html())}, no lists")
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


def _measure_args(c: Construct) -> dict:
    """A template of arguments as the engine renders it, from its samples
    (`Construct.args`): {"parts": the html around and between the arguments it
    shows, "order": which argument fills each gap, "after": what a newline
    right after it becomes} — html = parts[0] + arg[order[0]] + parts[1] + …"""
    written = f"{c.open}{'|'.join(c.args)}{c.close}"
    body = _render(f"A {written} Z").removeprefix("A").removesuffix("Z").strip()
    shown = []
    for i, sample in enumerate(c.args):
        if not sample or sample not in body:
            continue                                    # the engine never prints it
        if body.count(sample) > 1:
            raise ValueError(f"{c.name}: sample {sample!r} shows twice: {body!r}")
        shown.append((body.index(sample), i))
    parts, order, at = [], [], 0
    for pos, i in sorted(shown):
        parts.append(body[at:pos])
        order.append(i)
        at = pos + len(c.args[i])
    parts.append(body[at:])
    after = _render(f"A {written}\nZ").removeprefix("A").removesuffix("Z").strip()
    after = after[len(body):] if after.startswith(body) else ""
    return {"parts": parts, "order": order, "after": after if after.strip() else " "}


def render_table() -> dict:
    """What the engine renders for each construct, measured, as
    {name: [html_open, html_close, after_newline]} — or, for a template of
    arguments, {"parts", "order", "after"} (`_measure_args`) — plus "paragraph": html
    between two paragraphs.  `after_newline` is what a single newline right
    after the construct becomes: a block that ends its paragraph makes it a new
    one ("<p>"), anything else a space.  The desk applies this table and
    nothing else."""
    table = {}
    for c in constructs():
        if c.furniture:
            # page furniture never reaches the engine (assembly sets it in the
            # page's footer): the desk shows it set apart, as furniture
            table[c.name] = ['<span class="furniture">', "</span>", " "]
            continue
        if c.args:
            table[c.name] = _measure_args(c)
            continue
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
        # an attribute made FROM the content (a margin heading's anchor,
        # id="section-<its words>") is a link target, not the construct's look:
        # the desk shows the look, and leaves the anchor to the engine
        head = re.sub(rf'\s+id="[^"]*{_PROBE.lower()}[^"]*"', "", head)
        after = _render(f"A {c.open}{_PROBE}{c.close}\nZ").partition(_PROBE)[2]
        after = after.removeprefix(close).removesuffix("Z")
        table[c.name] = [head.removeprefix("A").strip(), close, after if after.strip() else " "]
    para = _render(f"A{PARAGRAPH}Z")
    table["paragraph"] = para.removeprefix("A").removesuffix("Z")
    # the tables: each allowed opening as the engine renders it, and the close
    # — measured; rows and cells are the plain <tr>/<td> the parity gate checks.
    # {"open": {opening as written: html}, "close": html}
    openings = {}
    for attrs in ("", TABLE_STYLE, FORMULA_CLASS, COLUMN_RULED, GRID_RULED):
        html = _render(f"A{PARAGRAPH}{{| {attrs}\n|{_PROBE}||rowspan=2|{_PROBE}\n|-\n|{_PROBE}\n|}}{PARAGRAPH}Z")
        body = html.removeprefix("A" + table["paragraph"]).removesuffix(table["paragraph"] + "Z")
        opening, sep, _ = body.partition("<tr>")
        if not sep or not body.endswith("</table>") or 'rowspan="2"' not in body:
            raise ValueError(f"the engine no longer renders the vocabulary's table as a table: {html!r}")
        openings[attrs] = opening
    table["table"] = {"open": openings, "close": "</table>", **_table_furniture()}
    return table


def _table_furniture() -> dict:
    """What a table's rules, headings, alignment and braces render as, each
    measured through the engine's chain: {"ts": {code: css}, "th": [open,
    close], "brace": {"Nl"|"Nr": html}}.  The desk joins a cell's codes'
    css with ";" — and the parity gate holds that to the engine's own."""
    def cell(row_attrs: str, cell_text: str) -> str:
        return _render(f"{{|\n|-{row_attrs}\n{cell_text}\n|}}")

    ts = {}
    for code in TABLE_CODES:
        m = re.search(rf'<td style="([^"]*)">{_PROBE}</td>', cell("", f"|{{{{Ts|{code}}}}}|{_PROBE}"))
        r = re.search(rf'<tr style="([^"]*)"><td>{_PROBE}</td>', cell(f"{{{{Ts|{code}}}}}", f"|{_PROBE}"))
        if not m or not r or m.group(1) != r.group(1):
            raise ValueError(f"the engine no longer renders {{{{Ts|{code}}}}} as one style on a cell and a row")
        ts[code] = m.group(1)
    th = re.search(rf"(<th[^>]*>){_PROBE}(</th>)", cell("", f"!{_PROBE}"))
    if not th:
        raise ValueError("the engine no longer renders a heading cell (!) as <th>")
    brace = {}
    for n in BRACE_SIZES:
        for d in "lr":
            m = re.search(r'<td rowspan="\d+">(.*?)</td>', cell("", f"|rowspan={n}|{{{{brace2|{n}|{d}}}}}"))
            if not m or not m.group(1):
                raise ValueError(f"the engine no longer renders {{{{brace2|{n}|{d}}}}}")
            brace[f"{n}{d}"] = m.group(1)
    return {"ts": ts, "th": [th.group(1), th.group(2)], "brace": brace}
