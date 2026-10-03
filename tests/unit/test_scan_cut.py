"""wikikit.scan.cut on drawn pages: three columns of "type" (blocks a line
high, white between lines), a running head, and figures placed where a
locator would report them.  The rules the cutter promises: columns from the
gutters, strips never cut through a line, a figure is one item with its
caption and inset text separate, and the ink's second opinion is reported."""
import numpy as np
from PIL import Image, ImageDraw

from wikikit.scan.cut import INK, plan

W, H = 1800, 2600
COLS = [(60, 600), (630, 1170), (1200, 1740)]
TOP, LINE, LEAD = 140, 22, 32


def page(figures=(), light=()):
    """A page; `figures` are dense blocks (x0, y0, x1, y1) set INTO the type
    (the lines they cover are not drawn); `light` are sparse line drawings."""
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((60, 60, 400, 60 + LINE), fill=0)             # the running head
    d.rectangle((1400, 60, 1740, 60 + LINE), fill=0)
    covered = list(figures) + list(light)
    rng = np.random.default_rng(1897)
    for x0, x1 in COLS:
        for y in range(TOP, H - 120, LEAD):
            # the part of the line no figure covers (an inset leaves the rest)
            spans = [(x0, x1)]
            for fx0, fy0, fx1, fy1 in covered:
                if fy0 - LEAD < y < fy1:
                    spans = [(s, min(e, fx0 - 30)) for s, e in spans if s < fx0 - 30] + \
                            [(max(s, fx1 + 30), e) for s, e in spans if e > fx1 + 30]
            for s, e in spans:
                wx = s
                while wx < e - 20:                            # words, spaced as type is:
                    ww = int(rng.integers(25, 90))            # irregularly, so no white
                    d.rectangle((wx, y, min(e, wx + ww), y + LINE), fill=0)   # river runs
                    wx += ww + int(rng.integers(8, 16))       # down the column
    for x0, y0, x1, y1 in figures:
        for yy in range(y0, y1, 3):                           # hatching: no white row
            d.line((x0, yy, x1, yy + 2), fill=0, width=2)
    for x0, y0, x1, y1 in light:
        for k in range(0, x1 - x0, 60):                       # a few thin strokes
            d.line((x0 + k, y0, x0 + k + 40, y1), fill=0, width=1)
    return im


def dark(im):
    return np.asarray(im) < INK


def test_three_columns_and_strips_cut_only_in_white():
    im = page()
    p = plan(im, [], max_h=700, angle=0.0)
    assert p.items[0].kind == "head"
    assert sorted({i.column for i in p.items if i.kind == "text"}) == [1, 2, 3]
    d = dark(im)
    body = p.items[1:]
    assert all(i.box[3] - i.box[1] <= 700 for i in body)
    cuts = [(a, b) for a, b in zip(body, body[1:]) if a.column == b.column]
    assert cuts
    for a, b in cuts:
        # where one strip ends the next begins, on a row with no ink in the column
        assert a.box[3] == b.box[1]
        assert not d[b.box[1], b.box[0]:b.box[2]].any(), (a, b)


def test_a_full_width_figure_is_one_item_and_no_strip_crosses_it():
    fig = (630, 900, 1170, 1250)
    im = page(figures=[fig])
    located = [{"box": [640, 905, 1160, 1245], "caption": None, "inset": False}]
    p = plan(im, located, angle=0.0)
    figs = [i for i in p.items if i.kind == "figure"]
    assert len(figs) == 1 and figs[0].column == 2
    fx0, fy0, fx1, fy1 = figs[0].box
    assert fy0 <= fig[1] and fy1 >= fig[3]          # the whole picture, edges snapped outward
    for i in p.items:
        if i.kind == "text" and i.column == 2:
            assert i.box[3] <= fy0 or i.box[1] >= fy1, i
    assert p.disagreements == {}                      # the ink sees it too


def test_a_caption_is_its_own_text_item_after_the_figure():
    im = page(figures=[(630, 900, 1170, 1200)])
    d = ImageDraw.Draw(im)
    d.rectangle((760, 1215, 1040, 1215 + LINE), fill=0)           # the caption line
    located = [{"box": [640, 905, 1160, 1195], "caption": [760, 1213, 1040, 1239], "inset": False}]
    items = [i for i in plan(im, located, angle=0.0).items if i.column == 2]
    k = next(n for n, i in enumerate(items) if i.kind == "figure")
    cap = items[k + 1]
    assert cap.kind == "text" and cap.box[1] <= 1215 and cap.box[3] >= 1215 + LINE
    assert items[k].box[3] <= cap.box[1]


def test_an_inset_figure_yields_figure_then_the_text_beside_it():
    im = page(figures=[(1450, 1000, 1740, 1300)])
    located = [{"box": [1455, 1005, 1735, 1295], "caption": None, "inset": True}]
    items = [i for i in plan(im, located, angle=0.0).items if i.column == 3]
    k = next(n for n, i in enumerate(items) if i.kind == "figure")
    fig, beside = items[k], items[k + 1]
    assert beside.kind == "text"
    assert beside.box[2] <= fig.box[0]                # the text runs on the left
    assert beside.box[1] <= 1000 <= fig.box[3]


def test_a_light_engraving_the_ink_cannot_see_is_a_disagreement():
    im = page(light=[(70, 700, 590, 1000)])
    located = [{"box": [70, 700, 590, 1000], "caption": None, "inset": False}]
    p = plan(im, located, angle=0.0)
    assert [i for i in p.items if i.kind == "figure"]
    assert p.disagreements == {1: (1, 0)}
