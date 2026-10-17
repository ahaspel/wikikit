"""The parity gate: the desk's reader view (desk_render.js, under Node) renders
every text exactly as the engine's own chain does — or flags it as a leak.
The desk is a second renderer, tolerated only because this holds it to the
first.  Skipped where Node is not installed."""
import shutil

import pytest

from wikikit.scan.desk import desk_render, parity
from wikikit.scan.vocabulary import render_table

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")

SAME = [
    "'''bating'''<sup>1</sup> (bā′ting), ''n.'' [Verbal n. of ''bate''<sup>1</sup>, ''v.''] The act.",
    "especially at sea.—{{smaller|'''Bathymetric zone''', in ''zoögeog.'', one of the belts.}}",
    "{{smaller block|''Bating'' the outward respect due to his birth.\n{{right|''Bp. Burnet'', Hist. Own Times.}}}}",
    "first paragraph.\n\n'''second''' paragraph.",
    "{{sc|Kay-Shuttleworth}}, {{sc|Sir James}}",
    "{{smaller block|colored wool.}}\n(''b'') A garment of similar shape.",     # a block ends its paragraph
    "a quotation\n{{right|''Shak.'', As you Like it, ii. 4.}}\nand on",        # right does not
    "{{c|Capital of the Parthenon. ''A'', abacus.}}",
    "[< Gr. βαθύς, deep, + μέτρον, a measure.] (ka̤l) < > & †",
    "the coefficients ''a''<sub>0</sub>, ''a''<sub>1</sub> of the quantic",
    # formulas: braces are LaTeX's, inside a display or not
    "When <math>\\text{R}=0</math>, the covariant <math>\\alpha_x</math> is a factor of <math>f</math>.",
    "The two equations\n\n{{block center|<math>\\frac{\\partial f}{\\partial \\delta} = 5\\text{B}\\delta^4</math>,}}\n"
    "{{block center|<math>a < b</math>.}}\n\nyield by elimination",
    # tables: a formula set out in rows (FULMINIC ACID), spans and all
    "who proposed the formulae:\n\n{| style=\"margin:auto\"\n|C : N·OH||rowspan=2|O||N : CH\n|-\n"
    "|C : N·OH||N : C·OH\n|-\n|Steiner||colspan=2|Divers\n|}\n\nThe formulae of Kekulé",
    # a constitution with upright bonds, subscripts, a formula and italic in its cells
    "may be represented as\n\n{| style=\"margin:auto\"\n|H<sub>2</sub>C||CH—CH<sub>2</sub>\n|-\n"
    "|│||│\n|-\n|<math>a_1</math>||''Tropine''\n|}\n\nThese compounds",
    # a plain table of figures
    "{|\n|As||12||1\n|-\n|Decunx||11||11/12\n|}",
    # a table set as the print sets it: a heading row, rules on rows and
    # cells, figures set right, a span with a code, a brace over two rows
    "{| style=\"margin:auto\"\n!Name.!!Initial.!!Speed.\n|-{{Ts|bt}}\n|Principal lunar||M<sub>2</sub>"
    "||{{Ts|ar}}|28·9841042°\n|-\n|rowspan=2 {{Ts|ac}}|Lunar||{{Ts|ar|bb}}|·45426||1\n|-\n"
    "|rowspan=2|{{brace2|2|r}}||x\n|-{{Ts|bt|bb}}\n|colspan=3|''Long period.''\n|}",
    # a table the print frames and rules between its columns, and one ruled
    # round every cell (Wikisource's own classes)
    "{| class=\"_tablecolhdborder\"\n!Name.!!Speed.\n|-\n|Principal lunar||{{Ts|ar}}|28·98°\n|}",
    "{| class=\"_tablegridhdborder\"\n|a||b\n|-\n|c||d\n|}",
    # TIDE's schedule: a brace gathering the rows of one argument
    "{| style=\"margin:auto\"\n!Descriptive Name.!!Argument.\n|-{{Ts|bb}}\n|Larger elliptic||"
    "rowspan=3|{{brace2|3|l}}\n|-\n|Smaller elliptic\n|-\n|Variational\n|}",
    # a formula as a compact grid, its connectors in cells of their own
    "represented as\n\n{| class=\"chem-grid\"\n|H<sub>2</sub>C||·||CH||—||CH<sub>2</sub>\n|-\n"
    "|rowspan=3|│||||│||||│\n|-\n|||NMe||||CHOH\n|-\n|||│||||║\n|-\n|colspan=5|Tropine\n|}\n\nThese",
    # lines the book keeps as lines: verse in stanzas, centred, in small type,
    # with inline constructs and a source on its line; a list set line by line
    "Before the verse.\n\n<poem>line one\nline two\n\nstanza two</poem>\n\nAfter it.",
    "{{block center|<poem>line one\nline two</poem>}}",
    "{{smaller block|<poem>line one\nline two</poem>}}",
    "<poem>'''bold''' and ''it'' {{sc|Sc}} x<sup>2</sup> and <math>a^2</math>\n{{right|—Author}}</poem>",
    "<poem>Pike . . . . Turkey . . See ''Dir'aa''.\nPint . . . U. States . 0·8325 imperial pint.</poem>",
]


def test_a_table_is_laid_out_as_the_engine_lays_it_out():
    [shown] = desk_render(["A\n\n{| style=\"margin:auto\"\n|x||rowspan=2|y\n|-\n|z\n|}\n\nB"], render_table())
    assert shown["leaks"] == []
    assert '<td rowspan="2">y</td>' in shown["html"] and shown["html"].startswith("A<p><table")


def test_a_poem_keeps_its_lines_where_a_paragraph_joins_them():
    [shown] = desk_render(["one\ntwo\n\n<poem>one\ntwo</poem>"], render_table())
    assert shown["leaks"] == []
    assert shown["html"].startswith("one two<p>") and "one<br>two" in shown["html"]


def test_a_table_with_attributes_outside_the_vocabulary_leaks():
    [shown] = desk_render(["{| class=\"wikitable\"\n|a\n|}"], render_table())
    assert shown["leaks"]


def test_a_table_style_code_outside_the_vocabulary_leaks():
    # only the print's rules and alignment: no padding, no widths
    [shown] = desk_render(["{|\n|{{Ts|pl2}}|a||b\n|}"], render_table())
    assert shown["leaks"] == ["{{Ts|pl2}}"]


def test_a_table_sets_its_headings_rules_figures_and_braces_as_the_engine_does():
    [shown] = desk_render(["{|\n!a!!b\n|-{{Ts|bt}}\n|rowspan=2|{{brace2|2|l}}||{{Ts|ar}}|1·5\n|}"],
                          render_table())
    assert shown["leaks"] == []
    html = shown["html"]
    assert "<th>a</th><th>b</th>" in html and '<tr style="border-top:1px solid">' in html
    assert '<td style="text-align:right">1·5</td>' in html and "scaleY(2)" in html


def test_a_formula_is_left_whole_for_the_page_to_typeset():
    [shown] = desk_render(["of <math>\\frac{a}{b} < c^{2}</math> and"], render_table())
    assert shown == {"html": 'of <span class="tex">\\frac{a}{b} &lt; c^{2}</span> and', "leaks": []}


@pytest.mark.parametrize("text", SAME)
def test_the_desk_renders_as_the_engine_does(text):
    assert parity([text]) == []


@pytest.mark.parametrize("text", [
    "a template the engine does not read: {{lang|grc|λόγος}}",
    "a link [[Somewhere]] left in",
    "an unbalanced ''italic",
])
def test_markup_outside_the_vocabulary_is_a_leak_on_the_desk(text):
    [shown] = desk_render([text], render_table())
    assert shown["leaks"]


def test_the_joining_signal_is_not_text():
    assert parity(["¶\n'''abacus''' (ab′a̤-kus), ''n.''"]) == []


@pytest.fixture
def book_with_arguments(monkeypatch):
    """A book whose own constructs take arguments (EB1911's, 2026-10-15): an
    empty one, two shown, one never shown, one made an attribute — and a
    margin heading, whose anchor the engine makes from its words."""
    import dataclasses
    from wikikit import corpora
    from wikikit.scan.vocabulary import Construct
    book = dataclasses.replace(corpora.current_corpus(), scan_constructs=(
        Construct("eb1911 footer initials", "{{EB1911 footer initials|", "}}", "initials",
                  args=("", "Q. X. Z.")),
        Construct("dual line", "{{dual line|", "}}", "two lines", args=("QQA1QQ", "QQA2QQ")),
        Construct("ditto", "{{ditto|", "}}", "a ditto mark", args=("QQA1QQ",)),
        Construct("rule", "{{rule|", "}}", "a rule", args=("13em",)),
        Construct("eb1911 shoulder heading", "{{EB1911 Shoulder Heading|", "}}", "margin")))
    monkeypatch.setattr(corpora, "current_corpus", lambda: book)


@pytest.mark.parametrize("text", [
    "the last words.\n{{EB1911 footer initials||F. R. C.}}",
    "{| style=\"margin:auto\"\n|{{dual line|Discharge immediately|after rest}}||48\n|}",
    "{| style=\"margin:auto\"\n|1901||days\n|-\n|1902||{{ditto|days}}\n|}",
    "A table follows.\n\n{{rule|8em}}\n\nAnd below it.",
    "Body text {{EB1911 Shoulder Heading|Santa Laura.}}continues here.",
])
def test_a_template_of_arguments_renders_as_the_engine_renders_it(book_with_arguments, text):
    assert parity([text]) == []


def test_an_argument_the_engine_never_shows_is_not_shown(book_with_arguments):
    entry = render_table()["ditto"]
    assert entry["order"] == [] and entry["parts"] == ["″"]
    [shown] = desk_render(["{{ditto|days}}"], render_table())
    assert shown == {"html": "″", "leaks": []}


def test_a_page_checked_strip_by_strip_is_checked_whole_once_edited(tmp_path, monkeypatch):
    """A page proofread on the old desk, its every strip checked, is level 3;
    edited whole, only its own check counts — the strips' checks were of a
    text it no longer has."""
    from test_scan_supplied import payload
    from wikikit.scan import desk, supplied
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    page = payload()
    page["provenance"]["checked"] = {n: {"by": "x"} for n in ("head", "c1s1", "c1s2")}
    supplied.write(supplied.reassemble(page))
    assert supplied.level(supplied.read(1, 29)) == 3
    head, body, _ = supplied.page_parts(supplied.read(1, 29)["raw_text"])
    pid = supplied.page_id(1, 29)
    [line] = desk.apply_pages({pid: {"head": head, "text": body + " more", "status": "unchecked"}})
    assert line == "vol 1 p29: corrected, level 1"
    [line] = desk.apply_pages({pid: {"head": head, "text": body + " more", "status": "checked"}})
    assert line == "vol 1 p29: text unchanged, level 3 (now proofread)"


def test_a_page_proofread_whole_keeps_its_text(tmp_path, monkeypatch):
    """The desk proofreads a page as one text: saved, the page keeps it —
    reassembly no longer makes it from the readings — and a check makes it
    proofread."""
    from test_scan_supplied import payload
    from wikikit.scan import desk, supplied
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    supplied.write(payload())
    head, body, _ = supplied.page_parts(supplied.read(1, 29)["raw_text"])
    fixed = body.replace("(ab′ạ-kus)", "(ab′a̤-kus)") + "\n\nA line the cut missed."
    pid = supplied.page_id(1, 29)
    [line] = desk.apply_pages({pid: {"head": head, "text": fixed, "status": "unchecked", "updated_by": "u_x"}})
    assert line == "vol 1 p29: corrected, level 1"
    page = supplied.reassemble(supplied.read(1, 29))            # as any later assembly would
    assert supplied.page_parts(page["raw_text"])[1] == fixed
    assert page["provenance"]["page_edited"]["by"] == "u_x"
    assert desk.apply_pages({pid: {"head": head, "text": fixed, "status": "unchecked"}}) == []
    [line] = desk.apply_pages({pid: {"head": head, "text": fixed, "status": "checked"}})
    assert line == "vol 1 p29: text unchanged, level 3 (now proofread)"
    raw = supplied.read(1, 29)["raw_text"]
    assert raw.startswith('<noinclude><pagequality level="3" user="proofreader" />{{RunningHeader|')
    assert raw.endswith("A line the cut missed.<noinclude><references/></noinclude>")


def test_a_page_is_its_fetchers_until_given_away(tmp_path, monkeypatch):
    """Each page is proofread by whoever fetched it, on their own key, until
    it is given to someone else at the desk; given back, it is theirs again."""
    from test_scan_supplied import payload
    from wikikit.scan import desk, supplied
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    page = payload()
    page["provenance"]["fetched_by"] = "ahaspel"
    supplied.write(page)
    pid = supplied.page_id(1, 29)
    head, body, _ = supplied.page_parts(page["raw_text"])
    assert supplied.owner(supplied.read(1, 29)) == "ahaspel"
    assert desk.apply_pages({pid: {"head": head, "text": body, "owner": "ahaspel"}}) == []
    [line] = desk.apply_pages({pid: {"head": head, "text": body, "owner": "alaemmle"}})
    assert line == "vol 1 p29: text unchanged, level 1, given to alaemmle"
    after = supplied.read(1, 29)
    assert supplied.owner(after) == "alaemmle" and "page_edited" not in after["provenance"]
    [line] = desk.apply_pages({pid: {"head": head, "text": body, "owner": "ahaspel"}})
    assert "owner" not in supplied.read(1, 29)["provenance"]


@pytest.mark.parametrize("text", [
    # what assembly writes into a page: a figure, a figure with type beside
    # it, the mark that the page opens a paragraph, a caption's lines
    "{{nop}}'''abacus''' (ab′a̤-kus), ''n.''",
    "before\n\n[[File:century v01 p0029 c1f1.png|center|251px]]\n{{c|Fig. 1.}}\n\nafter",
    "text {{img float|file=century v01 p0034 c3f1.png|width=181px|align=left|cap=''a'', Abdomen.}} runs on",
    "x {{img float|file=a b.png|width=99px|align=right|cap=Abutment.<br />''A, A,'' arch.}} y",
    # after a note in small type the paragraph runs on through the rest of a
    # sentence, and opens a new one otherwise (`elements._opens_paragraph_after_note`)
    "a\n{{smaller block|q\n{{right|S, ii.}}}}\n{{smaller block|r}}\nb",
    "a\n{{smaller block|q}}\nhence the name",
    "a\n{{smaller block|q}}\n2. To be averse",
    "a\n{{smaller block|q}}\n{{sc|Bacon}} said",
    "a\n{{smaller block|q,}}\n\nand so on",
    "'''''hate'''''",
    # an indent: a line of verse set in, or a space of fixed width
    "<poem>Line one\n{{gap}}line two\n{{gap|1em}}three\n{{em|3}}four\n{{gap|2.5em}}five</poem>",
    "Total{{gap}}12 {{em}}a{{gap|4}}b",
])
def test_a_whole_page_renders_as_the_engine_does(text):
    assert parity([text]) == []


def test_a_figure_shows_the_desks_own_copy():
    [shown] = desk_render(["[[File:x y.png|center|20px]]"], render_table())
    assert 'src=""' in shown["html"]
    from wikikit.scan.desk import DESK_RENDER_JS
    import json, subprocess
    runner = ("const r=require(process.argv[1]);"
              "process.stdout.write(JSON.stringify(r.render('[[File:x y.png|center|20px]]', "
              "JSON.parse(process.argv[2]), {'x y.png': 'figures/p/c1f1.png'})))")
    out = json.loads(subprocess.run(["node", "-e", runner, str(DESK_RENDER_JS), json.dumps(render_table())],
                                    capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert 'src="figures/p/c1f1.png"' in out["html"] and out["leaks"] == []


@pytest.mark.parametrize("line", [
    "now written ''o''', a reduced form of ''of''",           # a run of 3 left open
    "< Hind. '''ădālat'', < Ar. '''adāla''(''t'')",            # bold and italic crossed
    "''Monomero''somata'''' and",                              # four apostrophes
])
def test_apostrophes_the_edition_reads_otherwise_are_a_leak(line):
    [shown] = desk_render([line], render_table())
    assert line in shown["leaks"]


def _desk_curly(texts: list[str]) -> list[str]:
    import json, subprocess
    from wikikit.scan.desk import DESK_RENDER_JS
    runner = ("const r=require(process.argv[1]);let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
              "const a=JSON.parse(s);process.stdout.write(JSON.stringify(a.texts.map(t=>r.curlyQuotes(t,a.q))))});")
    out = subprocess.run(["node", "-e", runner, str(DESK_RENDER_JS)], capture_output=True, text=True,
                         encoding="utf-8", check=True,
                         input=json.dumps({"texts": texts, "q": render_table()["quotes"]}))
    return json.loads(out.stdout)


def test_the_desk_sets_quotation_marks_curly_as_the_engine_does():
    """A book that sets its quotation marks curly (`Corpus.curly_quotes`):
    the desk's reader view sets them as the engine does, every case the
    engine's own tests hold it to, and words in any alphabet."""
    import test_curly_quotes as cases
    from wikikit.pipeline.stages.preprocess import curly_quotes
    texts = ['the explanation of "overtides" and "compound tides."', '“The Proud." The duke',
             'and "Riflemen, Form!” published', "only to “long” waves", 'a 12" gun and a 9" howitzer',
             "15° 20' 30\" and 16° 2' 4\"", 'cwt. " " ditto', '{| style="margin:auto"\n|a||b\n|}',
             '<span style="font-family:"x"">a</span>', '<math>\text{"a"}</math>',
             '{{Css image crop|Description = "Fig. 2"}}', '<ref name="Hart">a</ref>',
             '[[File:Sail plan of the "Santa Maria", 1893.png|center|400px]]', '[[The Nigger of the "Narcissus"]]',
             '{{img float|file=The "Iowa".jpg|cap=x}}', '[[Conrad|The "Narcissus"]]',
             'he said "first<ref>a note</ref> second" then', '(2 sin 0)"-n(2 sin 0)" 2',
             '{{EB1911 article link|Alabama Arbitration|"Alabama" Arbitration}}',
             "spirited away, \"''enlevé'',\" by some person", '"Été" and "ŝi" and "Ἀθῆναι" and "über"',
             'as, "such rudiments or \'\'abecedaries,\'\'" \'\'Fuller\'\'', 'a "b"\nc "d" e\n"f']
    assert cases.curly_quotes is curly_quotes
    assert _desk_curly(texts) == [curly_quotes(t) for t in texts]
