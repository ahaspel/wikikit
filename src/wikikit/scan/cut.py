"""A scanned page as reading-order items: the running head, then each column top
to bottom — text strips cut only in the white space between lines, and figures,
each with its caption and, for an inset, the text running beside it.

Pure pixels: no model is called here.  Where the figures are is decided before
(`wikikit.scan.figures`, a vision model) and passed in; this module snaps each
located figure to the ink.  The ink's own figure detector (`ink_figures`) is
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
    model and the ink disagreed about figures (column -> (model, ink) counts)."""
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


def _head_split(dark, top, bottom):
    """The running head is the first line, set off by the largest gap in the
    page's first tenth."""
    per_row = dark[top:bottom].sum(axis=1) > dark.shape[1] * 0.004
    gaps = [(e - s, s, e) for s, e in runs(~per_row[:(bottom - top) // 10]) if s > 0]
    if not gaps:
        return top
    _, s, e = max(gaps)
    return top + (s + e) // 2


def columns(dark, top, bottom, left, right) -> list[tuple[int, int]]:
    """Columns from the ink: gutters are where the ink falls far below the
    page's typical density — relative, because skew and hanging headwords leave
    some ink in them — with a hairline column rule smoothed away beside them."""
    per_col = dark[top:bottom, left:right].sum(axis=0) / max(1, bottom - top)
    width = right - left
    k = max(3, width // 150)
    smooth = np.convolve(per_col, np.ones(k) / k, mode="same")
    win = max(5, width // 120)
    floor = np.array([smooth[max(0, i - win):i + win].min() for i in range(width)])
    blank = floor < np.median(smooth) * 0.5
    gutters = [(s, e) for s, e in runs(blank) if s > width * 0.12 and e < width * 0.88]
    cuts = [left + int(s + np.argmin(smooth[s:e])) for s, e in gutters]
    edges = [left] + cuts + [right]
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b - a > width * 0.12]


def strips(dark, top, bottom, a, b, max_h=MAX_H) -> list[tuple[int, int]]:
    """Rows [top, bottom) of one column cut into strips of at most `max_h`,
    each cut in the middle of a white band — never through a line."""
    per_row = dark[top:bottom, a:b].sum(axis=1) > (b - a) * 0.003
    cuts = [top + (s + e) // 2 for s, e in runs(~per_row) if e - s >= 3]
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


def page_columns(level: Image.Image, single_column: bool = False) -> list[tuple[int, int]]:
    """The column extents of a deskewed page — one, the whole text width, when
    the book says the page is read across (`single_column`)."""
    dark = np.asarray(level.convert("L")) < INK
    _, split, bottom, left, right = _frame(dark)
    return [(left, right)] if single_column else columns(dark, split, bottom, left, right)


def plan(img: Image.Image, figures: list[dict], max_h: int = MAX_H,
         angle: float | None = None, single_column: bool = False) -> Plan:
    """The page cut into reading-order items.  `figures` are the located figures
    (`wikikit.scan.figures`), in page pixels of the DESKEWED page: each
    {"box", "caption" | None, "inset"}; an empty list is a page with none.

    `single_column`: the book says this page is read across its full width —
    a table of short independent rows (a list of writers), whose dot leaders
    inside each column look to the ink like gutters and would be cut through."""
    if angle is None:
        angle = skew_angle(img)
    dark = np.asarray(deskew(img.convert("L"), angle)) < INK
    top, split, bottom, left, right = _frame(dark)
    out = Plan(angle, [Item("head", (left, top, right, split), 0, "head")])
    cols = [(left, right)] if single_column else columns(dark, split, bottom, left, right)
    for ci, (a, b) in enumerate(cols, 1):
        mine = [f for f in figures if a <= (f["box"][0] + f["box"][2]) / 2 < b]
        seen = len(ink_figures(dark, split, bottom, a, b))
        if seen != len(mine):
            out.disagreements[ci] = (len(mine), seen)
        n = {"text": 0, "figure": 0}
        for kind, box in _column_items(dark, split, bottom, a, b, mine, max_h):
            n[kind] += 1
            out.items.append(Item(kind, tuple(int(v) for v in box), ci,
                                  f"c{ci}{'s' if kind == 'text' else 'f'}{n[kind]}"))
    return out
