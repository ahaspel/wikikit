"""A supplied page carries the fetched page's section tags and links onto its
own text (EB1911 vol 24 p248's shape): a section before its headword, a
headless one at the top, ends where they stood, links on their words."""
from wikikit.scan.carry import lay_on, marks

FETCHED = ('<noinclude><pagequality level="1" user="x" />{{EB1911 Page Heading|x}}</noinclude>'
           '<section begin="s1" />the tail of the last article.\n<section end="s1" />\n'
           '<section begin="Saturnia" />\'\'\'SATURNIA\'\'\' (mod. \'\'Saturnia\'\'), a town; '
           'see {{1911link|Etruria}} and [[Vulci|the city]].\n<section end="Saturnia" />\n'
           '<section begin="s3" />\'\'\'SATURNIAN METRE\'\'\' (Lat. \'\'Saturnius\'\').\n'
           '<section end="s3" /><noinclude></noinclude>')
OURS = ('<noinclude><pagequality level="1" user="transcriber" />{{RunningHeader|x}}</noinclude>'
        "the tail of the last article.\n\n'''SATURNIA''' (mod. ''Saturnia''), a town; see Etruria "
        "and the city.\n\n'''SATURNIAN METRE''' (Lat. ''Saturnius'')."
        "<noinclude><references/></noinclude>")


def test_sections_and_links_go_back_where_their_words_are():
    out, unplaced = lay_on(OURS, marks(FETCHED))
    assert unplaced == []
    body = out.split("</noinclude>", 1)[1].rsplit("<noinclude>", 1)[0]
    assert body.startswith('<section begin="s1" />the tail')
    assert '<section end="s1" /><section begin="Saturnia" />\'\'\'SATURNIA\'\'\'' in body
    assert "see {{1911link|Etruria}} and [[Vulci|the city]]." in body
    assert '<section end="Saturnia" /><section begin="s3" />\'\'\'SATURNIAN METRE' in body
    assert body.endswith('<section end="s3" />')


def test_a_mark_whose_words_are_not_found_is_reported():
    ours = OURS.replace("see Etruria", "see the region")
    out, unplaced = lay_on(ours, marks(FETCHED))
    assert [m["text"] for m in unplaced] == ["Etruria"]
    assert "{{1911link|Etruria}}" not in out and "[[Vulci|the city]]" in out


def test_a_headword_is_found_inside_a_link_and_past_nested_templates():
    # vol 25 p943: [[Author:Ezra Stiles|'''STILES, EZRA''']]; and a template
    # nested two deep before a headword
    fetched = ('<section begin="s4" />[[Author:Ezra Stiles|\'\'\'STILES, EZRA\'\'\']] (1727–1795)'
               '<section begin="s5" />{{anchor|{{x|y}}}}\'\'\'STILLICIDIUM\'\'\' dripping')
    assert [m["headword"] for m in marks(fetched) if m["kind"] == "begin"] == ["STILESEZRA", "STILLICIDIUM"]


def test_a_headword_opens_one_section_even_when_the_next_begins_with_it():
    # vol 24 p765: SEWER, then SEWERAGE — whose letters start with SEWER's.
    # SEWERAGE's begin must not take SEWER's run, emptying SEWER's section
    # and giving the two articles one id.
    fetched = ("<section begin=\"s3\" />'''SEWER,''' a large drain.\n<section end=\"s3\" />\n"
               "<section begin=\"s4\" />'''SEWERAGE,''' a general term.\n<section end=\"s4\" />")
    ours = "'''SEWER,''' a large drain.\n\n'''SEWERAGE,''' a general term."
    out, unplaced = lay_on(ours, marks(fetched))
    assert unplaced == []
    assert out.startswith("<section begin=\"s3\" />'''SEWER,'''")
    assert "drain.\n\n<section end=\"s3\" /><section begin=\"s4\" />'''SEWERAGE,'''" in out


def test_a_section_of_templated_words_is_headless():
    # vol 26 p464: s1 is the last article's tail in {{EB1911 Fine Print|…}};
    # its headword is not TARTAN, which is past its end, in s2
    fetched = ("<section begin=\"s1\" />{{EB1911 Fine Print|Tartaglia's own account.}}<section end=\"s1\" />\n"
               "<section begin=\"s2\" />'''TARTAN''' (from F.)<section end=\"s2\" />")
    assert [m["headword"] for m in marks(fetched) if m["kind"] == "begin"] == ["", "TARTAN"]
    ours = "{{smaller block|Tartaglia's own account.}}\n\n'''TARTAN''' (from F.)"
    out, unplaced = lay_on(ours, marks(fetched))
    assert unplaced == []
    assert out.startswith("<section begin=\"s1\" />{{smaller block|Tartaglia")
    assert "<section end=\"s1\" /><section begin=\"s2\" />'''TARTAN'''" in out


def test_a_page_without_marks_is_untouched():
    assert lay_on(OURS, []) == (OURS, [])
    assert lay_on(OURS, None) == (OURS, [])
