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

from britannica.contributors.frontmatter import iter_entries, parse_field
from britannica.wikitext import iter_template_bodies, template_end

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


ENTRY = """
{{EB1911 contributor table/entry
| initials = J. D. {{sc|v. d.}} W.
| name = [[Author:Johannes van der Waals|J. D. van der Waals]]
| description = Professor of Physics {{brace2|Amsterdam}}
| subject1 = MOLECULE
| lnksubject2 = [[EB1911:CONDENSATION|CONDENSATION OF GASES]]
}}
"""


def test_entry_reader_survives_nested_templates_in_every_field():
    (body,) = iter_entries(ENTRY)
    assert parse_field(body, "initials") == "J. D. {{sc|v. d.}} W."
    assert parse_field(body, "subject1") == "MOLECULE"
    assert parse_field(body, "lnksubject2") == \
        "[[EB1911:CONDENSATION|CONDENSATION OF GASES]]"
    assert parse_field(body, "nosuchfield") == ""


def test_entry_reader_finds_every_entry_in_a_page():
    assert len(list(iter_entries(ENTRY * 3))) == 3
