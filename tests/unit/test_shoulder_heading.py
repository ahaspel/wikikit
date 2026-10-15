"""A margin heading's display text: its inner markers stripped, a formula in it
kept whole for the math pass (TRIGONOMETRY, vol 27 p. 277)."""
from wikikit.render.article import _render_sh


def test_a_formula_in_a_margin_heading_stays_whole():
    html = ("a regular «SH:the-nth-roots»«I»The«/I» «MATH:n«/MATH»«I»th Roots.«/I»«/SH» "
            "polygon of «MATH:n«/MATH» sides")
    out = _render_sh(html)
    assert out == ('a regular <span class="shoulder-heading" id="section-the-nth-roots">'
                   "The «MATH:n«/MATH»th Roots.</span> polygon of «MATH:n«/MATH» sides")


def test_a_margin_heading_without_a_formula_is_stripped_as_before():
    assert _render_sh("«SH:x»«I»Santa«/I» Laura.«/SH»") == \
        '<span class="shoulder-heading" id="section-x">Santa Laura.</span>'
