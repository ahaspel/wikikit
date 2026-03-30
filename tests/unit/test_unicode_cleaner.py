from britannica.cleaners.unicode import normalize_unicode


def test_normalize_unicode_preserves_plain_ascii() -> None:
    text = "ABACUS"

    result = normalize_unicode(text)

    assert result == "ABACUS"


def test_normalize_unicode_preserves_subscripts() -> None:
    result = normalize_unicode("H\u2082O")  # H₂O
    assert result == "H\u2082O"


def test_normalize_unicode_preserves_superscripts() -> None:
    result = normalize_unicode("x\u00b2")  # x²
    assert result == "x\u00b2"


def test_normalize_unicode_composes_diacritics() -> None:
    # NFC composes combining characters
    result = normalize_unicode("e\u0301")  # e + combining acute
    assert result == "\u00e9"  # é
