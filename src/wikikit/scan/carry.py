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

from wikikit.pipeline.stages.extract_contributors import unwrap_templates

_SECTION = re.compile(r'<section\s+(begin|end)\s*=\s*"?([^"/>]+?)"?\s*/>')
_LINK = re.compile(r"\{\{\s*(?:1911link|11link|EB1911 lkpl|EB1911 link)\s*\|([^{}|]*)(?:\|([^{}]*))?\}\}"
                   r"|\[\[(?!\s*(?:File|Image|Category)\s*:)([^\[\]|]*)(?:\|([^\[\]]*))?\]\]", re.I)
_BOLD = re.compile(r"'''(.+?)'''", re.S)
_NOINCLUDE = re.compile(r"<noinclude>[\s\S]*?</noinclude>")


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
    """The words, whatever space a transcription puts between them."""
    return re.compile(r"\s+".join(re.escape(w) for w in words.split()), re.I)


def marks(fetched: str) -> list[dict]:
    """The fetched page's section tags and links, in page order: each
    {"kind": "begin" | "end" | "link", "markup": as written, and the anchor:
    "headword" (a section's first bold words, or "" where the section opens
    without one — the tail of an article from the page before) or "text" (a
    link's displayed words)}."""
    body = _NOINCLUDE.sub(lambda m: " " * len(m.group(0)), fetched)
    found = []
    for m in _SECTION.finditer(body):
        mark = {"kind": m.group(1), "markup": m.group(0), "pos": m.start()}
        if m.group(1) == "begin":
            head = _headword_after(body, m.end())
            mark["headword"] = _letters(head) if head else ""
        found.append(mark)
    for m in _LINK.finditer(body):
        target, shown = (m.group(1), m.group(2)) if m.group(1) is not None else (m.group(3), m.group(4))
        raw_shown = shown if shown is not None else target
        words = _words(raw_shown)
        markup = m.group(0)
        if words != raw_shown.strip():
            # the link shows marked-up words ({{small-caps|Pianoforte}}, I.&nbsp;Scientific):
            # it shows OUR words for them, whatever markup our reading gives them
            markup = markup[:len(markup) - 2 - len(raw_shown)] + words + markup[-2:]
        found.append({"kind": "link", "markup": markup, "pos": m.start(), "text": words})
    found.sort(key=lambda x: x["pos"])
    for x in found:
        del x["pos"]
    return found


def lay_on(raw_text: str, page_marks: list[dict] | None) -> tuple[str, list[dict]]:
    """`raw_text` with the marks laid on, and the marks that could not be.
    Section begins go before their headword's bold run (a headword-less one at
    the top of the page), each end where it stood — just before the next
    begin, or at the page's foot; links replace the first occurrence of their
    words after the last mark placed."""
    if not page_marks:
        return raw_text, []
    head_end = raw_text.find("</noinclude>")
    start = head_end + len("</noinclude>") if head_end >= 0 else 0
    foot = raw_text.rfind("<noinclude>")
    foot = foot if foot > start else len(raw_text)
    body = raw_text[start:foot]
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
        m = _pattern(mark["text"]).search(body, cursor) if mark["text"] else None
        if m is None:
            unplaced.append(mark)
            continue
        out.append(body[cursor:m.start()])
        out.append(mark["markup"])
        cursor = m.end()
    out.append(body[cursor:])
    out.append("".join(pending))
    return raw_text[:start] + "".join(out) + raw_text[foot:], unplaced
