"""What the replaced page has that a reading cannot make, carried onto ours:
its TABLES and its LINKS (its images: `wikikit.scan.images`).

A reading is better than Wikisource's page at words and formulas, and worse at
what a person built on that page.  Shipping our page alone lost Wikisource's
tables (borders, spans, headers) from 36 articles and its links from 26
(2026-10-12).  So our page takes Wikisource's, at the place the two pages'
WORDS agree on:

  tables  every Wikisource table replaces what ours has between the words
          around it — our table, or our prose reading of the table.  Wikisource's
          table always wins (a decision: 2026-10-12) — except a table that only
          lays out a formula ours sets as <math> (`formula_layout`), or figures
          ours already sets.
  links   every Wikisource link outside a table is laid on our words for its
          display — Wikisource's markup when the words agree, its target on our
          words when ours read them differently.

Places come from an alignment of the two pages' words, markup masked
(`word_tokens`).  An element whose place the words cannot fix — anchors not
found, or a span that would take anything but the element's own words
(a footnote, an image, a section, prose) — is never guessed at: it is listed
unplaced, and the gate fails the page until it is resolved."""
from __future__ import annotations

import difflib
import re
from collections import Counter

from wikikit.scan.patches import balanced
from wikikit.scan.vocabulary import TABLE_CLOSES, TABLE_OPENS

#: Link forms carried, by opener: `[[target|display]]`, and the templates the
#: walker reads as links (`_classifier`: target-first, and display-first).
_TARGET_FIRST = re.compile(r"\{\{\s*(?:EB1911\s+lkpl|1911link|11link|EB1911\s+link)\s*\|", re.I)
_DISPLAY_FIRST = re.compile(r"\{\{\s*EB1911\s+article\s+link\s*\|", re.I)
#: A table carried may take, outside the table itself, at most this many of
#: Wikisource's words lying between it and the words that place it.
STRETCH = 12
#: A table carried may lack at most this share of our words at its place
#: (hyphenation, OCR); measured 2026-10-12: carried tables lack <=18%, an
#: unfinished Wikisource table 35%.
MAX_LOST = 0.25
#: Why an element is unplaced when the words either side of it do not agree.
NO_ANCHOR = " (the words around it do not agree)"
#: Markup a span may begin by closing, or end by opening: an edge of an
#: edit placed by its words cuts through it (PYRIDINE, vol 22 p. 690: the
#: next paragraph's `{{smaller block|` taken with the table, its `}}` left).
_CLOSERS = re.compile(r"(?:\}\}|\]\]|</[a-zA-Z]+\s*>|(?<=\n)\|\}|\s)+")
_OPENERS = re.compile(r"(?:\{\{[^{}|\n]*\||<[a-zA-Z]+[^<>/]*>|\[\[[^\[\]|\n]*\||\s)+$")
_PAIRS = (("{{", "}}"), ("[[", "]]"))
_MARKUP_EDGE = re.compile(r"[{}\[\]<>|]")
#: html elements that pair: an OCR "<Q'" or "<j>" is none
_HTML = {"math", "sub", "sup", "span", "div", "small", "big", "b", "i", "u", "s", "center", "ref", "poem",
         "table", "tr", "td", "th", "section", "noinclude", "includeonly", "p", "blockquote", "font", "tt",
         "code", "pre", "ol", "ul", "li", "dl", "dd", "dt", "references", "score", "chem", "ce", "sc"}


def markup_balance(s: str) -> tuple:
    """What `s` opens and does not close, or closes and did not open — of
    templates, links, tables and html elements (TeX inside <math> is not
    markup; a table opens and closes at a line's start: the `|}}` of
    `{{block center|<math>…</math>}}` closes none).  Two spans with the same
    balance can stand for one another."""
    s = re.sub(r"(?s)<math[^>]*>.*?</math>", "", s)
    s = re.sub(r"<[^<>]*/>", "", s)
    net = Counter({o: s.count(o) - s.count(c) for o, c in _PAIRS})
    net["{|"] = len(TABLE_OPENS.findall(s)) - len(TABLE_CLOSES.findall(s))
    # a tag's name ends at a space, a slash or its close: OCR's `<s- s>` is text
    for m in re.finditer(r"<(/?)([a-zA-Z]+)(?=[\s/>])[^<>]*>", s):
        if m.group(2).lower() in _HTML:
            net["<" + m.group(2).lower()] += -1 if m.group(1) else 1
    return tuple(sorted((k, v) for k, v in net.items() if v))


def _inner(text: str, x: int, y: int) -> tuple[int, int]:
    """x..y less the closers it begins with and the openers it ends with."""
    m = _CLOSERS.match(text, x, y)
    x = m.end() if m else x
    m = _OPENERS.search(text[x:y])
    return x, (x + m.start() if m else y)


def tables(text: str) -> list[tuple[int, int]]:
    """Each table's span — wikitext `{|`…`|}` and HTML `<table>`…`</table>` —
    outermost only (a nested table travels inside its own)."""
    spans = sorted(_wiki_tables(text) + _html_tables(text))
    out = []
    for a, b in spans:
        if out and a < out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
            continue
        out.append((a, b))
    return out


def _html_tables(text: str) -> list[tuple[int, int]]:
    out, i = [], 0
    while (m := re.compile(r"<table\b", re.I).search(text, i)):
        depth, j = 0, m.start()
        for t in re.compile(r"<(/?)table\b[^>]*>", re.I).finditer(text, m.start()):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                j = t.end()
                break
        else:
            j = len(text)
        out.append((m.start(), j))
        i = j
    return out


def _wiki_tables(text: str) -> list[tuple[int, int]]:
    out, i = [], 0
    while (i := text.find("{|", i)) >= 0:
        if i and text[i - 1] != "\n":
            i += 2
            continue
        depth, j = 0, i
        while j < len(text):
            if text.startswith("{|", j) and (j == 0 or text[j - 1] == "\n"):
                depth += 1
            elif text.startswith("|}", j) and (j == 0 or text[j - 1] == "\n"):
                depth -= 1
                if depth == 0:
                    j += 2
                    break
            j += 1
        out.append((i, j))
        i = j
    return out


def links(text: str) -> list[dict]:
    """Each link: its span, its markup, and the span of its display words."""
    out = []
    for m in re.finditer(r"\[\[|\{\{", text):
        i = m.start()
        if any(a < i < b for a, b, *_ in ((x["start"], x["end"]) for x in out)):
            continue
        if text.startswith("[[", i):
            j = balanced(text, i, "[[", "]]")
            body = text[i + 2:j - 2]
            if re.match(r"\s*(?:File|Image|Category)\s*:", body, re.I):
                continue
            bar = body.find("|")
            d0 = i + 2 + (bar + 1 if bar >= 0 else 0)
            out.append({"start": i, "end": j, "markup": text[i:j], "d": (d0, j - 2), "form": "bracket"})
        elif _TARGET_FIRST.match(text, i) or _DISPLAY_FIRST.match(text, i):
            j = balanced(text, i, "{{", "}}")
            parts = _params(text, i, j)
            if not parts:
                continue
            if _DISPLAY_FIRST.match(text, i):
                d, form = parts[0], "display-first"
            else:
                d, form = (parts[1] if len(parts) > 1 else parts[0]), "target-first"
            out.append({"start": i, "end": j, "markup": text[i:j], "d": d, "form": form})
    return out


def _params(text: str, i: int, j: int) -> list[tuple[int, int]]:
    """The spans of a template's unnamed parameters (nesting respected)."""
    out, k, start, depth = [], i + 2, None, 0
    while k < j - 2:
        if text.startswith("{{", k) or text.startswith("[[", k):
            depth += 1
            k += 2
            continue
        if text.startswith("}}", k) or text.startswith("]]", k):
            depth -= 1
            k += 2
            continue
        if text[k] == "|" and depth == 0:
            if start is not None:
                out.append((start, k))
            start = k + 1
        k += 1
    if start is not None:
        out.append((start, j - 2))
    return [(a, b) for a, b in out if not re.match(r"\s*[\w -]+\s*=", text[a:b])]


#: Templates whose parameters are no words of the page: cell styles, spaces,
#: braces, a placeholder image's file name.
_NO_WORDS = re.compile(r"\{\{\s*(?:ts|em|gap|brace\d?|raw image)\s*\|[^{}]*\}\}", re.I)
#: A table attribute written without quotes (`colspan=2`, `width=22%`).
_ATTR = re.compile(r"\b(?:colspan|rowspan|width|height|align|valign|class|border|cellpadding"
                   r"|cellspacing|bgcolor)\s*=\s*[^\s|\"]+", re.I)


def _spaces(s: str) -> str:
    return re.sub(r"[^\n]", " ", s)


def _blank(m: re.Match) -> str:
    return _spaces(m.group(0))


def mask(text: str) -> str:
    """`text` with markup blanked, offsets kept: what is left is the page's
    words.  Formulas are blanked whole — ours writes TeX, Wikisource text."""
    t = text
    h = t.find("</noinclude>")
    if h >= 0 and t.startswith("<noinclude>"):
        t = " " * (h + 12) + t[h + 12:]
    t = re.sub(r"<math[\s\S]*?</math>", _blank, t)
    t = re.sub(r"\[\[\s*(?:File|Image)\s*:[^\]]*\]\]", _blank, t, flags=re.I)
    t = re.sub(r"\{\{\s*Css image crop[\s\S]*?\}\}", _blank, t, flags=re.I)
    t = re.sub(_NO_WORDS, _blank, t)
    t = re.sub(r"<noinclude>[\s\S]*?</noinclude>", _blank, t)
    t = re.sub(r"\b[\w-]+\s*=\s*\"[^\"\n]*\"", _blank, t)
    t = re.sub(_ATTR, _blank, t)
    t = re.sub(r"<[^>\n]*>", _blank, t)
    for l in links(text):                      # a link reads as its display
        (s, e), d0, d1 = (l["start"], l["end"]), *l["d"]
        t = t[:s] + _spaces(t[s:d0]) + t[d0:d1] + _spaces(t[d1:e]) + t[e:]
    t = re.sub(r"\{\{[^|{}]*\|?", _blank, t)
    t = re.sub(r"\[\[[^|\]]*\|", _blank, t)
    t = re.sub(r"\]\]|\[\[|\}\}|\{\||\|\}|\|-|\|\||!!|'{2,}", _blank, t)
    return t


def word_tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).lower(), m.start(), m.end())
            for m in re.finditer(r"[^\W\d_]+|\d+", mask(text))]


def _align(a: list, b: list) -> dict[int, int]:
    """a's token index -> b's, for tokens in a run of at least two that agree."""
    sm = difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=False)
    out = {}
    for blk in sm.get_matching_blocks():
        if blk.size >= 2:
            for k in range(blk.size):
                out[blk.a + k] = blk.b + k
    return out


def _body(text: str) -> tuple[int, int]:
    h = text.find("</noinclude>")
    s = h + 12 if text.startswith("<noinclude>") and h >= 0 else 0
    f = text.rfind("<noinclude>")
    return s, (f if f > s else len(text))


def _anchors(wt, ot, amap, a, b, ours_text, nearest=False):
    """Our span for Wikisource's span a..b: from the end of our word matching
    the last agreed word before it to the start of ours matching the first
    after it.  `nearest`: the nearest AGREED words, however far (the caller
    bounds what lies between); else the adjacent words, which must agree.
    None when an anchor is not agreed."""
    before = [k for k, x in enumerate(wt) if x[2] <= a and (k in amap or not nearest)]
    after = [k for k, x in enumerate(wt) if x[1] >= b and (k in amap or not nearest)]
    pk = before[-1] if before else None
    nk = after[0] if after else None
    if (pk is not None and pk not in amap) or (nk is not None and nk not in amap):
        return None
    s, e = _body(ours_text)
    x = ot[amap[pk]][2] if pk is not None else s
    y = ot[amap[nk]][1] if nk is not None else e
    wa = wt[pk][2] if pk is not None else a
    wb = wt[nk][1] if nk is not None else b
    return x, y, wa, wb, (pk, nk)


_FORBIDDEN = re.compile(r"<section|\{\{RunningHeader", re.I)
#: A table's opening line declares a ruled table: borders, rules, a class.
_RULED = re.compile(r"border|rules\s*=|class\s*=", re.I)
#: The words a display-formula line of ours holds outside its formulas
#: ("from equation (2)", "But", a name and year) — at most this many.
FORMULA_LINE_WORDS = 4
#: Our grid of a chemical structure: a formula set in cells, as ours sets
#: OXIMES' and PURIN's (user, 2026-10-11: "our chem grids stand").
_CHEM_GRID = re.compile(r"""\{\|[^\n]*class\s*=\s*["']?chem-grid""")


def _table_head(table: str) -> str:
    if table.startswith("{|"):
        return table.split("\n", 1)[0]
    m = re.match(r"<table[^>]*>", table, re.I)
    return m.group(0) if m else ""


def _row_cells(table: str) -> list[int]:
    """The number of cells in each row of a table (nested tables not entered)."""
    if table.startswith("{|"):
        rows, n = [], 0
        for ln in table.split("\n")[1:]:
            s = ln.lstrip()
            if s.startswith(("|-", "|}")):
                rows.append(n)
                n = 0
            elif s[:1] in "|!" and not s.startswith("|+"):
                n += s.count("||") + s.count("!!") + 1
        return [r for r in rows + [n] if r]
    return [len(re.findall(r"<t[dh]\b", r, re.I)) for r in re.split(r"<tr\b", table, flags=re.I)[1:]]


def box(table: str) -> bool:
    """An unruled table of one cell to a row lays out lines — a caption, a
    list — and has no table in it to carry."""
    return not _RULED.search(_table_head(table)) and max(_row_cells(table) or [0]) <= 1


def formula_layout(table: str, chunk: str) -> bool:
    """Is Wikisource's `table` a layout of what ours sets as formulas?
    Wikisource builds displayed equations from table cells (FUNCTION's
    products, aligned systems, lists of formulas); ours sets them as <math>,
    which does that job better (a decision: 2026-10-12).  Such a table is
    unruled and brings no image, and ours at its place is formulas: lines
    holding at most FORMULA_LINE_WORDS words outside their formulas, or a
    table of ours setting in <math> more than Wikisource's does — "tables are
    just a poor choice for handling these, and math markup does the job much
    better", the borderless ones lacking styling anyway (2026-10-13:
    THERMOELECTRICITY's list of formulas, each with its author and year)."""
    if _RULED.search(_table_head(table)) or _image_names(table):
        return False
    if _CHEM_GRID.search(chunk):
        return True                            # our grid of a chemical formula
    if "<math" not in chunk:
        return False
    if tables(chunk):
        return chunk.count("<math") > table.count("<math")
    return all(len(word_tokens(ln)) <= FORMULA_LINE_WORDS for ln in chunk.split("\n"))


def _notes(text: str) -> tuple[int, list[str]]:
    """A span's footnotes: how many carry their text, and the names of those
    that point at text set elsewhere (`<ref name=x/>`)."""
    bare = re.findall(r"<ref\b([^>]*)/>", text)
    names = sorted(re.sub(r"[\"'\s]", "", re.sub(r"(?i)^\s*name\s*=", "", b)) for b in bare)
    return len(re.findall(r"<ref\b[^>]*[^/]>|<ref>", text)), names


def _words_outside_notes(text: str) -> Counter:
    return Counter(w for w, *_ in word_tokens(re.sub(r"<ref\b[^>/]*>[\s\S]*?</ref>", "", text)))


def _image_names(text: str) -> set[str]:
    """The images a span sets: wiki-store files (linked, or set bare by
    `{{raw image|…}}`) and scan regions, by name."""
    files = {(link or raw).strip().replace("_", " ").lower()
             for link, raw in re.findall(r"\[\[\s*(?:File|Image)\s*:([^|\]]+)|\{\{\s*raw image\s*\|([^{}|]+)\}\}",
                                         text, re.I)}
    regions = {re.sub(r"\s+", "", m) for m in re.findall(r"\{\{\s*Css image crop([\s\S]*?)\}\}", text, re.I)}
    return files | regions


def _our_table(ours: str, ot: list, amap: dict, own: list[int]) -> tuple[int, int] | None:
    """Our span for a Wikisource table whose words are `own`: our table (or
    tables, one after another) holding at least half its agreed words, each
    at least half made of them, with the lines next to it whose words are all
    the table's (its caption, set apart in ours).  None when ours has no such
    table."""
    mapped = {amap[k] for k in own if k in amap}
    if not mapped:
        return None
    hits = []
    for s, e in tables(ours):
        mine = [i for i, x in enumerate(ot) if s <= x[1] and x[2] <= e]
        n = sum(i in mapped for i in mine)
        if mine and n >= 0.5 * len(mine):
            hits.append((s, e, n))
    if not hits or sum(n for *_, n in hits) < 0.5 * len(mapped):
        return None

    def theirs_only(lo: int, hi: int) -> bool:
        words = [i for i, x in enumerate(ot) if lo <= x[1] and x[2] <= hi]
        return all(i in mapped for i in words) and not _FORBIDDEN.search(ours[lo:hi]) \
            and not _image_names(ours[lo:hi])

    for (_, e1, _), (s2, _, _) in zip(hits, hits[1:]):
        if not theirs_only(e1, s2):
            return None                        # ours has other matter between
    x, y = hits[0][0], hits[-1][1]
    while True:                                # lines before, all the table's
        lo = len(ours[:x].rstrip())            # over blank lines
        p = ours.rfind("\n", 0, lo) + 1
        if lo <= p or not word_tokens(ours[p:lo]) or not theirs_only(p, lo):
            break
        x = p
    while True:                                # lines after
        hi = len(ours) - len(ours[y:].lstrip())
        n = ours.find("\n", hi)
        n = len(ours) if n < 0 else n
        if n <= hi or not word_tokens(ours[hi:n]) or not theirs_only(hi, n):
            break
        y = n
    return x, y


def carry_tables(ours: str, ws: str) -> tuple[str, dict]:
    """Ours with Wikisource's tables in place.
    -> (text, {"seen", "carried", "kept", "unplaced"}).  `kept`: {"formulas",
    "figures", "boxes", "incomplete"}: spans of Wikisource's tables ours stands against —
    layouts of what ours already sets (formulas, `formula_layout`; figures
    whose images stand in ours, carried as images; lines whose words ours has,
    `box`), and our own table left unfinished on Wikisource (lacking more than
    MAX_LOST of its words).

    A table goes where ours has the same table — matched by the table's own
    words (`_our_table`) — else between the agreed words around it."""
    kept = {"formulas": [], "figures": [], "boxes": [], "incomplete": []}
    spans, layout = [], []
    for a, b in tables(ws):
        lays_out = bool(box(ws[a:b]) or _image_names(ws[a:b]))
        if spans and not ws[spans[-1][1]:a].strip() and not lays_out and not layout[-1]:
            spans[-1] = (spans[-1][0], b)      # tables back to back are placed as one
        else:                                  # (vol 23 p. 716: a list set as two);
            spans.append((a, b))               # a layout is judged alone
            layout.append(lays_out)
    if not spans:
        return ours, {"seen": 0, "carried": 0, "kept": kept, "unplaced": []}
    wt, ot = word_tokens(ws), word_tokens(ours)
    amap = _align(wt, ot)
    ours_words = Counter(x[0] for x in ot)
    edits, unplaced = [], []
    for a, b in spans:
        table = ws[a:b]
        imgs = _image_names(table)
        own = [k for k, x in enumerate(wt) if a <= x[1] and x[2] <= b]
        # its words in ours, wherever ours sets them: a caption box beside a
        # figure ours captions a paragraph on (vol 28 p. 473, Fig. 13)
        here = Counter(x[0] for x in wt[own[0]:own[-1] + 1]) if own else Counter()
        words_in_ours = not own or sum((here & ours_words).values()) >= 0.7 * len(own)
        if imgs and not _RULED.search(_table_head(table)) and imgs <= _image_names(ours):
            # its figures stand in ours, captioned as the reading has them
            # (`transcribe._legend_line`); carried, a float would land where
            # Wikisource set it — mid-sentence (SPIRAL, vol 25 p. 692)
            kept["figures"].append((a, b))
            continue
        if not imgs and box(table) and own and words_in_ours:
            kept["boxes"].append((a, b))      # its lines stand in ours
            continue
        label = f"table at {a}: {ws[a:a + 40]!r}"
        same = _our_table(ours, ot, amap, own)
        if same:
            (x, y), theirs = same, table
            chunk = ours[x:y]
            if formula_layout(table, chunk):
                kept["formulas"].append((a, b))   # ours sets it as a formula
                continue
        else:
            r = _anchors(wt, ot, amap, a, b, ours, nearest=True)
            if r is None:
                unplaced.append(label + NO_ANCHOR)
                continue
            x, y, wa, wb, (pk, nk) = r
            if x > y:
                unplaced.append(label + " (anchors out of order)")
                continue
            # the edges are the words' edges: the markup about them stays
            # whole, on both pages — the table alone, when what lies
            # between it and its words on Wikisource cuts through markup
            x, y = _inner(ours, x, y)
            wa, wb = _inner(ws, wa, wb)
            if markup_balance(ws[wa:a]) or markup_balance(ws[b:wb]):
                wa, wb = a, b
            chunk, theirs = ours[x:y], ws[wa:wb]
            if formula_layout(table, chunk):
                kept["formulas"].append((a, b))   # ours sets it as formulas
                continue
            stretch = ws[wa:a] + ws[b:wb]
            if len(word_tokens(stretch)) > STRETCH:
                unplaced.append(label + f" ({len(word_tokens(stretch))} words between it and its anchors)")
                continue
            if theirs.startswith(("{|", "<table")) and ours[:x].rstrip(" ") and not ours[:x].endswith("\n"):
                theirs = "\n" + theirs             # a table opens a line
            if theirs.rstrip(" ").endswith(("|}", "</table>")) and ours[y:].lstrip(" ")[:1] not in ("", "\n"):
                theirs += "\n"
        if markup_balance(chunk) != markup_balance(theirs):
            unplaced.append(label + f" (would cut through markup: ours {markup_balance(chunk)}, "
                                    f"theirs {markup_balance(theirs)})")
            continue
        mine = _words_outside_notes(chunk)          # a note's text: `_notes` below
        lost = sum((mine - _words_outside_notes(theirs)).values())
        if lost > MAX_LOST * sum(mine.values()):
            if same:                           # our table, unfinished on Wikisource
                kept["incomplete"].append((a, b))   # (". . ." for rows): ours stands
            else:
                unplaced.append(label + f" (lacks {lost} of our {sum(mine.values())} words there)")
            continue
        if _FORBIDDEN.search(chunk) or _FORBIDDEN.search(theirs):
            unplaced.append(label + " (would take a section or a head)")
            continue
        if not _image_names(chunk) <= _image_names(theirs):
            # a table laying out figures brings its own: it may take ours only
            # where ours IS its image (carried); never our own crop
            unplaced.append(label + " (would take an image it does not bring)")
            continue
        if (_image_names(theirs) - _image_names(chunk)) & _image_names(ours[:x] + ours[y:]):
            unplaced.append(label + " (would set an image ours sets elsewhere)")
            continue
        if _notes(chunk) != _notes(theirs):
            # every note ours has there must come back with its text: a
            # `<ref name=x/>` brings no text
            unplaced.append(label + f" (footnotes: ours {_notes(chunk)} here, theirs {_notes(theirs)})")
            continue
        edits.append((x, y, theirs, label))
    out, n, overlap = _make_edits(ours, edits)
    return out, {"seen": len(spans), "carried": n, "kept": kept, "unplaced": unplaced + overlap}


def _make_edits(text: str, edits: list) -> tuple[str, int, list[str]]:
    """`text` with each (x, y, new, label) edit made.  Edits that overlap in
    `text` are never guessed between: none is made, each is listed."""
    edits = sorted(edits)
    if any(x2 < y1 for (_, y1, *_), (x2, *_) in zip(edits, edits[1:])):
        return text, 0, [f"{e[3]} (overlaps another in ours)" for e in edits]
    for x, y, new, _ in reversed(edits):
        text = text[:x] + new + text[y:]
    return text, len(edits), []


def _inside_link(text: str, x: int) -> bool:
    """Is offset x inside a link of ours (any form `links` knows)?"""
    return any(l["start"] < x < l["end"] for l in links(text))


def carry_links(ours: str, ws: str) -> tuple[str, dict]:
    """Ours with Wikisource's links (outside its tables) on our words."""
    in_table = tables(ws)
    ls = [l for l in links(ws) if not any(a <= l["start"] < b for a, b in in_table)]
    if not ls:
        return ours, {"seen": 0, "carried": 0, "unplaced": []}
    wt, ot = word_tokens(ws), word_tokens(ours)
    amap = _align(wt, ot)
    edits, unplaced, already = [], [], 0
    for l in ls:
        d0, d1 = l["d"]
        disp = [k for k, x in enumerate(wt) if d0 <= x[1] and x[2] <= d1]
        label = f"link {l['markup'][:50]!r}"
        if not disp:
            unplaced.append(label + " (no words to place it by)")
            continue
        own = [amap.get(k) for k in disp]
        if None not in own and own == list(range(own[0], own[0] + len(own))):
            lo, hi = own[0], own[-1] + 1       # its own words agree: placed by them
        else:                                  # else by the words either side
            r = _anchors(wt, ot, amap, wt[disp[0]][1], wt[disp[-1]][2], ours)
            if r is None:
                unplaced.append(label + NO_ANCHOR)
                continue
            _, _, _, _, (pk, nk) = r
            lo = amap[pk] + 1 if pk is not None else 0
            hi = amap[nk] if nk is not None else len(ot)
            wpk = pk if pk is not None else -1
            wnk = nk if nk is not None else len(wt)
            if [k for k in range(wpk + 1, wnk)] != disp or not (0 < hi - lo <= len(disp) + 1):
                unplaced.append(label + " (our words there are not its display)")
                continue
        x, y = ot[lo][1], ot[hi - 1][2]
        if _inside_link(ours, x):
            already += 1                       # ours links these words already
            continue
        # what its display holds around its words ("§ 2", "Charles I.",
        # ''italics''), taken with ours where ours has it too
        # — punctuation and italics only: a template's `}}` is not the
        # display's (SPINET, vol 25 p. 685: `{{small-caps|…}}`'s close taken
        # from our `{{sc|…}}`)
        head, tail = ws[d0:wt[disp[0]][1]], ws[wt[disp[-1]][2]:d1]
        if head and not _MARKUP_EDGE.search(head) and ours[max(0, x - len(head)):x] == head:
            x -= len(head)
        if tail and not _MARKUP_EDGE.search(tail) and ours[y:y + len(tail)] == tail:
            y += len(tail)
        span = ours[x:y]
        if re.search(r"\[\[|\{\{|<|\|", span):
            unplaced.append(label + " (our words there carry markup)")
            continue
        m, rel = l["markup"], (d0 - l["start"], d1 - l["start"])
        unnamed = _params(ws, l["start"], l["end"])
        if re.sub(r"\s+", " ", span) == re.sub(r"\s+", " ", ws[d0:d1]):
            new = m
        elif l["form"] == "bracket":
            new = f"[[{m[2:-2].split('|', 1)[0]}|{span}]]"
        elif len(unnamed) > 1:                 # a display of its own: ours there
            new = m[:rel[0]] + span + m[rel[1]:]
        elif l["form"] == "target-first":      # one parameter: ours added as display
            new = m[:-2] + "|" + span + "}}"
        else:                                  # display first, one parameter: ours
            new = m[:rel[0]] + span + "|" + m[rel[0]:]   # as display, its as target
        edits.append((x, y, new, label))
    out, n, overlap = _make_edits(ours, edits)
    return out, {"seen": len(ls), "carried": n + already, "unplaced": unplaced + overlap}


class CarryCountError(RuntimeError):
    """Elements seen and elements handled differ: the carry lost one."""


def carry(ours: str, ws: str) -> tuple[str, dict]:
    """Tables, then links (a link inside a carried table came with it).
    -> (text, {"tables", "tables kept", "links", "unplaced"}).  Every table and
    every link Wikisource's page has is carried, kept or listed unplaced — or
    the carry stops (`CarryCountError`)."""
    t, tr = carry_tables(ours, ws)
    t, lr = carry_links(t, ws)
    kept = sum(len(v) for v in tr["kept"].values())
    table_unplaced = sum(u.startswith("table") for u in tr["unplaced"])
    if tr["seen"] != tr["carried"] + kept + table_unplaced:
        raise CarryCountError(f"tables: {tr['seen']} seen, {tr['carried']} carried, "
                              f"{kept} kept, {table_unplaced} unplaced")
    if lr["seen"] != lr["carried"] + len(lr["unplaced"]):
        raise CarryCountError(f"links: {lr['seen']} seen, {lr['carried']} carried, "
                              f"{len(lr['unplaced'])} unplaced")
    return t, {"tables": tr["carried"], "tables kept": tr["kept"], "links": lr["carried"],
               "unplaced": tr["unplaced"] + lr["unplaced"]}
