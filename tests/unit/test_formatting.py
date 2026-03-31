"""Tests for formatting preservation through the cleaning pipeline."""
from britannica.cleaners.reflow import reflow_paragraphs
from britannica.cleaners.unicode import normalize_unicode


def test_italic_markers_survive_reflow():
    text = "The \u00abI\u00bbArs Poetica\u00ab/I\u00bb of Horace."
    result = reflow_paragraphs(text)
    assert "\u00abI\u00bb" in result
    assert "\u00ab/I\u00bb" in result


def test_bold_markers_survive_reflow():
    text = "A \u00abB\u00bbvery important\u00ab/B\u00bb point."
    result = reflow_paragraphs(text)
    assert "\u00abB\u00bb" in result
    assert "\u00ab/B\u00bb" in result


def test_small_caps_markers_survive_reflow():
    text = "Written by \u00abSC\u00bbJ. Smith\u00ab/SC\u00bb."
    result = reflow_paragraphs(text)
    assert "\u00abSC\u00bb" in result
    assert "\u00ab/SC\u00bb" in result


def test_subscripts_survive_nfc():
    text = "H\u2082O and CO\u2082"
    result = normalize_unicode(text)
    assert "\u2082" in result  # NFC preserves subscripts


def test_superscripts_survive_nfc():
    text = "x\u00b2 + y\u00b2"
    result = normalize_unicode(text)
    assert "\u00b2" in result


def test_greek_survives_reflow():
    text = "The Greek word \u03b1\u03bb\u03c6\u03b1 means first."
    result = reflow_paragraphs(text)
    assert "\u03b1\u03bb\u03c6\u03b1" in result


def test_footnote_markers_survive_reflow():
    text = "Some text\u00abFN:A footnote.\u00ab/FN\u00bb more text."
    result = reflow_paragraphs(text)
    assert "\u00abFN:" in result
    assert "\u00ab/FN\u00bb" in result


def test_link_markers_survive_reflow():
    text = "See \u00abLN:Aristotle|Aristotle\u00ab/LN\u00bb for details."
    result = reflow_paragraphs(text)
    assert "\u00abLN:" in result
    assert "\u00ab/LN\u00bb" in result


def test_image_markers_not_reflowed():
    text = "Text before.\n\n{{IMG:test.png}}\n\nText after."
    result = reflow_paragraphs(text)
    assert "{{IMG:test.png}}" in result
    assert "Text before.\n\n{{IMG:" in result


def test_table_markers_not_reflowed():
    text = "Text.\n\n{{TABLE:\nRow 1\nRow 2\n}TABLE}\n\nMore."
    result = reflow_paragraphs(text)
    assert "Row 1\nRow 2" in result


def test_verse_markers_not_reflowed():
    text = "He said:\n\n{{VERSE:\nLine one\nLine two\n}VERSE}\n\nThen continued."
    result = reflow_paragraphs(text)
    assert "Line one\nLine two" in result


def test_math_markers_survive_reflow():
    text = "The equation \u00abMATH:x^2\u00ab/MATH\u00bb is quadratic."
    result = reflow_paragraphs(text)
    assert "\u00abMATH:x^2\u00ab/MATH\u00bb" in result


def test_section_markers_survive_reflow():
    text = "\u00abSEC:Abacus\u00bbABACUS\nA device.\n\n\u00abSEC:Abalone\u00bbABALONE\nA shellfish."
    result = reflow_paragraphs(text)
    assert "\u00abSEC:Abacus\u00bb" in result
    assert "\u00abSEC:Abalone\u00bb" in result


def test_unicode_fractions_survive_nfc():
    result = normalize_unicode("\u00bd and \u00be")  # ½ and ¾
    assert "\u00bd" in result
    assert "\u00be" in result
