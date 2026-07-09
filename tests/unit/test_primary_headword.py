"""Unit battery for `primary_headword` — the canonical-headword extraction that
the title/display split keys identity, search, and xref matching on
(docs/title-display-split.md).  Each case is a real `«TITLE»` shape from the corpus."""
import pytest

from britannica.util.strings import primary_headword


@pytest.mark.parametrize("heading,expected", [
    # simple — 66% of the corpus, passes through
    ("ALGEBRA", "ALGEBRA"),
    ("ADEN", "ADEN"),
    ("WILLIAM II", "WILLIAM II"),                       # regnal numeral stays in the headword

    # person: surname before the given-name / alt-surname clause
    ("AARSSENS, or Aarssen, FRANCIS VAN", "AARSSENS"),
    ("BARANTE, AMABLE GUILLAUME PROSPER BRUGIÈRE, BARON DE", "BARANTE"),

    # regnal / place qualifier after a comma
    ("WILLIAM II, King of England", "WILLIAM II"),
    ("BANGOR, a city of Wales", "BANGOR"),

    # sobriquet: comma form and the bare "surnamed" form (no preceding comma)
    ("ALEXANDER III, king of Macedon, surnamed the Great", "ALEXANDER III"),
    ("PETER surnamed the Hermit", "PETER"),

    # trailing parenthetical descriptor → stripped
    ("ACTON (JOHN EMERICH EDWARD DALBERG ACTON)", "ACTON"),
    ("AGRICOLA (the Latinized form of the name Bauer), G. J.", "AGRICOLA"),

    # inline bracket alt-spelling → removed, name flows around it
    ("ADAM (or Adan) DE LE HALE", "ADAM DE LE HALE"),
    ("ATTAR [or Otto] OF ROSES", "ATTAR OF ROSES"),

    # comma INSIDE a bracket — removing brackets first avoids the mis-cut
    ("SMITH (JONES, John)", "SMITH"),

    # dangling / unclosed bracket = truncated descriptor → cut to end
    ("ALBERT (FRANCIS CHARLES AUGUSTUS ALBERT", "ALBERT"),

    # genuine multi-word titles with no comma/bracket — kept whole (they ARE the headword)
    ("ACTS OF THE APOSTLES", "ACTS OF THE APOSTLES"),
    ("ALCÁZAR DE SAN JUAN", "ALCÁZAR DE SAN JUAN"),
    ("BALLARAT [Ballaarat] and BALLARAT EAST", "BALLARAT and BALLARAT EAST"),

    # markers are stripped; whitespace collapses; empties are safe
    ("«B»ALGEBRA«/B»", "ALGEBRA"),
    ("  ALGEBRAIC   FORMS  ", "ALGEBRAIC FORMS"),
    ("", ""),
])
def test_primary_headword(heading, expected):
    assert primary_headword(heading) == expected


def test_none_is_safe():
    assert primary_headword(None) == ""
