"""Cut items -> one Wikisource page.

Each text item is read by the vision model into the standard markup of
`wikikit.scan.vocabulary`; the page is then assembled from the readings in
reading order.  The prompt has three owners: the engine's mechanics (here), the
engine's vocabulary (`vocabulary.prompt_text`), and the book's own words — its
pages (`templates/scan_page.md`) and its notation (`scan_notation.json`), both
declared data, so nothing here asks which book it is reading.

Joining strips.  A strip cut mid-paragraph continues the one before it; the
model marks a strip that BEGINS a paragraph with a ¶ line, and leaves a word
broken at a strip's foot as "frag-", joined here to the next strip's first
word.  ¶ is a signal between the model and this assembly, never page text.

Figures float to the next paragraph boundary: an inset interrupts a paragraph,
sometimes mid-word ("mold-" [figure] "ings"), and set there the image would
split both.  Its caption, read like any text, goes with it, centred.
"""
from __future__ import annotations

import json
import re

from PIL import Image

from wikikit.corpora import current_corpus
from wikikit.scan.cut import Item
from wikikit.scan.model import ask
from wikikit.scan.vocabulary import VOCABULARY, prompt_text

PARA = "¶"
PARAGRAPH_SEP = "\n\n"
#: A figure as wide as a column is shown at this width, in display pixels.
COLUMN_DISPLAY_PX = 360

_RULES = """Transcribe only what is printed: never correct, modernise, complete
or add. Keep the printed spelling, punctuation and every diacritic.

Markup — use only this:
{vocabulary}

Lines: join the printed lines of a paragraph into one line. A word hyphenated
at a line end inside the image is joined without the hyphen, unless the hyphen
belongs to the word. A new paragraph starts on a new line with a blank line
before it."""

STRIP = """This image is one horizontal strip cut from one column of a scanned
page. Transcribe it into Wikisource wikitext, exactly as printed.

{book}

{rules}

Strip edges: if the strip's FIRST line begins a new paragraph (a bold
title-word at the margin, or an indented first line), start your output with
{para} on its own line. If its LAST line ends with a word broken by a hyphen,
end your output with that fragment and the hyphen, e.g. "diffi-".
{notation}
Output only the wikitext."""

CAPTION = """This image is the caption printed beneath an illustration on a
scanned page. Transcribe it into Wikisource wikitext, exactly as printed,
wrapped in {{{{c|...}}}}; keep its italics and small capitals.

{rules}
{notation}
Output only the wikitext."""

HEAD = """This image is the running head of a scanned page: a guide word at the
left, the page number in the middle, a guide word at the right (one may be
empty). Output exactly {{{{RunningHeader|left|number|right}}}} with the printed
text. Nothing else."""


def notation_text() -> str:
    """The book's notation, as the prompt states it ("" if it declares none)."""
    book = current_corpus()
    if not book.has_data("scan_notation.json"):
        return ""
    k = json.loads(book.data("scan_notation.json").read_text(encoding="utf-8"))
    lines = ["", "The book's own notation — use exactly these characters for its marks:"]
    for g in k["groups"]:
        lines.append(f"  {g['label']}" + (f" ({g['meaning']})" if g.get("meaning") else "") + ":")
        for s in g["symbols"]:
            gloss = f"as in {s['as_in']}" if "as_in" in s else s.get("meaning", "")
            lines.append(f"    {s['symbol']} — {gloss}")
    return "\n".join(lines) + "\n"


def book_text() -> str:
    book = current_corpus()
    return book.template("templates/scan_page.md").strip() if book.has_data("templates/scan_page.md") else ""


def prompt_for(item: Item, is_caption: bool = False) -> str:
    rules = _RULES.format(vocabulary=prompt_text())
    if item.kind == "head":
        return HEAD
    if is_caption:
        return CAPTION.format(rules=rules, notation=notation_text())
    return STRIP.format(book=book_text(), rules=rules, para=PARA, notation=notation_text())


def read_item(level: Image.Image, item: Item, is_caption: bool = False) -> str:
    """One text item read by the model.  Raises `model.Refused` when the API
    declines: the caller records it against the page."""
    return ask(level.crop(item.box), prompt_for(item, is_caption))


def captions(items: list[Item]) -> set[str]:
    """Names of the text items that are figure captions: a text item directly
    after a figure, inside the figure's own horizontal extent."""
    out = set()
    for prev, it in zip(items, items[1:]):
        if prev.kind == "figure" and it.kind == "text" and \
                it.box[0] >= prev.box[0] - 2 and it.box[2] <= prev.box[2] + 2:
            out.add(it.name)
    return out


#: The constructs' closers, longest first: what a reading may end with.
_CLOSERS = sorted({c.close for c in VOCABULARY}, key=len, reverse=True)


def join_across_cut(before: str, after: str) -> str:
    """Two readings of one paragraph, cut between them, as one.

    Each strip closes its markup at its own edge, so a construct running across
    the cut comes back closed on one side and reopened on the other: the pair
    collapses (`{{smaller|…}}` + `{{smaller|…}}` is one span).  A word broken
    at the cut ("mold-") joins the next strip's first word even through
    closing markup the next strip did not reopen — the word stays whole; where
    the markup then ends is for the proofreader to see and settle."""
    before, after = before.rstrip(), after.lstrip()
    merged = True
    while merged:                             # constructs continuing across the cut, innermost first
        merged = False
        for c in VOCABULARY:
            if before.endswith(c.close) and after.startswith(c.open):
                before, after = before[:-len(c.close)], after[len(c.open):]
                merged = True
                break
    return _join_words(before, after)


def _join_words(before: str, after: str) -> str:
    closers = "|".join(re.escape(c) for c in _CLOSERS)
    m = re.search(rf"(\w)-((?:{closers})*)$", before)
    if not m:
        return before + " " + after
    word = re.match(r"[\w′″'’]*", after).group(0)          # the rest of the broken word
    return before[:m.start() + 1] + word + m.group(2) + after[len(word):]


def assemble_page(items: list[Item], texts: dict[str, str], figure_files: dict[str, str],
                  column_width: int, level: int = 1) -> str:
    """The page's wikitext from its items and their readings.

    `texts` maps item name -> the model's (or a proofreader's) reading;
    `figure_files` maps figure item name -> its image file name."""
    head = ""
    paras: list[str] = []         # the page's blocks: paragraphs and figures
    open_para = False             # is the last paragraph still running?
    pending: list[str] = []       # figure blocks waiting for a paragraph boundary
    caps = captions(items)

    def boundary():
        """A paragraph has ended: waiting figures take their place here."""
        nonlocal open_para
        paras.extend(pending)
        pending.clear()
        open_para = False

    def add(text: str, continues: bool):
        """Append one reading; `continues` = it carries on the open paragraph."""
        nonlocal open_para
        for n, piece in enumerate(text.split(PARAGRAPH_SEP)):
            piece = piece.strip()
            joins = n == 0 and continues and open_para
            if not joins:
                boundary()                      # whatever ran before has ended
            if not piece:
                continue
            if joins:
                paras[-1] = join_across_cut(paras[-1], piece)
            else:
                paras.append(piece)
            open_para = True

    i = 0
    while i < len(items):
        it = items[i]
        if it.kind == "head":
            head = texts.get(it.name, "").strip()
        elif it.kind == "figure":
            w = it.box[2] - it.box[0]
            px = max(60, round(w / column_width * COLUMN_DISPLAY_PX))
            block = f"[[File:{figure_files[it.name]}|center|{px}px]]"
            nxt = items[i + 1] if i + 1 < len(items) else None
            if nxt is not None and nxt.name in caps:
                block += "\n" + texts.get(nxt.name, "").strip()
                i += 1
            pending.append(block)
        else:
            text = texts.get(it.name, "")
            begins = text.lstrip().startswith(PARA)
            text = re.sub(rf"^\s*{PARA}[ \t]*\n?", "", text).strip()
            add(text, continues=not begins)
        i += 1
    boundary()
    who = "transcriber" if level <= 1 else "proofreader"
    return (f'<noinclude><pagequality level="{level}" user="{who}" />{head}</noinclude>'
            + PARAGRAPH_SEP.join(paras) + "<noinclude><references/></noinclude>")
