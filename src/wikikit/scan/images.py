"""A supplied page carries the IMAGES of the page it replaces.

A supplied page replaces a page's TEXT.  Its images are source: Wikisource's
page sets each printed figure either as a file from the wiki store
(``[[File:…]]``) or as a region of the volume's scan an editor marked out
(``{{Css image crop|…}}``).  Replacing the page wholesale had dropped both,
and our own crop of the scan stood in their place — 335 figures on 152
pages, live from 2026-10-09 to the repair.  Our crop stands only for a
printed figure Wikisource's page has no image of.

Which of our figures a Wikisource image takes the place of is decided by
the PRINTED FIGURE NUMBER, the one fact both sides read off the print
(`pair`); order and an editor's region decide only what numbers cannot.
The pairing is made once and stored with the page (`provenance.images`);
assembly sets Wikisource's own markup where our crop was."""
from __future__ import annotations

import re

#: Wikisource's images in text order: a file link (or `{{raw image|file}}`,
#: the same file set bare: WEIGHING MACHINES, vol 28 pp. 469, 473), or an
#: editor's region of the scan.  An .svg file is an inline glyph (Langle.svg:
#: ⟨), not a figure.
SOURCE_IMAGE = re.compile(
    r"\[\[\s*(?:File|Image)\s*:(?P<file>[^\]|]+)[^\]]*\]\]"
    r"|\{\{\s*raw image\s*\|(?P<raw>[^{}|]+)\}\}"
    r"|(?P<css>\{\{\s*Css image crop\s*\|[^{}]*\}\})", re.I)
#: Our figure as assembly sets it, and the caption line under it.
OUR_FIGURE = re.compile(r"\[\[File:(?P<file>[^\]|]+)\|[^\]]*\]\](?P<cap>\n\{\{c\|[^\n]*)?")
#: A figure number in a file name: "Fig 15", "Fig. 15", "Tunnel, 7-8".
_NAME_NUM = re.compile(r"(?:\bFig(?:ure)?s?\.?\s*|,\s*)(\d+)(?:\s*[-–]\s*(\d+))?", re.I)
#: A caption's own figure number: "{{sc|Fig.}} 18", "Figs. 33 and 34" —
#: capital "Fig", never a caption's cross-reference ("…Gun shown in fig.
#: 78": vol 20 p. 227's Fig. 79)
_CAP_NUM = re.compile(r"\b(?:Fig|FIG)(?:ure|URE)?[sS]?\b\.?\s*(?:\}\}\s*)?\.?\s*(\d+)(?:\s*(?:[-–]|and|&)\s*(\d+))?")
_LAYOUT = {"center", "centre", "left", "right", "frameless", "thumb", "none", "border", "upright"}
#: An image set straight after `{{missing table}}`: a picture of the table
#: standing in for its transcription (vol 24 p. 936) — no figure.
_STANDS_FOR_TABLE = re.compile(r"\{\{\s*missing[ _]table\s*\}\}\s*\Z", re.I)


def without_table_pictures(text: str) -> str:
    """`text` without the pictures standing in for missing tables: what a
    reading answers for with the table itself."""
    out, last = [], 0
    for m in SOURCE_IMAGE.finditer(text):
        if _STANDS_FOR_TABLE.search(text, 0, m.start()):
            out.append(text[last:m.start()])
            last = m.end()
    return "".join(out) + text[last:]


def _figure_range(a: str, b: str) -> set[int]:
    a = int(a)
    if b and a <= int(b) <= a + 4:
        return set(range(a, int(b) + 1))
    return {a, int(b)} if b else {a}


def caption_numbers(text: str) -> set[int]:
    return {n for a, b in _CAP_NUM.findall(text or "") for n in _figure_range(a, b)}


def _param(body: str, name: str) -> int | None:
    m = re.search(rf"\|\s*{name}\s*=\s*(\d+)", body)
    return int(m.group(1)) if m else None


#: At or below this width a file is set inline as a character, not a figure
#: (FSlash45.png at 20px: vol 22 p. 663)
GLYPH_PX = 30


def _glyph(markup: str, file: str) -> bool:
    """An inline glyph, not a figure: an .svg (Langle.svg: ⟨), or a file set
    at a character's width."""
    if file.strip().lower().endswith(".svg"):
        return True
    px = re.search(r"\|\s*(\d+)\s*px\s*(?:\||\]\])", markup)
    return bool(px) and int(px.group(1)) <= GLYPH_PX


def _caption_under(after: str, lines: int = 4) -> str:
    """The caption Wikisource sets under an image: the first of the next
    `lines` lines that names a figure, before a blank line or the next image
    — for a file in a table, a row or two below ("|-{{Ts|ac}}" /
    "|{{sc|Fig.}} 2.—Imperial Standard Pound": vol 28 p. 478).  An editor's
    region takes only the line directly under it: a row of captions below
    two regions names both ("Fig. 25 … Fig. 26": vol 21 p. 508)."""
    for line in after.split("\n\n")[0].split("\n")[1:1 + lines] if after.startswith("\n") \
            else after.split("\n\n")[0].split("\n")[:lines]:
        if SOURCE_IMAGE.search(line):
            break
        if caption_numbers(line):
            return line
    return ""


def source_images(text: str) -> list[dict]:
    """Wikisource's images on a page, in text order: the markup as written,
    the printed figure numbers it names (file name, else alt text, else —
    an editor's region — the caption line under it), and an editor's
    region as shares of the page's width."""
    out = []
    for m in SOURCE_IMAGE.finditer(text):
        if m.group("file") and _glyph(m.group(0), m.group("file")):
            continue
        if _STANDS_FOR_TABLE.search(text, 0, m.start()):
            continue                           # a reading transcribes the table
        after = _caption_under(text[m.end():m.end() + 400], lines=1 if m.group("css") else 4)
        if m.group("css"):
            body = m.group("css")
            b = _param(body, "bSize")
            region = None
            if b:
                x, y = _param(body, "oLeft") or 0, _param(body, "oTop") or 0
                w, h = _param(body, "cWidth") or 0, _param(body, "cHeight") or 0
                region = [x / b, y / b, (x + w) / b, (y + h) / b]
            out.append({"markup": body, "numbers": sorted(caption_numbers(after)), "region": region})
        else:
            stem = (m.group("file") or m.group("raw")).strip().rsplit(".", 1)[0]
            nums = {n for a, b in _NAME_NUM.findall(stem) for n in _figure_range(a, b)}
            if not nums:
                alt = [p for p in m.group(0)[2:-2].split("|")[1:]
                       if not p.strip().endswith("px") and p.strip().lower() not in _LAYOUT]
                nums = caption_numbers(" ".join(alt)) or caption_numbers(after)
            out.append({"markup": m.group(0), "numbers": sorted(nums), "region": None})
    return out


def our_figures(raw_text: str, figure_files: dict[str, str], boxes: dict[str, list],
                scan_width: int | None) -> list[dict]:
    """Our figures in text order: item name, the numbers our caption names,
    the box as shares of the scan's width."""
    by_file = {v.strip(): k for k, v in figure_files.items()}
    out = []
    for m in OUR_FIGURE.finditer(raw_text):
        name = by_file.get(m.group("file").strip())
        if name is None:
            continue
        box = boxes.get(name)
        out.append({"item": name, "numbers": sorted(caption_numbers(m.group("cap"))),
                    "rel": [c / scan_width for c in box] if box and scan_width else None})
    return out


def _overlap(a: list, b: list) -> float:
    """Share of box a inside box b."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))


def _in(region: list, rel: list) -> bool:
    cx, cy = (region[0] + region[2]) / 2, (region[1] + region[3]) / 2
    return rel[0] <= cx <= rel[2] and rel[1] <= cy <= rel[3]




def pair(source: list[dict], ours: list[dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Where each of Wikisource's images goes: our item name -> the markups
    set in its place (empty: our figure is covered by an image set at an
    earlier one), and the problems that leave the page undecided.  Our
    figures not in the result are ones Wikisource's page has no image of.

      1. numbers: an image goes to the first figure of ours sharing a
         printed figure number, and covers every figure of ours whose
         numbers it holds
      2. an image numbered next to one placed, with no figure of ours of its
         own, is in the same crop (Shipbuilding 57-59: our caption named 57)
      3. what is left pairs in order when equally many — or, one figure of
         ours left, every image left is in it (Spiral: four printed
         figures, one crop)
      4. an editor's region decides only what is still open: the figures of
         ours it holds (vol 21 p. 508: figs 18 and 19 cut as one), else the
         one it overlaps most.  A region is never trusted over a number or
         over order: the DjVu and our scan are framed differently (vol 21)
      5. a numbered region over no figure of ours, its number on none of
         ours, is a printed figure our cut missed (vol 20 p. 227's Fig. 80
         beside Fig. 79): set after the image before it
    The placements must keep the page's order."""
    onums = [set(o["numbers"]) for o in ours]
    every_num = set().union(*onums) if onums else set()
    place: dict[int, int] = {}
    covered: dict[int, list[int]] = {}

    def put(i, j, cover=()):
        place[i] = j
        for k in (j, *cover):
            covered.setdefault(k, []).append(i)

    for i, s in enumerate(source):
        hits = [j for j, on in enumerate(onums) if set(s["numbers"]) & on]
        if hits:
            put(i, hits[0], hits[1:])
    # the DjVu an editor's region is measured on is framed differently from
    # our scan (vol 21): the regions already placed by number give the
    # page's offset, and every region is moved by it before its place is
    # judged.  No numbered region: no correction.
    shifts = []
    for i, j in place.items():
        r, o = source[i]["region"], ours[j]["rel"]
        if r and o and len(covered.get(j, [])) == 1:
            shifts.append(((o[0] + o[2]) - (r[0] + r[2])) / 2)
            shifts.append(((o[1] + o[3]) - (r[1] + r[3])) / 2)
    if shifts:
        dx = sorted(shifts[0::2])[len(shifts) // 4]
        dy = sorted(shifts[1::2])[len(shifts) // 4]
        source = [dict(s, region=[s["region"][0] + dx, s["region"][1] + dy,
                                  s["region"][2] + dx, s["region"][3] + dy]) if s["region"] else s
                  for s in source]
    # a region placed by its number covers every other figure of ours inside
    # it (vol 24 p. 973: one region, Fig. 108 with the body plan beside it)
    for i, j in list(place.items()):
        r = source[i]["region"]
        if r:
            for k, o in enumerate(ours):
                if k not in covered and o["rel"] and _overlap(o["rel"], r) >= 0.5:
                    covered.setdefault(k, []).append(i)
    missed = [i for i, s in enumerate(source) if i not in place and s["region"] and s["numbers"]
              and not set(s["numbers"]) & every_num
              and not any(o["rel"] and _overlap(s["region"], o["rel"]) > 0 for o in ours)]

    def fits(i, j):
        # a figure of ours whose numbers contradict the image's is not its
        # place (vol 24 p. 738: figs 17-18 are not our fig 12)
        return not source[i]["numbers"] or not onums[j] or bool(set(source[i]["numbers"]) & onums[j])

    rest = [i for i in range(len(source)) if i not in place and i not in missed]
    free = [j for j in range(len(ours)) if j not in covered and all(fits(i, j) for i in rest)]
    if rest and len(free) == 1:
        for i in rest:
            put(i, free[0])
    elif rest and len(rest) == len(free):
        for i, j in zip(rest, free):
            put(i, j)
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(source):
            n = set(s["numbers"])
            if i in place or not n or n & every_num:
                continue
            for k in sorted(place):
                if any(abs(a - b) == 1 for a in n for b in source[k]["numbers"]):
                    put(i, place[k])
                    changed = True
                    break
    for i, s in enumerate(source):
        if i in place or i in missed or not s["region"]:
            continue
        inside = [j for j, o in enumerate(ours) if o["rel"] and _overlap(o["rel"], s["region"]) >= 0.5]
        if inside:
            put(i, inside[0], inside[1:])
            continue
        best = max(((_overlap(s["region"], o["rel"]), j) for j, o in enumerate(ours) if o["rel"]),
                   default=(0.0, None))
        if best[0] > 0:
            put(i, best[1])
    for i in missed:
        prev = [k for k in sorted(place) if k < i]
        nxt = [k for k in sorted(place) if k > i]
        if prev or nxt:
            put(i, place[prev[-1]] if prev else place[nxt[0]])
    problems = [f"Wikisource's image {i + 1} ({s['markup'][:60]}) has no place on our page"
                for i, s in enumerate(source) if i not in place]
    # an editor's region placed by order must at least touch the figure it
    # is set at: order slid one place over a page where each side lacks a
    # figure the other has (vol 22 p. 663)
    for i, s in enumerate(source):
        if i in place and i not in missed and s["region"] and ours[place[i]]["rel"]                 and not _overlap(s["region"], ours[place[i]]["rel"]):
            problems.append(f"Wikisource's image {i + 1}: its region is not at our figure {ours[place[i]]['item']}")
    seq = [place[i] for i in sorted(place)]
    if seq != sorted(seq):
        problems.append(f"Wikisource's images out of our order: {seq}")
    result: dict[str, list[str]] = {}
    for j, idx in covered.items():
        result[ours[j]["item"]] = [source[i]["markup"] for i in sorted(set(idx)) if place[i] == j]
    return result, problems
