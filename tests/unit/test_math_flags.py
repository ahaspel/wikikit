"""The look-alike check: one symbol read as Greek in one place and Latin in
another, on one page, is flagged where it is the fewer reading."""
from wikikit.scan.math_flags import lookalikes


def _page(**readings):
    return {"provenance": {"readings": readings}}


def test_alpha_and_a_with_one_subscript_flags_the_fewer():
    p = _page(c1s1=r"<math>x_1 = \alpha_{11}\xi_1 + \alpha_{12}\xi_2</math>",
              c1s2=r"<math>x_2 = a_{21}\xi_1 + \alpha_{22}\xi_2</math> the <math>\alpha_{11}</math>, <math>a_{11}</math>")
    f = lookalikes(p)
    assert [x["text"] for x in f["c1s2"]] == ["a_{11}"]
    assert "c1s1" not in f
    assert "alpha" in f["c1s2"][0]["reason"]


def test_different_subscripts_or_one_reading_are_not_flagged():
    assert lookalikes(_page(c1s1=r"<math>a_0 + \alpha_1</math> and <math>a_0</math>")) == {}
    assert lookalikes(_page(c1s1=r"<math>\alpha_{11}</math>", c1s2=r"<math>\alpha_{11}</math>")) == {}


def test_text_outside_math_is_not_read():
    assert lookalikes(_page(c1s1=r"the a_{11} <math>\alpha_{11}</math>")) == {}
