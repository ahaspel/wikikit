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

from britannica.pipeline.stages.transform_articles import _transform_text_v2


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

IMG_RE = re.compile(r"\{\{IMG:([^|}]+)(?:\|([^{}]*))?\}\}")


def extract_imgs(body: str) -> list[tuple[str, str | None]]:
    """Return [(filename, caption), ...] for every IMG marker in body."""
    return [(m.group(1), m.group(2)) for m in IMG_RE.finditer(body)]


def count_substring(body: str, needle: str) -> int:
    return body.count(needle)


# ── Tests: SEWING_MACHINES_2 — the working baseline ───────────────────

def test_sewing_machines_fig1_renders_as_single_img():
    """Simple image+caption wikitable → single `{{IMG:filename|caption}}`."""
    body = _transform_text_v2(SEWING_MACHINES_2, volume=24, page_number=774)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected exactly one IMG, got {len(imgs)}: {imgs}"
    filename, caption = imgs[0]
    assert filename == "EB1911 Sewing Machine - Howe's original.jpg"
    assert caption is not None and caption.startswith("Fig. 1")
    assert "Howe's original Machine" in caption
    # Caption must not contain raw template markup
    assert "{{" not in caption and "}}" not in caption


def test_sewing_machines_fig1_no_orphan_caption():
    """Caption must appear ONLY inside the IMG marker, not as a sibling
    paragraph below the figure (previous regression: duplicate caption)."""
    body = _transform_text_v2(SEWING_MACHINES_2, volume=24, page_number=774)
    # Remove the IMG marker entirely and check what's left
    stripped = IMG_RE.sub("", body).strip()
    assert "Howe's original Machine" not in stripped, (
        f"Caption leaked as sibling text outside IMG. Leftover:\n{stripped!r}")


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
    imgs = extract_imgs(body)
    assert len(imgs) >= 1, f"Expected at least 1 IMG, got {imgs!r}"
    # Find the Automatic Coal image among the IMGs
    auto = [i for i in imgs if "Automatic Coal" in i[0]]
    assert auto, f"Automatic Coal image missing. IMGs: {imgs}"
    filename, caption = auto[0]
    assert caption is not None
    assert "Fig. 19" in caption
    assert "Automatic Coal Weighing Machine" in caption
    # The "From the Notice issued..." attribution may be stripped
    # (it's a source-attribution prefix, not part of the caption)


# ── Tests: ABBEY_3 — the nested-legend bug ────────────────────────────

def test_abbey_3_single_img_with_caption():
    """Image + caption + nested legend table → exactly one IMG marker
    with the Fig. 3 caption folded in."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert filename == "Abbey_3.png"
    assert caption is not None
    assert "Fig. 3" in caption
    assert "Ground-plan of St Gall" in caption


def test_abbey_3_no_duplicate_caption():
    """The Fig. 3 caption text must not appear outside the IMG marker.
    Prior to fix: it leaked as a sibling paragraph."""
    body = _transform_text_v2(ABBEY_3, volume=1, page_number=44)
    stripped = IMG_RE.sub("", body)
    # Strip any SC/SH/B/I markers so we compare clean text
    clean = re.sub(r"\u00ab/?[A-Z]+\u00bb", "", stripped)
    # The caption text should not appear as an orphan paragraph
    assert "Ground-plan of St Gall" not in clean, (
        f"Caption text leaked outside IMG marker. Leftover:\n{clean[:500]!r}")


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
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert filename == "Abbey_01.png"
    assert caption is not None
    assert "Fig. 1" in caption
    assert "Santa Laura" in caption
    assert "Mount Athos" in caption


def test_abbey_01_legend_preserved_and_ordered():
    body = _transform_text_v2(
        ABBEY_01_INLINE_LEGEND, volume=1, page_number=43)
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    # All 12 entries present, in original order, ONE PER LINE (not
    # run together — this is the bug mode where `_parse_inline_legend_cell`
    # collapsed \s+ and emitted a single pseudo-entry).
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    labels = [re.match(r"([A-Z])\.", ln).group(1) for ln in lines
              if re.match(r"[A-Z]\.", ln)]
    assert labels == ["A", "B", "C", "D", "E", "F", "G", "H",
                      "I", "K", "L", "M"], (
        f"Legend lost its per-entry line structure: labels={labels!r}\n"
        f"text={text!r}")


def test_abbey_01_no_leaks():
    body = _transform_text_v2(
        ABBEY_01_INLINE_LEGEND, volume=1, page_number=43)
    assert_no_leaks(body)


# ── Tests: WIKI_IMG_MULTICOL_LEGEND (Cluny vol 1 p. 46) ───────────────

def test_cluny_single_img_with_caption():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert "pg. 46 img 2.png" in filename
    assert caption is not None
    assert "Fig. 5" in caption
    assert "Abbey of Cluny" in caption


def test_cluny_legend_in_reading_order():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    # The source rows are (A,F,M), (B,G,N), (C,H,O), (D,K,P), (E,L).
    # After reading-order sort (alphabetic) we expect
    # A B C D E F G H K L M N O P.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    labels = [re.match(r"([A-Z])\.", ln).group(1) for ln in lines
              if re.match(r"[A-Z]\.", ln)]
    assert labels == ["A", "B", "C", "D", "E", "F", "G", "H",
                      "K", "L", "M", "N", "O", "P"], (
        f"Legend labels out of reading order: {labels}")


def test_cluny_no_leaks():
    body = _transform_text_v2(
        CLUNY_MULTICOL_LEGEND, volume=1, page_number=46)
    assert_no_leaks(body)


# ── Tests: WIKI_IMG_POEM_LEGEND WITH SUBHEADINGS (Abbey_3 full) ───────

def test_abbey_3_sub_single_img_with_caption():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert filename == "Abbey_3.png"
    assert caption is not None and "Ground-plan of St Gall" in caption


def test_abbey_3_sub_subheadings_preserved():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    # Subheadings must render with the ### prefix
    assert "### Church." in text, f"Church. subheading missing:\n{text!r}"
    assert "### Monastic Buildings." in text, (
        f"Monastic Buildings. subheading missing:\n{text!r}")
    assert "### Menial Department." in text, (
        f"Menial Department. subheading missing:\n{text!r}")


def test_abbey_3_sub_entries_preserved():
    body = _transform_text_v2(
        ABBEY_3_WITH_SUBHEADINGS, volume=1, page_number=44)
    legends = extract_legends(body)
    text = legends[0]
    for entry in ["A. High altar", "G. Cloister",
                  "U. House for blood-letting",
                  "Z. Factory", "a. Threshing Floor"]:
        assert entry in text, f"Entry {entry!r} missing from LEGEND"


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
    legends = extract_legends(body)
    text = legends[0]
    # Find line indices of each marker
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    def idx(needle):
        for i, ln in enumerate(lines):
            if needle in ln:
                return i
        return -1
    # Each subheading's line must come BEFORE its associated entries
    assert idx("### Church.") < idx("A. High altar"), (
        f"Church. heading not before A. High altar:\n{text}")
    assert idx("A. High altar") < idx("### Monastic Buildings."), (
        f"A. High altar should precede Monastic Buildings.:\n{text}")
    assert idx("### Monastic Buildings.") < idx("G. Cloister"), (
        f"Monastic Buildings. not before G. Cloister:\n{text}")


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
    # No stray VERSE blocks from the poems
    assert "{{VERSE:" not in body, (
        f"Numeric poems leaked as VERSE:\n{body[:500]!r}")
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    for entry in ["1. Church", "2. Chapels", "8. Cellars",
                  "10. Common room", "16-19. Uncertain",
                  "20. Infirmary"]:
        assert entry in text, f"Entry {entry!r} missing:\n{text}"


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
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Every line must be a single entry with numeric label 1-8
    labels = [re.match(r"(\d+)\.", ln).group(1) for ln in lines
              if re.match(r"\d+\.", ln)]
    assert labels == ["1", "2", "3", "4", "5", "6", "7", "8"], (
        f"Numeric labels wrong/missing: {labels!r}\n{text!r}")
    # No two entries on one line (previous failure mode)
    for ln in lines:
        assert ln.count(". ") <= 2, (
            f"Line has multiple entries glued together:\n{ln!r}")


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
    imgs = extract_imgs(body)
    assert len(imgs) == 1, f"Expected 1 IMG, got {imgs!r}"
    filename, caption = imgs[0]
    assert "pg. 47 img 1.png" in filename
    assert caption is not None and "Fig. 6" in caption
    assert "Clairvaux" in caption


def test_clairvaux_poem_columns_legend_entries():
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    # All entries from all three columns
    for entry in ["A. Cloisters", "B. Ovens", "C. St Bernard",
                  "H. Stables", "I. Wine-press", "K. Parlour",
                  "O. Public presse", "P. Gateway"]:
        assert entry in text, f"Entry {entry!r} missing:\n{text}"


def test_clairvaux_poem_columns_no_orphan_caption():
    """The Fig. 6 caption must not appear outside the IMG marker
    (previous failure: duplicate caption rendered as body paragraph)."""
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    no_img = IMG_RE.sub("", body)
    no_img = re.sub(r"\u00ab/?[A-Z]+\u00bb", "", no_img)
    assert "Clairvaux, No. 1" not in no_img, (
        f"Caption leaked outside IMG marker:\n{no_img[:500]!r}")


def test_clairvaux_poem_columns_no_leaks():
    body = _transform_text_v2(
        CLAIRVAUX_POEM_COLUMNS, volume=1, page_number=47)
    assert_no_leaks(body)
    # And no stray VERSE blocks (entries must land in LEGEND, not VERSE)
    assert "{{VERSE:" not in body, (
        f"Poems leaked as VERSE instead of going to LEGEND:\n{body[:500]!r}")


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
    legends = extract_legends(body)
    assert len(legends) == 1, f"Expected 1 LEGEND, got {len(legends)}"
    text = legends[0]
    assert re.search(r"^P₁\. Scriptorium", text, re.M), (
        f"P₁ label missing:\n{text}")
    assert re.search(r"^X₁X₁\. Guest-house", text, re.M), (
        f"X₁X₁ label missing:\n{text}")
    assert re.search(r"^c,\s*c\. Mills", text, re.M), (
        f"c,c label missing:\n{text}")
    assert re.search(r"^k,\s*k,\s*k\. Chambers", text, re.M), (
        f"k,k,k label missing:\n{text}")
