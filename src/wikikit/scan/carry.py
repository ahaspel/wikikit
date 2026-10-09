"""The fetched page's editorial markup, carried onto the book's own
transcription of it.

A supplied page replaces a fetched one, but Wikisource's proofreaders had put
markup on the old page that is not print and no reading of the scan can
recover: the SECTION tags that name each article on the page (an article's
stable id hashes its section name — without them 170 EB1911 articles on
replaced pages took new ids, their URLs broken) and the LINKS laid on printed
cross-references ("see {{1911link|Stereo-isomerism}}": 179 of them, about 200
resolved links between articles).  The printed words are on both pages; the
markup goes back onto ours where those words are found.

The marks are taken from the fetched page once (`marks`) and kept in the
supplied page's provenance; `lay_on` lays them on whenever the page is
assembled, so the readings — what the transcriber read and a proofreader
corrects — stay the scan's alone.  A mark whose words cannot be found is
returned unplaced, never dropped silently."""
from __future__ import annotations

import html
import re
import unicodedata

from wikikit.pipeline.stages.elements._tables import _SHOULDER_HEADING_RE
from wikikit.pipeline.stages.extract_contributors import unwrap_templates
from wikikit.wikitext import split_top_pipes, template_end

_SECTION = re.compile(r'<section\s+(begin|end)\s*=\s*"?([^"/>]+?)"?\s*/>')
_LINK = re.compile(r"\{\{\s*(1911link|11link|EB1911 lkpl|EB1911 link|EB1911 article link)\s*\|([^{}]*)\}\}"
                   r"|\[\[(?!\s*(?:File|Image|Category)\s*:)([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", re.I)
#: Link templates whose FIRST positional argument is the printed words
#: (`{{EB1911 article link|onyx|Onyx}}`); the others put them last
#: (`{{1911link|Thermodynamics|thermodynamics}}`).  Either is filed the other
#: way round often enough (the engine's count: 2,291 of 5,016) that the other
#: argument is tried too.
_DISPLAY_FIRST = {"eb1911 article link"}
_NAMED_ARG = re.compile(r"^\s*[A-Za-z][\w ]*=")
_BOLD = re.compile(r"'''(.+?)'''", re.S)
_NOINCLUDE = re.compile(r"<noinclude>[\s\S]*?</noinclude>")
#: A shoulder heading, printed in the margin beside a paragraph: Wikisource
#: sets it on its own line where it stands, between two lines of the text.
# The engine's own answer to "which templates are a shoulder heading" — the
# EB1911 family, {{EB9 Margin Note}} (VARIATIONS, CALCULUS OF has seven): the
# classifier's opening, not a second list to drift from it.
_SHOULDER_OPEN = _SHOULDER_HEADING_RE


def _shoulders(body: str):
    """(start, end, heading) of each shoulder heading, its close found by the
    balanced scan (`template_end`) — the heading is its last argument and may
    hold templates of its own."""
    for m in _SHOULDER_OPEN.finditer(body):
        end = template_end(body, m.start())
        if end is None:
            continue                     # never closes: left as found, not guessed
        yield m.start(), end, split_top_pipes(body[m.end():end - 2])[-1]
#: Our transcriber's own rendering of one, having no shoulder construct: an
#: emphasised run standing at the start of a paragraph.
_LEAD_EMPHASIS = re.compile(r"(?:^|(?<=\n))('{2,5})((?:[^'\n]|'(?!')){2,120}?)\1[ \t]*")   # "De Moivre's"
#: An emphasised run anywhere in the text.
_EMPHASIS = re.compile(r"('{2,5})((?:[^'\n]|'(?!')){2,120}?)\1")
#: Words either side of a shoulder heading that anchor it in our text.
ANCHOR_WORDS = 4


def _headword_after(body: str, i: int) -> str | None:
    """A section's headword: its first bold run, past whatever is set before
    it — spaces, a template however deep ({{…{{…}}…}}), a tag, a link's
    target ([[Author:Ezra Stiles|'''STILES, EZRA''']]) — or None if the
    section opens with words, not a bold headword."""
    n = len(body)
    while i < n:
        if body[i].isspace():
            i += 1
        elif body.startswith("{{", i):
            depth = 0
            while i < n:
                if body.startswith("{{", i):
                    depth, i = depth + 1, i + 2
                elif body.startswith("}}", i):
                    depth, i = depth - 1, i + 2
                    if depth == 0:
                        break
                else:
                    i += 1
        elif body.startswith("<section", i):
            # the section ended before any headword: it is headless (vol 26
            # p464, TARTAGLIA's tail in fine print) — the next bold run is the
            # NEXT section's
            return None
        elif body[i] == "<":
            end = body.find(">", i)
            i = n if end < 0 else end + 1
        elif body.startswith("[[", i) and "|" in body[i:body.find("]]", i) if "]]" in body[i:] else n]:
            i = body.index("|", i) + 1
        elif body.startswith("'''", i):
            m = _BOLD.match(body, i)
            return m.group(1) if m else None
        else:
            return None
    return None


def _words(s: str) -> str:
    """What a stretch of wikitext shows: a template by its last argument (the
    words it displays: {{sc|X}}, {{small-caps|X}}, an author template's
    printed name), entities decoded, quotes and tags gone, spaces single."""
    for _ in range(4):
        s = unwrap_templates(s)
    s = re.sub(r"\[\[[^\[\]|]*\|([^\[\]]*)\]\]|\[\[([^\[\]]*)\]\]", lambda m: m.group(1) or m.group(2) or "", s)
    s = re.sub(r"\{\{[^{}]*\}\}|<[^>]+>|'''|''", "", html.unescape(s))
    return " ".join(s.replace("\xa0", " ").split())


def _letters(s: str) -> str:
    """The letters of a headword, to compare two transcriptions of it:
    markup, accents, case and punctuation aside."""
    s = unicodedata.normalize("NFKD", _words(s))
    return "".join(c for c in s if c.isalpha()).upper()


def _pattern(words: str) -> re.Pattern:
    """The words, whatever space a transcription puts between them — and
    whatever emphasis opens or closes there ("'''VARIATIONS, CALCULUS OF,'''
    in": the marks' words are plain)."""
    return re.compile(r"(?:'{2,5})?\s+(?:'{2,5})?".join(re.escape(w) for w in words.split()), re.I)


def marks(fetched: str) -> list[dict]:
    """The fetched page's section tags, links and shoulder headings, in page
    order: each {"kind": "begin" | "end" | "link" | "shoulder", "markup": as
    written, and the anchor: "headword" (a section's first bold words, or ""
    where the section opens without one — the tail of an article from the
    page before), "text" (a link's displayed words), or for a shoulder heading
    "letters" (its own) with "before" and "after" (the words either side of it
    in the text)}."""
    body = _NOINCLUDE.sub(lambda m: " " * len(m.group(0)), fetched)
    found = []
    spans = list(_shoulders(body))

    def without_shoulders(a, b):
        text, at = [], a
        for s, e, _ in spans:
            if e > a and s < b:
                text.append(body[at:max(at, s)])
                at = max(at, e)
        text.append(body[at:b] if at < b else "")
        return " ".join(text)

    running = _letters(without_shoulders(0, len(body)))
    for s, e, heading in spans:
        before = _words(without_shoulders(max(0, s - 400), s)).split()
        after = _words(without_shoulders(e, e + 400)).split()
        letters = _letters(heading)
        found.append({"kind": "shoulder", "markup": body[s:e], "pos": s, "letters": letters,
                      "before": " ".join(before[-ANCHOR_WORDS:]), "after": " ".join(after[:ANCHOR_WORDS]),
                      # its words are also running text on the fetched page: then an
                      # emphasised run of them in ours may be that text, not the note
                      "in_text": letters in running})
    for m in _SECTION.finditer(body):
        mark = {"kind": m.group(1), "markup": m.group(0), "pos": m.start()}
        if m.group(1) == "begin":
            head = _headword_after(body, m.end())
            mark["headword"] = _letters(head) if head else ""
        found.append(mark)
    for m in _LINK.finditer(body):
        markup = m.group(0)
        if m.group(1) is not None:                     # a link template: its positional arguments
            args = [a for a in split_top_pipes(m.group(2)) if not _NAMED_ARG.match(a)]
            if not args:
                continue
            order = args if m.group(1).lower() in _DISPLAY_FIRST else args[::-1]
        else:                                          # [[target|shown]] / [[target]]
            order = [m.group(4), m.group(3)] if m.group(4) is not None else [m.group(3)]
        raw_shown = order[0]
        words = _words(raw_shown)
        if words != raw_shown.strip() and m.group(1) is None:
            # the link shows marked-up words ({{small-caps|Pianoforte}}, I.&nbsp;Scientific):
            # it shows OUR words for them, whatever markup our reading gives them
            markup = markup[:len(markup) - 2 - len(raw_shown)] + words + markup[-2:]
        elif words != raw_shown.strip():
            markup = markup.replace(raw_shown, words, 1)
        alt = _words(order[1]) if len(order) > 1 else ""
        found.append({"kind": "link", "markup": markup, "pos": m.start(), "text": words,
                      "alt": alt if alt and alt != words else ""})
    found.sort(key=lambda x: x["pos"])
    for x in found:
        del x["pos"]
    return found


def lay_on(raw_text: str, page_marks: list[dict] | None) -> tuple[str, list[dict]]:
    """`raw_text` with the marks laid on, and the marks that could not be.
    Section begins go before their headword's bold run (a headword-less one at
    the top of the page), each end where it stood — just before the next
    begin, or at the page's foot; links replace the first occurrence of their
    words after the last mark placed; a shoulder heading goes on a line of its
    own after the words it followed (or before those it preceded), and our own
    emphasised rendering of it at a paragraph's start is taken out."""
    if not page_marks:
        return raw_text, []
    head_end = raw_text.find("</noinclude>")
    start = head_end + len("</noinclude>") if head_end >= 0 else 0
    foot = raw_text.rfind("<noinclude>")
    foot = foot if foot > start else len(raw_text)
    body = raw_text[start:foot]
    # our own rendering of a shoulder heading the fetched page carries — an
    # emphasised run with its letters at a paragraph's start — gives way to it
    # — leaving a mark where it stood, the place to put the heading if the
    # fetched page's words around it are not ours (its OCR garbled a formula)
    # — and anywhere, mid-sentence where it sits beside its line (VARIATIONS'
    # "made by ''Origin of the Calculus.'' mathematicians"), when the fetched
    # page has those words only as the note, never as running text
    shoulders = {m["letters"] for m in page_marks if m["kind"] == "shoulder"}
    only_notes = {m["letters"] for m in page_marks if m["kind"] == "shoulder" and not m.get("in_text")}
    stood = lambda e, keep: f"{_STOOD}{_letters(e.group(2))}{_STOOD}" if _letters(e.group(2)) in keep else e.group(0)
    if shoulders:
        body = _LEAD_EMPHASIS.sub(lambda e: stood(e, shoulders), body)
    if only_notes:
        body = _EMPHASIS.sub(lambda e: stood(e, only_notes), body)
    bold_runs = list(_BOLD.finditer(body))
    out, cursor, pending, unplaced = [], 0, [], []
    # where the last begin was placed: a headword opens one section, so the
    # next begin looks past it — else SEWERAGE, whose letters start with
    # SEWER's, takes SEWER's run and leaves SEWER's section empty
    opened = -1
    for i, mark in enumerate(page_marks):
        if mark["kind"] == "end":
            pending.append(mark["markup"])
            continue
        if mark["kind"] == "begin":
            if not mark["headword"]:
                at = cursor if cursor == 0 or not out else None
            else:
                at = None
                # the page's bold runs, found from its top: a cursor left inside a
                # run (a link placed on a headword) must not pair its closing quotes
                for b in (b for b in bold_runs if b.start() >= cursor and b.start() > opened):
                    if _letters(b.group(1)).startswith(mark["headword"][:20]) or \
                            mark["headword"].startswith(_letters(b.group(1))[:20] or "\0"):
                        at = b.start()
                        break
            if at is None:
                unplaced.append(mark)
                continue
            out.append(body[cursor:at])
            out.append("".join(pending) + mark["markup"])
            pending = []
            cursor = at
            if mark["headword"]:        # a headless begin opens no headword
                opened = at
            continue
        if mark["kind"] == "shoulder":
            at = _shoulder_at(body, cursor, mark)
            if at is None:
                unplaced.append(mark)
                continue
            out.append(body[cursor:at])
            out.append("\n" + mark["markup"] + "\n")
            cursor = at
            continue
        m = _pattern(mark["text"]).search(body, cursor) if mark["text"] else None
        if m is None and mark.get("alt"):              # filed the other way round
            m = _pattern(mark["alt"]).search(body, cursor)
        if m is None:
            unplaced.append(mark)
            continue
        out.append(body[cursor:m.start()])
        out.append(mark["markup"])
        cursor = m.end()
    out.append(body[cursor:])
    out.append("".join(pending))
    laid = re.sub(f"{_STOOD}[A-Z]*{_STOOD}", "", "".join(out))
    return raw_text[:start] + laid + raw_text[foot:], unplaced


#: Where our own rendering of a shoulder heading stood, while marks are laid.
_STOOD = "\x07"


def _shoulder_at(body: str, cursor: int, mark: dict) -> int | None:
    """Where a shoulder heading goes in our text: after the words it followed
    on the fetched page, or before those it preceded; failing both (the
    fetched page's OCR garbled the words around it), where our own reading
    set it; failing that, after or before the two nearest words."""
    def outside(m, end):
        # never inside emphasis: past the quotes closing the words before it,
        # back over those opening the words after it (VARIATIONS' headword,
        # '''VARIATIONS, CALCULUS OF,''' — the note goes before the ''')
        at = m.end() if end else m.start()
        if end:
            while at < len(body) and body[at] == "'":
                at += 1
        else:
            while at > cursor and body[at - 1] == "'":
                at -= 1
        return at

    for words, end in ((mark["before"], True), (mark["after"], False)):
        if words and (m := _pattern(words).search(body, cursor)):
            return outside(m, end)
    stood = body.find(f"{_STOOD}{mark['letters']}{_STOOD}", cursor)
    if stood >= 0:
        return stood
    for words, end in ((" ".join(mark["before"].split()[-2:]), True), (" ".join(mark["after"].split()[:2]), False)):
        if words and (m := _pattern(words).search(body, cursor)):
            return outside(m, end)
    return None
