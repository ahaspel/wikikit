"""wikikit.scan.cut on drawn pages: three columns of "type" (blocks a line
high, white between lines), a running head, and figures placed where a
locator would report them.  The rules the cutter promises: columns from the
gutters, strips never cut through a line nor through a figure, a figure read
inside the strip that holds it and following it as a crop, and the ink's
second opinion reported."""
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


def test_a_figure_is_read_inside_its_strip_and_follows_it_as_a_crop():
    fig = (630, 900, 1170, 1250)
    im = page(figures=[fig])
    located = [{"box": [640, 905, 1160, 1245], "caption": None, "inset": False}]
    p = plan(im, located, max_h=700, angle=0.0)
    figs = [i for i in p.items if i.kind == "figure"]
    assert len(figs) == 1 and figs[0].column == 2
    holder = p.items[p.items.index(figs[0]) - 1]
    assert holder.kind == "text" and holder.column == 2
    hx0, hy0, hx1, hy1 = holder.box
    assert hy0 <= 900 and hy1 >= 1250                  # the strip holds the whole figure
    fx0, fy0, fx1, fy1 = figs[0].box
    assert fx0 <= 640 and fy0 <= 905 and fx1 >= 1160 and fy1 >= 1245   # the crop: the located box
    assert p.disagreements == {}                      # the ink sees it too


def test_no_strip_is_cut_through_a_figure_or_its_caption():
    im = page(figures=[(630, 900, 1170, 1200)])
    d = ImageDraw.Draw(im)
    d.rectangle((760, 1215, 1040, 1215 + LINE), fill=0)           # the caption line
    located = [{"box": [640, 905, 1160, 1195], "caption": [760, 1213, 1040, 1239], "inset": False}]
    for max_h in (300, 700, 1100):
        p = plan(im, located, max_h=max_h, angle=0.0)
        for i in p.items:
            if i.kind == "text" and i.column == 2:
                assert not (900 < i.box[3] < 1239), (max_h, i)      # no cut inside figure + caption
        assert not [i for i in p.items if i.kind == "caption"]


def test_an_inset_and_the_text_beside_it_are_one_strip():
    im = page(figures=[(1450, 1000, 1740, 1300)])
    located = [{"box": [1455, 1005, 1735, 1295], "caption": None, "inset": True}]
    items = [i for i in plan(im, located, max_h=700, angle=0.0).items if i.column == 3]
    k = next(n for n, i in enumerate(items) if i.kind == "figure")
    holder = items[k - 1]
    assert holder.kind == "text" and holder.box[0] == items[0].box[0]   # the column's full width
    assert holder.box[1] <= 1000 and holder.box[3] >= 1300


def test_a_column_read_whole_is_one_strip_with_its_figures_after_it():
    im = page(figures=[(630, 900, 1170, 1250), (630, 1600, 1170, 1900)])
    located = [{"box": [640, 905, 1160, 1245], "caption": None, "inset": False},
               {"box": [640, 1605, 1160, 1895], "caption": None, "inset": False}]
    p = plan(im, located, max_h=None, angle=0.0)
    col2 = [i for i in p.items if i.column == 2]
    assert [i.kind for i in col2] == ["text", "figure", "figure"]
    assert [i.name for i in col2] == ["c2s1", "c2f1", "c2f2"]
    assert [i.box[1] < 1000 for i in col2[1:]] == [True, False]       # in reading order
    assert {i.column for i in p.items if i.kind == "text"} == {1, 2, 3}
    assert all(len([i for i in p.items if i.column == c]) >= 1 for c in (1, 3))




def test_a_light_engraving_the_ink_cannot_see_is_a_disagreement():
    im = page(light=[(70, 700, 590, 1000)])
    located = [{"box": [70, 700, 590, 1000], "caption": None, "inset": False}]
    p = plan(im, located, angle=0.0)
    assert [i for i in p.items if i.kind == "figure"]
    assert p.disagreements == {1: (1, 0)}


# ── a list of writers: two declared columns of short rows ───────────────────

LIST_COLS = [(60, 880), (920, 1740)]
RULE_X = 900


def list_page(title=True, footnote=False, rule=True):
    """Rows of "initials . . . name": the initials, dot leaders, the name — a
    blank as wide as a gutter inside every row, where a finder looks for one.
    A centred title across both columns, a footnote across the foot, a rule
    (tilted a little, as scanned rules are) between the columns."""
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((60, 60, 400, 60 + LINE), fill=0)                     # the running head
    top = TOP
    if title:
        d.rectangle((560, top, 860, top + 2 * LINE), fill=0)          # LIST OF
        d.rectangle((940, top, 1240, top + 2 * LINE), fill=0)         # WRITERS: its word space on the cut
        top += 3 * LEAD
    first = top
    last = H - 400 if footnote else H - 120
    for x0, x1 in LIST_COLS:
        for y in range(top, last, LEAD):
            d.rectangle((x0, y, x0 + 110, y + LINE), fill=0)          # initials
            for dx in range(x0 + 300, x0 + 360, 15):                  # leaders
                d.rectangle((dx, y + LINE - 4, dx + 3, y + LINE), fill=0)
            d.rectangle((x0 + 380, y, x1 - 140, y + LINE), fill=0)    # the name
    if rule:
        d.line((RULE_X, first, RULE_X + 4, last), fill=0, width=3)
    if footnote:
        d.rectangle((60, H - 300, 1740, H - 300 + LINE), fill=0)
    return im


def _columns_of(p):
    return sorted({(i.box[0], i.box[2]) for i in p.items if i.column > 0})


def test_declared_columns_are_cut_at_their_division_not_beside_the_leaders():
    found = plan(list_page(title=False), [], angle=0.0)
    assert len(_columns_of(found)) > 2                               # the ink alone is misled
    p = plan(list_page(title=False), [], angle=0.0, n_columns=2)
    (a0, a1), (b0, b1) = _columns_of(p)
    assert 880 - 140 <= a1 == b0 <= 920                             # in the white between them


def test_one_declared_column_is_the_whole_width():
    p = plan(list_page(title=False), [], angle=0.0, n_columns=1)
    assert len(_columns_of(p)) == 1


# The title's box as the model gives it: roughly on the type, a little short of
# its foot (snapping must find the white beside it, not cut through it).
TITLE_BOX = [540, TOP - 5, 1260, TOP + 2 * LINE - 6]
FOOTNOTE_BOX = [70, H - 296, 1730, H - 300 + LINE - 4]


def test_a_title_across_the_columns_is_read_across_before_them():
    p = plan(list_page(), [], angle=0.0, n_columns=2, across=[TITLE_BOX])
    names = [i.name for i in p.items]
    assert names[:2] == ["head", "c0s1"]
    span = p.items[1]
    assert span.box[0] <= 560 and span.box[2] >= 1240                 # the title whole
    assert TOP + 2 * LINE <= span.box[3] <= TOP + 3 * LEAD            # and no row of the list
    assert all(i.box[1] >= span.box[3] for i in p.items if i.column > 0)
    assert 0 not in p.disagreements


def test_a_footnote_across_the_columns_is_read_last():
    p = plan(list_page(title=False, footnote=True), [], angle=0.0, n_columns=2, across=[FOOTNOTE_BOX])
    last = p.items[-1]
    assert last.column == 0 and last.box[1] < H - 300 < H - 300 + LINE <= last.box[3]
    assert all(i.box[3] <= last.box[1] for i in p.items if i.column > 0)


def test_a_title_the_model_missed_is_a_disagreement_not_a_guess():
    im = list_page()
    ImageDraw.Draw(im).rectangle((860, TOP, 940, TOP + 2 * LINE), fill=0)   # type across the cut
    p = plan(im, [], angle=0.0, n_columns=2)
    assert not [i for i in p.items if i.column == 0 and i.kind == "text"]   # the ink decides nothing
    assert p.disagreements[0] == (0, 1)                                     # but says where to look


def test_a_box_in_the_running_head_is_the_heads():
    im = head_page()
    p = plan(im, [], angle=0.0, n_columns=2, across=[[290, 115, 1510, 120 + 2 * LINE + 4]])
    assert p.items[0].kind == "head" and p.items[0].box[3] >= 120 + 2 * LINE
    assert not [i for i in p.items if i.column == 0 and i.kind == "text"]


def test_a_rule_between_the_columns_is_not_type_across_them():
    p = plan(list_page(title=False), [], angle=0.0, n_columns=2)
    assert not [i for i in p.items if i.column == 0 and i.kind == "text"]
    assert 0 not in p.disagreements


# ── the running head: the first line of type, and nothing else ──────────────

def head_page(tint=0, edge=False, rule=True, dark_edge=False, shade=False):
    """A running head with a rule under it, then rows set closer to the head
    than to each other — where "the largest gap near the top" falls AFTER the
    first row.  `tint` greys the paper (a toned scan); `edge` darkens a faint
    band along the image's top (the scan's edge); `dark_edge` a dark one, as
    dark as Anthon's (leaf 401: 14% ink in rows 0-29); `shade` lays mottled
    grey bands across the margin below the edge, darker than the ink threshold
    but nowhere black — EB1911 vol 28's tinted tops and show-through (p487)."""
    im = Image.new("L", (W, H), 255 - tint)
    d = ImageDraw.Draw(im)
    if shade:
        rng = np.random.default_rng(13)
        for y0, y1 in ((45, 70), (80, 105)):
            for _ in range(5000):
                x, y = int(rng.integers(0, W)), int(rng.integers(y0, y1))
                d.point((x, y), fill=INK - 30)
    if dark_edge:
        rng = np.random.default_rng(12)
        for _ in range(8000):
            x, y = int(rng.integers(0, W)), int(rng.integers(0, 30))
            d.point((x, y), fill=0)
    if edge:
        rng = np.random.default_rng(11)
        for _ in range(900):                                      # speckle: ink, but faint
            x, y = int(rng.integers(0, W)), int(rng.integers(0, 30))
            d.point((x, y), fill=0)
    d.rectangle((300, 120, 1500, 120 + 2 * LINE), fill=0)          # the running head
    if rule:
        d.rectangle((200, 190, 1600, 194), fill=0)                 # its rule
    for y in range(225, H - 120, 70):                              # rows, 70 apart
        d.rectangle((60, y, 860, y + 30), fill=0)
        d.rectangle((940, y, 1740, y + 30), fill=0)
    return im


def _head(im):
    return plan(im, [], angle=0.0).items[0]


def test_the_head_is_the_first_line_even_when_the_widest_gap_follows_the_first_row():
    h = _head(head_page())
    assert h.kind == "head" and h.box[1] <= 120 and 194 <= h.box[3] <= 225


def test_a_toned_scan_has_the_same_head():
    h = _head(head_page(tint=60))
    assert 194 <= h.box[3] <= 225


def test_the_scans_edge_is_not_the_head():
    h = _head(head_page(edge=True))
    assert 194 <= h.box[3] <= 225


def test_a_dark_edge_touching_the_images_top_is_not_the_head():
    h = _head(head_page(dark_edge=True))
    assert 194 <= h.box[3] <= 225, h.box


def test_a_shaded_margin_above_the_head_is_paper_not_the_head():
    h = _head(head_page(shade=True))
    assert 194 <= h.box[3] <= 225, h.box


def test_without_a_rule_the_head_ends_before_the_first_row():
    h = _head(head_page(rule=False))
    assert 120 + 2 * LINE <= h.box[3] <= 225


def test_figures_located_twice_are_one_figure():
    # four rings located one by one, each refined to the whole row (EB1911
    # vol 22 p706): one figure, no copy cutting the column into nonsense
    from wikikit.scan.cut import _distinct
    row = [100, 200, 900, 380]
    figs = [{"box": list(row), "caption": None, "inset": False} for _ in range(4)]
    figs.append({"box": [120, 700, 300, 860], "caption": None, "inset": True})
    out = _distinct(figs)
    assert [f["box"] for f in out] == [row, [120, 700, 300, 860]]




def test_a_figure_drawn_a_little_past_the_gutter_stays_in_its_column():
    # EB1911 vol 24 p1051: a column-2 figure whose box crosses the gutter by a
    # few dozen pixels is column 2's — not a band across the page
    from wikikit.scan.cut import spans_columns
    cols = [(60, 860), (940, 1740)]
    assert not spans_columns([890, 300, 1700, 900], cols)          # 50 px over: column 2's
    assert spans_columns([100, 300, 1700, 900], cols)              # across both: a band
    assert not spans_columns([100, 300, 820, 900], cols)           # column 1's


def test_a_figure_across_the_columns_mid_page_has_columns_above_and_below():
    # two columns of type, a figure across both in the middle of the page
    # (Anthon's family tree, leaf 1450): read the columns above, the figure,
    # then the columns below — never a column running through the figure
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((60, 60, 400, 60 + LINE), fill=0)                       # the running head
    fig = (100, 1000, 1700, 1500)
    for x0, x1 in ((60, 860), (940, 1740)):
        for y in list(range(TOP, 900, LEAD)) + list(range(1600, H - 120, LEAD)):
            d.rectangle((x0, y, x1, y + LINE), fill=0)
    d.rectangle(fig, outline=0, width=6)
    d.line((300, 1100, 1500, 1400), fill=0, width=6)
    located = [{"box": [100, 1000, 1700, 1500], "caption": None, "inset": False}]
    p = plan(im, located, angle=0.0, n_columns=2)
    figs = [i for i in p.items if i.kind == "figure"]
    assert len(figs) == 1 and figs[0].column == 0
    fy0, fy1 = figs[0].box[1], figs[0].box[3]
    assert fy0 <= 1000 and fy1 >= 1500 and figs[0].box[0] <= 100 and figs[0].box[2] >= 1700
    k = p.items.index(figs[0])
    band = p.items[k - 1]                                              # read across, the figure in it
    assert band.kind == "text" and band.column == 0
    assert band.box[1] <= 1000 and band.box[3] >= 1500
    before, after = p.items[1:k - 1], p.items[k + 1:]
    assert {i.column for i in before} == {1, 2} and all(i.box[3] <= band.box[1] for i in before)
    assert {i.column for i in after} == {1, 2} and all(i.box[1] >= band.box[3] for i in after)
    assert len({i.name for i in p.items}) == len(p.items)            # every item named once


def test_no_strip_is_cut_between_a_fraction_and_its_bar():
    # eight lines of type, a display fraction (numerator, a thin bar,
    # denominator, white between each), then more type; at this height the
    # last white band that fits is the one above the bar (ALGEBRAIC FORMS)
    from wikikit.scan.cut import strips
    d = np.zeros((700, 500), dtype=bool)
    for y in range(0, 8 * LEAD, LEAD):
        d[y:y + LINE, 20:480] = True
    d[260:280, 190:310] = True          # numerator
    d[286:289, 150:350] = True          # the bar
    d[295:315, 190:310] = True          # denominator
    for y in range(330, 650, LEAD):
        d[y:y + LINE, 20:480] = True
    cuts = [e for _, e in strips(d, 0, 700, 0, 500, max_h=290)][:-1]
    assert cuts and not any(260 <= c <= 315 for c in cuts), cuts


def test_a_drawing_in_the_gutter_does_not_move_it():
    # EB1911 vol 24 p1052: a column-1 drawing reaching past the gutter, the
    # text of column 2 set beside it; the gutter stays where the type parts
    im = page(figures=[(300, 900, 690, 1700)])
    located = [{"box": [300, 900, 690, 1700], "caption": None, "inset": True}]
    plain = sorted({(i.box[0], i.box[2]) for i in plan(page(), [], angle=0.0).items if i.column > 0})
    p = plan(im, located, angle=0.0)
    assert sorted({(i.box[0], i.box[2]) for i in p.items if i.column > 0 and i.kind == "text"}) == plain


def test_a_page_with_something_across_its_gutter_says_why():
    seen = [{"kind": "illustration", "where": "top", "detail": "Fig. 3 reaches into column 1"}]
    assert plan(page(), [], angle=0.0).crossing == []
    assert plan(page(), [], angle=0.0, gutter=seen).crossing == [
        "across the gutter: illustration (top): Fig. 3 reaches into column 1"]


def test_figures_side_by_side_are_read_left_to_right():
    from wikikit.scan.cut import reading_order
    right_higher = {"box": (1671, 2341, 2200, 2568)}           # vol 21 p. 508: Fig. 26
    left = {"box": (1221, 2348, 1638, 2544)}                    # Fig. 25
    below = {"box": (1221, 2700, 1638, 2900)}
    assert reading_order([right_higher, below, left], key=lambda f: f["box"]) == [left, right_higher, below]
