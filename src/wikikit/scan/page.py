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


def cut_page(img: Image.Image, max_h: int = cut.MAX_H) -> CutPage:
    angle = cut.skew_angle(img)
    level = cut.deskew(img, angle)
    figs = figures.find(level, cut.page_columns(level))
    p = cut.plan(level, figs, max_h=max_h, angle=0.0)
    p.angle = angle
    return CutPage(level, p, figs)
