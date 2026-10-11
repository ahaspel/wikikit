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
from wikikit.scan.cut import Item
from wikikit.scan.model import ask, request
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
    """The figure items a text item holds: those that follow it (`cut`)."""
    k = items.index(item)
    out = []
    for it in items[k + 1:]:
        if it.kind != "figure":
            break
        out.append(it)
    return out


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
    return text, check_reading(level, item, text, figures)


def check_reading(level: Image.Image, item: Item, text: str, figures: list[Item] = ()) -> str | None:
    """Why a reading is not a reading of its image, or None if it is.  A
    reading marks each figure its item holds, once: a mark short is a figure
    the page would lose, or text read as a drawing; a mark over is a drawing
    the locator never found, with no crop to show."""
    import numpy as np
    marks = len(_MARK_LINE.findall(text))
    if marks != len(figures):
        return f"{marks} figure mark(s) for {len(figures)} figure(s)"
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
    """The index just past the `}}` closing the template opened at `start`."""
    depth, i = 0, start
    while i < len(text) - 1:
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
    notes, each beginning a line — or None if the body does not open with one."""
    starts = [m for m in re.finditer(r"(?m)^[ \t]*" + _NOTE_MARK.pattern, body)]
    if not starts or body[:starts[0].start()].strip():
        return None
    out = []
    for m, nxt in zip(starts, starts[1:] + [None]):
        out.append((m.group(1), body[m.end():nxt.start() if nxt else len(body)].strip()))
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
#: A sentence ends at a full stop, query or exclamation (closing quotes or
#: brackets after it) before the next sentence's capital.
_SENTENCE_END = re.compile(r"[.!?][\"”’)\]]*(?=\s+[\"“‘(\[]?[A-Z])")
#: The text before a block already ends a sentence, or a clause the block
#: completes ("printed with:—").
_ENDS_SENTENCE = re.compile(r"[.!?:;—][\"”’)\]]*\s*$")
_WORD_BROKEN = re.compile(r"(\S+)-\s*$")
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
            cap = lines[k + 1] if k + 1 < len(lines) and lines[k + 1].lstrip().startswith("{{c|") else None
            out.append(("figure", line + ("\n" + cap if cap else "")))
            k += 2 if cap else 1
        else:
            out.append(("text", line))
            k += 1
    # a centred line printed directly above a table or verse is its title: it
    # goes where the block goes (vol 18 p296: "Marvin's Table for the
    # Reduction of Velocities…" had stayed mid-sentence when its table moved)
    centred = next(c.open for c in VOCABULARY if c.name == "c")
    merged: list[tuple[str, str]] = []
    for kind, s in out:
        if kind in ("table", "verse") and merged and merged[-1][0] == "text" \
                and merged[-1][1].strip().startswith(centred):
            merged[-1] = (kind, merged[-1][1].strip() + "\n" + s)
        else:
            merged.append((kind, s))
    return merged


def _is_anchor_line(line: str) -> bool:
    """A line that is one construct standing apart, not a display: a margin
    heading.  It goes where it stands and breaks nothing."""
    s = line.strip()
    apart = tuple(c.open for c in VOCABULARY if c.name in ("block center", "c", "smaller block"))
    return (s.startswith("{{") and s.endswith("}}") and _balanced_end(s, 0) == len(s)
            and not s.startswith(apart))


def _join_text(lines: list[str]) -> str:
    """A paragraph's text lines as one: lines kept apart by a newline (a
    margin heading on its own line, a display); a word broken at a line end
    joined across whatever stands between its halves, the margin heading set
    before the broken word ("deter-" | heading | "mining": vol 26 p169)."""
    out, held = "", []
    for line in lines:
        if not line.strip():
            continue
        if _is_anchor_line(line) and _WORD_BROKEN.search(out):
            held.append(line.strip())
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
    """A paragraph as our page sets it: ("text" | "block" | "figure", …) in
    order.  A block the print sets WITHIN the paragraph (a table, verse, an
    illustration) moves to the end of the sentence it interrupts — or stays,
    if the text before it already ends one — and the paragraph's text after
    it begins a new one.  The reading recorded where it stood; this is the
    one place that decides where it goes."""
    units = _units(piece)
    segs: list[tuple[str, str]] = []
    text: list[str] = []
    k = 0
    while k < len(units):
        kind, s = units[k]
        if kind == "text":
            text.append(s)
            k += 1
            continue
        before = _join_text(text)
        text = []
        # the text that follows, up to the next block, in which the sentence ends
        j, after = k + 1, []
        while j < len(units) and units[j][0] == "text":
            after.append(units[j][1])
            j += 1
        rest = _join_text(after)
        if rest and ((before and not _ENDS_SENTENCE.search(before)) or (not before and continues and not segs)):
            m = _SENTENCE_END.search(rest)
            cut_at = m.end() if m else len(rest)
            before = _join_words(before.rstrip(), rest[:cut_at].lstrip()) if before.rstrip().endswith("-") \
                else (before.rstrip() + " " + rest[:cut_at].lstrip()).strip()
            rest = rest[cut_at:].lstrip()
        if before:
            segs.append(("text", before))
        segs.append(("figure" if kind == "figure" else "block", s))
        text = [rest] if rest else []
        k = j
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


def _unwrapped(piece: str) -> str:
    """A piece without the whole-piece small-type wrapper the reading gave it."""
    p = piece.strip()
    o = _SMALL["smaller block"].open
    if p.startswith(o) and _balanced_end(p, 0) == len(p):
        return p[len(o):-2].strip()
    return piece


def apply_type(text: str, sizes: list | None) -> str:
    """A reading with each paragraph set in its MEASURED type (`sizes`, one per
    recorded paragraph start — `positions`): the reading's own whole-paragraph
    size marks taken off, and every piece of a paragraph measured "small"
    wrapped in small type.  A table or verse is never wrapped (a template
    cannot hold a table), nor a footnote (small by definition, placed apart).
    No sizes: the reading as it is."""
    if not sizes:
        return text
    ms = list(POSITION.finditer(text))
    if not ms:
        return text
    out = [text[:ms[0].start()]]
    block = _SMALL["smaller block"]
    for k, m in enumerate(ms):
        seg = text[m.end():ms[k + 1].start() if k + 1 < len(ms) else len(text)]
        small = k < len(sizes) and sizes[k] == "small"
        pieces = []
        for piece in seg.split(PARAGRAPH_SEP):
            if not piece.strip() or _is_note(piece) or piece.lstrip().startswith((TABLE_START, "<poem>")) \
                    or _MARK_LINE.match(piece.strip()):
                pieces.append(piece)
                continue
            p = _unwrapped(piece).strip()
            pieces.append(block.open + p + block.close if small else p)
        out.append(m.group(0) + PARAGRAPH_SEP.join(pieces).strip() + (PARAGRAPH_SEP if k + 1 < len(ms) else ""))
    return "".join(out)


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
                  sections: list[dict] | None = None, types: dict | None = None) -> str:
    """The page's wikitext from its items and their readings.

    `texts` maps item name -> the model's (or a proofreader's) reading;
    `figure_files` maps figure item name -> its image file name;
    `sections` the page's section addresses (`place_sections`); `types` item
    name -> each recorded paragraph's measured type (`apply_type`)."""
    head = ""
    paras: list[str] = []         # the page's blocks: paragraphs and figures
    open_para = False             # is the last paragraph still running?
    pending: list[str] = []       # figure blocks waiting for a paragraph boundary
    notes: list[tuple[str, str]] = []   # the page's footnotes, from its columns' feet
    furniture: list[str] = []     # the printer's marks the readings record: the footer's
    caps = captions(items, legacy=legacy_captions)

    def boundary():
        """A paragraph has ended: waiting figures take their place here."""
        nonlocal open_para
        paras.extend(pending)
        pending.clear()
        open_para = False

    def figure_block(fig: Item, caption: str = "") -> str:
        w = fig.box[2] - fig.box[0]
        px = max(60, round(w / column_width * COLUMN_DISPLAY_PX))
        return f"[[File:{figure_files[fig.name]}|center|{px}px]]" + (f"\n{caption}" if caption else "")

    def marked_figure(unit: str, figs: list[Item]) -> str:
        """A figure mark (and the caption line after it) as the crop it stands
        for; a mark with no figure left (a proofreader's): its caption alone."""
        _, _, caption = unit.partition("\n")
        return figure_block(figs.pop(0), caption.strip()) if figs else caption.strip()

    def add(text: str, continues: bool, figs: list[Item] = ()):
        """Append one reading; `continues` = it carries on the open paragraph.
        `figs`: the figure items its marks stand for, in order.  What the
        reading records, set by `_segments`: a block standing within a
        paragraph goes to the end of its sentence."""
        nonlocal open_para
        figs = list(figs)
        first = True
        for raw in text.split(PARAGRAPH_SEP):
            piece = raw.strip()
            segs = _segments(piece, continues=first and continues and open_para)
            if len(segs) == 1 and segs[0][0] == "figure":
                # a figure alone between blank lines (a reading made before
                # blocks were written where they stand): the paragraph runs
                # on past it, and the figure waits for its end
                pending.append(marked_figure(segs[0][1], figs))
                continue
            for n, (kind, s) in enumerate(segs):
                if kind in ("figure", "block"):
                    # a block is a paragraph of its own: it runs on from
                    # nothing, and nothing runs on from it (Anthon leaf 1459:
                    # a table had joined the sentence before it, "{|" mid-line)
                    boundary()
                    paras.append(marked_figure(s, figs) if kind == "figure" else s)
                    first = False
                    continue
                joins = first and n == 0 and continues and open_para
                first = False
                if not joins:
                    boundary()                  # whatever ran before has ended
                if joins:
                    paras[-1] = join_across_cut(paras[-1], s)
                else:
                    paras.append(s)
                open_para = True
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
            pending.append(figure_block(it, caption))
        else:
            # the paragraph positions the reading records are facts for
            # assembly's measurements, never page text (`positions`)
            text = POSITION.sub("", apply_type(texts.get(it.name, ""), (types or {}).get(it.name)))
            begins = text.lstrip().startswith(PARA)
            text = re.sub(rf"^\s*{PARA}[ \t]*\n?", "", text).strip()
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
    body, left = place_footnotes(body, notes)
    if left:
        # a note with no single mark on the page (continued from the page
        # before) stays as printed: small type, at the page's foot
        block = _SMALL["smaller block"]
        body += PARAGRAPH_SEP + block.open + "\n".join(f"<sup>{n}</sup> {t}" for n, t in left) + block.close
    who = "proofreader" if level >= 3 else "transcriber"
    return (f'<noinclude><pagequality level="{level}" user="{who}" />{head}</noinclude>'
            + body + f"<noinclude>{''.join(furniture)}<references/></noinclude>")
