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


def test_corrections_come_back_and_a_fully_checked_page_is_proofread(tmp_path, monkeypatch):
    from test_scan_supplied import payload
    from wikikit.scan import desk, supplied
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    supplied.write(payload())
    sid = lambda name: desk.strip_id(1, 29, name)
    fixed = "¶\n'''abacus''' (ab′a̤-kus), ''n.''"                   # ạ -> a̤, from the scan
    [line] = desk.apply({sid("c1s1"): {"text": fixed, "status": "checked", "updated_by": "u_x"}})
    assert line.startswith("vol 1 p29: 1 reading(s) corrected") and line.endswith("level 1")
    page = supplied.read(1, 29)
    assert "(ab′a̤-kus)" in page["raw_text"] and 'level="1"' in page["raw_text"]
    rest = {sid(n): {"text": None, "status": "checked"} for n in ("head", "c1s2")}
    [line] = desk.apply(rest)
    assert line.endswith("level 3 (now proofread)")
    assert '<pagequality level="3" user="proofreader" />' in supplied.read(1, 29)["raw_text"]
