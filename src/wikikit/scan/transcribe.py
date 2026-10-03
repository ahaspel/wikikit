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
from wikikit.scan.model import ask, request
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

#: The fixed instructions: the same for every image of every page, so they are
#: sent once per page run as a cached system prompt (`model.ask`), not with
#: every call.  Each call then names its kind of image in one line.
INSTRUCTIONS = """You transcribe images cut from scanned pages into Wikisource
wikitext, exactly as printed. Each image is one of three kinds, named in the
request that comes with it.

{book}

{rules}

A STRIP is one horizontal strip cut from one column of a page. Strip edges: if
the strip's FIRST line begins a new paragraph (a bold title-word at the margin,
or an indented first line), start your output with {para} on its own line. If
its LAST line ends with a word broken by a hyphen, end your output with that
fragment and the hyphen, e.g. "diffi-".

A CAPTION is the text printed beneath an illustration: wrap it in
{{{{c|...}}}}, keeping its italics and small capitals.

A RUNNING HEAD is the line at the top of a page: a guide word at the left, the
page number in the middle, a guide word at the right (one may be empty).
Output exactly {{{{RunningHeader|left|number|right}}}} with the printed text.
If that top line is not a running head — a title or heading, as on a page that
opens a section — transcribe it as printed instead, like any text.
{notation}
Output only the wikitext, nothing else."""

STRIP = "This image is a STRIP. Transcribe it."
CAPTION = "This image is a CAPTION. Transcribe it."
HEAD = "This image is a RUNNING HEAD. Transcribe it."


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


def instructions() -> str:
    """The fixed instructions, the same for every call (cached)."""
    return INSTRUCTIONS.format(book=book_text(), rules=_RULES.format(vocabulary=prompt_text()),
                               para=PARA, notation=notation_text())


def prompt_for(item: Item, is_caption: bool = False) -> str:
    """The one line that names an image's kind."""
    if item.kind == "head":
        return HEAD
    return CAPTION if is_caption else STRIP


class Unread(Exception):
    """The model answered, but not with a reading of the image: empty, a
    placeholder, or far too short for the ink.  Recorded against the page like
    a refusal, never stored as text."""


#: A reading this many characters per inked column or fewer is not a reading.
#: Measured on 33 Century strips of 3+ lines (2026-10-03): 0.101 to 0.321,
#: median 0.143; a strip that lost half its text reads about 0.07.
MIN_CHARS_PER_INK_COLUMN = 0.06
_MARKUP = re.compile(r"\{\{[^|{}]*\||\}\}|'''|''|</?(?:sup|sub)\b[^>]*>|" + PARA)
RUNNING_HEADER = "{{RunningHeader"
_HEAD = re.compile(r"^\{\{RunningHeader\|([^{}]*)\}\}$")


def plain_length(text: str) -> int:
    """Characters of printed text in a reading, its markup removed.  Only the
    vocabulary's own markup: a `<` is often print (the etymology sign)."""
    return len(_MARKUP.sub("", text).strip())


def check_reading(level: Image.Image, item: Item, text: str) -> str | None:
    """Why a reading is not a reading of its image, or None if it is."""
    if not text.strip():
        return "empty"
    if item.kind == "head":
        m = _HEAD.match(text.strip())
        if not m:
            return None if not text.strip().startswith(RUNNING_HEADER) else "a malformed running head"
        if {f.strip().lower() for f in m.group(1).split("|")} & {"left", "right", "number"}:
            return "the prompt's placeholder, not the printed head"
        return None
    import numpy as np
    from wikikit.scan.cut import INK, runs
    x0, y0, x1, y1 = item.box
    dark = np.asarray(level.convert("L").crop(item.box)) < INK
    lines = [r for r in runs(dark.sum(axis=1) > (x1 - x0) * 0.02) if r[1] - r[0] >= 6]
    if len(lines) < 3:
        return None
    ink = sum(int(dark[s:e].any(axis=0).sum()) for s, e in lines)
    ratio = plain_length(text) / max(1, ink)
    if ratio < MIN_CHARS_PER_INK_COLUMN:
        return f"too short for its ink ({plain_length(text)} characters for {len(lines)} printed lines)"
    return None


def read_request(level: Image.Image, item: Item, is_caption: bool = False,
                 cache_ttl: str | None = None) -> dict:
    """The call that reads one text item — made now by `read_item`, or in a
    batch (`wikikit.scan.batch`), where `cache_ttl` "1h" keeps the cached
    instructions alive through the batch's own pace."""
    return request(level.crop(item.box), prompt_for(item, is_caption),
                   instructions=instructions(), cache_ttl=cache_ttl)


def read_item(level: Image.Image, item: Item, is_caption: bool = False) -> str:
    """One text item read by the model, checked (`check_reading`) and read
    once more if the first answer is not a reading.  Raises `model.Refused`
    when the API declines and `Unread` when both answers fail the check: the
    caller records either against the page."""
    crop = level.crop(item.box)
    reason = None
    for _ in range(2):
        text = ask(crop, prompt_for(item, is_caption), instructions=instructions())
        reason = check_reading(level, item, text)
        if reason is None:
            return text
    raise Unread(reason)


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
            if head and not head.startswith(RUNNING_HEADER):
                add(head, continues=False)       # a title, not a running head: it is page text
                head = ""
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
