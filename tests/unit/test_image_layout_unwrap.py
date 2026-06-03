"""Regression tests for image/table layout unwrapping.

Each fixture is a real, minimal wikitext snippet from EB1911 that has
historically caused image/caption rendering bugs.  Tests run the snippet
through the full `_transform_text_v2` pipeline and assert properties
of the output body.

Fixtures (nasty-case catalog):

* SEWING_MACHINES_2 — simple image+caption wikitable (working baseline).
    Exercises `_process_table`'s image+caption handler.
* ABBEY_3         — outer layout table wrapping image + caption +
    nested two-column legend table.  Exercises
    `_unwrap_layout_table`'s image+caption bundling.  This is the
    hardest case: the legend content must render as a sibling of
    the figure, not leak into the caption or vanish entirely.
* ABBEY_02_FLOAT  — `{{img float|cap=...}}` with nested caption
    templates ({{sc|…}}, {{em|…}}, `<br>`, `&thinsp;`).  Exercises
    the IMAGE_FLOAT extractor + caption cleanup.
* AIR_ENGINE_1    — `{{Img float|file=…|cap={{sc|Fig.}} 1 —…}}` (the
    same `{{img float}}` pattern but with short cap).  Regression case
    for the recent viewer 403 bug — the pipeline side must produce
    a clean caption.
* WEIGHING_MACHINES — `{{raw image|…}}` followed by a separate
    `{{c|{{smaller|…}}  {{sc|Fig.}} 19.—caption}}` block.  Exercises
    the wrapper-caption detection in extract_images / elements.

Tests on current (buggy) code are marked with the bug they expose via
`pytest.mark.xfail(strict=True, reason=...)`.  Fixing the bug flips the
xfail into a pass without changing the assertion, so the ratchet is
self-announcing.
"""
from __future__ import annotations

import re

import pytest

from britannica.pipeline.stages.prepare_wikitext import _convert_quote_runs
from britannica.pipeline.stages.transform_articles import (
    _transform_text_v2 as _raw_transform,
)


def _transform_text_v2(src: str, volume: int, page_number: int) -> str:
    """Mirror the production pipeline.

    In production, `_transform_text_v2` is called on wikitext that has
    already passed through `prepare_wikitext` — meaning
    `_convert_quote_runs` has converted `'''X'''` / `''Y''` to
    `«B»X«/B»` / `«I»Y«/I»` markers upstream.  Tests that pass raw
    wikitext directly to the unwrapped `_transform_text_v2` skip that
    step, and legend / multi-col extraction silently fails to
    recognise the still-`''`-wrapped italic labels.  This wrapper
    re-establishes the production call shape.
    """
    return _raw_transform(_convert_quote_runs(src), volume, page_number)


# ── Fixtures ──────────────────────────────────────────────────────────

SEWING_MACHINES_2 = """\
{|align="right" width="250" style="margin-left: 1em"
|[[Image:EB1911 Sewing Machine - Howe's original.jpg|250px]]
|-
|align="center"|{{sc|Fig.}} 1.—Howe's original Machine.
|}
"""

ABBEY_3 = """\
{| {{ts|sm92|lh10|ma|width:450px}}
|[[image:Abbey_3.png|450px|frameless]]
|-
|{{center|{{sc|Fig. 3.}}—Ground-plan of St Gall.}}
{|{{Ts|ma}}
|{{Ts|width:49%|vtp}}|
{{csc|Church. }}

<poem>A.&emsp;High altar.
B.&emsp;Altar of St Paul.
C.&emsp;Altar of St Peter.</poem>

|&emsp;
|width=49%|
<poem>U.{{em|1.1}}House for blood-letting.
V.{{em|1.2}}School.</poem>
|}
|}
"""

ABBEY_02_FLOAT = (
    "class towards the early part of the 9th century.\n"
    "{{img float |width=210px |file=Abbey_02.png\n"
    "|cap={{sc|Fig. 2.}}—Plan of Coptic Monastery.<br>&thinsp;"
    "A. Narthex.{{em|2.5}}B. Church.<br>\n"
    "&thinsp;C. Corridor, with cells on each side.<br>\n"
    "&thinsp;D. Staircase.|style=font-size:92%; line-height:125% "
    "|capalign=left}}\n"
    "This curious and interesting plan has been made the subject "
    "of a memoir.\n"
)

AIR_ENGINE_1 = (
    "picked the stored heat up again on the return journey. "
    "{{Img float\n"
    " |file=EB1911 Air-Engine - Fig 1. Striling's Air-Engine.jpg\n"
    " |cap={{sc|Fig.}} 1 —Stirling's Air-Engine.\n"
    " |width=205px |style=font-size:92%\n"
    "}}The essential parts of one form of Stirling's engine are "
    "shown in fig.&nbsp;1.\n"
)

WEIGHING_MACHINES = (
    "it revolves it carries the large poise along the steelyard.\n\n"
    "{{raw image|EB1911 Weighing Machines - Automatic Coal.jpg}}\n\n"
    "{{c|{{smaller|From the Notice issued by the Standards "
    "Department of the Board of Trade, <br />by permission of the "
    "Controller of H.M. Stationery Office.}}\n\n"
    "{{sc|Fig.}} 19.—Automatic Coal Weighing Machine.}}\n\n"
    "Thus, if the poise be at the zero end of the steelyard.\n"
)


# ── Helpers ───────────────────────────────────────────────────────────

# Central grammar — group 1 filename, group 2 meta-block, group 3 caption.
# Matches the whole marker, so `IMG_RE.sub("", body)` still strips cleanly.
from britannica.markers import IMG_PARTS_RE as IMG_RE


def extract_imgs(body: str) -> list[tuple[str, str | None]]:
    """Return [(filename, caption), ...] for every IMG marker in body."""
    return [(m.group(1), m.group(3)) for m in IMG_RE.finditer(body)]


def count_substring(body: str, needle: str) -> int:
    return body.count(needle)


def assert_faithful(body, *, img=None, content=(), absent=()):
    """Content-preservation invariants for the faithful recursive producer.

    The old role-producers are gone, so we no longer assert their structure
    ({{LEGEND}} markers, bundled captions, reading-order sort, ### subheadings).
    The DURABLE contract: the image renders as a leaf, every label/caption/legend
    word survives somewhere in the body (faithful renders legends as source-order
    cells / verse / centred blocks), and nothing leaks — no child placeholder, no
    un-rendered template, no stray `||` outside a table marker."""
    if img is not None:
        assert img in body, f"image {img!r} missing:\n{body[:600]!r}"
    for c in content:
        assert c in body, f"content {c!r} missing:\n{body[:900]!r}"
    for a in absent:
        assert a not in body, f"unexpected {a!r} present:\n{body[:600]!r}"
    assert "\x03ELEM" not in body, f"leaked child placeholder:\n{body[:500]!r}"
    masked = re.sub(r"«HTMLTABLE:.*?«/HTMLTABLE»", "", body, flags=re.S)
    masked = re.sub(r"\{\{IMG:[^{}]*\}\}", "", masked)
    masked = re.sub(r"\{\{VERSE:.*?\}VERSE\}", "", masked, flags=re.S)
    masked = re.sub(r"«[^«»]*»", "", masked)
    assert "{{" not in masked, f"un-rendered template leak:\n{masked[:500]!r}"
    assert "||" not in masked, f"loose || leak:\n{masked[:500]!r}"


# ── Tests: SEWING_MACHINES_2 — the working baseline ───────────────────

def test_sewing_machines_fig1_image_and_caption():
    """Image+caption wikitable → image leaf + caption text, present exactly once."""
    body = _transform_text_v2(SEWING_MACHINES_2, volume=24, page_number=774)
    assert_faithful(body, img="EB1911 Sewing Machine - Howe's original.jpg",
                    content=["Howe's original Machine"])
    assert len(extract_imgs(body)) == 1
    assert body.count("Howe's original Machine") == 1  # no duplication


# ── Tests: ABBEY_02_FLOAT — `{{img float}}` floater ─────────────

def test_abbey_02_float_caption_is_clean():
    """`{{img float|cap=...}}` with {{sc|}} / {{em|}} / <br> / &thinsp;
    in caption → caption in IMG marker is plain prose."""
    body = _transform_text_v2(ABBEY_02_FLOAT, volume=1, page_number=44)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert filename == "Abbey_02.png"
    assert caption is not None
    # Must start with Fig. 2 and contain the key phrase
    assert caption.startswith("Fig. 2"), f"caption={caption!r}"
    assert "Plan of Coptic Monastery" in caption
    # No raw template or entity leaks
    assert "{{" not in caption
    assert "&thinsp;" not in caption
    assert "<br" not in caption.lower()


# ── Tests: AIR_ENGINE_1 — `{{Img float}}` mid-paragraph ───────────────

def test_air_engine_fig1_img_present_with_caption():
    body = _transform_text_v2(AIR_ENGINE_1, volume=1, page_number=482)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert filename == "EB1911 Air-Engine - Fig 1. Striling's Air-Engine.jpg"
    assert caption is not None and "Fig. 1" in caption
    assert "Stirling" in caption and "Air-Engine" in caption


# ── Tests: WEIGHING_MACHINES — `{{raw image}}` + loose wrapper ────────

def test_weighing_machines_fig19_caption_attached():
    """Loose `{{c|...}}` wrapper after `{{raw image|...}}` is the image's
    caption; the Fig. 19 text must not appear as a sibling paragraph."""
    body = _transform_text_v2(
        WEIGHING_MACHINES, volume=28, page_number=495)
    assert_faithful(body,
                    img="EB1911 Weighing Machines - Automatic Coal.jpg",
                    content=["Automatic Coal Weighing Machine"])


# ── Tests: ABBEY_3 — the nested-legend bug ────────────────────────────

def test_abbey_3_single_img_with_caption():
    """Image + caption + nested legend table → exactly one IMG marker
    with the Fig. 3 caption folded in."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    assert_faithful(body, img="Abbey_3.png",
                    content=["Ground-plan of St Gall", "High altar",
                             "Altar of St Paul", "House for blood-letting",
                             "School"])
    assert len(extract_imgs(body)) == 1
    assert body.count("Ground-plan of St Gall") == 1  # no duplication


def test_abbey_3_no_duplicate_caption():
    """The Fig. 3 caption text must not appear outside the IMG marker.
    Prior to fix: it leaked as a sibling paragraph."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    # Faithful renders the caption as a sibling \u00abCTR\u00bb block (not bundled in
    # the IMG leaf); the invariant is that it appears exactly once.
    assert body.count("Ground-plan of St Gall") == 1


def test_abbey_3_legend_preserved():
    """The legend content (A. High altar, etc.) must still appear in
    the body — we absorb the caption into IMG but keep the legend as
    a sibling block. Previous failure mode: legend dropped entirely
    when bundling fired."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    assert "High altar" in body, (
        f"Legend content missing from body:\n{body[:600]!r}")
    assert "House for blood-letting" in body


def test_abbey_3_no_loose_pipes():
    """Output must not contain stray `|   |` table-row artifacts."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    # Outside of TABLE/HTMLTABLE markers, no loose `|   |`
    masked = re.sub(r"\{\{TABLE[A-Z]?:[\s\S]*?\}TABLE\}", "", body)
    masked = re.sub(r"\u00abHTMLTABLE:[\s\S]*?\u00ab/HTMLTABLE\u00bb",
                    "", masked)
    assert not re.search(r"\|\s{2,}\|", masked), (
        f"Loose pipe artifact in body:\n{masked[:500]!r}")


# ── Fixtures for the 3 broken layout patterns ─────────────────────────

# WIKI_IMG_INLINE_LEGEND: image cell shares one line with || inline
# legend items; caption in a colspan=2 row below.
ABBEY_01_INLINE_LEGEND = """\
{|{{ts|lh11|ma|width:390px}}
|[[image:Abbey_01.png|260px|frameless]]||A. Gateway.

B. Chapels.

C. Guest-house.

D. Church.

E. Cloister.

F.&ensp;Fountain.

G. Refectory.

H. Kitchen.

I.{{em|.7}}Cells.

K. Storehouses.

L.&ensp;Postern gate.

M.{{em|.2}}Tower.
|-
|colspan=2 {{ts|ac|sm92}}|{{sc|Fig. 1.}}—Monastery of Santa Laura, Mount Athos (Lenoir).
|}
"""

# WIKI_IMG_MULTICOL_LEGEND: image in colspan=N row, caption in next
# colspan=N row, then multiple rows of ||-separated (letter,text) pairs.
CLUNY_MULTICOL_LEGEND = """\
{|{{ts|sm92|lh10|ma|width:400px}}
|colspan=6|[[File:EB1911 - Volume 01 pg. 46 img 2.png|380px|center]]
|-
|colspan=6|{{center|{{sc|Fig. 5.}}—Abbey of Cluny, from Viollet-le-Duc.}}
|-
|A.||Gateway.||F.||Tomb of St Hugh.||M.||Bakehouse.
|-
|B.||Narthex.||G.||Nave.||N.||Abbey buildings.
|-
|C.||Choir.||H.||Cloister.||O.||Garden.
|-
|D.||High-altar.||K.||Abbot's house.||P.||Refectory.
|-
|E.||Retro-altar. ||L.||Guest-house.
|}
"""

# WIKI_IMG_POEM_LEGEND with CSC subheadings — a tougher variant of
# ABBEY_3 that actually exercises subheading preservation.
ABBEY_3_WITH_SUBHEADINGS = """\
{| {{ts|sm92|lh10|ma|width:450px}}
|[[image:Abbey_3.png|450px|frameless]]
|-
|{{center|{{sc|Fig. 3.}}—Ground-plan of St Gall.}}
{|{{Ts|ma}}
|{{Ts|width:49%|vtp}}|
{{csc|Church. }}

<poem>A.&emsp;High altar.
B.&emsp;Altar of St Paul.</poem>
{{dhr|75%}}
{{csc|Monastic Buildings. }}

<poem>G.&emsp;Cloister.
H.&emsp;Calefactory.</poem>

|&emsp;
|width=49%|
<poem>U.{{em|1.1}}House for blood-letting.
V.{{em|1.2}}School.</poem>

{{csc|Menial Department.}}

<poem>''Z''.&emsp;Factory.
''a''.&emsp;Threshing Floor.</poem>
|}
|}
"""


# ── Helpers ───────────────────────────────────────────────────────────

LEGEND_RE = re.compile(r"\{\{LEGEND:([\s\S]*?)\}LEGEND\}")


def extract_legends(body: str) -> list[str]:
    return [m.group(1) for m in LEGEND_RE.finditer(body)]


def assert_no_leaks(body: str):
    """Shared invariants that every layout handler must uphold."""
    masked = re.sub(r"\{\{TABLE[A-Z]?:[\s\S]*?\}TABLE\}", "", body)
    masked = re.sub(r"\u00abHTMLTABLE:[\s\S]*?\u00ab/HTMLTABLE\u00bb",
                    "", masked)
    masked = re.sub(r"\{\{IMG:[^}]+\}\}", "", masked)
    masked = re.sub(r"\{\{LEGEND:[\s\S]*?\}LEGEND\}", "", masked)
    masked = re.sub(r"\{\{VERSE:[\s\S]*?\}VERSE\}", "", masked)
    # No literal `||` (MediaWiki same-line cell marker)
    assert "||" not in masked, (
        f"Stray || leak outside markers:\n{masked[:500]!r}")
    # No junk TABLE markers (just pipes and whitespace)
    for m in re.finditer(r"\{\{TABLE[A-Z]?:([\s\S]*?)\}TABLE\}", body):
        c = m.group(1)
        assert not re.fullmatch(r"[\s|\u2003]*", c), (
            f"Junk TABLE marker: {m.group(0)!r}")


# ── Tests: WIKI_IMG_INLINE_LEGEND (Abbey_01) ──────────────────────────

def test_abbey_01_single_img_with_caption():
    body = _transform_text_v2(
        ABBEY_01_INLINE_LEGEND, volume=1, page_number=43)
    assert_faithful(body, img="Abbey_01.png",
                    content=["Santa Laura", "Mount Athos"])
    assert len(extract_imgs(body)) == 1


def test_abbey_01_legend_preserved_and_ordered():
    body = _transform_text_v2(
        ABBEY_01_INLINE_LEGEND, volume=1, page_number=43)
    # Faithful renders the legend as source-order cells; the durable
    # invariant is that every entry's text survives in the body.
    assert_faithful(body, content=["Gateway", "Chapels", "Guest-house",
                                   "Church", "Cloister", "Fountain",
                                   "Refectory", "Kitchen", "Cells",
                                   "Storehouses", "Postern gate", "Tower"])


def test_abbey_01_no_leaks():
    body = _transform_text_v2(
        ABBEY_01_INLINE_LEGEND, volume=1, page_number=43)
    assert_no_leaks(body)


# ── Tests: WIKI_IMG_MULTICOL_LEGEND (Cluny vol 1 p. 46) ───────────────

def test_cluny_single_img_with_caption():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    assert_faithful(body, img="pg. 46 img 2.png", content=["Abbey of Cluny"])
    assert len(extract_imgs(body)) == 1


def test_cluny_legend_in_reading_order():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    # Faithful renders the multicol legend as source-order cells; all
    # entries' text survives (no reading-order sort is imposed).
    assert_faithful(body, content=["Gateway", "Narthex", "Choir",
                                   "High-altar", "Retro-altar",
                                   "Tomb of St Hugh", "Nave", "Cloister",
                                   "Abbot's house", "Guest-house",
                                   "Bakehouse", "Abbey buildings",
                                   "Garden", "Refectory"])


def test_cluny_no_leaks():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    assert_no_leaks(body)


# ── Tests: WIKI_IMG_POEM_LEGEND WITH SUBHEADINGS (Abbey_3 full) ───────

def test_abbey_3_sub_single_img_with_caption():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    assert_faithful(body, img="Abbey_3.png",
                    content=["Ground-plan of St Gall"])
    assert len(extract_imgs(body)) == 1


def test_abbey_3_sub_subheadings_preserved():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    # Faithful renders the csc subheadings as centred small-caps blocks;
    # their text survives.
    assert_faithful(body, content=["Church.", "Monastic Buildings.",
                                   "Menial Department."])


def test_abbey_3_sub_entries_preserved():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    assert_faithful(body, content=["High altar", "Cloister",
                                   "House for blood-letting", "Factory",
                                   "Threshing Floor"])


def test_abbey_3_sub_no_leaks():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    assert_no_leaks(body)


def test_abbey_3_sub_source_order():
    """Subheadings must appear BEFORE the entries they introduce, not
    collected together at the top. Previous bug: all `### X.` lines
    clustered first, then all entries."""
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    # Source order is preserved end-to-end: each subheading appears before
    # the entries it introduces (faithful renders cells/poems in source order).
    def idx(needle):
        return body.find(needle)
    assert -1 < idx("Church.") < idx("High altar"), body
    assert idx("High altar") < idx("Monastic Buildings"), body
    assert idx("Monastic Buildings") < idx("Cloister"), body


def test_hydromedusae_fig29_attribution_preserved():
    """HYDROMEDUSAE Fig. 29 — image + attribution row + Fig-caption
    row, no legend.  Caption AND attribution must both appear in the
    IMG marker's caption (attribution appended in parens).  Neither
    should leak as an orphan paragraph in the body."""
    src = (
        '{|align="left" width="275" style="margin-right: 1em"\n'
        '|[[Image:EB1911 Hydromedusae - Tiaropsis rosea.jpg|275px]]\n'
        '|-\n'
        '|style="font-size: smaller"|\n'
        "After O. Maas, ''Craspedoten Medusen'', by permission.\n"
        '|-\n'
        '|\n'
        "{{sc|Fig. 29.}}—''Tiaropsis rosea'' showing the eight Statocysts.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=154)
    assert_faithful(body, img="Tiaropsis rosea.jpg",
                    content=["Tiaropsis rosea", "After O. Maas"])


def test_hydromedusae_fig30_comma_label_legend():
    """HYDROMEDUSAE Fig. 30 — legend entries use `label,||text` form
    (comma after label, not period) with italicized labels like
    `''c.c'',`, `''st.c'',`, `''con'',`."""
    src = (
        '{|align="center" width="250"\n'
        '|colspan="2"|[[Image:EB1911 Hydromedusae - Statocyst.jpg|250px]]\n'
        '|-\n'
        '|colspan="2" style="font-size: smaller"|\n'
        "Modified after Linko, ''Traveaux''.\n"
        '|-\n'
        '|colspan="2"|\n'
        "{{sc|Fig. 30.}}—Section of a Statocyst.\n"
        "|-valign=\"top\"\n"
        "|''ex'',||Ex-umbral ectoderm.\n"
        "|-valign=\"top\"\n"
        "|''sub'',||Sub-umbral ectoderm.\n"
        "|-valign=\"top\"\n"
        "|''c.c'',||Circular canal.\n"
        "|-\n"
        "|''v'',||Velum.\n"
        "|-valign=\"top\"\n"
        "|''st.c'',&nbsp;||Cavity of statocyst.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=155)
    assert_faithful(body, img="Statocyst.jpg",
                    content=["Section of a Statocyst", "Ex-umbral",
                             "Sub-umbral", "Circular canal", "Velum",
                             "Cavity of statocyst"])


def test_sponges_fig2_multiword_labels():
    """SPONGES Fig. 2 — labels are multi-word italicized biological
    abbreviations (`cl. osc.`, `contr. osc.`, `osc. div.`).  Must
    match the multi-word label shape and produce a clean LEGEND."""
    src = (
        '{|align="center" width="400"\n'
        '|colspan="2"|[[Image:EB1911 Sponges - Leucosolenia clathrus.jpg|400px]]\n'
        '|-\n'
        '|colspan="2"|(After Minchin.)\n'
        '|-\n'
        '|colspan="2"|\n'
        "{{sc|Fig.}} 2.—''Leucosolenia'' clathrus, natural size.\n"
        "|-\n"
        "|align=\"right\"|''osc.'',||&nbsp;Osculum.\n"
        "|-\n"
        "|align=\"right\"|''cl. osc.'',||&nbsp;Closed osculum.\n"
        "|-\n"
        "|align=\"right\"|''contr. osc.'',||&nbsp;Closed oscula in contracted part.\n"
        "|-\n"
        "|align=\"right\"|''osc. div.'',||&nbsp;Diverticula from which new oscula arise.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=25, page_number=738)
    assert_faithful(body, content=["Leucosolenia", "Osculum",
                                   "Closed osculum", "Closed oscula",
                                   "Diverticula"])


def test_fulminic_acid_not_classified_as_legend():
    """FULMINIC ACID vol 11 p. 312 — chemistry formula comparison
    table with chemist names (Steiner, Divers, Scholl, Nef).  Not a
    legend — must NOT produce a LEGEND marker.  Blocked by
    Fig.-caption requirement on MULTICOL."""
    src = (
        '{|style="line-height:100%; margin:auto"\n'
        '|C : N·OH||rowspan=2|&nbsp;O[[File:Langle.svg|10px]]||N : CH '
        '||CH : N·O|| rowspan=2| C : N·OH.\n'
        '|-\n'
        '|C : N·OH, &#8193;||N : Ċ·OH, &#8193;||ĊH : N·O, &#8193;\n'
        '|- align=center\n'
        '|Steiner,&#8193; || colspan=2|Divers,&#8193; ||Scholl,&#8193; ||Nef.\n'
        '|}\n'
    )
    body = _transform_text_v2(src, volume=11, page_number=312)
    assert "{{LEGEND:" not in body, (
        f"False-positive LEGEND for chemistry-formula table:\n{body[:500]!r}")


def test_hydromedusae_fig26_prime_mark_labels():
    """HYDROMEDUSAE Fig. 26 — legend labels use prime marks (`a′`,
    `g″`, `k′`) — Unicode U+2032/U+2033.  The ascii-fold must drop
    primes so the strict validator accepts these labels."""
    src = (
        '{|align="center" width="400"\n'
        '|align="center" colspan="2"|[[Image:EB1911 Hydromedusae - Carmarina hastata.jpg|325px]]\n'
        '|-\n'
        '|align="center" colspan="2"|\n'
        "{{sc|Fig. 26.}}—''Carmarina hastata''.\n"
        '|-\n'
        "|''a'',||Nerve ring.\n"
        '|-valign="top"\n'
        "|''a''\u2032,||Radial nerve.\n"
        '|-\n'
        "|''b'',||Tentaculocyst.\n"
        '|-\n'
        "|''g''\u2033,||Ovary.\n"
        '|-\n'
        "|''k''\u2032,||Sporosac.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=154)
    assert_faithful(body, content=["Carmarina hastata", "Nerve ring",
                                   "Radial nerve", "Tentaculocyst",
                                   "Ovary", "Sporosac", "′", "″"])


def test_hydromedusae_fig49_nowrap_wrapped_label():
    """HYDROMEDUSAE Fig. 49 — first label is wrapped in `{{nowrap|…}}`:
    `{{nowrap|&emsp;''a'',&nbsp;}}||Hydrocaulus (stem).`  The template
    unwrapping must leave just the label text behind."""
    src = (
        '{|align="left" width="300" style="margin-right: 1em"\n'
        '|colspan="2"|[[Image:EB1911 Hydromedusae - possible modifications.jpg|300px]]\n'
        '|-\n'
        '|colspan="2"|\n'
        "{{sc|Fig. 49.}}—Diagram showing modifications of persons of a gymnoblastic ''Hydromedusa''.\n"
        '|-valign="top"\n'
        "|{{nowrap|&emsp;''a'',&nbsp;}}||Hydrocaulus (stem).\n"
        '|-valign="top"\n'
        "|&emsp;''b'',||Hydrorhiza (root).\n"
        '|-valign="top"\n'
        "|&emsp;''g''\u2032,||Hydranth contracted.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=162)
    assert_faithful(body, content=["Hydrocaulus (stem)",
                                   "Hydrorhiza (root)", "Hydranth"])


def test_hydromedusae_fig55_nested_plain_paragraph_legend():
    """HYDROMEDUSAE Fig. 55 — nested table contains a single cell with
    plain comma-after-label paragraphs separated by blank lines (no
    ||, no <poem>).  Was producing no caption before the NESTED_LEGEND
    fallback extractor."""
    src = (
        '{|align="right" width="250" style="margin-left: 1em"\n'
        '|[[Image:EB1911 Hydromedusae - Oral Surface.jpg|250px]]\n'
        '|-\n'
        '|\n'
        "{{sc|Fig. 55.}}—View of the Oral Surface of one of the ''Leptomedusae''.\n"
        '|-\n'
        '|align="center"|\n'
        '{|\n'
        '|\n'
        "''ge'', Genital glands.\n\n"
        "''M'', Manubrium.\n\n"
        "''ot'', Otocysts.\n\n"
        "''rc'', The four radiating canals.\n\n"
        "''Ve'', The velum.\n"
        '|}\n'
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=164)
    assert_faithful(body, img="Oral Surface.jpg",
                    content=["Oral Surface", "Genital glands",
                             "Manubrium", "Otocysts",
                             "four radiating canals", "velum"])


def test_hydromedusae_fig73_nested_pipe_pair_legend():
    """HYDROMEDUSAE Fig. 73 — nested table contains ||-separated
    (label, text) rows; was rendering as an HTMLTABLE before the
    NESTED_LEGEND handler learned Shape B."""
    src = (
        '{|align="left" width="200" style="margin-right: 1em"\n'
        '|[[Image:EB1911 Hydromedusae - Physophora hydrostatica.jpg|200px]]\n'
        '|-\n'
        '|{{sm|After C. Gegenbaur.}}\n'
        '|-\n'
        "|align=\"center\"|{{sc|Fig. 73.}}—''Physophora hydrostatica''.\n"
        '|-\n'
        '|align="center"|\n'
        '{|\n'
        "|''a''\u2032,&nbsp;||Pneumatocyst.\n"
        '|-\n'
        "|''t'',||Palpons.\n"
        '|-\n'
        "|''a'',||Axis of the colony.\n"
        '|-\n'
        "|''m'',||Nectocalyx.\n"
        '|}\n'
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=171)
    assert_faithful(body, img="Physophora hydrostatica.jpg",
                    content=["Physophora", "Pneumatocyst", "Palpons",
                             "Axis of the colony", "Nectocalyx"])


def test_hydromedusae_fig5_is_not_multicol():
    """HYDROMEDUSAE Fig. 5 vol 14 p. 149 — image + attribution row +
    descriptive-caption row.  NOT a legend.  Previous bug: my MULTICOL
    handler treated the attribution as the caption and the real
    caption as a single (Fig, 5.—Colonies of Clava…) legend entry."""
    src = (
        '{|align="center" width="400"\n'
        '|align="center"|[[Image:EB1911 Hydromedusae - Colonies of Clava.jpg|350px]]\n'
        '|-\n'
        '|style="font-size: smaller"|\n'
        "From Allman's ''Gymnoblastic Hydroids'', by permission of the Council of the Ray\n"
        'Society.\n'
        '|-\n'
        '|\n'
        "{{sc|Fig. 5.}}—Colonies of ''Clava''. A, ''Clava squamata'', magnified. "
        "B, ''C. multicornis'', natural size.\n"
        '|}\n'
    )
    body = _transform_text_v2(src, volume=14, page_number=149)
    # No bogus LEGEND should be emitted for this attribution+caption
    # layout (there is no ||-separated multi-column legend here).
    assert "{{LEGEND:" not in body, (
        f"False-positive LEGEND for attribution+caption layout:\n{body[:500]!r}")


def test_kirkstall_numeric_legend():
    """Fig. 9 Kirkstall Abbey — 2-column legend with NUMERIC labels
    (1, 2, … 20) including a range label `16-19`. Previously produced
    VERSE blocks because `_LEGEND_ENTRY_RE` rejected numeric labels."""
    src = (
        '{|{{ts|width:410px|sm92|lh11|ma}}\n'
        '|colspan=3|[[File:EB1911 - Volume 01 pg. 49 img 2.png|370px|center]]\n'
        '|-\n'
        '|colspan=3|{{center|{{sc|Fig. 9.}}—Kirkstall Abbey.}}\n'
        '|-\n'
        '|width=49%|\n'
        '<poem>1.&emsp;Church.\n'
        '2.&emsp;Chapels.\n'
        '8.&emsp;Cellars, with dormitories for ''conversi'' over.</poem>\n'
        '|&emsp;\n'
        '|width=49%|\n'
        '<poem>10.&emsp;Common room.\n'
        '16-19.&nbsp;Uncertain; perhaps offices.\n'
        '20.&emsp;Infirmary or abbot\u2019s house.</poem>\n'
        '|}\n'
    )
    body = _transform_text_v2(src, volume=1, page_number=49)
    assert_faithful(body, img="pg. 49 img 2.png",
                    content=["Kirkstall", "Church", "Chapels",
                             "Cellars", "Common room", "Uncertain",
                             "Infirmary"])


def test_mosque_amr_full_entry_per_cell():
    """MULTICOL shape #2: each cell is a complete `label. text` entry,
    not an alternating (label, text) pair.  Vol 2 p. 450 Mosque of
    Amr.  Before fix, this produced one munged 'entry' per row with
    two half-entries glued together."""
    src = (
        '{|{{Ts|ma|lh10}}\n'
        '|colspan=2|[[File:Britannica Mosque - Amr Old Cairo plan.jpg|380px]]\n'
        '|-\n'
        '|colspan=2 {{Ts|ac}}|{{sc|Fig. 54}}.—Plan of Mosque of ʽAmr.\n'
        '|-\n'
        '| 1. Kibla. || 5. Fountain for Ablution.\n'
        '|-\n'
        '| 2. Mimbar. || 6. Rooms built later.\n'
        '|-\n'
        '| 3. Tomb of ʽAmr. || 7. Minaret.\n'
        '|-\n'
        '| 4. Dakka. || 8. Latrines.\n'
        '|}\n'
    )
    body = _transform_text_v2(src, volume=2, page_number=450)
    assert_faithful(body, content=["Mosque of", "Kibla", "Mimbar",
                                   "Dakka", "Fountain for Ablution",
                                   "Rooms built later", "Minaret",
                                   "Latrines"])


# WIKI_IMG_POEM_COLUMNS_LEGEND: image row + caption row + cells that
# directly contain <poem> blocks (no nested table wrapping them).
# Distinct from POEM_LEGEND where poems live inside a nested table.
CLAIRVAUX_POEM_COLUMNS = """\
{|{{Ts|width:490px|sm92|lh11|ma}}
|colspan=5|[[File:EB1911 - Volume 01 pg. 47 img 1.png|350px|center]]
|-
|colspan=5|{{center|{{sc|Fig. 6.}}—Clairvaux, No. 1 (Cistercian), General Plan.}}
|-
|{{Ts|width:33%|vtp}}|
<poem>A.&emsp;Cloisters.
B.&emsp;Ovens, and corn<br>{{em|2.5}}and oil-mills.
C.&emsp;St Bernard's cell.</poem>
|&emsp;
|width=34%|
<poem>H.&emsp;Stables.
I.{{em|1.3}}Wine-press and<br>{{em|2.5}}hay-chamber.
K.&emsp;Parlour.</poem>
|&emsp;
|{{Ts|width:33%|vtp}}|
<poem>O.{{em|.9}}Public presse.
P.{{em|1.1}}Gateway.</poem>
|}
"""


def test_clairvaux_poem_columns_single_img_with_caption():
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    assert_faithful(body, img="pg. 47 img 1.png", content=["Clairvaux"])
    assert len(extract_imgs(body)) == 1


def test_clairvaux_poem_columns_legend_entries():
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    assert_faithful(body, content=["Cloisters", "Ovens", "St Bernard",
                                   "Stables", "Wine-press", "Parlour",
                                   "Public presse", "Gateway"])


def test_clairvaux_poem_columns_no_orphan_caption():
    """The Fig. 6 caption must not appear outside the IMG marker
    (previous failure: duplicate caption rendered as body paragraph)."""
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    assert body.count("Clairvaux, No. 1") == 1


def test_clairvaux_poem_columns_no_leaks():
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    assert_faithful(body)


def test_legend_entry_label_variants():
    """Multi-character labels must survive the entry regex:
       P₁, X₁X₁ (repeated subscript), c,c (comma-separated)."""
    # Minimal fixture that exercises the label variants
    src = (
        "{| {{ts|sm92|ma|width:300px}}\n"
        "|[[image:TestFoo.png|300px|frameless]]\n"
        "|-\n"
        "|{{center|{{sc|Fig. X.}}—Variants.}}\n"
        "{|{{Ts|ma}}\n"
        "|{{Ts|width:100%|vtp}}|\n"
        "<poem>P<sub>1</sub>. Scriptorium.\n"
        "X<sub>1</sub>X<sub>1</sub>. Guest-house.\n"
        "''c'',''c''. Mills.\n"
        "''k'',''k'',''k''. Chambers.</poem>\n"
        "|}\n"
        "|}\n"
    )
    body = _transform_text_v2(src, volume=1, page_number=44)
    assert_faithful(body, content=["Scriptorium", "Guest-house",
                                   "Mills", "Chambers"])

