from britannica.xrefs.extractor import extract_xrefs


def test_extract_xrefs_finds_explicit_see_reference() -> None:
    text = "For the related topic, See ABANDONMENT."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "see"
    assert results[0]["surface_text"] == "See ABANDONMENT"
    assert results[0]["normalized_target"] == "ABANDONMENT"


def test_extract_xrefs_finds_see_also_reference() -> None:
    text = "See also ABACUS."

    results = extract_xrefs(text)

    assert len(results) == 1
    assert results[0]["xref_type"] == "see_also"
    assert results[0]["surface_text"] == "See also ABACUS"
    assert results[0]["normalized_target"] == "ABACUS"