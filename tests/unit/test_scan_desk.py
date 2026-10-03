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
]


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
