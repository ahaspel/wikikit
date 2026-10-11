"""A scanned page as reading-order items: the running head, any type set across
the columns above them, then each column top to bottom — read whole, or in
strips cut only in the white space between lines — then any type across below
them.  Figures are READ where they stand, inside the column or strip that holds
them, and each follows that item as the crop the page shows: nothing is cut
out of what the model reads (`_column_items`).

Pure pixels: no model is called here.  Where the figures are, and the type set
across the columns, is decided before (`wikikit.scan.figures`, a vision model)
and passed in.  The ink's own figure detector (`ink_figures`) is kept only as
a second opinion: it sees heavy woodcuts and misses light line engravings, so
where it and the model disagree is a place to look, never an answer
(`Plan.disagreements`).

Whole columns or strips is the book's to declare (`layout.PageLayout`): a
column read whole arrives scaled to what the model accepts — an EB1911 column,
1100 x 2940, at about 0.87 (Opus 5.5, 2026-10-11: five columns read, nothing
lost) — but a dictionary's small type, a double dot under a vowel, wants the
native resolution a strip of at most `max_h` rows keeps.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PIL import Image

#: A pixel darker than this (0-255) is ink.
INK = 150
#: Strips are at most this many rows: near native resolution for the model.
MAX_H = 1100


@dataclass(frozen=True)
class Item:
    """One reading-order piece of a page: `kind` is "head", "text" or "figure"
    (the crop shown for a figure the text item before it marks); `box` is
    (x0, y0, x1, y1) on the deskewed page; `name` is stable within the page
    ("head", "c1s3" — column 1 strip 3 —, "c2f1" — column 2 figure 1).
    Pages cut before figures were read in place also hold "caption" items
    ("c2c1"), read on their own; they assemble as they always did."""
    kind: str
    box: tuple[int, int, int, int]
    column: int
    name: str


@dataclass
class Plan:
    """A cut page: the deskew applied, the items in reading order, and where the
    model and the ink disagreed (column -> (model, ink) counts): figures in a
    column; for column 0, blocks set across the columns against the columns'
    edge lines the ink sees crossing a cut (`_crossing_edges`).  `crossing`:
    why the page is not set purely in columns, or empty (`_crossing`)."""
    angle: float
    items: list[Item]
    disagreements: dict[int, tuple[int, int]] = field(default_factory=dict)
    crossing: list[str] = field(default_factory=list)


def runs(mask) -> list[tuple[int, int]]:
    """(start, end) of each run of True in a 1-D boolean array."""
    m = np.concatenate([[False], np.asarray(mask, bool), [False]]).astype(int)
    d = np.diff(m)
    return [(int(s), int(e)) for s, e in zip(np.where(d == 1)[0], np.where(d == -1)[0])]


def skew_angle(img: Image.Image) -> float:
    """Degrees to rotate so text lines run level: the angle whose horizontal
    ink profile is sharpest (highest variance), on a reduced copy."""
    small = img.convert("L").copy()
    small.thumbnail((800, 1200))
    best, angle = -1.0, 0.0
    for a in np.arange(-1.5, 1.51, 0.05):
        rot = np.asarray(small.rotate(a, resample=Image.BILINEAR, fillcolor=255)) < INK
        v = rot.sum(axis=1).var()
        if v > best:
            best, angle = v, float(a)
    return round(angle, 2)


def deskew(img: Image.Image, angle: float) -> Image.Image:
    """The page rotated level; white where the rotation exposes the corners."""
    if abs(angle) < 0.05:
        return img
    fill = 255 if img.mode == "L" else (255,) * len(img.getbands())
    return img.rotate(angle, resample=Image.BICUBIC, fillcolor=fill)


def _body_rows(dark):
    per_row = dark.sum(axis=1)
    rows = np.where(per_row > dark.shape[1] * 0.004)[0]
    return int(rows.min()), int(rows.max()) + 1


#: Rows either side over which the paper's own floor of ink is the least found.
FLOOR_REACH = 50
#: A rule printed under a running head is no taller than this; a line of type
#: is (the DNB's small-capital list rows measure 28px and more).
RULE_HEIGHT = 15
#: Faint ink (less than this share of a row) beginning in this top fraction of
#: the image is the scan's edge or tint, not type: a running head, even one
#: cropped close, is darker (the faintest measured, 0.04, sits well below).
SCAN_EDGE = 0.01
EDGE_INK = 0.08
#: Type is black ink: its darkest pixels are darker than this (grey 18-64
#: measured, a running head as sparse as WAVE's "WAVE  425" included); a tinted
#: margin or the next leaf's type showing through is paper a shade darker
#: (111-140), with none.  BLACK_SHARE is the least share of a band's ink that
#: is black for the band to be type: type's measured 0.08 and up, paper's 0.00.
BLACK = INK // 2
BLACK_SHARE = 0.02


def _head_split(dark, top, bottom, black):
    """The running head is the first line of type; the head ends in the white
    after it — after a rule printed under it, which goes with the head.

    Not "the largest gap near the top": under a running head set close to its
    page, the widest gap can follow the page's FIRST LINE, and a head read as
    a running head keeps nothing else — the line would be lost.  A line is
    ink above the paper's floor nearby (a tinted scan has ink in every row),
    at least 6 pixels tall (a speck is not a line); ink standing down most of
    the page (the binding's shadow) is no line's; and a band with no black in
    it is the paper — a tinted margin, the next leaf showing through (vol 28
    p487: WEIGHING MACHINES's head lost to the margin above it) — not type."""
    shadow = dark[top:bottom].mean(axis=0) > 0.5
    body = dark[top:bottom] & ~shadow
    ink = body.sum(axis=1) / dark.shape[1]
    black_rows = (black[top:bottom] & ~shadow).sum(axis=1)
    ink_rows = body.sum(axis=1)
    window = np.lib.stride_tricks.sliding_window_view(np.pad(ink, FLOOR_REACH, mode="edge"),
                                                      2 * FLOOR_REACH + 1)
    lines = [(s, e) for s, e in runs(ink > window.min(axis=1) + 0.01) if e - s >= 6]

    def rule(line):                                # whole, dashed or broken: thinner than type
        s, e = line
        return e - s <= RULE_HEIGHT

    def edge(line):                                # at the image's very top: the scan's edge
        s, e = line
        # Ink touching the image's first row is the scan's edge however dark
        # (Anthon leaf 401: 14%) — no page prints type against the border;
        # just below it, only a faint band is.
        return top + s < dark.shape[0] * SCAN_EDGE and (top + s == 0 or ink[s:e].max() < EDGE_INK)

    def paper(line):                               # a shade, not ink: tint, show-through
        s, e = line
        return black_rows[s:e].sum() < BLACK_SHARE * max(1, ink_rows[s:e].sum())

    lines = [ln for ln in lines if not paper(ln)]
    first = next((k for k, ln in enumerate(lines) if not (rule(ln) or edge(ln))), None)
    if first is None or first + 1 >= len(lines):
        return top
    e0, n = lines[first][1], first + 1
    while n + 1 < len(lines) and rule(lines[n]):                 # the rules under the head
        e0, n = lines[n][1], n + 1
    return top + (e0 + lines[n][0]) // 2


def _ink_across(dark, top, bottom, left, right):
    """The ink across the text block, column by column: smoothed, and its
    floor — the least ink near each point, so a hairline column rule is
    smoothed away into the blank beside it."""
    per_col = dark[top:bottom, left:right].sum(axis=0) / max(1, bottom - top)
    width = right - left
    k = max(3, width // 150)
    smooth = np.convolve(per_col, np.ones(k) / k, mode="same")
    win = max(5, width // 120)
    floor = np.array([smooth[max(0, i - win):i + win].min() for i in range(width)])
    return smooth, floor


def _lines_inked(dark, top, bottom, left, right):
    """For each pixel column of the text block, the share of its lines with
    any ink there."""
    block = dark[top:bottom, left:right]
    lines = runs(block.sum(axis=1) > (right - left) * 0.003)
    if not lines:
        return np.zeros(right - left)
    return np.array([block[s:e].any(axis=0) for s, e in lines]).mean(axis=0)


#: A printed column rule runs down at least this share of the columns' height
#: unbroken; no letter, leader or justified line-end does.  Measured on the 147
#: DNB list pages (2026-10-03): rules, even broken ones, 0.20-1.00; pages
#: without a rule 0.05 at most; every cut this moved was checked by eye.
RULE_SHARE = 0.2


def _column_rule(dark, top, bottom, a, b) -> int | None:
    """The x of a printed column rule between `a` and `b`: the longest
    near-vertical run of ink (a 5px band follows a scan's slight tilt) if it
    runs down `RULE_SHARE` of the height; else None."""
    h = bottom - top
    if h <= 0 or b - a < 5:
        return None
    band = dark[top:bottom, a:b]
    best, at = 0, None
    for x in range(2, band.shape[1] - 2):
        col = band[:, x - 2:x + 3].any(axis=1)
        d = np.diff(np.concatenate(([0], col.astype(np.int8), [0])))
        s, e = np.where(d == 1)[0], np.where(d == -1)[0]
        run = int((e - s).max()) if len(s) else 0
        if run > best:
            best, at = run, a + x
    return at if best >= RULE_SHARE * h else None


def columns(dark, top, bottom, left, right, n_columns: int | None = None) -> list[tuple[int, int]]:
    """Columns from the ink: gutters are where the ink falls far below the
    page's typical density — relative, because skew and hanging headwords leave
    some ink in them.

    `n_columns`: how many columns the book says the page prints.  Then the
    gutters are not looked for everywhere but at the page's divisions — each the
    blankest place near where an equal division falls — because a table of
    short rows (a list of writers) leaves blanks inside a column, beside its dot
    leaders, that look to the ink like gutters."""
    width = right - left
    if n_columns == 1:
        return [(left, right)]
    smooth, floor = _ink_across(dark, top, bottom, left, right)
    if n_columns:
        # Not how MUCH ink, but how many lines have any: a column's leader dots
        # are sparse ink in every line, a gutter is white in almost every line.
        # A rule along the gutter, inked in every line but a few pixels wide,
        # is opened away (anything narrower than 7px goes: leader dots ALIGNED
        # line under line would go too — the DNB's are not; a book whose are
        # needs the rule told apart by the white beside it).  Then the share
        # is averaged: the blankest stretch, not the blankest pixel.
        share = _lines_inked(dark, top, bottom, left, right)
        view = np.lib.stride_tricks.sliding_window_view
        opened = view(np.pad(view(np.pad(share, 3, mode="edge"), 7).min(axis=1), 3, mode="edge"), 7).max(axis=1)
        win = max(5, width // 120)
        free = np.convolve(opened, np.ones(2 * win + 1) / (2 * win + 1), mode="same")
        cuts = []
        for i in range(1, n_columns):
            lo, hi = int(width * (i / n_columns - 0.15)), int(width * (i / n_columns + 0.15))
            rx = _column_rule(dark, top, bottom, left + lo, left + hi)
            if rx is not None:                 # the book drew the division: cut on it
                cuts.append(rx)
                continue
            near = free[lo:hi]
            s, e = min((r for r in runs(near <= near.min() + 0.01)),
                       key=lambda r: abs(lo + (r[0] + r[1]) / 2 - width * i / n_columns))
            cuts.append(left + lo + (s + e) // 2)
        edges = [left] + cuts + [right]
        return list(zip(edges[:-1], edges[1:]))
    blank = floor < np.median(smooth) * 0.5
    gutters = [(s, e) for s, e in runs(blank) if s > width * 0.12 and e < width * 0.88]
    cuts = [left + int(s + np.argmin(smooth[s:e])) for s, e in gutters]
    edges = [left] + cuts + [right]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a > width * 0.12]


def _standing(block, reach: float):
    """Which pixel columns of `block` hold a vertical run of ink longer than
    `reach` — a scan's edge, a rule: no letter's stroke is that long."""
    padded = np.vstack([np.zeros((1, block.shape[1]), bool), block, np.zeros((1, block.shape[1]), bool)])
    out = np.zeros(block.shape[1], bool)
    d = np.diff(padded.astype(np.int8), axis=0)
    for x in np.where(block.any(axis=0))[0]:
        s, e = np.where(d[:, x] == 1)[0], np.where(d[:, x] == -1)[0]
        out[x] = (e - s).max() > reach
    return out


def strips(dark, top, bottom, a, b, max_h=MAX_H, keep=()) -> list[tuple[int, int]]:
    """Rows [top, bottom) of one column cut into strips of at most `max_h`
    (None: the column whole, one strip), each cut in the middle of a white
    band — never through a line, and never inside a `keep` span (y0, y1): a
    figure and its caption, which a strip holds whole, however tall.  Ink
    standing down most of the column (a scan's dark edge, a rule at the
    column's side) is no line's, and would leave no white band anywhere:
    it is set aside before rows are measured.

    A white band beside a THIN run of ink is no place to cut: a fraction's
    bar, or an overline, belongs with what it divides or marks.  At full
    resolution the white between a display fraction's numerator and its bar
    is a band like any other, and a cut there (ALGEBRAIC FORMS, ws p670)
    gave one strip four bare numerators and the next their denominators."""
    if max_h is None:
        return [(top, bottom)] if bottom - top > 20 else []
    block = dark[top:bottom, a:b]
    line = _line_height(dark, top, bottom, a, b)
    if bottom - top > 0:
        block = block[:, ~_standing(block, 4 * line)]
    ink = block.sum(axis=1)
    per_row = ink > (b - a) * 0.003
    thin = max(3.0, line / 4)
    inked = runs(per_row)
    ends_at = {e: s for s, e in inked}         # an inked run, by the row it ends at
    starts_at = {s: e for s, e in inked}
    cuts = [top + (s + e) // 2 for s, e in runs(~per_row) if e - s >= 3
            and (s not in ends_at or s - ends_at[s] >= thin)
            and (e not in starts_at or starts_at[e] - e >= thin)]
    # Type set tight leaves no white row where descenders meet the next line's
    # ascenders.  Only where the white rows leave a stretch longer than a strip
    # may be, the least-inked row between two lines serves instead: it touches
    # a tail or two, never a line's body.
    marks = [top] + [c for c in cuts if top < c < bottom] + [bottom]
    for s, e in zip(marks, marks[1:]):
        if e - s > max_h:
            lines = runs(ink[s - top:e - top] > (b - a) * 0.02)
            cuts += [s + gs + int(np.argmin(ink[s - top + gs:s - top + ge])) for (_, gs), (ge, _)
                     in zip(lines, lines[1:]) if ge > gs]
    cuts.sort()
    candidates = [c for c in cuts if top < c < bottom
                  and not any(y0 - 3 <= c <= y1 + 3 for y0, y1 in keep)] + [bottom]
    out, start, last_ok = [], top, None
    for c in candidates:
        if c - start <= max_h:
            last_ok = c
            continue
        end = last_ok if last_ok is not None else c      # one line taller than max_h
        out.append((start, end))
        start, last_ok = end, (c if c - end <= max_h else None)
    if bottom - start > 20:
        out.append((start, bottom))
    return [(s, e) for s, e in out if e - s > 20]


#: The line pitch is sought between these, in pixels (a fleck's height below,
#: a heading set apart above).
PITCH_MIN, PITCH_MAX = 8, 300
#: A peak matching at least this share of the best is a candidate pitch.
PEAK_SHARE = 0.7


def _line_height(dark, top, bottom, a, b):
    """The pitch of the block's lines of type: the spacing at which its
    row-by-row ink best matches itself.  Lines repeat; a column rule, the
    scan's dark edge or toned paper add the same ink to every row, which the
    mean takes away; type set so close that no row between lines is white
    repeats all the same.  Measured as runs of inked rows instead, Anthon's
    leaf 401 gave one 2,080-pixel "line" (a rule in every row) and leaf 1450
    6.5 (flecks)."""
    profile = dark[top:bottom, a:b].sum(axis=1).astype(float)
    n = len(profile)
    hi = min(PITCH_MAX, n // 3)
    if hi <= PITCH_MIN or not profile.any():
        return 30.0
    profile -= profile.mean()
    ac = np.correlate(profile, profile, mode="full")[n - 1:]
    if ac[0] <= 0:
        return 30.0
    # Every multiple of the pitch matches too, and a page broken by displays
    # or set in list rows can match best at two lines or five: the pitch is
    # the FIRST peak that matches nearly as well as the best.
    window = ac[PITCH_MIN:hi]
    best = window.max()
    peaks = [i for i in range(1, len(window) - 1)
             if window[i] >= window[i - 1] and window[i] >= window[i + 1] and window[i] >= PEAK_SHARE * best]
    return float(PITCH_MIN + (peaks[0] if peaks else int(np.argmax(window))))


# ── type size: a print fact the pixels measure, the eye misses ───────────────
#: A window this tall slides down a column, a quarter of it at a step: its
#: line pitch is measured by `_line_height` (autocorrelation — the touching
#: lines of small type do not matter, as they do to a white-row count).
PITCH_WINDOW = 160
#: Smaller type: a pitch between these shares of the body type's.  EB1911,
#: 2026-10-11: body 40-41 px, small 32-33 (0.80); below, a table's rules or a
#: figure's hatching, which have no line pitch.
SMALL_TYPE_SHARE = (0.65, 0.9)


def _pitches(dark, box) -> list[tuple[int, float]]:
    x0, y0, x1, y1 = box
    step = PITCH_WINDOW // 4
    return [(y, _line_height(dark, y, y + PITCH_WINDOW, x0, x1))
            for y in range(y0, max(y0 + 1, y1 - PITCH_WINDOW), step)]


#: The body type is the largest pitch at least this share of a page's windows
#: share (within 2 px): a heading or a display is too rare to pass for it, and
#: a page mostly in small type still shows its body type in a few paragraphs.
BODY_SHARE = 0.05


def small_type(level: Image.Image, boxes) -> list[list[tuple[int, int]]]:
    """For each column box, (y0, y1) of each run of SMALLER type in it (page
    pixels).  The body type is the PAGE's (`BODY_SHARE`) — a column mostly in
    small type (vol 18 p296's second) is measured against the page's body,
    not its own commonest pitch; a window whose pitch is a share of the body's
    in `SMALL_TYPE_SHARE` is small type.  Measured, not judged: the model,
    reading or judging, took a column's opening lines in body type for small
    (vol 18 p296) and missed small type running on at a column's head (vol 26
    p169, vol 24 p251)."""
    dark = np.asarray(level.convert("L")) < INK
    per = [_pitches(dark, b) for b in boxes]
    usable = sorted(p for ps in per for _, p in ps if p >= 15)
    if not usable:
        return [[] for _ in boxes]
    need = BODY_SHARE * len(usable)
    body = max(p for p in usable if sum(1 for q in usable if abs(q - p) <= 2) >= need)
    lo, hi = SMALL_TYPE_SHARE
    step = PITCH_WINDOW // 4
    out = []
    for ps in per:
        runs: list[tuple[int, int]] = []
        for y, p in ps:
            if not lo * body <= p <= hi * body:
                continue
            a, b = y + step, y + PITCH_WINDOW - step    # the window's middle: where it is sure
            if runs and a <= runs[-1][1] + step:
                runs[-1] = (runs[-1][0], b)
            else:
                runs.append((a, b))
        box = boxes[len(out)]
        out.append([(_edge(dark, box, a, body, rising=True), _edge(dark, box, b, body, rising=False))
                    for a, b in runs if b - a >= 2 * step])
    return out


#: An edge between sizes, found to within a few pixels: a window about two
#: lines tall slides across the coarse edge (± REACH), `FINE_STEP` at a time.
FINE_WINDOW, FINE_STEP, REACH = 80, 6, 120


def _edge(dark, box, y, body, rising: bool) -> int:
    """Where the small type really begins (`rising`) or ends near `y`: the
    first centre, scanning down, at which a narrow window's pitch turns small
    (or back to body).  The coarse window places an edge only to a line or
    two (vol 18 p296: "Rain and Snow Gauge", in body type, fell a line inside
    the small run); a bar a line too long invites the very mismarking it is
    there to prevent."""
    x0, y0, x1, y1 = box
    lo, hi = SMALL_TYPE_SHARE
    half = FINE_WINDOW // 2
    for c in range(max(y0 + half, y - REACH), min(y1 - half, y + REACH), FINE_STEP):
        p = _line_height(dark, c - half, c + half, x0, x1)
        small = lo * body <= p <= hi * body
        if small == rising and p >= 15:
            return c
    return y




def type_pitches(level: Image.Image, boxes) -> tuple[float, float] | None:
    """(body pitch, small pitch) of a page by `small_type`'s rule — the body
    the page's (`BODY_SHARE`), the small the median of the windows in
    `SMALL_TYPE_SHARE` of it (the body's share, when none are) — or None for a
    page with no measurable type."""
    dark = np.asarray(level.convert("L")) < INK
    usable = sorted(p for b in boxes for _, p in _pitches(dark, b) if p >= 15)
    if not usable:
        return None
    need = BODY_SHARE * len(usable)
    body = max(p for p in usable if sum(1 for q in usable if abs(q - p) <= 2) >= need)
    lo, hi = SMALL_TYPE_SHARE
    small = [p for p in usable if lo * body <= p <= hi * body]
    return body, (float(np.median(small)) if small else (lo + hi) / 2 * body)


#: A paragraph is measured over its first lines only — at most this many
#: pixels from where it begins: a paragraph's opening is text, and a display
#: further on would disturb the pitch.
PARAGRAPH_PROBE = 200
#: A measured pitch within this many pixels of the body's or the small's is
#: that size; between, the paragraph is not measured.
PITCH_TOLERANCE = 3


def paragraph_type(level: Image.Image, box, starts: list[int], pitches) -> list[str | None]:
    """Each paragraph's type, "body" or "small", MEASURED: its line pitch over
    its own first lines, from `starts` (page y, where the reading records each
    paragraph begins) — never a column's, never the eye's.  A paragraph too
    short to measure before the next begins (two may share a tick) is measured
    a `PARAGRAPH_PROBE` on from its start; still unmeasured, None — the caller
    decides by the book's rules (a paragraph continued across a column keeps
    its size; a display takes the size of the text before it).  Checked
    against the print on five paragraphs (EB1911, 2026-10-11): all five right,
    where the model reading and the model judging each erred."""
    if pitches is None:
        return [None] * len(starts)
    body, small = pitches
    x0, y0, x1, y1 = box
    dark = np.asarray(level.convert("L")) < INK
    out: list[str | None] = []
    for k, y in enumerate(starts):
        nxt = starts[k + 1] if k + 1 < len(starts) else y1
        a = max(y, y0)
        b = min(nxt, a + PARAGRAPH_PROBE, y1)
        if b - a < 2.5 * small:
            b = min(a + PARAGRAPH_PROBE, y1)
        pitch = _line_height(dark, a, b, x0, x1) if b - a >= 2.5 * small else None
        out.append("body" if pitch is not None and abs(pitch - body) <= PITCH_TOLERANCE else
                   "small" if pitch is not None and abs(pitch - small) <= PITCH_TOLERANCE else None)
    return out


#: The margin added to the left of the image the model reads: white, never on
#: the column itself, so nothing drawn there can touch the print.  In it, a
#: tick every TICK_EVERY page pixels labelled with its page y (the reading
#: records where each paragraph begins by these), and a mid-grey bar beside
#: measured small type (never ink: INK is darker).
MARGIN_PX, TICK_EVERY = 72, 100
TYPE_BAR_PX, TYPE_BAR_GREY = 8, 170


def margin_marks(crop: Image.Image, box, runs) -> Image.Image:
    """The image the model reads: the column, and to its left a white margin
    holding page-y ticks (`TICK_EVERY`) and a grey bar beside each run of
    small type (`small_type`) — measured facts shown where they are, so the
    reading can record them and the judge check them."""
    from PIL import ImageDraw
    img = Image.new("RGB", (crop.width + MARGIN_PX, crop.height), "white")
    img.paste(crop.convert("RGB"), (MARGIN_PX, 0))
    d = ImageDraw.Draw(img)
    top = box[1]
    for y in range((top // TICK_EVERY + 1) * TICK_EVERY, box[3], TICK_EVERY):
        r = y - top
        d.line((MARGIN_PX - 26, r, MARGIN_PX - 14, r), fill=(60, 60, 60), width=2)
        d.text((2, max(0, r - 6)), str(y), fill=(60, 60, 60))
    for a, b in runs:
        d.rectangle((MARGIN_PX - TYPE_BAR_PX - 2, max(0, a - top), MARGIN_PX - 2, min(img.height, b - top)),
                    fill=(TYPE_BAR_GREY,) * 3)
    return img


# ── the ink's own opinion of where the figures are (a cross-check only) ─────
INK_FIGURE_LINES = 3      # taller than this many text lines …
INK_FIGURE_DENSITY = 0.20  # … and denser than any text (text measures ≤ ~0.16)


def ink_figures(dark, top, bottom, a, b) -> list[tuple[int, int]]:
    """(y0, y1) of each band the ink alone takes for a figure: an inked run with
    no white row in it, taller than a few lines and denser than type."""
    region = dark[top:bottom, a:b]
    rr = runs(region.sum(axis=1) > (b - a) * 0.003)
    line = float(np.median([e - s for s, e in rr])) if rr else 0.0
    return [(top + s, top + e) for s, e in rr
            if e - s > INK_FIGURE_LINES * line and region[s:e].mean() > INK_FIGURE_DENSITY]


# ── figures: read where they stand, cropped after ───────────────────────────
#: A figure's display crop reaches this many pixels past its located box.
FIGURE_PAD = 12


def _figure_span(fig) -> tuple[int, int]:
    """Rows a figure holds, its caption with it: no cut falls inside them."""
    cap = fig.get("caption") or fig["box"]
    return fig["box"][1], max(fig["box"][3], cap[3])


def _column_items(dark, top, bottom, a, b, figs, max_h):
    """("text" | "figure", box) for one column, in reading order.

    The column is read with its figures IN it: strips are cut only between
    lines and never through a figure or its caption (`strips`' `keep`), so a
    figure, its caption and the text beside it reach the model together, and
    the model marks where the figure stands (`transcribe.FIGURE_MARK`).  Each
    figure follows the strip that holds it, as the crop the page shows for
    that mark — never a hole cut in what is read.  Cutting figures OUT before
    reading lost text in every way a box can be wrong (EB1911, 2026-10): a
    box grown over type (SOUND, vol 25 p460), two figures merged over the
    paragraph between them (TIRE, vol 26 p1049), type below a caption left in
    no strip (SHIPBUILDING, vol 24 p1052), letters sliced at a box's edge."""
    keep = [_figure_span(f) for f in figs]
    cut_rows = strips(dark, top, bottom, a, b, max_h, keep=keep)
    items = []
    for n, (s, e) in enumerate(cut_rows):
        items.append(("text", (a, s, b, e)))
        last = n == len(cut_rows) - 1
        for f in sorted(figs, key=lambda f: f["box"][1]):
            cy = (f["box"][1] + f["box"][3]) / 2
            if s <= cy < e or (last and cy >= e) or (n == 0 and cy < s):
                x0, y0, x1, y1 = f["box"]
                items.append(("figure", (max(a, x0 - FIGURE_PAD), max(top, y0 - FIGURE_PAD),
                                         min(b, x1 + FIGURE_PAD), min(bottom, y1 + FIGURE_PAD))))
    return items


def _frame(grey):
    """(top, head split, bottom, left, right) of the page's text block, from
    the page's grey levels."""
    dark = grey < INK
    top, bottom = _body_rows(dark)
    cols_any = np.where(dark[top:bottom].sum(axis=0) > (bottom - top) * 0.002)[0]
    return (top, _head_split(dark, top, bottom, grey < BLACK), bottom,
            int(cols_any.min()), int(cols_any.max()) + 1)


#: A printed column rule is narrower than this in any one row; type crossing a
#: column cut is wider somewhere (a serif, a bar, a bowl).
RULE_WIDTH = 7
RULE_REACH = 12


def _white_row(dark, y, step, left, right, reach, top, bottom):
    """The first row from `y` (moving by `step`) with no line's ink across the
    text block, within `reach` rows; else `y`."""
    for r in range(y, y + step * reach, step):
        if top <= r < bottom and dark[r, left:right].sum() <= (right - left) * 0.003:
            return r
    return y


#: An item's kind, as its name spells it: c1s3, c2f1.
ITEM_LETTER = {"text": "s", "figure": "f"}

#: Two located figures overlapping by more than this share of the smaller are
#: one figure.
SAME_FIGURE = 0.5


def _distinct(figures):
    """The located figures, one per drawing: boxes that mostly overlap are
    merged into their union (a caption kept from either).  Four small rings in
    a row, located one by one, each came back from its close look as the whole
    row (EB1911 vol 22 p706): four copies of one figure, and the later copies
    cut the column into nonsense."""
    out = []
    for f in sorted(figures, key=lambda f: (f["box"][1], f["box"][0])):
        x0, y0, x1, y1 = f["box"]
        for g in out:
            gx0, gy0, gx1, gy1 = g["box"]
            ix = max(0, min(x1, gx1) - max(x0, gx0))
            iy = max(0, min(y1, gy1) - max(y0, gy0))
            smaller = min((x1 - x0) * (y1 - y0), (gx1 - gx0) * (gy1 - gy0))
            if smaller > 0 and ix * iy > SAME_FIGURE * smaller:
                g["box"] = [min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1)]
                g["caption"] = g.get("caption") or f.get("caption")
                g["inset"] = bool(g.get("inset")) or bool(f.get("inset"))   # text beside, if either saw it
                break
        else:
            out.append(dict(f))
    return out


#: A figure spans the columns only if it reaches this share of a column's
#: width into the columns on BOTH sides of a gutter.
SPAN_REACH = 0.25


def spans_columns(box, cols) -> bool:
    """Does a located figure span the columns (a band of its own, the columns
    above and below it), rather than sit in one?  Its box must reach well into
    both sides of a gutter: a column's figure whose box the model drew a few
    dozen pixels past the gutter (EB1911 vol 24 p1051) is that column's — read
    as spanning, it took the other column's text into its image and the page
    below it was read across both columns at once."""
    x0, _, x1, _ = box
    for (a0, a1), (b0, b1) in zip(cols, cols[1:]):
        g = b0
        if g - x0 >= SPAN_REACH * (a1 - a0) and x1 - g >= SPAN_REACH * (b1 - b0):
            return True
    return False


def _crossing_a_gutter(across, cols):
    """The model's boxes of type set across the columns that do cross one.  A
    box inside a single column is that column's, whatever the model called it:
    Anthon's page number at the foot of column 2 (ws leaf 1457), taken for type
    across, put the columns' foot at its top edge — a cut through column 1's
    last line, read twice.  The columns here are the whole body's, cut before
    the across type is set aside; one column has no gutter to cross."""
    gutters = [a for a, _ in cols[1:]]
    if not gutters:
        return list(across)
    return [b for b in across if any(b[0] < g < b[2] for g in gutters)]


def _blocks_across(dark, split, bottom, left, right, across, spanning, reach):
    """The blocks set across the columns, top to bottom, as (top, bottom,
    figure | None): the type the model saw across them (`figures.locate_answer`)
    and the figures whose boxes cross a gutter — each edge snapped to the white
    beside it, overlapping blocks merged.  A block in the running head is the
    head's.  The ink measures nothing here: telling a title from a row that runs
    up to the column rule is the model's judgement."""
    blocks = []
    for x0, y0, x1, y1 in across:
        if (y0 + y1) / 2 >= split:
            blocks.append((y0, y1, None))
    for f in spanning:
        y0, y1 = f["box"][1], max(f["box"][3], (f.get("caption") or f["box"])[3])
        blocks.append((y0, y1, f))
    out = []
    for y0, y1, f in sorted(blocks, key=lambda b: b[0]):
        top = _white_row(dark, y0 + 3, -1, left, right, reach, split, bottom)
        end = _white_row(dark, y1 - 3, 1, left, right, reach, split, bottom)
        # No white row between the block and the body's edge (a footnote on
        # the last line): the block runs to that edge.
        if top == y0 + 3 and y0 - split <= reach:
            top = split
        if end == y1 - 3 and bottom - y1 <= reach:
            end = bottom
        # Overlapping, or too near to leave room for columns between them (a
        # line or two: the heading of Anthon's family tree, between its title
        # and its drawing): one block, read across.
        if out and top < out[-1][1] + 3 * reach // 2:
            t, e, g = out[-1]
            out[-1] = (t, max(e, end), g or f)
        else:
            out.append((max(top, split), min(end, bottom), f))
    return out


def _crossing_edges(dark, begin, end, left, right, cuts) -> int:
    """The ink's second opinion on the type across the columns: of the
    columns' first and last lines, how many cross a column cut (a printed
    rule along it is not type).  A title the model missed would — unless a word
    space falls on the cut; so may a row that runs up to the rule.  Weak, and
    meant to be: a disagreement is for a person to look at, never an answer."""
    lines = [(s, e) for s, e in runs(dark[begin:end, left:right].sum(axis=1) > (right - left) * 0.02)
             if e - s >= 6]
    n = 0
    for s, e in {lines[0], lines[-1]} if lines else ():
        for x in cuts:
            window = dark[begin + s:begin + e, x - RULE_REACH:x + RULE_REACH + 1]
            if any(e2 - s2 >= RULE_WIDTH and s2 <= RULE_REACH + 3 and e2 > RULE_REACH - 3
                   for row in window for s2, e2 in runs(row)):
                n += 1
                break
    return n


def _crossing(across, spanning, gutter) -> list[str]:
    """Why a page is not set purely in columns — every sign, none ranked: type
    the model located across them, a figure spanning them, or what the model
    saw crossing a gutter when asked (`figures.gutter`).  Empty for a page of
    columns.  The ink cannot answer this: measured on 16 EB1911 pages checked
    by eye (2026-10-11), a stroke-through-the-gutter test missed drawings in
    thin or broken lines (vol 24 p1000, p1038, vol 26 p1047), and ink near the
    gutter rated a plain page (TIRE, vol 26 p1049) above one a drawing crosses
    — a speck, a drawing's edge beside the gutter and a stroke across it are
    the same pixels to a threshold.  The model's look named all 16 right."""
    why = []
    if across:
        why.append(f"type across the columns ({len(across)})")
    if spanning:
        why.append(f"a figure across the columns ({len(spanning)})")
    for g in gutter:
        why.append(f"across the gutter: {g.get('kind', '?')} ({g.get('where', '?')}): {g.get('detail', '')}")
    return why




def page_columns(level: Image.Image, n_columns: int | None = None) -> list[tuple[int, int]]:
    """The column extents of a deskewed page — as many as the book says it
    prints (`n_columns`), else as the ink shows (`columns`)."""
    grey = np.asarray(level.convert("L"))
    _, split, bottom, left, right = _frame(grey)
    return columns(grey < INK, split, bottom, left, right, n_columns)


def plan(img: Image.Image, figures: list[dict], max_h: int | None = MAX_H,
         angle: float | None = None, n_columns: int | None = None,
         across: list | tuple = (), gutter: list | tuple = ()) -> Plan:
    """The page cut into reading-order items.  `figures` are the located figures
    (`wikikit.scan.figures`), in page pixels of the DESKEWED page: each
    {"box", "caption" | None, "inset"}; an empty list is a page with none.
    `max_h`: the tallest strip, or None to read each column whole.

    `across`: the boxes of type set across the columns, as the same look
    located them (`figures.locate_answer`) — read across, as column 0.
    `gutter`: what the model saw crossing a gutter (`figures.gutter`), for
    `Plan.crossing`; it moves no cut.
    `n_columns`: how many columns the book says this page prints (`columns`) —
    for a page whose ink misleads the gutter finder; None, as the ink shows.

    The page is cut into BANDS down its height: each block set across the
    columns — type the model saw across them, or a figure whose box crosses a
    gutter — is a band of its own, read across as column 0; between them, each
    band is cut into its own columns, read column by column.  A title at the
    top and a footnote at the foot are such blocks; so is a family tree in the
    middle of a page (Anthon leaf 1450), with columns above it and below."""
    if angle is None:
        angle = skew_angle(img)
    grey = np.asarray(deskew(img.convert("L"), angle))
    dark = grey < INK
    top, split, bottom, left, right = _frame(grey)
    out = Plan(angle, [Item("head", (left, top, right, split), 0, "head")])
    line = _line_height(dark, split, bottom, left, right)
    figures = _distinct(figures)
    # The gutters are where the TYPE parts: measured on the rows no figure
    # holds.  A drawing near or across the gutter fills it, and measured
    # through the drawing the gutter moved into a column (EB1911 vol 24
    # p1052, 127 px: column 2's first letters cut off down the page).
    type_ink = dark.copy()
    for f in figures:
        y0, y1 = _figure_span(f)
        type_ink[max(0, y0 - FIGURE_PAD):y1 + FIGURE_PAD] = False

    def columns_of(y0, y1):
        ink = type_ink if type_ink[y0:y1, left:right].any() else dark
        return columns(ink, y0, y1, left, right, n_columns)

    whole = columns_of(split, bottom)
    gutters = [a for a, _ in whole[1:]]
    across = _crossing_a_gutter(across, whole)
    spanning = [f for f in figures if spans_columns(f["box"], whole)]
    in_column = [f for f in figures if f not in spanning]
    blocks = _blocks_across(dark, split, bottom, left, right, across, spanning, int(2 * line))
    counts: dict[tuple[int, str], int] = {}
    model_ink: dict[int, list[int]] = {}

    def add(kind, box, ci):
        key = (ci, kind)
        counts[key] = counts.get(key, 0) + 1
        out.items.append(Item(kind, tuple(int(v) for v in box), ci,
                              f"c{ci}{ITEM_LETTER[kind]}{counts[key]}"))

    def columned(y0, y1):
        if y1 - y0 < line or not (dark[y0:y1, left:right].sum(axis=1) > (right - left) * 0.003).any():
            return
        cols = columns_of(y0, y1)
        model_ink.setdefault(0, [0, 0])[1] += _crossing_edges(dark, y0, y1, left, right, [a for a, _ in cols[1:]])
        for ci, (a, b) in enumerate(cols, 1):
            mine = [f for f in in_column if a <= (f["box"][0] + f["box"][2]) / 2 < b
                    and y0 <= (f["box"][1] + f["box"][3]) / 2 < y1]
            mi = model_ink.setdefault(ci, [0, 0])
            mi[0] += len(mine)
            mi[1] += len(ink_figures(dark, y0, y1, a, b))
            for kind, box in _column_items(dark, y0, y1, a, b, mine, max_h):
                add(kind, box, ci)

    y = split
    for b0, b1, fig in blocks:
        columned(y, b0)
        for kind, box in _column_items(dark, b0, b1, left, right, [fig] if fig else [], max_h):
            add(kind, box, 0)
        y = max(y, b1)
    columned(y, bottom)
    model_ink.setdefault(0, [0, 0])[0] = len(across)
    for ci, (model, ink) in model_ink.items():
        if (ci == 0 and ink) or (ci > 0 and model != ink):
            out.disagreements[ci] = (model, ink)
    out.crossing = _crossing(across, spanning, gutter)
    # An item with no area is not an item: a page with no gap under its first
    # line (a title page) has no separate running head, and a band can close
    # where it opened.  Nothing on the page lies in it.
    out.items = [it for it in out.items if it.box[2] > it.box[0] and it.box[3] > it.box[1]]
    return out
