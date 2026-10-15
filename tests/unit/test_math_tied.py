"""Punctuation printed straight after an inline formula is set with it: a
rendered formula is one object a line may break after, and SOUND p. 454's
full stop after "(1/2l)√(T/m)" began the next line."""
from types import SimpleNamespace

from wikikit.render.article import _render_math_markers

SITE = SimpleNamespace(target="site")


def test_a_full_stop_after_a_formula_is_tied_to_it():
    out = _render_math_markers("frequency «MATH:n_1=U/2l«/MATH». The middle", SITE)
    assert out == ('frequency <span class="math-tied"><span class="tex-math" data-display="0">'
                   'n_1=U/2l</span>.</span> The middle')


def test_several_marks_go_together_and_a_space_ends_the_tie():
    out = _render_math_markers("(as «MATH:a«/MATH»), and «MATH:b«/MATH» then", SITE)
    assert out == ('(as <span class="math-tied"><span class="tex-math" data-display="0">a</span>),'
                   '</span> and <span class="tex-math" data-display="0">b</span> then')


def test_display_math_is_never_tied():
    out = _render_math_markers("«MATH[display]:x=1«/MATH».", SITE)
    assert out == '<span class="tex-math" data-display="1">x=1</span>.'
