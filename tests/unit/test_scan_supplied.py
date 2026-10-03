"""A supplied page: its readings are the source, its text is assembled from
them, and its figures are regenerated from the scan.  No model is called: the
payload is built as `transcribe_page` would leave it."""
import json

from PIL import Image, ImageDraw

from wikikit.image_assets import local_image_filename
from wikikit.scan import supplied


def payload():
    items = [
        {"name": "head", "kind": "head", "column": 0, "box": [0, 0, 600, 30]},
        {"name": "c1s1", "kind": "text", "column": 1, "box": [0, 30, 300, 200]},
        {"name": "c1f1", "kind": "figure", "column": 1, "box": [20, 200, 280, 380]},
        {"name": "c1s2", "kind": "text", "column": 1, "box": [20, 380, 280, 410]},
    ]
    p = {"volume": 1, "page_number": 29, "source": "scan", "title": "scan n28",
         "provenance": {"angle": 0.0, "column_width": 300, "items": items,
                        "readings": {"head": "{{RunningHeader|abacus|4|abannition}}",
                                     "c1s1": "¶\n'''abacus''' (ab′ạ-kus), ''n.''",
                                     "c1s2": "{{c|Capital of the Parthenon.}}"},
                        "figure_files": {"c1f1": "fixture v01 p0029 c1f1.png"}}}
    return supplied.reassemble(p)


def test_the_page_text_is_assembled_from_its_readings():
    p = payload()
    assert "'''abacus''' (ab′ạ-kus)" in p["raw_text"]
    assert "[[File:fixture v01 p0029 c1f1.png|center|312px]]\n{{c|Capital of the Parthenon.}}" in p["raw_text"]


def test_a_correction_to_a_reading_reassembles_the_page():
    p = payload()
    p["provenance"]["readings"]["c1s1"] = "¶\n'''abacus''' (ab′a̤-kus), ''n.''"     # ạ -> a̤
    supplied.reassemble(p)
    assert "(ab′a̤-kus)" in p["raw_text"] and "(ab′ạ-kus)" not in p["raw_text"]


def test_figures_are_regenerated_from_the_scan(tmp_path, monkeypatch):
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    scan = Image.new("RGB", (600, 450), "white")
    ImageDraw.Draw(scan).rectangle((40, 220, 260, 360), fill="black")
    p = payload()
    [dest] = supplied.materialize_figures(p, scan)
    assert dest.name == local_image_filename("fixture v01 p0029 c1f1.png")
    crop = Image.open(dest)
    assert crop.size == (260, 180)
    assert crop.convert("L").getpixel((130, 90)) == 0           # the picture is in it


def test_a_supplied_page_round_trips(tmp_path, monkeypatch):
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    path = supplied.write(payload())
    assert path.relative_to(tmp_path).as_posix() == "data/supplied/vol_01/vol01-page0029.json"
    assert supplied.read(1, 29) == json.loads(path.read_text(encoding="utf-8"))
