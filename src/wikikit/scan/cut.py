"""A scanned page as reading-order items: the running head, any type set across
the columns above them, then each column top to bottom — text strips cut only
in the white space between lines, and figures, each with its caption and, for
an inset, the text running beside it — then any type across below them.

Pure pixels: no model is called here.  Where the figures are, and the type set
across the columns, is decided before (`wikikit.scan.figures`, a vision model)
and passed in; this module snaps each to the ink.  The ink's own figure detector (`ink_figures`) is
kept only as a second opinion: it sees heavy woodcuts and misses light line
engravings, so where it and the model disagree is a place to look, never an
answer (`Plan.disagreements`).

Why strips, not whole columns: the model reads an image of at most ~1568 px on
its long side, and a dictionary column is ~3100 px tall — whole, it arrives at
half resolution, where a double dot under a vowel is a pixel.  A strip of at
most `max_h` rows arrives near native resolution.
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
    """One reading-order piece of a page: `kind` is "head", "text" or "figure";
    `box` is (x0, y0, x1, y1) on the deskewed page; `name` is stable within the
    page ("head", "c1s3" — column 1 strip 3 —, "c2f1" — column 2 figure 1)."""
    kind: str
    box: tuple[int, int, int, int]
    column: int
    name: str


@dataclass
class Plan:
    """A cut page: the deskew applied, the items in reading order, and where the
    model and the ink disagreed (column -> (model, ink) counts): figures in a
    column; for column 0, blocks set across the columns against the columns'
    edge lines the ink sees crossing a cut (`_crossing_edges`)."""
    angle: float
    items: list[Item]
    disagreements: dict[int, tuple[int, int]] = field(default_factory=dict)


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


def _head_split(dark, top, bottom):
    """The running head is the first line of type; the head ends in the white
    after it — after a rule printed under it, which goes with the head.

    Not "the largest gap near the top": under a running head set close to its
    page, the widest gap can follow the page's FIRST LINE, and a head read as
    a running head keeps nothing else — the line would be lost.  A line is
    ink above the paper's floor nearby (a tinted scan has ink in every row),
    at least 6 pixels tall (a speck is not a line); ink standing down most of
    the page (the binding's shadow) is no line's."""
    body = dark[top:bottom] & ~(dark[top:bottom].mean(axis=0) > 0.5)
    ink = body.sum(axis=1) / dark.shape[1]
    window = np.lib.stride_tricks.sliding_window_view(np.pad(ink, FLOOR_REACH, mode="edge"),
                                                      2 * FLOOR_REACH + 1)
    lines = [(s, e) for s, e in runs(ink > window.min(axis=1) + 0.01) if e - s >= 6]

    def rule(line):                                # whole, dashed or broken: thinner than type
        s, e = line
        return e - s <= RULE_HEIGHT

    def edge(line):                                # faint, at the image's very top: the scan's edge
        s, e = line
        return top + s < dark.shape[0] * SCAN_EDGE and ink[s:e].max() < EDGE_INK

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


def strips(dark, top, bottom, a, b, max_h=MAX_H) -> list[tuple[int, int]]:
    """Rows [top, bottom) of one column cut into strips of at most `max_h`,
    each cut in the middle of a white band — never through a line.  Ink
    standing down most of the column (a scan's dark edge, a rule at the
    column's side) is no line's, and would leave no white band anywhere:
    it is set aside before rows are measured."""
    block = dark[top:bottom, a:b]
    if bottom - top > 0:
        block = block[:, ~_standing(block, 4 * _line_height(dark, top, bottom, a, b))]
    ink = block.sum(axis=1)
    per_row = ink > (b - a) * 0.003
    cuts = [top + (s + e) // 2 for s, e in runs(~per_row) if e - s >= 3]
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
    candidates = [c for c in cuts if top < c < bottom] + [bottom]
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


def _line_height(dark, top, bottom, a, b):
    rr = runs(dark[top:bottom, a:b].sum(axis=1) > (b - a) * 0.003)
    return float(np.median([e - s for s, e in rr])) if rr else 30.0


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


# ── snapping a located figure to the ink ─────────────────────────────────────
def _grow(dark, y0, y1, a, b, line, lo, hi):
    """Take in a woodcut's pale fringe and its frame: neighbouring inked runs
    that are thin (under a third of a line) or hold a long horizontal rule, and
    sit within ¾ of a line of the figure.  Type is neither."""
    rr = [(lo + s, lo + e) for s, e in runs(dark[lo:hi, a:b].any(axis=1))]

    def picture(s, e):
        widest = dark[s:e, a:b].sum(axis=1).max() / (b - a)
        return e - s <= 0.35 * line or widest >= 0.45

    for s, e in reversed([r for r in rr if r[1] <= y0]):
        if y0 - e <= 0.75 * line and picture(s, e):
            y0 = s
        else:
            break
    for s, e in [r for r in rr if r[0] >= y1]:
        if s - y1 <= 0.75 * line and picture(s, e):
            y1 = e
        else:
            break
    return y0, y1


def _inset_gutter(dark, y0, y1, side, fig_x0, fig_x1):
    """The white vertical band between an inset figure and the text beside it:
    the widest run of ink-free columns between them, or None."""
    lo, hi = (side[1], fig_x0) if side[1] <= fig_x0 else (fig_x1, side[0])
    lo, hi = min(lo, hi) - 30, max(lo, hi) + 30
    rr = runs(~dark[y0:y1, lo:hi].any(axis=0))
    if not rr:
        return None
    s, e = max(rr, key=lambda r: r[1] - r[0])
    return lo + s, lo + e


def _snap(dark, a, b, top, bottom, fig, line):
    """One located figure (rough boxes, page pixels) snapped to the ink.

    The figure's edges move OUTWARD from the model's to the nearest white row
    (a framed picture has white rows inside it), then through its pale fringe.
    An inset is split from the text beside it at the white gutter, and its
    region begins at a row white across the whole column so no line is halved.
    Returns (figure, caption | None, text beside | None, region_top, y_end)."""
    bx0, by0, bx1, by1 = fig["box"]
    if fig.get("inset"):
        side_left = (bx0 - a) > (b - bx1)          # the text runs on the wider side
        side = (a, bx0) if side_left else (bx1, b)
        g = _inset_gutter(dark, by0, by1, side, bx0, bx1)
        gx = (g[0] + g[1]) // 2 if g else (bx0 if side_left else bx1)
        crossing = dark[:, gx - 2:gx + 3].any(axis=1)
        fig_x = (gx, b) if side_left else (a, gx)
        text_x = (a, gx) if side_left else (gx, b)
    else:
        fig_x, text_x, crossing = (a, b), None, None
    white = ~dark[:, fig_x[0]:fig_x[1]].any(axis=1)
    slack = int(1.2 * line)

    def white_out(y, step, reach, allow=3):
        """The first white row from just inside the model's edge outward, at
        most `reach` rows beyond it (up when step is -1, down when +1)."""
        start = y - step * allow
        stop = y + step * reach + (step if step < 0 else 0)
        for r in range(start, stop, step):
            if top <= r < bottom and white[r]:
                return r
        return None

    w0, w1 = white_out(by0, -1, slack), white_out(by1, 1, slack)
    y0 = w0 + 1 if w0 is not None else by0
    y1 = w1 if w1 is not None else by1
    cap_top = fig["caption"][1] if fig.get("caption") else bottom
    y0, y1 = _grow(dark, y0, y1, fig_x[0], fig_x[1], line, top, min(bottom, cap_top - 2))
    pad = 3
    fig_box = (fig_x[0], max(top, y0 - pad), fig_x[1], min(bottom, y1 + pad))
    y_end = fig_box[3]

    def white_near(y, lo_off, hi_off):
        cands = [r for r in range(max(top, y + lo_off), min(bottom, y + hi_off)) if white[r]]
        return min(cands, key=lambda r: abs(r - y)) if cands else None

    cap_box = None
    if fig.get("caption"):
        _, cy0, _, cy1 = fig["caption"]
        c0, c1 = white_near(cy0, -slack // 2, slack // 3), white_near(cy1, -(slack // 3), slack // 2)
        cap_box = (fig_x[0], max(fig_box[3], c0 if c0 is not None else cy0), fig_x[1],
                   c1 if c1 is not None else cy1)
        y_end = cap_box[3]
    region_top, side_box = fig_box[1], None
    if text_x:
        whole_white = ~dark[:, a:b].any(axis=1)
        for r in range(fig_box[1], max(top, fig_box[1] - 3 * int(line)) - 1, -1):
            if whole_white[r]:
                region_top = r
                break
        end = y_end                       # the text beside runs on until a line crosses the gutter
        for s, e in runs(dark[y_end:bottom, a:b].any(axis=1)):
            s, e = y_end + s, y_end + e
            if crossing[s:e].any():
                break
            end = e
        side_box = (text_x[0], region_top, text_x[1], end)
        y_end = max(y_end, end)
    return fig_box, cap_box, side_box, region_top, y_end


def _column_items(dark, top, bottom, a, b, figs, max_h):
    """("text" | "figure", box) for one column, in reading order: strips down to
    each figure, the figure, its caption, the text beside an inset, then on."""
    line = _line_height(dark, top, bottom, a, b)
    items, y = [], top
    for fig in sorted(figs, key=lambda f: f["box"][1]):
        fig_box, cap_box, side_box, region_top, y_end = _snap(dark, a, b, y, bottom, fig, line)
        items += [("text", (a, s, b, e)) for s, e in strips(dark, y, region_top, a, b, max_h)]
        items.append(("figure", fig_box))
        if cap_box:
            items.append(("text", cap_box))
        if side_box:
            items.append(("text", side_box))
        y = y_end
    items += [("text", (a, s, b, e)) for s, e in strips(dark, y, bottom, a, b, max_h)]
    return items


def _frame(dark):
    """(top, head split, bottom, left, right) of the page's text block."""
    top, bottom = _body_rows(dark)
    cols_any = np.where(dark[top:bottom].sum(axis=0) > (bottom - top) * 0.002)[0]
    return top, _head_split(dark, top, bottom), bottom, int(cols_any.min()), int(cols_any.max()) + 1


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


def _bounds(dark, split, bottom, left, right, across, reach):
    """Where the columns begin and end: the type the model saw set across them
    (`figures.locate_answer`) is no column's — a block above the body's middle
    puts the columns' top below it, one below puts their foot above it, each
    edge snapped to the white beside the block.  A block in the running head
    is the head's.  The ink measures nothing here: telling a title from a row
    that runs up to the column rule is the model's judgement."""
    begin, end = split, bottom
    mid = (split + bottom) / 2
    for x0, y0, x1, y1 in across:
        centre = (y0 + y1) / 2
        if centre < split:
            continue
        if centre < mid:
            begin = max(begin, _white_row(dark, y1 - 3, 1, left, right, reach, split, bottom))
        else:
            end = min(end, _white_row(dark, y0 + 3, -1, left, right, reach, split, bottom))
    return begin, max(begin, end)


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


def page_columns(level: Image.Image, n_columns: int | None = None) -> list[tuple[int, int]]:
    """The column extents of a deskewed page — as many as the book says it
    prints (`n_columns`), else as the ink shows (`columns`)."""
    dark = np.asarray(level.convert("L")) < INK
    _, split, bottom, left, right = _frame(dark)
    return columns(dark, split, bottom, left, right, n_columns)


def plan(img: Image.Image, figures: list[dict], max_h: int = MAX_H,
         angle: float | None = None, n_columns: int | None = None,
         across: list | tuple = ()) -> Plan:
    """The page cut into reading-order items.  `figures` are the located figures
    (`wikikit.scan.figures`), in page pixels of the DESKEWED page: each
    {"box", "caption" | None, "inset"}; an empty list is a page with none.

    `across`: the boxes of type set across the columns, as the same look
    located them (`figures.locate_answer`) — read across, as column 0, before
    and after the columns (`_bounds`).
    `n_columns`: how many columns the book says this page prints (`columns`) —
    for a page whose ink misleads the gutter finder; None, as the ink shows."""
    if angle is None:
        angle = skew_angle(img)
    dark = np.asarray(deskew(img.convert("L"), angle)) < INK
    top, split, bottom, left, right = _frame(dark)
    out = Plan(angle, [Item("head", (left, top, right, split), 0, "head")])
    line = _line_height(dark, split, bottom, left, right)
    begin, end = _bounds(dark, split, bottom, left, right, across, int(2 * line))
    cols = columns(dark, begin, end, left, right, n_columns)
    seen = _crossing_edges(dark, begin, end, left, right, [a for a, _ in cols[1:]])
    if seen:
        out.disagreements[0] = (len(across), seen)
    # type across the columns is read across, column 0: above them first, below them last
    above = [Item("text", (left, s, right, e), 0, f"c0s{k}")
             for k, (s, e) in enumerate(strips(dark, split, begin, left, right, max_h), 1)]
    below = [Item("text", (left, s, right, e), 0, f"c0s{k}")
             for k, (s, e) in enumerate(strips(dark, end, bottom, left, right, max_h), len(above) + 1)]
    out.items += above
    for ci, (a, b) in enumerate(cols, 1):
        mine = [f for f in figures if a <= (f["box"][0] + f["box"][2]) / 2 < b]
        seen = len(ink_figures(dark, begin, end, a, b))
        if seen != len(mine):
            out.disagreements[ci] = (len(mine), seen)
        n = {"text": 0, "figure": 0}
        for kind, box in _column_items(dark, begin, end, a, b, mine, max_h):
            n[kind] += 1
            out.items.append(Item(kind, tuple(int(v) for v in box), ci,
                                  f"c{ci}{'s' if kind == 'text' else 'f'}{n[kind]}"))
    out.items += below
    # An item with no area is not an item: a page with no gap under its first
    # line (a title page) has no separate running head, and a band can close
    # where it opened.  Nothing on the page lies in it.
    out.items = [it for it in out.items if it.box[2] > it.box[0] and it.box[3] > it.box[1]]
    return out
