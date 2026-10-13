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


def test_the_replaced_pages_own_image_stands_where_our_crop_was():
    p = payload()
    supplied.carry_images(p, fetched="[[File:EB1911 Abacus - Capital.png|center|200px]]\n"
                                     "{{c|Fig. 1.—Capital of the Parthenon.}}", scan_width=600)
    assert "[[File:EB1911 Abacus - Capital.png|center|200px]]" in p["raw_text"]
    assert "fixture v01 p0029 c1f1.png" not in p["raw_text"]


def test_a_recorded_placement_is_kept_never_recomputed():
    p = payload()
    p["provenance"]["images"] = {"c1f1": ["[[File:Decided.png|center|100px]]"]}
    p["provenance"]["images_decided"] = "placed by looking at the scan"
    supplied.carry_images(p, fetched="[[File:Other.png|center|200px]]", scan_width=600)
    assert "Decided.png" in p["raw_text"] and "Other.png" not in p["raw_text"]


def test_a_region_of_the_commons_render_stands_for_a_figure_wikisource_lacks():
    p = payload()
    p["provenance"]["regions"] = {"c1f1": "{{Css image crop\n|Image = EB1911 - Volume 01.djvu\n|Page = 29}}"}
    supplied.reassemble(p)
    assert "{{Css image crop\n|Image = EB1911 - Volume 01.djvu" in p["raw_text"]
    assert "fixture v01 p0029 c1f1.png" not in p["raw_text"]
    assert "{{c|Capital of the Parthenon.}}" in p["raw_text"]


def test_an_empty_region_sets_no_figure():
    p = payload()
    p["provenance"]["regions"] = {"c1f1": ""}
    supplied.reassemble(p)
    assert "[[File:" not in p["raw_text"]


def test_the_carried_markup_survives_every_reassembly():
    p = payload()
    ws = "'''abacus''' (ab′ạ-kus), ''n.'' see {{1911link|Capital}} of the Parthenon."
    p["provenance"]["readings"]["c1s2"] = "{{c|Capital of the Parthenon.}}"
    supplied.carry_markup(p, fetched=ws)
    assert "{{1911link|Capital}}" in p["raw_text"] and p["provenance"]["carried"]["links"] == 1
    supplied.reassemble(p, fetched=ws)
    assert "{{1911link|Capital}}" in p["raw_text"]
    supplied.reassemble(p, carried=False)
    assert "{{1911link|Capital}}" not in p["raw_text"]
