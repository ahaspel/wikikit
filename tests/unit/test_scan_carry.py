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


def test_a_page_without_marks_is_untouched():
    assert lay_on(OURS, []) == (OURS, [])
    assert lay_on(OURS, None) == (OURS, [])
