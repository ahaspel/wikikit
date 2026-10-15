"""Punctuation printed straight after an inline formula stays with it: a
rendered formula is one object a line may break after, and SOUND p. 454's
full stop after "(1/2l)√(T/m)" began the next line.  On the site it is set
inside the formula, as text, so the formula still breaks between its terms
(made unbreakable whole, it fell below Fig. 34); in the EPUB, where a
formula is one image, the two are tied."""
from types import SimpleNamespace

from wikikit.render.article import _render_math_markers

SITE = SimpleNamespace(target="site")


def test_a_full_stop_after_a_formula_is_set_inside_it_on_the_site():
    out = _render_math_markers("frequency «MATH:n_1=U/2l«/MATH». The middle", SITE)
    assert out == 'frequency <span class="tex-math" data-display="0">n_1=U/2l\\text{.}</span> The middle'


def test_several_marks_go_together_and_a_space_ends_them():
    out = _render_math_markers("(as «MATH:a«/MATH»), and «MATH:b«/MATH» then", SITE)
    assert out == ('(as <span class="tex-math" data-display="0">a\\text{),}</span> and '
                   '<span class="tex-math" data-display="0">b</span> then')


def test_display_math_keeps_its_punctuation_outside():
    out = _render_math_markers("«MATH[display]:x=1«/MATH».", SITE)
    assert out == '<span class="tex-math" data-display="1">x=1</span>.'
