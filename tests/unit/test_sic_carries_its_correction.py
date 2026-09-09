"""`{{SIC}}` carries the editor's correction; it does not throw it away.

The template is a tooltip underneath:

    {{SIC|lire|life}}  ->  {{tooltip|lire|[sic] 'life'}}

so the page's own reading is what a reader sees and the EMENDATION is the hint.
It had been grouped with `lang`/`dropinitial` as metadata "genuinely droppable",
which discarded the correction in 111 places — the same loss as discarding an
erratum.

This matters more than 111 suggests.  Wikisource is migrating hand-rolled
`<span title="amended from 'lire'">life</span>` markup into `{{SIC}}`, and our
corpus holds 1,240 of those spans whose hints `_handle_title_spans` already
carries.  Without this, a refetch would convert 1,240 preserved corrections into
dropped ones.
"""
from britannica.pipeline.stages.elements._content import (
    _content_parse, _wrap_content_extract)


def render(raw: str) -> str:
    _name, display, _tip = _content_parse(raw)
    return _wrap_content_extract(raw, display, None)


def test_the_reader_sees_the_page_not_the_emendation():
    """Fidelity to the source: the printed word is what is on the page."""
    assert "»lire«" in render("{{SIC|lire|life}}")
    assert "»and«" in render("{{SIC|and|und}}")


def test_the_correction_survives_as_a_tooltip():
    out = render("{{SIC|lire|life}}")
    assert "«SPAN[title:[sic] 'life']»" in out, "the emendation was dropped"


def test_a_bare_sic_still_marks_it():
    """`{{SIC|word}}` — flagged as printed, with no correction offered."""
    out = render("{{SIC|word}}")
    assert "«SPAN[title:[sic]]»" in out and "»word«" in out


def test_the_tooltip_wording_is_the_template_s_own():
    """Not an invention of ours.

    Template:SIC renders `[sic]` and, when a correction is given, ` 'correction'`.
    Reproducing it means the reader sees what Wikisource shows.
    """
    _n, _d, tip = _content_parse("{{SIC|lire|life}}")
    assert tip == "[sic] 'life'"
    _n, _d, bare = _content_parse("{{SIC|word}}")
    assert bare == "[sic]"


def test_sic_uses_the_same_marker_as_the_span_form():
    """ONE marker, one viewer decoder, two source shapes.

    `_handle_title_spans` emits `«SPAN[title:…]»` for the HTML form; a second
    marker for the template form would need a second decoder and would drift.
    """
    assert render("{{SIC|lire|life}}").startswith("«SPAN[title:")
    assert render("{{tooltip|x|hint}}").startswith("«SPAN[title:")


def test_the_genuinely_droppable_ones_still_drop():
    """A regression guard on the group `sic` just left.

    `lang`'s code and a drop-cap's size ARE metadata — no reader wants them —
    and moving sic must not sweep them along.
    """
    assert render("{{lang|fr|mot}}") == "mot"
    assert "«SPAN" not in render("{{lang|fr|mot}}")
    assert "«SPAN" not in render("{{di|J|4em}}")


# ---------------------------------------------------------------------------
# {{SIC}} and {{sic}} are DIFFERENT TEMPLATES.  Every expectation below was
# checked against Wikisource's own renderer, not inferred from the source.
# ---------------------------------------------------------------------------

def test_the_blank_template_renders_nothing():
    """`Template:Sic` is "INTENTIONALLY LEFT BLANK".

    It annotates the PRECEDING word for proofreaders and is invisible to
    readers, whatever arguments it is handed.  Lowercasing the template name
    collapsed it into `{{SIC}}` and printed its arguments as body text — live on
    the site: "maufacturedhide=y by fermenting with yeast".
    """
    for raw in ("{{sic}}", "{{Sic}}", "{{sic|hide=y}}",
                "{{sic|lowercase in original}}", "{{Sic|note}}"):
        assert render(raw) == "", f"{raw} should render nothing"


def test_the_uppercase_template_is_a_tooltip():
    assert render("{{SIC|youngest|eldest}}") == "«SPAN[title:[sic] 'eldest']»youngest«/SPAN»"
    assert render("{{SIC|Geologists}}") == "«SPAN[title:[sic]]»Geologists«/SPAN»"


def test_named_parameters_are_not_display_text():
    """`target=` and `texttip=` are the template's own named slots."""
    assert render("{{SIC|target=lire|texttip=life}}") == "«SPAN[title:[sic] 'life']»lire«/SPAN»"


def test_a_tooltip_needs_something_to_hang_on():
    """No display text, no span.  A hint on nothing cannot be hovered."""
    assert "«SPAN" not in render("{{SIC|}}")
    assert "«SPAN" not in render("{{tooltip||hint}}")
