"""Where are a page's figures?  A vision model answers in two looks:

1. the whole page, reduced, with a labelled grid: is there any illustration,
   which column, inset or not, roughly where, and its caption.  Measured on 40
   Century pages (2026-10-02): every page with figures found, no false page,
   all 67 figures found — so a page it calls empty costs one call and is done;
2. per figure, a full-resolution window gridded in ITS OWN pixels: the exact
   edges (the model reads positions off a grid well; converting coordinates
   itself it does not — that is done here).

The boxes are then snapped to the ink by `wikikit.scan.cut`.  Works on the
DESKEWED page, so figures and cuts share coordinates.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

from wikikit.scan.model import ask_json

PAGE_LONG_EDGE = 1500
GRID_PAGE, GRID_WINDOW = 100, 50

PAGE_PROMPT = """This is a scanned page, reduced to {w} x {h} pixels. A blue grid
is drawn over it every {g} pixels, each line labelled with its pixel
coordinate: read positions off the grid.

List every ILLUSTRATION on it: a picture, engraving, woodcut, diagram or map —
anything drawn rather than set in type. Not text, not rules or borders, not
decorative initials. For each, give:
- "box": [x0, y0, x1, y1] in this image's pixels, tight around the drawing
  itself, including any letters or labels drawn inside it, but NOT its caption;
- "caption": [x0, y0, x1, y1] around its caption text, or null if it has none;
- "column": which text column it sits in (1 = leftmost);
- "inset": true if running text continues BESIDE the drawing on the same lines
  (the drawing occupies only part of the column's width), else false.

Answer with JSON only: {{"figures": [ ... ]}} — an empty list if there are none."""

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


def locate_figures(page: Image.Image) -> list[dict]:
    """The first look: rough figures in page pixels ([] for a page with none)."""
    small = page.convert("RGB").copy()
    small.thumbnail((PAGE_LONG_EDGE, PAGE_LONG_EDGE))
    scale = page.width / small.width
    data = ask_json(_gridded(small, GRID_PAGE),
                    PAGE_PROMPT.format(w=small.width, h=small.height, g=GRID_PAGE))

    def up(box):
        return [round(v * scale) for v in box] if box else None

    return [{"box": up(f["box"]), "caption": up(f.get("caption")),
             "column": f.get("column"), "inset": bool(f.get("inset"))}
            for f in data.get("figures", [])]


def refine(page: Image.Image, fig: dict, column: tuple[int, int]) -> dict:
    """The close look at one figure: exact edges, in page pixels.  The rough
    box is kept as `page_box` for the record."""
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
    got = ask_json(_gridded(win, GRID_WINDOW),
                   WINDOW_PROMPT.format(w=win.width, h=win.height, g=GRID_WINDOW), 800)

    def to_page(box):
        x0, y0, x1, y1 = (v / shrink for v in box)
        return [round(x0 + x0w), round(y0 + lo), round(x1 + x0w), round(y1 + lo)]

    return {**fig, "page_box": fig["box"], "box": to_page(got["box"]),
            "caption": to_page(got["caption"]) if got.get("caption") else None}


def find(page: Image.Image, columns: list[tuple[int, int]]) -> list[dict]:
    """Both looks: the page's figures with exact edges.  `columns` are the
    page's column extents (`wikikit.scan.cut.columns`), for the close looks."""
    out = []
    for f in locate_figures(page):
        cx = (f["box"][0] + f["box"][2]) / 2
        col = next(((a, b) for a, b in columns if a <= cx < b),
                   (min(a for a, _ in columns), max(b for _, b in columns)))
        out.append(refine(page, f, col))
    return out
