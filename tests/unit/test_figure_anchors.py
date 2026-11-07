"""Figure anchors and the prose references that land on them.

An image and its caption are siblings no producer sees together, so the post-walk
pass pairs them and names each figure's anchor after the number its caption
prints; "fig. N" in the text then links to the nearest anchor of that number.
Bodies below are walked-body shapes taken from the articles named.
"""
from __future__ import annotations

import re

import pytest

from wikikit.export.sections import detect_sections, match_section
from wikikit.markers import iter_ln_markers
from wikikit.pipeline.stages.elements._figure_anchors import (
    stamp_figure_anchors, stamp_figure_refs)

_ANCHOR = re.compile(r"«ANCHOR:[^|»]*\|([^»]*)»(\{\{IMG:[^|}]*)")


def anchors(body: str) -> list[tuple[str, str]]:
    """[(anchor name, image filename)] in body order."""
    return [(n, f[len("{{IMG:"):]) for n, f in _ANCHOR.findall(stamp_figure_anchors(body))]


def cap(n) -> str:
    return f"«BR»«CTR»«SC»Fig.«/SC» {n}.«/CTR»"


def test_an_image_takes_the_caption_after_it():
    assert anchors("{{IMG:a.png}}" + cap(1) + "«P»text") == [("Fig. 1", "a.png")]


def test_side_by_side_images_take_the_captions_in_order():
    """MAP: two images in one row, "Fig. 9. Fig. 10." in the row below."""
    body = ("«TABLE[]»«TR»«TD»{{IMG:a.png}}«/TD»«TD»{{IMG:b.png}}«/TD»«/TR»"
            "«TR»«TD»«SC»Fig.«/SC» 9.«/TD»«TD»«SC»Fig.«/SC» 10.«/TD»«/TR»«/TABLE»")
    assert anchors(body) == [("Fig. 9", "a.png"), ("Fig. 10", "b.png")]


def test_a_caption_spaced_before_its_point_is_read():
    """LOCK, RIFLE, TIRE print "Fig . 1"."""
    assert anchors("{{IMG:a.png}}«BR»Fig . 1.—Lever.«P»") == [("Fig. 1", "a.png")]


def test_a_numbered_file_takes_its_own_caption_in_a_run():
    """CERAMICS: captions printed 67, 66 under images filed 66, 67."""
    body = ("{{IMG:EB1911 Ceramics Fig. 66.png}}{{IMG:EB1911 Ceramics Fig. 67.png}}"
            "«BR»Fig. 67. Fig. 66.«P»")
    assert anchors(body) == [("Fig. 66", "EB1911 Ceramics Fig. 66.png"),
                             ("Fig. 67", "EB1911 Ceramics Fig. 67.png")]


def test_a_file_and_caption_that_disagree_get_no_anchor():
    """BREAKWATER's Table Bay figure is filed "Fig. 2" and printed "Fig. 1"."""
    assert anchors("{{IMG:EB1911 Breakwater - Fig. 2.jpg}}" + cap(1) + "«P»") == []


def test_a_paragraph_break_ends_the_run():
    """A sentence opening "Fig. 3 shows …" after a paragraph claims no image."""
    assert anchors("{{IMG:a.png}}«P»Fig. 3 shows the valve.") == []


def test_a_repeated_number_gets_a_distinct_name():
    """MAP restarts its numbering for Map Projections."""
    body = "{{IMG:a.png}}" + cap(1) + "«P»…«P»{{IMG:b.png}}" + cap(1) + "«P»"
    assert anchors(body) == [("Fig. 1", "a.png"), ("Fig. 1 second", "b.png")]


def test_the_sources_own_anchor_is_not_doubled():
    """CRYSTALLOGRAPHY's `{{anchor|fig87}}` and our "Fig. 87" are one name; two
    would tie and `match_section` abstains."""
    body = "«ANCHOR:fig87|fig87»{{IMG:a.png}}" + cap(87) + "«P»as in fig. 87 above"
    stamped = stamp_figure_anchors(body)
    assert stamped.count("«ANCHOR:") == 1
    assert [ln.target for ln in iter_ln_markers(stamp_figure_refs(stamped))] == ["#fig87"]


def _links(text: str, figures=(1, 2, 3)) -> list[tuple[str, str]]:
    body = "".join("{{IMG:f%d.png}}" % n + cap(n) + "«P»" for n in figures) + text
    out = stamp_figure_refs(stamp_figure_anchors(body))
    return [(ln.target, ln.display) for ln in iter_ln_markers(out)]


@pytest.mark.parametrize("text,want", [
    ("as shown (fig. 2).", [("#Fig. 2", "fig. 2")]),
    ("compare figs. 1 and 3.", [("#Fig. 1", "1"), ("#Fig. 3", "3")]),
    ("figs. 1-2 show it", [("#Fig. 1", "1"), ("#Fig. 2", "2")]),
    # ECHINODERMA: after singular "fig." a second number is a part OF the figure
    ("(fig. 3, 2)", [("#Fig. 3", "fig. 3")]),
    ("see fig. 9", []),                                  # no such figure here
    ("Plate II. fig. 2", []),                            # a plate's figure
    ("(fig. 2, Plate XXV.)", []),                        # SHIPBUILDING
    ("«I»Gymnoblastic Hydroids«/I», fig. 3", []),        # another work's figure
    ("op. cit. p. 40, fig. 2", []),
    ("«SC»Fig.«/SC» 2 is a caption", []),                # capital Fig. is a caption
])
def test_references(text, want):
    assert _links(text) == want


def test_every_reference_lands_on_exactly_its_anchor():
    body = ("{{IMG:a.png}}" + cap(1) + "«P»{{IMG:b.png}}" + cap(1) + "«P»"
            "near the second (fig. 1)")
    out = stamp_figure_refs(stamp_figure_anchors(body))
    secs = detect_sections(out)
    [ln] = list(iter_ln_markers(out))
    assert ln.target == "#Fig. 1 second", "the NEAREST figure of that number"
    assert match_section(secs, ln.target[1:])["title"] == "Fig. 1 second"
