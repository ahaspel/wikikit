"""Tests that unicode normalization preserves sub/superscripts and fractions."""
from britannica.cleaners.unicode import normalize_unicode


def test_subscripts_survive_nfc():
    text = "H₂O and CO₂"
    result = normalize_unicode(text)
    assert "₂" in result  # NFC preserves subscripts


def test_superscripts_survive_nfc():
    text = "x² + y²"
    result = normalize_unicode(text)
    assert "²" in result


def test_unicode_fractions_survive_nfc():
    result = normalize_unicode("½ and ¾")  # ½ and ¾
    assert "½" in result
    assert "¾" in result
