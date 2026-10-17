"""Straight quotation marks set curly, as the book prints them (EB1911,
2026-10-16: Wikisource's transcribers and our readings mixed the two); a
straight mark that is no quotation, or that is markup, left as it is."""
from wikikit.pipeline.stages.preprocess import curly_quotes


def test_a_straight_pair_is_set_curly():
    assert curly_quotes('the explanation of "overtides" and "compound tides."') == \
        "the explanation of “overtides” and “compound tides.”"


def test_a_pair_half_straight_is_set_whole():
    assert curly_quotes('“The Proud." The duke') == "“The Proud.” The duke"
    assert curly_quotes('and "Riflemen, Form!” published') == "and “Riflemen, Form!” published"


def test_curly_marks_are_left_as_they_are():
    s = "only to “long” waves"
    assert curly_quotes(s) == s


def test_inches_and_seconds_of_arc_are_no_quotation():
    for s in ('a 12" gun and a 9" howitzer', "15° 20' 30\" and 16° 2' 4\"", 'between 3" and 5" long'):
        assert curly_quotes(s) == s


def test_a_lone_mark_is_left():
    s = 'cwt. " " ditto'
    assert curly_quotes(s) == s


def test_markup_is_never_touched():
    for s in ('{| style="margin:auto"\n|a||b\n|}', '<span style="font-family:"x"">a</span>',
              '<math>\\text{"a"}</math>', '{{Css image crop|Description = "Fig. 2"}}',
              '<ref name="Hart">a</ref>'):
        assert curly_quotes(s) == s


def test_a_file_or_link_name_is_never_touched():
    for s in ('[[File:Sail plan of the "Santa Maria", 1893.png|center|400px]]',
              '[[The Nigger of the "Narcissus"]]',
              '{{img float|file=The "Iowa".jpg|cap=x}}'):
        assert curly_quotes(s) == s
    # the words a link shows are text
    assert curly_quotes('[[Conrad|The "Narcissus"]]') == "[[Conrad|The “Narcissus”]]"


def test_a_pair_never_spans_markup():
    s = 'he said "first<ref>a note</ref> second" then'
    assert curly_quotes(s) == s


def test_a_link_or_template_bar_opens_nothing():
    s = '{{EB1911 article link|Alabama Arbitration|"Alabama" Arbitration}}'
    assert curly_quotes(s) == s


def test_a_sign_opens_no_quotation():
    s = '(2 sin 0)"-n(2 sin 0)" 2'
    assert curly_quotes(s) == s


def test_offsets_are_kept():
    s = 'a "b" <i>c</i> "d"\nnext "e"'
    out = curly_quotes(s)
    assert len(out) == len(s) and out == "a “b” <i>c</i> “d”\nnext “e”"


def test_our_readings_set_apostrophes_curly_too():
    """A book whose readings are set curly as read (`Corpus.curly_readings`):
    an apostrophe within a word as well — never a run of them, which is
    markup, nor a prime beside a digit."""
    from wikikit.scan.supplied import curly_marks
    assert curly_marks("I'll ''able'' 'em; o'clock; \"don't\"") == "I’ll ''able'' 'em; o’clock; “don’t”"
    for s in ("''abduce'''s", "'''3'''†", "a 5' 6\" man", "15° 20' 30\""):
        assert curly_marks(s) == s
