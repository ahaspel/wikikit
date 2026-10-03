"""One scanned page, cut: deskew once, find the figures on the level page (a
vision model, `figures`), and cut it into reading-order items (`cut`) — the
figures and the cuts in the same coordinates."""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from wikikit.scan import cut, figures


@dataclass
class CutPage:
    level: Image.Image        # the deskewed page every box refers to
    plan: cut.Plan
    figures: list[dict]       # as located and refined, for the record
    across: list              # type set across the columns, as located


def cut_page(img: Image.Image, max_h: int = cut.MAX_H, n_columns: int | None = None) -> CutPage:
    """`n_columns`: how many columns the book says the page prints (`cut.plan`)."""
    angle = cut.skew_angle(img)
    level = cut.deskew(img, angle)
    figs, across = figures.find(level, cut.page_columns(level, n_columns))
    p = cut.plan(level, figs, max_h=max_h, angle=0.0, n_columns=n_columns, across=across)
    p.angle = angle
    return CutPage(level, p, figs, across)
