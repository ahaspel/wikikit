"""A footer that NAMES its first author must not lose that author.

`{{EB1911 footer initials}}` is written two ways.  Positionally:

    {{EB1911 footer initials|William Wallace|W. W.|name2=…|initials2=…}}

or entirely in named arguments:

    {{EB1911 footer initials|name=Hugh Chisholm|initials=H. Ch.}}

`_parse_contributors` read positional args and `name2`..`name9`, but never a
bare `name=` or `name1=` — so the FIRST author of a named-form footer was
dropped silently.  63 `name=` and 45 `name1=` occurrences across the corpus,
18 of them footers that parsed to nothing whatever.

The article that made it visible is SHERBROOKE, ROBERT LOWE, which shipped with
an empty byline: its only signature is Hugh Chisholm's — the editor of the
eleventh edition, uncredited on an article he signed.

Every shape below is taken verbatim from the corpus, not invented.
"""
from britannica.pipeline.stages.extract_contributors import (
    _iter_footers, _parse_contributors)


def parse(raw: str) -> list[tuple[str, str]]:
    return [(c["full_name"], c["initials"])
            for content in _iter_footers(raw) for c in _parse_contributors(content)]


def test_a_named_first_author_is_read():
    """SHERBROOKE, ROBERT LOWE — the byline that was empty."""
    assert parse("{{EB1911 footer initials|name=Hugh Chisholm|initials=H. Ch.}}") == [
        ("Hugh Chisholm", "H. Ch.")]


def test_the_numbered_first_form_is_read_too():
    assert parse(
        "{{EB1911 footer initials|name1=Philip Joseph Hartog|initials1=P. J. H.}}"
    ) == [("Philip Joseph Hartog", "P. J. H.")]


def test_positional_first_author_is_unchanged():
    """The regression guard on the 8,000 footers that already worked."""
    assert parse(
        "{{EB1911 footer initials|Francis Ernest Wentworth-Sheilds|F. E. W.-S.}}"
    ) == [("Francis Ernest Wentworth-Sheilds", "F. E. W.-S.")]


def test_positional_first_plus_named_second_is_not_doubled():
    """ARABIAN PHILOSOPHY's shape — 331 in the corpus, the commonest mixed form.

    Reading the named block for the first author as WELL as the positional one
    would credit the lead author twice.
    """
    assert parse(
        "{{EB1911 footer initials|William Wallace (1844-1897)|W. W.|"
        "name2=Griffithes Wheeler Thatcher|initials2=G. W. T.}}"
    ) == [("William Wallace (1844-1897)", "W. W."),
          ("Griffithes Wheeler Thatcher", "G. W. T.")]


def test_all_named_two_authors():
    """CELLINI, BENVENUTO — 47 in the corpus; previously only Jones survived."""
    assert parse(
        "{{EB1911 footer initials|name=William Michael Rossetti|initials=W. M. R.|"
        "name2=Edward Alfred Jones|initials2=E. A. J.}}"
    ) == [("William Michael Rossetti", "W. M. R."),
          ("Edward Alfred Jones", "E. A. J.")]


def test_three_named_authors():
    """CROMWELL, OLIVER."""
    assert parse(
        "{{EB1911 footer initials|name1=Philip Chesney Yorke|initials1=P. C. Y.|"
        "name2=Charles Francis Atkinson|initials2=C. F. A.|"
        "name3=Ronald John McNeill|initials3=R. J. M.}}"
    ) == [("Philip Chesney Yorke", "P. C. Y."),
          ("Charles Francis Atkinson", "C. F. A."),
          ("Ronald John McNeill", "R. J. M.")]


def test_a_named_footer_inside_a_font_size_wrapper():
    """Most named footers sit inside `{{Fs|108%|…}}`; the reader must still see
    them.  (The wrapper was never the problem — worth pinning so it stays that
    way.)"""
    assert parse(
        "{{Fs|108%|{{EB1911 footer initials|name=Hugh Chisholm|initials=H. Ch.}}}}"
    ) == [("Hugh Chisholm", "H. Ch.")]
