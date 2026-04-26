from britannica.cleaners.unicode import normalize_unicode, replace_print_artifacts


def test_normalize_unicode_preserves_plain_ascii() -> None:
    text = "ABACUS"

    result = normalize_unicode(text)

    assert result == "ABACUS"


def test_normalize_unicode_preserves_subscripts() -> None:
    result = normalize_unicode("H₂O")  # H₂O
    assert result == "H₂O"


def test_normalize_unicode_preserves_superscripts() -> None:
    result = normalize_unicode("x²")  # x²
    assert result == "x²"


def test_normalize_unicode_composes_diacritics() -> None:
    # NFC composes combining characters
    result = normalize_unicode("é")  # e + combining acute
    assert result == "é"  # é


def test_replace_print_artifacts_fullwidth_operators() -> None:
    # Fullwidth + and = in chem formulas -> ASCII
    text = "K₂Cr₂O₇＋4H₂SO₄＝K₂SO₄"
    result = replace_print_artifacts(text)
    assert "＋" not in result
    assert "＝" not in result
    # Subscripts must survive
    assert "₂" in result
    assert result == "K₂Cr₂O₇+4H₂SO₄=K₂SO₄"


def test_replace_print_artifacts_fullwidth_punctuation() -> None:
    # a<b>c-d via fullwidth forms
    result = replace_print_artifacts("a＜b＞c－d")
    assert result == "a<b>c-d"


def test_replace_print_artifacts_avoirdupois_pound() -> None:
    # ℔ -> lb (multi-char replacement)
    result = replace_print_artifacts("Weight: 5℔.")
    assert result == "Weight: 5lb."


def test_replace_print_artifacts_apothecary_units() -> None:
    # ℥ -> oz, ℈ -> scruple
    result = replace_print_artifacts("Dose: 2℥, 1℈.")
    assert result == "Dose: 2oz, 1scruple."


def test_replace_print_artifacts_multiplication_dingbat() -> None:
    # ✕ (dingbat) -> × (MULTIPLICATION SIGN)
    result = replace_print_artifacts("3✕ magnification")
    assert result == "3× magnification"


def test_replace_print_artifacts_preserves_math_operators() -> None:
    # Genuine math operators must pass through unchanged
    # minus, square root, integral, infinity, summation, partial
    text = "−x + √y = ∫∞ + ∑ + ∂"
    assert replace_print_artifacts(text) == text


def test_replace_print_artifacts_preserves_subscripts_and_superscripts() -> None:
    text = "H₂O and x² + x₃"
    assert replace_print_artifacts(text) == text


def test_replace_print_artifacts_preserves_greek_and_diacritics() -> None:
    # Polytonic Greek + Latin-extended transliterations
    text = "ἀαβγ ḥadith ṭasrif"
    assert replace_print_artifacts(text) == text


def test_replace_print_artifacts_empty_and_ascii_pass_through() -> None:
    assert replace_print_artifacts("") == ""
    assert replace_print_artifacts("Hello, world!") == "Hello, world!"


def test_replace_print_artifacts_idempotent() -> None:
    once = replace_print_artifacts("5℔＝5lb")
    twice = replace_print_artifacts(once)
    assert once == twice
