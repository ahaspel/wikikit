"""A book's signature templates (`wikikit.contributors.signatures`): a template
in the table it declares is a signature, rendered as the page prints it; a
book with no table has none of these."""
import pytest

from wikikit.pipeline.stages.elements._classifier import classify
from wikikit.pipeline.stages.elements._contributor import _process_contributor_footer
from wikikit.pipeline.stages.elements._shapes import SHAPE_DOUBLE_BRACE


@pytest.mark.parametrize("corpus", ["fixture_book:SIGNED"], indirect=True)
@pytest.mark.parametrize("raw, printed", [
    ("{{FIX AB}}", "A. B."),
    ("{{FIX_AB}}", "A. B."),                  # underscores are spaces, as on Wikisource
    ("{{ FIX  AB }}", "A. B."),
    ("{{FIX JT-t}}", "J. T-t."),              # case is kept: two templates,
    ("{{FIX JT-T}}", "J. T-T."),              # two signatures
])
def test_a_declared_signature_renders_as_printed(corpus, raw, printed):
    assert classify(SHAPE_DOUBLE_BRACE, raw).label == "CONTRIBUTOR_FOOTER"
    assert _process_contributor_footer(raw) == f"«SPAN[style:float:right]»{printed}«/SPAN»"


@pytest.mark.parametrize("corpus", ["fixture_book:SIGNED"], indirect=True)
def test_a_template_not_in_the_table_is_not_a_signature(corpus):
    assert classify(SHAPE_DOUBLE_BRACE, "{{FIX ZZ}}").label != "CONTRIBUTOR_FOOTER"


@pytest.mark.parametrize("corpus", ["fixture_book:BARE"], indirect=True)
def test_a_book_without_a_table_has_no_such_signatures(corpus):
    assert classify(SHAPE_DOUBLE_BRACE, "{{FIX AB}}").label != "CONTRIBUTOR_FOOTER"
