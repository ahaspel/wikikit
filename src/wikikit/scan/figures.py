"""Where are a page's figures — and its type set across the columns?  A
vision model answers in two looks:

1. the whole page, reduced, with a labelled grid: is there any illustration,
   which column, inset or not, roughly where, and its caption.  Measured on 40
   Century pages (2026-10-02): every page with figures found, no false page,
   all 67 figures found — so a page it calls empty costs one call and is done.
   The same look names the type set ACROSS the columns — a title above them,
   a footnote or a library's number below — which the ink cannot tell from a
   row that runs up to the column rule (three ink rules failed three ways).
   Measured (2026-10-03) on 75 DNB list pages: every title, footnote and
   number found, no row taken, nothing claimed on a page without; on the 40
   Century pages, nothing claimed and the figures unchanged;
2. per figure, a full-resolution window gridded in ITS OWN pixels: the exact
   edges (the model reads positions off a grid well; converting coordinates
   itself it does not — that is done here).

The boxes are then snapped to the ink by `wikikit.scan.cut`.  Works on the
DESKEWED page, so figures and cuts share coordinates.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from wikikit.scan.model import ask, parse_json

PAGE_LONG_EDGE = 1500
GRID_PAGE, GRID_WINDOW = 100, 50

PAGE_PROMPT = """This is a scanned page, reduced to {w} x {h} pixels. A blue grid
is drawn over it every {g} pixels, each line labelled with its pixel
coordinate: read positions off the grid.

List every ILLUSTRATION on it: a picture, engraving, woodcut, diagram or map —
anything drawn rather than set in type. Not text, not rules or borders, not
decorative initials. A diagram whose words are type but whose LINES are drawn
is an illustration: a family tree with its branches, a chemical formula drawn
as a ring or with slanting or curved bonds. A formula set out in rows of type,
its atoms joined only by straight upright and level bonds, is not — it is type
(like a table), and so is any table. For each illustration, give:
- "box": [x0, y0, x1, y1] in this image's pixels, tight around the drawing
  itself, including any letters or labels drawn inside it, but NOT its caption;
- "caption": [x0, y0, x1, y1] around its caption text, or null if it has none;
- "column": which text column it sits in (1 = leftmost);
- "inset": true if running text continues BESIDE the drawing on the same lines
  (the drawing occupies only part of the column's width), else false.

Then list every block of TYPE SET ACROSS THE COLUMNS: printed type that
belongs to no single column because it spans them, or is centred over them —
a title or heading above the columns, a note, footnote or number printed
below them. NOT the running head (the single top line with the page number),
NOT a line inside a column that merely runs up to the column rule, NOT an
illustration's caption. For each, give "box": [x0, y0, x1, y1] tight around
the block's type.

Answer with JSON only: {{"figures": [ ... ], "across": [ ... ]}} — empty lists
where there are none."""

WINDOW_PROMPT = """This is a crop of a scanned page, {w} x {h} pixels, around one
illustration. A blue grid is drawn every {g} pixels of THIS image, each line
labelled with its pixel coordinate in this image — read positions off the labels.

Give, in this image's pixel coordinates:
- "box": [x0, y0, x1, y1] tight around the drawing itself (its pale edges,
  shading, frame, base and any letters drawn inside it included), NOT any
  text above or below it and NOT its caption;
- "caption": [x0, y0, x1, y1] tight around its caption, or null.
Answer with JSON only: {{"box": [...], "caption": [...] or null}}"""


def _gridded(img: Image.Image, step: int) -> Image.Image:
    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    for x in range(step, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(80, 140, 255), width=1)
        d.text((x + 2, 2), str(x), fill=(30, 90, 220))
    for y in range(step, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(80, 140, 255), width=1)
        d.text((2, y + 2), str(y), fill=(30, 90, 220))
    return img


# Each look is a request built here and an answer read here, so it runs the
# same way now (`ask`) or in a batch (`wikikit.scan.batch`).
def locate_request(page: Image.Image) -> tuple[Image.Image, str, int, float]:
    """The first look's (image, prompt, max_tokens, scale to page pixels)."""
    small = page.convert("RGB").copy()
    small.thumbnail((PAGE_LONG_EDGE, PAGE_LONG_EDGE))
    return (_gridded(small, GRID_PAGE),
            PAGE_PROMPT.format(w=small.width, h=small.height, g=GRID_PAGE), 2000,
            page.width / small.width)


def locate_answer(text: str, scale: float) -> tuple[list[dict], list[list[int]]]:
    """The first look's answer, in page pixels: the rough figures, and the
    boxes of type set across the columns."""
    def up(box):
        return [round(v * scale) for v in box] if box else None
    got = parse_json(text)
    figs = [{"box": up(f["box"]), "caption": up(f.get("caption")),
             "column": f.get("column"), "inset": bool(f.get("inset"))}
            for f in got.get("figures", [])]
    return figs, [up(a["box"]) for a in got.get("across", []) if a.get("box")]


def locate_page(page: Image.Image) -> tuple[list[dict], list[list[int]]]:
    """The first look, now: rough figures and the type across the columns."""
    img, prompt, max_tokens, scale = locate_request(page)
    return locate_answer(ask(img, prompt, max_tokens), scale)


def refine_request(page: Image.Image, fig: dict, column: tuple[int, int]):
    """The close look's (image, prompt, max_tokens, window) — the window is
    what `refine_answer` needs to turn the answer into page pixels."""
    a, b = column
    bx0, by0, bx1, by1 = fig["box"]
    cap = fig.get("caption")
    lo = max(0, by0 - 350)
    hi = min(page.height, max(by1, cap[3] if cap else by1) + 350)
    x0w, x1w = max(0, a - 20), min(page.width, b + 20)
    win = page.convert("RGB").crop((x0w, lo, x1w, hi))
    shrink = min(1.0, PAGE_LONG_EDGE / max(win.size))
    if shrink < 1.0:
        win = win.resize((round(win.width * shrink), round(win.height * shrink)), Image.LANCZOS)
    return (_gridded(win, GRID_WINDOW),
            WINDOW_PROMPT.format(w=win.width, h=win.height, g=GRID_WINDOW), 800,
            {"x0": x0w, "y0": lo, "shrink": shrink})


def refine_answer(text: str, fig: dict, window: dict) -> dict:
    """The close look's answer: the figure with exact edges, in page pixels;
    the rough box kept as `page_box` for the record."""
    got = parse_json(text)

    def to_page(box):
        x0, y0, x1, y1 = (v / window["shrink"] for v in box)
        return [round(x0 + window["x0"]), round(y0 + window["y0"]),
                round(x1 + window["x0"]), round(y1 + window["y0"])]

    box = to_page(got["box"])
    rx0, ry0, rx1, ry1 = fig["box"]
    if box[2] <= rx0 or box[0] >= rx1 or box[3] <= ry0 or box[1] >= ry1:
        # the close look settled on ANOTHER drawing in its window (EB1911
        # vol 22 p706): the first look's box stands, and its caption
        return {**fig, "page_box": fig["box"]}
    return {**fig, "page_box": fig["box"], "box": box,
            "caption": to_page(got["caption"]) if got.get("caption") else None}


def refine(page: Image.Image, fig: dict, column: tuple[int, int]) -> dict:
    """The close look, now."""
    img, prompt, max_tokens, window = refine_request(page, fig, column)
    return refine_answer(ask(img, prompt, max_tokens), fig, window)


def column_of(fig: dict, columns: list[tuple[int, int]]) -> tuple[int, int]:
    """The column a figure sits in — all of them if it spans a gutter (Anthon's
    family tree, the width of the page: looked at inside one column, its crop
    lost half the tree) or its centre falls outside every column."""
    whole = (min(a for a, _ in columns), max(b for _, b in columns))
    x0, _, x1, _ = fig["box"]
    if any(x0 < a < x1 for a, _ in columns[1:]):
        return whole
    cx = (x0 + x1) / 2
    return next(((a, b) for a, b in columns if a <= cx < b), whole)


def find(page: Image.Image, columns: list[tuple[int, int]]) -> tuple[list[dict], list[list[int]]]:
    """Both looks, now: the page's figures with exact edges, and the type
    across its columns (`cut.plan` snaps those to the ink).  `columns` are
    the page's column extents (`wikikit.scan.cut.columns`), for the close
    looks."""
    rough, across = locate_page(page)
    return [refine(page, f, column_of(f, columns)) for f in rough], across
