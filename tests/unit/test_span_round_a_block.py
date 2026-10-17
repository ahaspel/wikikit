"""A styled span whose content holds a block is a div: HTML lets no block
stand in a span — a browser closes the open paragraph before the block, and
the span with it, and the span's style is lost ({{smaller|{{c|…}}}} came out
full size on the Century desk, 2026-10-17)."""
import pytest

from wikikit.pipeline.stages.elements._tables import styled_marker


@pytest.mark.parametrize("body", ["«CTR»x«/CTR»", "«DIV[style:font-size:83%]»q«/DIV»", "x«P»y",
                                  "«TABLE[cols:2]»«TR»«TD»a«/TD»«/TR»«/TABLE»", "{{VERSE:a«BR»b}VERSE}",
                                  "«UL»«LI»a«/LI»«/UL»"])
def test_a_span_holding_a_block_is_a_div(body):
    assert styled_marker("SPAN", "font-size:83%", body) == f"«DIV[style:font-size:83%]»{body}«/DIV»"


@pytest.mark.parametrize("body", ["small words", "«I»x«/I» and «SC»y«/SC»", "{{IMG:x.png|width=20}}"])
def test_a_span_round_words_stays_a_span(body):
    assert styled_marker("SPAN", "font-size:83%", body) == f"«SPAN[style:font-size:83%]»{body}«/SPAN»"
