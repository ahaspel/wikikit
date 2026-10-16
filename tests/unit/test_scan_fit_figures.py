"""A reading that marks more figures than the cut found: the figure the
locator found as one, split where blank paper parts it (Century vol 1 p. 10:
Figs. 1 and 2 stacked under one caption) — and nowhere else; and a running
head that is only a mark is a page with no head."""
from PIL import Image, ImageDraw

from wikikit.scan.cut import Item
from wikikit.scan.transcribe import FIGURE_MARK, fit_figures, settle, split_figure


def _page(gap: int) -> Image.Image:
    img = Image.new("L", (400, 600), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((50, 100, 350, 220), outline=0, width=4)            # Fig. 1
    d.rectangle((50, 220 + gap, 350, 340 + gap), outline=0, width=4)  # Fig. 2
    return img


def test_two_stacked_figures_are_parted_at_the_blank_band():
    fig = Item("figure", (40, 90, 360, 420), 1, "c1f1")
    boxes = split_figure(_page(60), fig, 2)
    assert boxes is not None and len(boxes) == 2
    assert boxes[0][3] <= 225 and boxes[1][1] >= 275


def test_no_band_no_split():
    fig = Item("figure", (40, 90, 360, 420), 1, "c1f1")
    assert split_figure(_page(4), fig, 2) is None


def test_the_page_items_take_the_split_and_a_new_name():
    text_item = Item("text", (0, 0, 400, 600), 1, "c1s1")
    fig = Item("figure", (40, 90, 360, 420), 1, "c1f1")
    items = [text_item, fig, Item("text", (0, 600, 400, 700), 1, "c1s2")]
    reading = f"before\n{FIGURE_MARK}\n{FIGURE_MARK}\n{{{{c|Fig. 1. Fig. 2.}}}}\nafter"
    fitted = fit_figures(_page(60), items, text_item, reading)
    assert [it.name for it in fitted] == ["c1s1", "c1f1", "c1f2", "c1s2"]


def test_a_mark_with_no_figure_at_all_is_never_fitted():
    text_item = Item("text", (0, 0, 400, 600), 1, "c1s1")
    assert fit_figures(_page(60), [text_item], text_item, f"{FIGURE_MARK}\ntext") is None


def test_a_head_that_is_only_a_mark_is_no_head():
    head = Item("head", (0, 0, 400, 40), 0, "head")
    assert settle(Image.new("L", (400, 600), 255), head, FIGURE_MARK) == ("", None)
