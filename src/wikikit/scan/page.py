"""One scanned page, cut: deskew once, find the figures on the level page (a
vision model, `figures`), and cut it into reading-order items (`cut`) — the
figures and the cuts in the same coordinates."""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from wikikit.scan import cut, figures
from wikikit.scan.layout import PageLayout


@dataclass
class CutPage:
    level: Image.Image        # the deskewed page every box refers to
    plan: cut.Plan
    figures: list[dict]       # as located and refined, for the record
    across: list              # type set across the columns, as located
    gutter: list              # what crosses a gutter, as the model saw it (`figures.gutter`)


def cut_page(img: Image.Image, layout: PageLayout = PageLayout()) -> CutPage:
    """`layout`: what the book declares of the page (`wikikit.scan.layout`);
    a book reading only pages set in columns asks the gutter look too."""
    angle = cut.skew_angle(img)
    level = cut.deskew(img, angle)
    figs, across = figures.find(level, cut.page_columns(level, layout.columns))
    gut = figures.gutter(level) if layout.columns_only else []
    p = cut.plan(level, figs, max_h=layout.max_h, angle=0.0, n_columns=layout.columns,
                 across=across, gutter=gut)
    p.angle = angle
    return CutPage(level, p, figs, across, gut)
