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
Output only the wikitext, nothing else. If nothing is printed in the image
(blank paper, a speck, the page's edge), output nothing at all — never a
sentence about the image."""

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
    a refusal, never stored as text — but the rejected answer is kept beside
    the reason (`answer`): the check can be wrong (a library's handwritten
    shelf marks counted as lines of type), and whoever settles the item sees
    what the model said."""

    def __init__(self, reason: str, answer: str | None = None):
        super().__init__(reason)
        self.reason, self.answer = reason, answer


#: A reading with fewer characters than this per line-height of printed ink
#: is not a reading.  Measured in the type's own units, so a scan's resolution
#: does not move it (in pixels, a 2482-px scan made complete readings look
#: short).  Accepted readings of 3+ lines (2026-10-03): DNB 510, lowest 1.8,
#: median 2.5; Century 63, lowest 2.0, median 3.3.  At 1.0 a reading that
#: lost about 60% of its text fails; letterspaced display capitals read
#: about 0.9 and are flagged for a person.
MIN_CHARS_PER_LINE_HEIGHT = 1.0
_MARKUP = re.compile(r"\{\{[^|{}]*\||\}\}|'''|''|</?(?:sup|sub)\b[^>]*>|" + PARA)
RUNNING_HEADER = "{{RunningHeader"
_HEAD = re.compile(r"^\{\{RunningHeader\|([^{}]*)\}\}$")


def plain_length(text: str) -> int:
    """Characters of printed text in a reading, its markup removed.  Only the
    vocabulary's own markup: a `<` is often print (the etymology sign)."""
    return len(_MARKUP.sub("", text).strip())


def _type_lines(level: Image.Image, item: Item) -> tuple[list[tuple[int, int]], object]:
    """The rows of an item that hold a line of ink, and the ink itself."""
    import numpy as np
    from wikikit.scan.cut import INK, runs
    x0, y0, x1, y1 = item.box
    dark = np.asarray(level.convert("L").crop(item.box)) < INK
    # a line of type spreads across the strip; the page's dark edge, a blot or
    # bleed-through occupies a narrow band and is not one
    spread = (x1 - x0) * 0.2
    return [(s, e) for s, e in runs(dark.sum(axis=1) > (x1 - x0) * 0.02)
            if e - s >= 6 and dark[s:e].any(axis=0).sum() >= spread], dark


#: The model describing its image instead of reading it ("The strip is blank:
#: there is no printed text to transcribe") — 22 times in 147 DNB list pages,
#: always on a strip with nothing printed.  No page prints these phrases.
_COMMENTARY = re.compile(r"(?i)^\s*(?:the|this) (?:strip|image)\b.*\b(?:blank|no (?:printed|legible|visible) text|"
                         r"nothing to transcribe)")


def settle(level: Image.Image, item: Item, text: str) -> tuple[str, str | None]:
    """The reading to keep, and why it is not a reading (None if it is).  A
    description of the image where the ink shows nothing printed is the
    model saying "nothing here" in words: the reading is empty.  Where type
    IS printed, a description is no reading at all."""
    if _COMMENTARY.search(text):
        lines, _ = _type_lines(level, item)
        return ("", None) if not lines else (text, "a description of the image, not a reading")
    return text, check_reading(level, item, text)


def check_reading(level: Image.Image, item: Item, text: str) -> str | None:
    """Why a reading is not a reading of its image, or None if it is."""
    import numpy as np
    lines, dark = _type_lines(level, item)
    if not text.strip():
        # nothing read is the right reading of a strip with nothing printed in
        # it (a blank page foot, dust, the page's edge)
        return "empty" if lines else None
    if item.kind == "head":
        m = _HEAD.match(text.strip())
        if not m:
            return None if not text.strip().startswith(RUNNING_HEADER) else "a malformed running head"
        if {f.strip().lower() for f in m.group(1).split("|")} & {"left", "right", "number"}:
            return "the prompt's placeholder, not the printed head"
        return None
    if len(lines) < 3:
        return None
    line_height = float(np.median([e - s for s, e in lines]))
    ink = sum(int(dark[s:e].any(axis=0).sum()) for s, e in lines) / line_height
    ratio = plain_length(text) / max(1.0, ink)
    if ratio < MIN_CHARS_PER_LINE_HEIGHT:
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
    reason = text = None
    for _ in range(2):
        text = ask(crop, prompt_for(item, is_caption), instructions=instructions())
        text, reason = settle(level, item, text)
        if reason is None:
            return text
    raise Unread(reason, text)


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

    # Type set across the columns (column 0, `cut.plan`) is a block of its own:
    # a title above the columns does not run on into them, nor a column's
    # last line into a footnote or a library's number below.
    last_column = None
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
            across = it.column == 0 or last_column == 0
            add(text, continues=not begins and not across)
            last_column = it.column
        i += 1
    boundary()
    who = "transcriber" if level <= 1 else "proofreader"
    return (f'<noinclude><pagequality level="{level}" user="{who}" />{head}</noinclude>'
            + PARAGRAPH_SEP.join(paras) + "<noinclude><references/></noinclude>")
