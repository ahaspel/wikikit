"""The raw-template lexicon: one brace walk, and what it does at the edges.

Four readers counted braces for themselves — the two contributor-table readers,
the footer author-link reader, the plate-title read.  Each had paid for the
lesson separately (a non-greedy capture truncates at the first INNER `}}`), and
by the time they were compared they had already drifted at the edge: for an
unterminated template one twin yielded a garbage slice while the other yielded
nothing.  These are the cases that separated them.
"""
from __future__ import annotations

import re

from wikikit.wikitext import iter_template_bodies, template_end

_OPEN = re.compile(r"\{\{tmpl\|")


def test_nested_templates_do_not_end_the_span():
    text = "{{tmpl|{{uc|TITLE}} and {{sc|more}}}}tail"
    bodies = [b for _o, b in iter_template_bodies(text, _OPEN)]
    assert bodies == ["{{uc|TITLE}} and {{sc|more}}"]


def test_unterminated_template_yields_nothing_and_never_a_partial():
    """The drift itself: a garbage slice here is silently wrong data
    downstream, where a skipped template stays visible as raw source."""
    assert template_end("{{tmpl|never closed", 0) is None
    assert [b for _o, b in iter_template_bodies("{{tmpl|never closed", _OPEN)] == []


def test_unterminated_template_does_not_hide_a_later_one():
    text = "{{tmpl|oops {{unclosed  … {{tmpl|good}}"
    assert [b for _o, b in iter_template_bodies(text, _OPEN)] == ["good"]


def test_offsets_address_the_open_brace():
    text = "lead {{tmpl|body}} tail"
    (off, body), = iter_template_bodies(text, _OPEN)
    assert text[off:off + 2] == "{{" and body == "body"
