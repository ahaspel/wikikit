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

Figures are read where they stand.  The model sees each figure inside the
column or strip that holds it, transcribes all the text around and beside it,
and writes FIGURE_MARK where it stands, its caption on the next line; the mark
becomes the crop of the figure item that follows the text item (`cut`).  The
model is asked to set the mark after the paragraph a figure interrupts, and a
figure still floats to the next paragraph boundary: an inset interrupts a
paragraph, sometimes mid-word ("mold-" [figure] "ings"), and set there the
image would split both.

A page cut before figures were read in place has figure and caption ITEMS and
no marks: its figures and captions assemble from those items, as before.
"""
from __future__ import annotations

import json
import re

from PIL import Image

from wikikit.corpora import current_corpus
from wikikit.scan.cut import Item, reading_order
from wikikit.scan.model import ask, request
from wikikit.pipeline.stages.preprocess import PARAGRAPH_ENDS_WITH_PAGE
from wikikit.scan.vocabulary import VOCABULARY, constructs, prompt_text

PARA = "¶"
PARAGRAPH_SEP = "\n\n"
#: A figure as wide as a column is shown at this width, in display pixels.
COLUMN_DISPLAY_PX = 360

_RULES = """Transcribe only what is printed: never correct, modernise, complete
or add. Keep the printed spelling, punctuation and every diacritic.
Read each accent as it is printed: an acute (é) is not a circumflex (ê) nor a
grave (è); a letter printed plain has none. Words in another language stay
exactly as printed, never translated or anglicised ("fonctions" stays
"fonctions").

Markup — use only this:
{vocabulary}

Record the print; never rearrange it. Everything is written where it is
printed, in the order it is printed: nothing is moved, left out, or set
differently because it would read better.

Lines: join the printed lines of a paragraph into one line. A word hyphenated
at a line end inside the image is joined without the hyphen, unless the hyphen
belongs to the word.

Paragraphs: a blank line means that a new paragraph begins in the print, and
nothing else. A block printed WITHIN a paragraph — a table, an illustration,
verse, a margin heading — is written on lines of its own exactly where it
stands among the printed lines, even in the middle of a sentence, with NO
blank line before or after it: the paragraph's text goes on, on the next line.

Type: mark every piece of text as it is printed. Text in smaller type, or in
italic, that runs on from the column before is marked so again where it
continues."""

#: The fixed instructions: the same for every image of every page, so they are
#: sent once per page run as a cached system prompt (`model.ask`), not with
#: every call.  Each call then names its kind of image in one line.
INSTRUCTIONS = """You transcribe images cut from scanned pages into Wikisource
wikitext, exactly as printed. Each image is one of two kinds, named in the
request that comes with it.

{book}

{rules}

A STRIP is one column of a page, whole or a horizontal strip of it. Strip
edges: if the strip's FIRST line begins a new paragraph (a bold title-word at
the margin, or an indented first line), start your output with {para} on its
own line. If its LAST line ends with a word broken by a hyphen, end your output
with that fragment and the hyphen, e.g. "diffi-".

Illustrations: a strip may hold a picture, engraving, diagram or map. Do not
transcribe the drawing, or any letters, numbers or labels drawn inside it.
Write {mark} on a line of its own exactly where it stands among the printed
lines — in the middle of a paragraph if that is where it is printed, with no
blank line around it — and on the next line its caption, if it has one, wrapped in
{{{{c|...}}}} with its italics and small capitals. Transcribe every line of
text around and beside an illustration: text running beside an inset
illustration belongs where it falls in its paragraph.

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
HEAD = "This image is a RUNNING HEAD. Transcribe it."
#: Where a figure stands in a reading: a line of its own.
FIGURE_MARK = "[[FIGURE]]"
_MARK_LINE = re.compile(r"^[ \t]*" + re.escape(FIGURE_MARK) + r"[ \t]*$", re.M)
#: Where a paragraph begins on the page, as the reading records it: the label
#: of the margin tick beside its first line (`cut.margin_marks`), ⟦y⟧ before
#: its first word.  A print fact for assembly's measurements, never page text.
POSITION = re.compile(r"⟦(\d+)⟧[ \t]*")


def positions(text: str) -> list[tuple[int, int]]:
    """(offset in the reading, page y) of each paragraph's recorded start."""
    return [(m.start(), int(m.group(1))) for m in POSITION.finditer(text)]


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
                               para=PARA, mark=FIGURE_MARK, notation=notation_text())


def prompt_for(item: Item) -> str:
    """The one line that names an image's kind."""
    return HEAD if item.kind == "head" else STRIP


def figures_of(items: list[Item], item: Item) -> list[Item]:
    """The figure items a text item holds: those that follow it (`cut`), in
    reading order (`cut.reading_order`) — pages cut before it was kept list
    side-by-side figures by top edge."""
    k = items.index(item)
    out = []
    for it in items[k + 1:]:
        if it.kind != "figure":
            break
        out.append(it)
    return reading_order(out)


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
_MARKUP = re.compile(r"\{\{[^|{}]*\||\}\}|'''|''|</?(?:sup|sub|math)\b[^>]*>|" + PARA)
RUNNING_HEADER = "{{RunningHeader"
#: A table, as the vocabulary writes one, opens and closes so: a block.
TABLE_START, TABLE_END = "{|", "|}"
_HEAD = re.compile(r"^\{\{RunningHeader\|([^{}]*)\}\}$")


def plain_length(text: str) -> int:
    """Characters of printed text in a reading, its markup removed.  Only the
    vocabulary's own markup: a `<` is often print (the etymology sign)."""
    return len(_MARKUP.sub("", text).strip())


def _type_lines(level: Image.Image, item: Item, figures: list[Item] = ()) -> tuple[list[tuple[int, int]], object]:
    """The rows of an item that hold a line of ink, and the ink itself — the
    ink of the `figures` it holds set aside: a drawing is no type."""
    import numpy as np
    from wikikit.scan.cut import INK, runs
    x0, y0, x1, y1 = item.box
    dark = np.asarray(level.convert("L").crop(item.box)) < INK
    if figures:
        dark = dark.copy()
        for f in figures:
            fx0, fy0, fx1, fy1 = f.box
            dark[max(0, fy0 - y0):max(0, fy1 - y0), max(0, fx0 - x0):max(0, fx1 - x0)] = False
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


def settle(level: Image.Image, item: Item, text: str, figures: list[Item] = ()) -> tuple[str, str | None]:
    """The reading to keep, and why it is not a reading (None if it is).  A
    description of the image where the ink shows nothing printed is the
    model saying "nothing here" in words: the reading is empty.  Where type
    IS printed, a description is no reading at all.  `figures`: the figure
    items the text item holds (`figures_of`)."""
    if _COMMENTARY.search(text):
        lines, _ = _type_lines(level, item, figures)
        return ("", None) if not lines else (text, "a description of the image, not a reading")
    if item.kind == "head" and text.strip() and not _MARK_LINE.sub("", text).strip():
        # a page with no running head, its top line ornament (the Century's
        # vol 1 p. 1 under its title banner): nothing printed to read
        return "", None
    return text, check_reading(level, item, text, figures)


#: Blank paper, in pixel rows at the scan's resolution, that parts two
#: figures the locator found as one: vol 1 p. 10 of the Century prints
#: Fig. 1 over Fig. 2 with ~60 rows between them.
MIN_FIGURE_GAP = 24


def split_figure(level: Image.Image, fig: Item, n: int) -> list[tuple[int, int, int, int]] | None:
    """The `n` figures one figure item holds, stacked, parted at the n-1
    deepest full-width bands of blank paper inside it, each trimmed to its
    ink — or None if the item has fewer such bands."""
    import numpy as np
    from wikikit.scan.cut import INK, runs
    x0, y0, x1, y1 = fig.box
    dark = np.asarray(level.crop(fig.box).convert("L")) < INK
    inked = dark.sum(axis=1) > max(2, dark.shape[1] * 0.003)
    gaps = [(s, e) for s, e in runs(~inked) if s > 0 and e < len(inked) and e - s >= MIN_FIGURE_GAP]
    if len(gaps) < n - 1:
        return None
    cuts = sorted((s + e) // 2 for s, e in sorted(gaps, key=lambda g: g[0] - g[1])[:n - 1])
    out = []
    for a, b in zip([0, *cuts], [*cuts, len(inked)]):
        rows = np.flatnonzero(inked[a:b])
        cols = np.flatnonzero(dark[a:b].any(axis=0))
        if not len(rows) or not len(cols):
            return None
        out.append((x0 + int(cols[0]), y0 + a + int(rows[0]), x0 + int(cols[-1]) + 1, y0 + a + int(rows[-1]) + 1))
    return out


def fit_figures(level: Image.Image, items: list[Item], item: Item, text: str) -> list[Item] | None:
    """A reading that marks more figures than its item holds, where it holds
    one: the locator found as one figure what the print sets as several,
    stacked (Century vol 1 p. 10's Figs. 1 and 2, under one caption).  The
    page's items with that figure split to match the reading
    (`split_figure`), or None — a mark the ink cannot account for stays a
    reading refused (a formula read as a drawing must not pass)."""
    figs = figures_of(items, item)
    marks = text.count(FIGURE_MARK)
    if len(figs) != 1 or marks <= 1:
        return None
    boxes = split_figure(level, figs[0], marks)
    if boxes is None:
        return None
    fig, taken, names = figs[0], {it.name for it in items}, [figs[0].name]
    k = 1
    while len(names) < marks:
        name = f"c{fig.column}f{k}"
        if name not in taken:
            names.append(name)
            taken.add(name)
        k += 1
    at = items.index(fig)
    return items[:at] + [Item("figure", b, fig.column, n) for b, n in zip(boxes, names)] + items[at + 1:]


def check_reading(level: Image.Image, item: Item, text: str, figures: list[Item] = ()) -> str | None:
    """Why a reading is not a reading of its image, or None if it is.  A
    reading marks each figure its item holds, once: a mark short is a figure
    the page would lose, or text read as a drawing; a mark over is a drawing
    the locator never found, with no crop to show."""
    import numpy as np
    # every mark counts, wherever written: one inside a line of text went
    # uncounted, and a formula the reader took for a drawing left the page
    # with no figure to show it (vol 20 p453's ε-leucine, 2026-10-11)
    marks = text.count(FIGURE_MARK)
    if marks != len(figures):
        return f"{marks} figure mark(s) for {len(figures)} figure(s)"
    if marks != len(_MARK_LINE.findall(text)):
        return "a figure mark inside a line of text"
    lines, dark = _type_lines(level, item, figures)
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
    ratio = plain_length(_MARK_LINE.sub("", text)) / max(1.0, ink)
    if ratio < MIN_CHARS_PER_LINE_HEIGHT:
        return f"too short for its ink ({plain_length(text)} characters for {len(lines)} printed lines)"
    return None


def item_image(level: Image.Image, item: Item, small: list | None = None) -> Image.Image:
    """The image the model reads for an item: its crop — with, for a page the
    book measures (`small` not None, even empty), a margin of page-y ticks and
    a bar beside the measured small type (`cut.margin_marks`)."""
    if small is None or item.kind != "text":
        return level.crop(item.box)
    from wikikit.scan.cut import margin_marks
    return margin_marks(level.crop(item.box), item.box, small)


def small_type_of(level: Image.Image, items: list[Item], layout) -> dict[str, list]:
    """Each text item's measured runs of small type (`cut.small_type`), for a
    book that marks it (`layout.marks_small_type`); else none."""
    if not getattr(layout, "marks_small_type", False):
        return {}
    from wikikit.scan.cut import small_type
    texts = [it for it in items if it.kind == "text"]
    return {it.name: runs for it, runs in zip(texts, small_type(level, [it.box for it in texts]))}


def read_request(level: Image.Image, item: Item, cache_ttl: str | None = None,
                 small: list | None = None) -> dict:
    """The call that reads one text item — made now by `read_item`, or in a
    batch (`wikikit.scan.batch`), where `cache_ttl` "1h" keeps the cached
    instructions alive through the batch's own pace.  `small`: the item's
    measured runs of small type (`cut.small_type`)."""
    return request(item_image(level, item, small), prompt_for(item), max_tokens=READ_MAX_TOKENS,
                   instructions=instructions(), cache_ttl=cache_ttl)


#: Room for a whole column's reading: an EB1911 column of formulae ran to
#: 5,041 output tokens (ALGEBRAIC FORMS, vol 1 p669).
READ_MAX_TOKENS = 12000


def read_item(level: Image.Image, item: Item, figures: list[Item] = (), small: list | None = None) -> str:
    """One text item read by the model, checked (`check_reading`) and read
    once more if the first answer is not a reading.  Raises `model.Refused`
    when the API declines and `Unread` when both answers fail the check: the
    caller records either against the page.  `figures`: those it holds;
    `small`: its measured runs of small type."""
    crop = item_image(level, item, small)
    reason = text = None
    for _ in range(2):
        text = ask(crop, prompt_for(item), max_tokens=READ_MAX_TOKENS, instructions=instructions())
        text, reason = settle(level, item, text, figures)
        if reason is None:
            return text
    raise Unread(reason, text)


def captions(items: list[Item], legacy: bool = False) -> set[str]:
    """Names of the items that are figure captions: those the cutter cut as
    captions (`cut.Item` kind "caption").

    `legacy`: a page cut before the cutter marked its captions — guessed as
    any text directly after a figure inside its horizontal extent.  The guess
    also takes the next strip of a column after an uncaptioned figure (Anthon
    leaf 1450: the entry below the family tree, read as its caption, let the
    column's next strip run on from the text above the tree); it is kept only
    so that pages cut before stay as they were assembled."""
    if not legacy:
        return {it.name for it in items if it.kind == "caption"}
    out = set()
    for prev, it in zip(items, items[1:]):
        if prev.kind == "figure" and it.kind == "text" and \
                it.box[0] >= prev.box[0] - 2 and it.box[2] <= prev.box[2] + 2:
            out.add(it.name)
    return out


def _closers() -> list[str]:
    """The constructs' closers, longest first: what a reading may end with."""
    return sorted({c.close for c in constructs()}, key=len, reverse=True)


def join_across_cut(before: str, after: str) -> str:
    """Two readings of one paragraph, cut between them, as one.

    Each strip closes its markup at its own edge, so a construct running across
    the cut comes back closed on one side and reopened on the other: the pair
    collapses (`{{smaller|…}}` + `{{smaller|…}}` is one span).  A word broken
    at the cut ("mold-") joins the next strip's first word even through
    closing markup the next strip did not reopen — the word stays whole; where
    the markup then ends is for the proofreader to see and settle."""
    before, after = before.rstrip(), after.lstrip()
    vocab = constructs()
    for c in vocab:
        # the word's hyphen set AFTER its construct's close ("{{sc|Bi}}-" then
        # "{{sc|metallism}}": SILVER's Bimetallism): it belongs inside, where
        # the joins below meet it
        if c.close and before.endswith(c.close + "-") and after.startswith(c.open):
            before = before[:-len(c.close) - 1] + "-" + c.close
            break
    merged = True
    while merged:                             # constructs continuing across the cut, innermost first
        merged = False
        for c in vocab:
            if before.endswith(c.close) and after.startswith(c.open):
                before, after = before[:-len(c.close)], after[len(c.open):]
                merged = True
                break
    return _join_words(before, after)


def _join_words(before: str, after: str) -> str:
    closers = "|".join(re.escape(c) for c in _closers())
    m = re.search(rf"(\w)-((?:{closers})*)$", before)
    if not m:
        return before + " " + after
    word = re.match(r"[\w′″'’]*", after).group(0)          # the rest of the broken word
    return before[:m.start() + 1] + word + m.group(2) + after[len(word):]


# ── footnotes: printed at a column's foot, placed as <ref> at their marks ────
#: A note's number as printed before it, and as its mark in the text.
_NOTE_MARK = re.compile(r"<sup>\s*(\d{1,2}|[*†‡§‖])\s*</sup>")
#: The small type a column's notes are set in: the vocabulary's own.
_SMALL = {c.name: c for c in VOCABULARY if c.name in ("smaller block", "smaller")}
_SMALL_OPEN = tuple(c.open for c in _SMALL.values())


def _balanced_end(text: str, start: int) -> int:
    """The index just past the `}}` closing the template opened at `start`.
    A formula is opaque: LaTeX's own braces are not the template's
    (`\\frac{d}{d\\text{X}}` ends in "}}": vol 1 p671's small-type wrapper
    "closed" inside it, and its real close leaked onto the page)."""
    depth, i = 0, start
    while i < len(text) - 1:
        if text.startswith("<math", i):
            end = text.find("</math>", i)
            if end >= 0:
                i = end + len("</math>")
                continue
        pair = text[i:i + 2]
        if pair == "{{":
            depth, i = depth + 1, i + 2
            continue
        if pair == "}}":
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
            continue
        i += 1
    return -1


def _split_notes(body: str) -> list[tuple[str, str]] | None:
    """(number, text) of each note in a footnote container's body — numbered
    notes, each beginning a line — or None if the body does not open with one.
    Short notes run on in one line ("…Johns. ²See…", vol 24 p. 937): the
    next number's mark after a sentence's end begins the next note."""
    starts = [m for m in re.finditer(r"(?m)^[ \t]*" + _NOTE_MARK.pattern, body)]
    if not starts or body[:starts[0].start()].strip():
        return None
    out = []
    for m, nxt in zip(starts, starts[1:] + [None]):
        number, text = m.group(1), body[m.end():nxt.start() if nxt else len(body)].strip()
        while number.isdigit():
            run_on = re.search(r"(?<=[.;)\]])\s+<sup>\s*" + str(int(number) + 1) + r"\s*</sup>", text)
            if not run_on:
                break
            out.append((number, text[:run_on.start()].strip()))
            number, text = str(int(number) + 1), text[run_on.end():].strip()
        out.append((number, text))
    return out


def take_footnotes(text: str) -> tuple[str, list[tuple[str, str]]]:
    """A reading with its footnotes taken out, and the notes in order.

    A footnote is printed at the foot of its column, numbered, in small type:
    read, it is a `{{smaller block|…}}` (or `{{smaller|…}}`) whose text opens
    with a numbered note — one note or several, each starting a line — or a
    paragraph opening with a numbered note.  Small type that does not open
    with a number is the book's text (EB1911 sets whole passages small) and
    stays."""
    notes: list[tuple[str, str]] = []
    out, i = [], 0
    while i < len(text):
        opener = next((o for o in _SMALL_OPEN if text.startswith(o, i)), None)
        if opener:
            end = _balanced_end(text, i)
            got = _split_notes(text[i + len(opener):end - 2]) if end > 0 else None
            if got:
                notes += got
                i = end
                continue
        out.append(text[i])
        i += 1
    kept = []
    for para in "".join(out).split(PARAGRAPH_SEP):
        got = _split_notes(para.strip()) if para.strip() else None
        if got:
            notes += got
        else:
            kept.append(para)
    return PARAGRAPH_SEP.join(p for p in kept if p.strip()), notes


def place_footnotes(body: str, notes: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """Each note set as `<ref>…</ref>` at its mark — the one `<sup>n</sup>`
    with its number in the text — as Wikisource sets a footnote.  A note
    whose mark is missing or appears more than once (a continuation from the
    page before; a homograph number) is returned, unplaced."""
    left = []
    for number, note in notes:
        marks = [m for m in _NOTE_MARK.finditer(body) if m.group(1) == number]
        if len(marks) != 1 or not note:
            left.append((number, note))
            continue
        m = marks[0]
        body = body[:m.start()] + f"<ref>{note}</ref>" + body[m.end():]
    return body, left


# ── sections: an address at each article the book divides the page into ─────
_BOLD = "'''"


def _opening_bold(para: str) -> str | None:
    """The bold run a paragraph opens with, past its lead layout (templates —
    a margin note, a shoulder heading — and images), or None."""
    i = 0
    while True:
        while i < len(para) and para[i].isspace():
            i += 1
        if para.startswith("{{", i) or para.startswith("[[File:", i) or para.startswith("[[Image:", i):
            end = _balanced_end(para, i) if para.startswith("{{", i) else para.find("]]", i) + 2
            if end <= i:
                return None
            i = end
            continue
        break
    if not para.startswith(_BOLD, i):
        return None
    close = para.find(_BOLD, i + 3)
    return para[i + 3:close] if close > 0 else None


def place_sections(body: str, sections: list[dict]) -> tuple[str, list[str]]:
    """`<section begin="NAME" />` set for each of the page's sections
    ([{"name", "before"}], the book's `page_sections`): at the page's top where
    `before` is None, else at the start of the paragraph its article's headword
    opens — the next such paragraph, in order.  Returns the body and the
    names it could not place."""
    from wikikit.util.strings import section_slug as slug
    paras = body.split(PARAGRAPH_SEP)
    at: dict[int, list[str]] = {}
    left, k = [], 0
    for s in sections:
        tag = f'<section begin="{s["name"]}" />'
        if s.get("before") is None:
            at.setdefault(0, []).append(tag)
            continue
        want = slug(s["before"])
        for j in range(k, len(paras)):
            bold = _opening_bold(paras[j])
            if bold is not None and want and slug(bold).startswith(want):
                at.setdefault(j, []).append(tag)
                k = j + 1
                break
        else:
            left.append(s["name"])
    for j, tags in at.items():
        paras[j] = "".join(tags) + paras[j]
    return PARAGRAPH_SEP.join(paras), left


# ── editorial assembly: what the reading records, set as our page sets it ────
#: The text before a figure or table introduces it ("shown below:—", "the
#: following table:—"): it stands there, where the text points to it.
_INTRODUCES = re.compile(r"[:—][\"”’)\]]*\s*$")
_WORD_BROKEN = re.compile(r"(\S+)-\s*$")
#: A centred line: over a table or verse, its title (`_units`).
_CENTRED_OPEN = next(c.open for c in VOCABULARY if c.name == "c")
#: A table cell opening with a printed brace: "{" there would open a table.
#: Inside a row a table cannot open (only at a line's start), so a "{" right
#: after a cell's start is the printed brace — unless it opens a template.
_CELL_BRACE = re.compile(r"(\|)(\s*)\{(?!\{)")


def _set_table(block: str) -> str:
    """A table as our page sets it: centred where the reading gives it no
    attributes (`vocabulary.TABLE_STYLE`), and a brace printed at the start of
    a cell written as the character, never as "{" (vol 26 p735: "|rowspan=4|{"
    opened a table inside the cell, and the recipe after it was lost)."""
    from wikikit.scan.vocabulary import TABLE_STYLE
    head, _, rest = block.partition("\n")
    if head.strip() == TABLE_START:
        head = f"{TABLE_START} {TABLE_STYLE}"
    rows = [line if line.startswith(("{|", "|}", "|-")) else _CELL_BRACE.sub(r"\1\2&#123;", line)
            for line in rest.split("\n")]
    return "\n".join([head] + rows)


def _legend_line(line: str) -> bool:
    """A line printed under a figure's caption, as part of it: another
    centred line (a second caption, a key: "1, 1 Primary circuit"), or a
    legend in small type that is a sentence of its own ("Platinum pound
    avoirdupois, …": vol 28 p. 478 — left behind, it stood mid-sentence).
    Small type opening in lower case carries on the paragraph's sentence (vol
    25 p. 61): not the figure's."""
    s = line.strip()
    if not (s.endswith("}}") and _balanced_end(s, 0) == len(s)):
        return False
    if s.startswith("{{c|"):
        return True
    small = next((o for o in _SMALL_OPEN if s.startswith(o)), None)
    return bool(small) and not s[len(small):].lstrip()[:1].islower()


def _figure_end(lines: list[str], k: int) -> int:
    """Past the figure whose mark is `lines[k]`: the mark, its caption line,
    and the caption's further lines (`_legend_line`)."""
    j = k + 1
    if j < len(lines) and lines[j].lstrip().startswith("{{c|"):
        j += 1
        while j < len(lines) and _legend_line(lines[j]):
            j += 1
    return j


def _caption_of(lines: list[str]) -> str:
    """A figure's caption lines as one caption: each line's own centring
    taken off (the float centres its caption), the lines kept apart."""
    from wikikit.wikitext import template_end
    out = []
    for line in (s.strip() for s in lines):
        if line.startswith("{{c|") and template_end(line, 0) == len(line):
            line = line[len("{{c|"):-2]
        if line:
            out.append(line)
    return "<br />".join(out)


_FILE_LINK = re.compile(r"\[\[(?:File|Image):([^|\]]+)[^\]]*\]\]", re.I)


def _img_float(file: str, px: int | str | None, side: str, caption: str) -> str:
    width = f"|width={px}px" if px else ""
    return f"{{{{img float|file={file}{width}|align={side}|cap={caption}}}}}"


def inset_markup(markup: str | None, caption: str, side: str,
                 file: str = "", px: int | None = None) -> str | None:
    """A figure the print sets with type beside it, as a float on `side`
    with its caption, on one line so the text runs on around it: the
    replaced page's crop (or a Commons region) told its `Location` and given
    its caption as `Description`; a wiki-store file, or our own crop
    (`markup` None: `file`, `px`), as `{{img float}}`.  None: the markup is
    not one image this can float (several, or a crop already captioned)."""
    from wikikit.pipeline.stages.elements import CSS_IMAGE_CROP
    if markup is None:
        return _img_float(file, px, side, caption)
    m = markup.strip()
    if CSS_IMAGE_CROP.match(m) and m.endswith("}}") and len(CSS_IMAGE_CROP.findall(m)) == 1:
        m = re.sub(r"\s*\n\s*", "", m)
        if re.search(r"\|\s*Description\s*=\s*[^|}\s]", m, re.I):
            return None
        m = re.sub(r"\|\s*(?:Location|Description)\s*=[^|}]*", "", m, flags=re.I)
        return m[:-2] + f"|Location = {side}|Description = {caption}}}}}"
    link = _FILE_LINK.fullmatch(m)
    if link:
        w = re.search(r"\|\s*(\d+)px", m)
        return _img_float(link.group(1).strip(), w and w.group(1), side, caption)
    return None


def _before_last_word(text: str, made: str) -> str:
    """`made` set before the text's last word — a word a line end broke,
    which then joins whole ("mold-" | figure | "ings")."""
    before, _, word = text.rstrip().rpartition(" ")
    return f"{before} {made} {word}".lstrip()


def _units(piece: str) -> list[tuple[str, str]]:
    """A paragraph as the reading records it, in printed order: its text
    lines, and the blocks standing among them — ("table" | "verse" | "small" |
    "figure" | "text", the lines)."""
    lines, out, k = piece.split("\n"), [], 0
    while k < len(lines):
        line = lines[k]
        s = line.strip()
        if s.startswith(TABLE_START):
            j = next((j for j in range(k, len(lines)) if lines[j].strip().startswith(TABLE_END)), len(lines) - 1)
            out.append(("table", _set_table("\n".join(lines[k:j + 1]))))
            k = j + 1
        elif s.startswith("<poem>"):
            j = next((j for j in range(k, len(lines)) if "</poem>" in lines[j]), len(lines) - 1)
            out.append(("verse", "\n".join(lines[k:j + 1])))
            k = j + 1
        elif _MARK_LINE.fullmatch(line):
            j = _figure_end(lines, k)
            out.append(("figure", "\n".join(lines[k:j])))
            k = j
        else:
            out.append(("text", line))
            k += 1
    # a centred line printed directly above a table or verse is its title: it
    # goes where the block goes (vol 18 p296: "Marvin's Table for the
    # Reduction of Velocities…" had stayed mid-sentence when its table moved),
    # every line of it: a title may be printed in several (vol 26 p582's
    # "Consumption of Alcohol…" over "Annual Average per Head" — its first
    # line, left behind, read as text whose sentence had not ended, and the
    # table was moved past the next sentence)
    centred = _CENTRED_OPEN
    merged: list[tuple[str, str]] = []
    for kind, s in out:
        if kind in ("table", "verse"):
            while merged and merged[-1][0] == "text" and merged[-1][1].strip().startswith(centred):
                s = merged.pop()[1].strip() + "\n" + s
        merged.append((kind, s))
    return merged


def _is_anchor_line(line: str) -> bool:
    """A line that is one construct standing apart, not a display: a margin
    heading.  It goes where it stands and breaks nothing."""
    s = line.strip()
    apart = tuple(c.open for c in VOCABULARY if c.name in ("block center", "c", "smaller block"))
    return (s.startswith("{{") and s.endswith("}}") and _balanced_end(s, 0) == len(s)
            and not s.startswith(apart))


def _quote_open(text: str) -> bool:
    """An italic or bold left open on the text's last line (wikitext closes
    either at the line's end)."""
    last = text.rsplit("\n", 1)[-1]
    bold = len(re.findall(r"'''", last))
    italic = len(re.findall(r"(?<!')''(?!')", last)) + len(re.findall(r"'''''", last))
    return bool(bold % 2 or italic % 2)


def _join_text(lines: list[str]) -> str:
    """A paragraph's text lines as one: lines kept apart by a newline (a
    margin heading on its own line, a display); a word broken at a line end
    joined across whatever stands between its halves, the margin heading set
    before the broken word ("deter-" | heading | "mining": vol 26 p169)."""
    out, held = "", []
    for line in lines:
        if not line.strip():
            continue
        if _is_anchor_line(line) and (_WORD_BROKEN.search(out) or _quote_open(out)):
            held.append(line.strip())
            continue
        if held and not _WORD_BROKEN.search(out):
            # held past an open italic or bold ("(''Phil." | heading |
            # "Trans.'', 1880": vol 25 p463): a quote cannot run across a
            # line, so the text joins on one line and the heading follows
            # once the quote has closed
            out = out.rstrip() + " " + line.lstrip()
            if not _quote_open(out):
                out += "\n" + "\n".join(held)
                held = []
            continue
        if held:
            m = _WORD_BROKEN.search(out)
            head = out[:m.start()].rstrip()
            out = (head + "\n" if head else "") + "\n".join(held) + "\n" \
                + _join_words(out[m.start():].rstrip(), line.lstrip())
            held = []
            continue
        if out and _WORD_BROKEN.search(out) and not _is_anchor_line(line):
            out = _join_words(out.rstrip(), line.lstrip())
            continue
        out = f"{out}\n{line}" if out else line
    if held:
        out += "\n" + "\n".join(held)
    return out


def _segments(piece: str, continues: bool = False) -> list[tuple[str, str]]:
    """A paragraph as our page sets it: (kind, …) in printed order, kind
    "text", "block", "figure", or a float — "figure float", "block float".

    In place ("block" | "figure") — a block
    without a title is in the text's flow, part of its sentence ("learn the
    tables of" + verse: vol 28 p508; "by distilling acetoxime," + equation:
    vol 22 p706), and stays where it is printed; so does an illustration or
    titled table the text INTRODUCES ("shown below:—", "the following
    table:—": TEMPERANCE p. 582), or that opens the paragraph.

    Floats — an illustration, or a table or verse
    under its own centred title (vol 18 p296's Marvin's Table), that
    INTERRUPTS the text: the print set it wherever the page had room.  Each
    is set AFTER its paragraph, and the text it interrupted runs on as one
    (user, 2026-10-15): set inside the paragraph, it would end the line
    before it short and start the text after it on a new line, a break the
    print's lines made and ours do not (SIGNAL p. 72: "…almost every" /
    Fig. 2 / "hill within range").  `continues`: the paragraph began in the
    reading before, so a float first in this one interrupts it."""
    units = _units(piece)
    if units and all(k == "figure" for k, _ in units):
        # figures alone between blank lines (a reading made before blocks
        # were written where they stand): the paragraph runs on past them
        return [("figure float", s) for _, s in units]
    segs: list[tuple[str, str]] = []
    text: list[str] = []           # runs on across a float: the float is not in it
    started = False                # has the paragraph begun (text or a block in place)?
    for kind, s in units:
        if kind == "text":
            text.append(s)
            continue
        name = "figure" if kind == "figure" else "block"
        if kind == "figure" or s.lstrip().startswith(_CENTRED_OPEN):
            before = _join_text(text) if text else ""
            introduced = (bool(_INTRODUCES.search(before)) if before
                          else not continues and not started)
            if not introduced:
                segs.append((name + " float", s))
                continue
        if text:
            segs.append(("text", _join_text(text)))
            text = []
        segs.append((name, s))
        started = True
    if text:
        segs.append(("text", _join_text(text)))
    return segs


def _is_note(piece: str) -> bool:
    """A footnote, as printed at a column's foot: small type by definition."""
    body = piece.strip()
    for o in _SMALL_OPEN:
        if body.startswith(o):
            body = body[len(o):]
            break
    return bool(re.match(r"\s*" + _NOTE_MARK.pattern, body))


#: Text ending in a figure mark and its caption lines: what comes next on the
#: following line may be the caption's legend (`_legend_line`).
_UNDER_CAPTION = re.compile(_MARK_LINE.pattern.removesuffix("$") + r"\n(?:[ \t]*\{\{c\|[^\n]*\n)+[ \t]*\Z", re.M)


def _without_small_blocks(text: str) -> str:
    """A reading with every small-type block wrapper it gave taken off — by
    its braces, wherever it closes: a wrapper often spans several paragraphs
    and their recorded positions, and one taken off paragraph by paragraph
    was missed and then wrapped again (`{{smaller block|{{smaller block|…`,
    vol 24 p601, 2026-10-11).  A footnote's wrapper stays: notes keep their
    small type."""
    o = _SMALL["smaller block"].open
    out, i = [], 0
    while (j := text.find(o, i)) >= 0:
        end = _balanced_end(text, j)
        if end < 0:
            break
        inner = text[j + len(o):end - 2]
        out.append(text[i:j])
        keep = _is_note(POSITION.sub("", inner)) or _UNDER_CAPTION.search(text, 0, j) \
            and _legend_line(text[j:end])      # a figure's legend: the figure's, not a paragraph's
        out.append(text[j:end] if keep else inner)
        i = end
    out.append(text[i:])
    return "".join(out)


def _whole_small(piece: str) -> str | None:
    """The inside of a piece wrapped whole in small type, or None."""
    o = _SMALL["smaller block"].open
    if piece.startswith(o) and _balanced_end(piece, 0) == len(piece):
        return piece[len(o):-2].strip()
    return None


_OPEN_THEN_POSITION = re.compile(r"((?:\{\{[^{}|]*\|[ \t]*)+)(⟦\d+⟧[ \t]*)")


def _read_small(text: str) -> list[bool]:
    """For each recorded paragraph start, whether the reading set that
    paragraph in small type (its text begins inside a small-type block)."""
    o = _SMALL["smaller block"].open
    spans, i = [], 0
    while (j := text.find(o, i)) >= 0:
        end = _balanced_end(text, j)
        if end < 0:
            break
        spans.append((j, end))
        i = j + len(o)
    out = []
    for m in POSITION.finditer(text):
        at = m.end() + len(text[m.end():]) - len(text[m.end():].lstrip())
        out.append(any(a <= at < b for a, b in spans))
    return out


def apply_type(text: str, sizes: list | None) -> str:
    """A reading with each paragraph set in its MEASURED type (`sizes`, one per
    recorded paragraph start — `positions`): the reading's own whole-paragraph
    size marks taken off, and every piece of a paragraph small wrapped in
    small type.  A paragraph not measured (None) keeps the size the reading
    gave it: measurement overrides the reader only where it measures.  A
    table or verse is never wrapped (a template cannot hold a table), nor a
    footnote (small by definition, placed apart).  No sizes: the reading as
    it is."""
    if not sizes:
        return text
    # a position written just inside a template's opening marks where that
    # template's paragraph begins: it goes before the opening, or the split
    # at positions parts a wrapper from its close (vol 28 p508's footnotes,
    # "{{smaller block|⟦3000⟧ <sup>4</sup> …}}")
    text = _OPEN_THEN_POSITION.sub(r"\2\1", text)
    block = _SMALL["smaller block"]
    # small type continued from the column before, closed with no opening
    # (vol 25 p130: "…a row of settlers.}}"): opened where the reading begins
    if _unopened_close(text):
        text = block.open + text
    # the text before the first recorded start continues a paragraph: it
    # keeps the size the reading gave it
    lead_small = text.lstrip().startswith(block.open)
    read = _read_small(text)
    sizes = [s if s is not None else ("small" if k < len(read) and read[k] else "body")
             for k, s in enumerate(sizes)]
    text = _without_small_blocks(text)
    ms = list(POSITION.finditer(text))
    if not ms:
        return text

    def set_(seg: str, small: bool) -> str:
        pieces = []
        for piece in seg.split(PARAGRAPH_SEP):
            if not piece.strip():
                continue                     # a wrapper taken off can leave an empty one
            if _is_note(piece) or piece.lstrip().startswith((TABLE_START, "<poem>")) \
                    or _MARK_LINE.match(piece.strip()):
                pieces.append(piece)
                continue
            p = piece.strip()
            # a paragraph ending in a figure mark or a block: its close on a
            # line of its own, or the mark's line is no mark and the figure
            # is lost (vol 20 p453: "obtained:—" + [[FIGURE]]}} lost ε-leucine)
            last = p.rsplit("\n", 1)[-1].strip()
            apart = "\n" if _MARK_LINE.fullmatch(last) or last.startswith(TABLE_END) or last.endswith("</poem>") \
                else ""
            pieces.append(block.open + p + apart + block.close if small else p)
        return PARAGRAPH_SEP.join(pieces).strip()

    # the lead is set only for its size, its spacing kept as read: a margin
    # heading then "arise from …" carries on the column before (vol 26 p995)
    lead = text[:ms[0].start()]
    out = [set_(lead, True) + lead[len(lead.rstrip()):] if lead_small and lead.strip() else lead]
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(text)
        seg = text[m.end():end]
        # a start recorded inside a table (a row's: vol 28 p511) begins no
        # paragraph to set — a template cannot stand in a table — and the
        # text before it runs into the table as read, with no break added
        if _in_table(text, m.start()):
            out.append(m.group(0) + seg)
            continue
        set_seg = set_(seg, k < len(sizes) and sizes[k] == "small")
        if k + 1 < len(ms) and _in_table(text, ms[k + 1].start()):
            out.append(m.group(0) + set_seg + seg[len(seg.rstrip()):])
        else:
            out.append(m.group(0) + set_seg + (PARAGRAPH_SEP if k + 1 < len(ms) else ""))
    return "".join(out)


def _in_table(text: str, at: int) -> bool:
    """Whether `at` falls inside a table (more opened before it than closed)."""
    from wikikit.scan.vocabulary import TABLE_CLOSES, TABLE_OPENS
    before = text[:at]
    return len(TABLE_OPENS.findall(before)) > len(TABLE_CLOSES.findall(before))


def _unopened_close(text: str) -> bool:
    """A template close with no opening before it (formulas opaque)."""
    depth, i = 0, 0
    while i < len(text) - 1:
        if text.startswith("<math", i) and (end := text.find("</math>", i)) >= 0:
            i = end + len("</math>")
            continue
        pair = text[i:i + 2]
        if pair == "{{":
            depth, i = depth + 1, i + 2
        elif pair == "}}":
            if depth == 0:
                return True
            depth, i = depth - 1, i + 2
        else:
            i += 1
    return False


def take_furniture(text: str, into: list[str]) -> str:
    """A reading with its page furniture taken out (`Construct.furniture`: a
    sheet signature), each kept in `into` for the page's footer — recorded
    where it was printed, set where Wikisource sets it: outside the text.  The
    text either side keeps the break it had (a paragraph's, or a line's)."""
    for c in constructs():
        if not c.furniture:
            continue
        while (i := text.find(c.open)) >= 0:
            end = _balanced_end(text, i) if c.close == "}}" else text.find(c.close, i) + len(c.close)
            if end <= i:
                break
            into.append(text[i:end])
            before, after = text[:i], text[end:]
            gap = PARAGRAPH_SEP if before.endswith(PARAGRAPH_SEP) and after.startswith(PARAGRAPH_SEP) else "\n"
            before, after = before.rstrip(), after.lstrip()
            text = before + (gap if before and after else "") + after
    return text


def assemble_page(items: list[Item], texts: dict[str, str], figure_files: dict[str, str],
                  column_width: int, level: int = 1, legacy_captions: bool = False,
                  sections: list[dict] | None = None,
                  source_images: dict[str, list[str]] | None = None,
                  regions: dict[str, str] | None = None,
                  insets: dict[str, str] | None = None) -> str:
    """The page's wikitext from its items and their readings.

    `texts` maps item name -> the model's (or a proofreader's) reading;
    `figure_files` maps figure item name -> its image file name;
    `sections` the page's section addresses (`place_sections`) — each
    paragraph's type size is the reading's, measured into it once
    (`supplied.measure_type`);
    `source_images` figure item name -> the replaced page's own image markup
    set in its place (`wikikit.scan.images.pair`; empty: covered by an image
    set at an earlier figure) — our crop stands only where it has none;
    `regions` figure item name -> a region of the clean Commons render of the
    volume (`{{Css image crop}}`) for a figure the replaced page has no image
    of, its box set by looking at the render; "" = no
    figure there (a box that held none);
    `insets` figure item name -> the side of its column it stands on, for a
    figure the print sets with type beside it (`supplied.measure_insets`):
    floated there, in its place in the text.  Any other figure that
    interrupts the text is set after its paragraph (`_segments`)."""
    source_images = source_images or {}
    regions = regions or {}
    insets = insets or {}
    head = ""
    paras: list[str] = []         # the page's blocks: paragraphs and figures
    open_para = False             # is the last paragraph still running?
    ends_block = False            # … and does it end in a block (a table, a figure)?
    pending: list[str] = []       # figure blocks waiting for a paragraph boundary
    notes: list[tuple[str, str]] = []   # the page's footnotes, from its columns' feet
    furniture: list[str] = []     # the printer's marks the readings record: the footer's
    caps = captions(items, legacy=legacy_captions)

    def boundary():
        """A paragraph has ended: waiting figures take their place here."""
        nonlocal open_para
        paras.extend(b for b in pending if b)    # a covered figure sets nothing
        pending.clear()
        open_para = False

    def display_px(fig: Item) -> int:
        return max(60, round((fig.box[2] - fig.box[0]) / column_width * COLUMN_DISPLAY_PX))

    def figure_block(fig: Item, caption: str = "") -> str:
        if fig.name in source_images:
            # the replaced page's own images, as it set them (a wiki-store
            # file, an editor's region of the scan): never our crop
            return "\n".join(source_images[fig.name] + ([caption] if caption else []))
        if fig.name in regions:
            # a clean region of the volume's Commons render, never the IA scan
            return "\n".join(x for x in (regions[fig.name], caption) if x)
        return f"[[File:{figure_files[fig.name]}|center|{display_px(fig)}px]]" + (f"\n{caption}" if caption else "")

    def inset_of(fig: Item, caption_lines: list[str]) -> str | None:
        """The figure as a float beside its type (`inset_markup`), or None."""
        side, caption = insets[fig.name], _caption_of(caption_lines)
        if fig.name in source_images:
            imgs = [m for m in source_images[fig.name] if m.strip()]
            return inset_markup(imgs[0], caption, side) if len(imgs) == 1 else None
        if fig.name in regions:
            return inset_markup(regions[fig.name], caption, side) if regions[fig.name] else None
        return inset_markup(None, caption, side, file=figure_files[fig.name], px=display_px(fig))

    def runs_on(line: str) -> bool:
        """A line of running text, which a float can stand in."""
        s = line.strip()
        return bool(s) and not _MARK_LINE.fullmatch(line) and not _is_anchor_line(line) \
            and not s.startswith((TABLE_START, "|", "!", "<poem", "{{c|"))

    def set_insets(text: str, figs: list[Item]) -> tuple[str, list[Item]]:
        """Each figure the print sets with type beside it (`insets`) as a
        float standing IN the text where its mark is — a float ends no line,
        so the text before and after it runs on as one (SOUND p. 453's Fig.
        30 beside two paragraphs).  The figures left for the other marks."""
        lines, out, kept, n, k = text.split("\n"), [], [], 0, 0
        while k < len(lines):
            if not _MARK_LINE.fullmatch(lines[k]):
                out.append(lines[k])
                k += 1
                continue
            j = _figure_end(lines, k)
            fig = figs[n] if n < len(figs) else None
            n += 1
            made = inset_of(fig, lines[k + 1:j]) if fig and fig.name in insets else None
            if made is None:
                kept += [fig] if fig else []
                out.extend(lines[k:j])
                k = j
                continue
            after = lines[j] if j < len(lines) and runs_on(lines[j]) else None
            if out and runs_on(out[-1]):
                prev = out[-1].rstrip()
                if after is not None and _WORD_BROKEN.search(prev):
                    # "mold-" | figure | "ings": the float before the broken word
                    out[-1] = _join_words(_before_last_word(prev, made), after.lstrip())
                    k = j + 1
                    continue
                out[-1] = f"{prev} {made}" + (f" {after.lstrip()}" if after is not None else "")
                k = j + (1 if after is not None else 0)
            elif after is not None:
                out.append(f"{made} {after.lstrip()}")
                k = j + 1
            else:
                out.append(made)
                k = j
        return "\n".join(out), kept + list(figs[n:])

    def marked_figure(unit: str, figs: list[Item]) -> str:
        """A figure mark (and the caption line after it) as the crop it stands
        for; a mark with no figure left (a proofreader's): its caption alone."""
        _, _, caption = unit.partition("\n")
        return figure_block(figs.pop(0), caption.strip()) if figs else caption.strip()

    opens: bool | None = None     # does the page's first text open a paragraph?

    def add(text: str, continues: bool, figs: list[Item] = ()):
        """Append one reading; `continues` = it carries on the open paragraph.
        `figs`: the figure items its marks stand for, in order.  A float
        (`_segments`) waits for its paragraph's end, however many readings
        the paragraph runs across; a block in the text's flow is set on lines
        of its own inside its paragraph, which runs on after it.  Only a
        blank line in the reading, the print's own break, begins a paragraph."""
        nonlocal open_para, ends_block, opens
        if opens is None and text.strip():
            opens = not continues
        text, figs = set_insets(text, list(figs))
        first = True
        for raw in text.split(PARAGRAPH_SEP):
            piece = raw.strip()
            # a paragraph set in small type (`apply_type`) is set as plain
            # text, then each of its text segments in small type: a block set
            # apart from it is never inside the wrapper (vol 28 p508: verse
            # within a small paragraph left the wrapper's close stranded)
            small = _whole_small(piece)
            if small is not None:
                piece = small
            segs = _segments(piece, continues=first and continues and open_para)
            if small is not None:
                # a table in small type is set small too, in a wrapper of its
                # own, on lines of its own (vol 25 p. 1017's Wöhler tables)
                o, c = _SMALL["smaller block"].open, _SMALL["smaller block"].close
                segs = [(k, o + s + c if k == "text"
                         else f"{o}\n{s}\n{c}" if k.startswith("block") and "<poem>" not in s else s)
                        for k, s in segs]
            # each figure mark takes its own figure, in reading order
            segs = [(k, marked_figure(s, figs) if k.startswith("figure") else s) for k, s in segs]
            waiting = [s for k, s in segs if k.endswith(" float")]
            segs = [(k, s) for k, s in segs if not k.endswith(" float")]
            if not segs and waiting:
                # floats alone between blank lines: the paragraph runs on
                # past them, and they wait for its end
                pending.extend(waiting)
                continue
            # this reading's paragraph carries on the one open before it (across a cut)
            inside = first and continues and open_para
            for n, (kind, s) in enumerate(segs):
                if kind in ("figure", "block"):
                    block = s
                    first = False
                    if not block:
                        continue                # a figure covered by an earlier image
                    # where the print sets it: on lines of its own (a table's
                    # "{|" opens a line — Anthon leaf 1459), inside its paragraph
                    if inside:
                        paras[-1] = paras[-1].rstrip() + "\n" + block
                    else:
                        boundary()
                        paras.append(block)
                    inside, open_para, ends_block = True, True, True
                    continue
                if n == 0 and first and continues and open_para:
                    # across a cut: words join words; after a block, a line of its own
                    paras[-1] = paras[-1].rstrip() + "\n" + s if ends_block else join_across_cut(paras[-1], s)
                elif inside:
                    paras[-1] = paras[-1].rstrip() + "\n" + s   # the paragraph runs on after a block
                else:
                    boundary()                  # whatever ran before has ended
                    paras.append(s)
                first = False
                inside, open_para, ends_block = True, True, False
            # after this paragraph's text: set at its end (the next boundary)
            pending.extend(waiting)
            if not segs:
                first, open_para = False, False
                boundary()

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
        elif it.kind == "figure":                # a page cut before figures were read in place
            nxt = items[i + 1] if i + 1 < len(items) else None
            caption = ""
            if nxt is not None and nxt.name in caps:
                caption = texts.get(nxt.name, "").strip()
                i += 1
            made = inset_of(it, caption.split("\n")) if it.name in insets else None
            if made is not None and open_para and paras and runs_on(paras[-1].rsplit("\n", 1)[-1]):
                # no mark to stand at: the type beside it is the reading that
                # follows, so it stands at the end of the text before it —
                # ahead of a word the cut broke ("par-" | "ticles": vol 18 p272)
                if _WORD_BROKEN.search(paras[-1].rstrip()):
                    paras[-1] = _before_last_word(paras[-1], made)
                else:
                    paras[-1] = f"{paras[-1].rstrip()} {made}"
            else:
                pending.append(figure_block(it, caption))
        else:
            # the paragraph positions the reading records are facts for
            # assembly's measurements, never page text (`positions`)
            text = POSITION.sub("", texts.get(it.name, ""))
            begins = text.lstrip().startswith(PARA)
            # a ¶ is a signal, never page text: only the leading one means
            # anything (the reading begins a paragraph); one written anywhere
            # else — before a new article, mid-column (vol 24 p236) — goes
            text = re.sub(rf"(?m)^[ \t]*{PARA}[ \t]*$\n?", "", text)
            text = re.sub(rf"^\s*{PARA}[ \t]*", "", text).strip()
            text, got = take_footnotes(text)     # placed at their marks below
            notes += got
            text = take_furniture(text, furniture)  # set in the page's footer
            across = it.column == 0 or last_column == 0
            figs = figures_of(items, it) if _MARK_LINE.search(text) else []
            add(text, continues=not begins and not across, figs=figs)
            last_column = it.column
            i += len(figs)                       # its figures are placed by its marks
        i += 1
    boundary()
    body, _ = place_sections(PARAGRAPH_SEP.join(paras), sections or [])
    if opens:
        # the page opens a paragraph: said as Wikisource says it, so the
        # build's page join is a paragraph break, not a run-on
        # (`preprocess.stream_with_keys`)
        body = PARAGRAPH_ENDS_WITH_PAGE + body
    body, left = place_footnotes(body, notes)
    if left:
        # a note with no single mark on the page (continued from the page
        # before) stays as printed: small type, at the page's foot
        block = _SMALL["smaller block"]
        body += PARAGRAPH_SEP + block.open + "\n".join(f"<sup>{n}</sup> {t}" for n, t in left) + block.close
    who = "proofreader" if level >= 3 else "transcriber"
    return (f'<noinclude><pagequality level="{level}" user="{who}" />{head}</noinclude>'
            + body + f"<noinclude>{''.join(furniture)}<references/></noinclude>")
