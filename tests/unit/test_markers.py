"""`markers_to_text` — the ONE marker-stream → plain-text converter (search index body +
previews).  Plain text must carry NO markup: the guillemet markers, the carried SAFE-HTML
tags (`sub/sup/small/big/br`), and dropped block markers all have to go."""
from britannica.markers import markers_to_text


class TestCarriedHtmlStripped:
    """Regression: chem/formula markup leaked into search results (FULMINIC ACID showed
    `H<sub>2</sub>C<sub>2</sub>N<sub>2</sub>O<sub>2</sub>`).  markers_to_text left the raw
    carried HTML tags untouched — strip the tag, keep the content."""

    def test_fulminic_acid_chem_formula(self):
        got = markers_to_text("HCNO or H<sub>2</sub>C<sub>2</sub>N<sub>2</sub>O<sub>2</sub>, an organic acid")
        assert got == "HCNO or H2C2N2O2, an organic acid"

    def test_superscript_kept_as_content(self):
        assert markers_to_text("a<sup>2</sup>+b<sup>2</sup>") == "a2+b2"

    def test_small_and_big_stripped(self):
        assert markers_to_text("<small>fine</small> and <big>large</big>") == "fine and large"

    def test_br_becomes_separator(self):
        # a line break must SEPARATE words, not join them
        assert markers_to_text("line1<br>line2<br/>line3") == "line1 line2 line3"

    def test_no_angle_brackets_survive(self):
        # paired tags for sub/sup/small/big; <br> is a void element (no close tag)
        for src in ("x<sub>y</sub>z", "x<sup>y</sup>z", "x<small>y</small>z",
                    "x<big>y</big>z", "x<br>y", "x<br/>y"):
            out = markers_to_text(src)
            assert "<" not in out and ">" not in out, f"leaked: {out!r}"


class TestMarkersConverter:
    def test_inline_markers_drop_delimiters_keep_text(self):
        assert markers_to_text("«I»italic«/I» «SC»caps«/SC»") == "italic caps"

    def test_link_collapses_to_display(self):
        assert markers_to_text("«LN:file|Target|display text«/LN»") == "display text"

    def test_link_display_with_carried_html(self):
        assert markers_to_text("«LN:f|T|H<sub>2</sub>O«/LN» is water") == "H2O is water"

    def test_equation_block_dropped(self):
        out = markers_to_text("before «EQN:1»x=y«/EQN» after")
        assert "EQN" not in out and "x=y" not in out
        assert "before" in out and "after" in out
