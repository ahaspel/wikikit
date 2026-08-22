"""The contributor slug is a URL id, so it must keep apart everyone EB1911 kept
apart — the opposite contract to the dedup key next door in the same module.

These pairs are the whole reason the encoding is not a plain slugify: in each
one the rolls distinguish two real people using nothing but the separator
between initials, and a slugifier that folds separators merges them.
"""
from britannica.contributors.names import contributor_slug, normalize_initials_token

# (signature A, signature B, who they are) — DIFFERENT people whose signatures
# differ only in how the initials are separated.
DISTINCT_PAIRS = [
    ("J. F.-K.", "J. F. K.", "Fitzmaurice-Kelly vs Furman Kemp"),
    ("A. H.-S.", "A. H. S.", "Houtum-Schindler vs Sayce"),
    ("W. M.-L.", "W. M. L.", "Meyer-Lübke vs Lindsay"),
    ("R. M‘L.", "R. M. L.", "M'Lachlan vs Leslie"),
    ("A. S.-P.", "A. Sp.", "St Paul vs Sharp"),
    ("L. D.", "L. D.*", "the star is EB1911's own disambiguator"),
]


def test_separators_are_not_flattened():
    for a, b, who in DISTINCT_PAIRS:
        assert contributor_slug(a) != contributor_slug(b), (
            f"{who}: {a!r} and {b!r} both slug to {contributor_slug(a)!r}")


def test_dedup_key_deliberately_does_flatten_them():
    """Guards the distinction between the two functions.  If someone ever
    'simplifies' the slug into the dedup key, four pairs of contributors merge
    and share a URL — so pin that these two really do disagree, on purpose."""
    merged = [(a, b) for a, b, _ in DISTINCT_PAIRS
              if normalize_initials_token(a) == normalize_initials_token(b)]
    assert merged, "the dedup key is supposed to be lossy; it no longer is"


def test_known_encodings():
    assert contributor_slug("C. A.") == "c-a"
    assert contributor_slug("L. D.*") == "l-d-star"
    assert contributor_slug("W. de W. A.") == "w-de-w-a"
    assert contributor_slug("J. H. van’t H.") == "j-h-vant-h"
    assert contributor_slug("E. Hü.") == "e-hu"          # accents fold
    assert contributor_slug("E. O'M.") == "e-om"         # apostrophes drop
    assert contributor_slug("J. A. M‘N.") == "j-a-mn"    # curly ones too


def test_url_safe_and_total():
    import re
    for sig in ["C. A.", "L. D.*", "J. F.-K.", "E. Hü.", "J. H. van’t H.",
                "R. M‘L.", "A. Sp.", "W. de W. A."]:
        s = contributor_slug(sig)
        assert s and re.fullmatch(r"[a-z0-9_-]+", s), f"{sig!r} -> {s!r}"
    # Total over junk: never raises, and never returns a leading/trailing joiner
    # that would produce `/contributors.html#-` style URLs.
    for junk in ["", "   ", ".", "*", "-", "..--.."]:
        s = contributor_slug(junk)
        assert not s.startswith("-") and not s.endswith("-") or s == "-star", s
