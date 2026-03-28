from britannica.cleaners.unicode import normalize_unicode


def test_normalize_unicode_nfkc_fullwidth_chars() -> None:
    text = "ＡＢＣ１２３"

    result = normalize_unicode(text)

    assert result == "ABC123"


def test_normalize_unicode_preserves_plain_ascii() -> None:
    text = "ABACUS"

    result = normalize_unicode(text)

    assert result == "ABACUS"