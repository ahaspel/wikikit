"""The shipped "N words" counts what a reader sees, not the marker stream.

`len(body.split())` counted the markers themselves: a display formula standing
between spaces scored as a word, and a «P» between two words fused them.  Over
the corpus that ran 2.84% high, and — the part that matters — wrong per article
in proportion to markup density, so comparing two articles' lengths was
unreliable in a way a round "approximately" does not excuse.

`markers_to_text` is not the answer either.  It exists for search and previews
and DROPS footnotes, tables, verse and legends whole, which would omit the poems
from CHANT ROYAL and the figure keys from TOOL.
"""
from __future__ import annotations

from britannica.markers import countable_words, markers_to_text


def test_markers_do_not_count_as_words():
    assert countable_words("one «I»two«/I» three") == 3
    assert countable_words("«CTR»alpha beta«/CTR»") == 2


def test_a_marker_between_words_separates_them():
    """The defect the paragraph work made visible: `note.«P»The` was ONE word."""
    assert countable_words("note.«P»The perfection") == 3
    assert countable_words("«TD»alpha«/TD»«TD»beta«/TD»") == 2


def test_raw_sub_sup_keep_one_word():
    """`H<sub>2</sub>O` is one word, which is why markers cannot simply all
    become separators."""
    assert countable_words("H<sub>2</sub>O is water") == 3


def test_link_targets_are_addresses_not_words():
    assert countable_words("see «LN:14-0147-abc.json|HYDROMEDUSAE|the medusae«/LN»") == 3


def test_apparatus_counts_because_a_reader_reads_it():
    """Footnotes, tables and verse are text; markers_to_text drops them."""
    for body in ("«FN:a note of four words«/FN»",
                 "{{VERSE:a line of verse}VERSE}"):
        assert countable_words(body) > 0
        assert markers_to_text(body).strip() == ""


def test_mathematics_and_title_are_not_words():
    assert countable_words("«MATH:\\int_0^1 x dx«/MATH»") == 0
    assert countable_words("«TITLE:ABBEY«/TITLE»body text here") == 3
