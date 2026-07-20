"""Two source-fidelity fixes, locked down.

1. A script wrapper's NAMED param must not leak into prose.  `{{Hebrew|small=y|X}}`
   rendered as the literal text `small=y|X` (GALILEE, 375 sites) because the LANG
   peel took everything after the FIRST pipe.  The param is presentation, so it is
   CARRIED as CSS — not dropped, not leaked.

2. An unbound template parameter renders as its DEFAULT.  `{{{width|100%}}}` is
   `100%` in MediaWiki article space; carrying the raw triple-brace leaked it into
   ALGEBRA's table styles and vol 1's title page.
"""
import pytest

from britannica.pipeline.stages.elements import process_elements
from britannica.pipeline.stages.elements._context import ElementContext
from britannica.pipeline.stages.preprocess import _resolve_param_defaults


@pytest.fixture
def ctx():
    return ElementContext(volume=11)


# ── 1. script-wrapper named params ───────────────────────────────────────
@pytest.mark.parametrize("src", [
    "{{Hebrew|small=y|ג}}",
    "{{Hebrew|small=yes|ג}}",
    "{{Hebrew|small=1|ג}}",
    "{{he|small=y|ג}}",
])
def test_script_wrapper_param_never_leaks(src, ctx):
    out = process_elements(src, ctx)
    assert "small=" not in out, f"param leaked into prose: {out!r}"
    assert "ג" in out, "the glyph itself must survive"


def test_script_wrapper_param_is_carried_as_css(ctx):
    """Carried, not dropped — `small` means render the glyph smaller."""
    out = process_elements("{{Hebrew|small=y|ג}}", ctx)
    assert "font-size:83%" in out


def test_unparameterised_script_wrapper_is_untouched(ctx):
    assert process_elements("{{Hebrew|ג}}", ctx) == "ג"
    assert process_elements("{{polytonic|Γ}}", ctx) == "Γ"


def test_script_wrapper_inside_tooltip_span(ctx):
    """The corpus shape: the glyph rides inside a transliteration tooltip."""
    out = process_elements("<span title=Galil>{{Hebrew|small=y|ג}}</span>", ctx)
    assert "small=" not in out
    assert "title:Galil" in out and "ג" in out


# ── 2. template parameter defaults ───────────────────────────────────────
@pytest.mark.parametrize("src,want", [
    ("width: {{{width|100%}}}", "width: 100%"),
    ("VOLUME {{{vol|I}}}", "VOLUME I"),
    ("{{{from|A}}} to {{{to|ANDROPHAGI}}}", "A to ANDROPHAGI"),
    ("{{{width|}}}", ""),                       # empty default → empty
    ("{{{a|{{{b|x}}}}}}", "x"),                 # nested, inside-out
])
def test_param_default_is_substituted(src, want):
    assert _resolve_param_defaults(src) == want


def test_param_without_default_is_left_literal():
    """MediaWiki renders a defaultless param literally too — leaking it is faithful."""
    assert _resolve_param_defaults("{{{foo}}}") == "{{{foo}}}"


def test_ordinary_template_is_not_touched():
    assert _resolve_param_defaults("{{sc|Real}}") == "{{sc|Real}}"
