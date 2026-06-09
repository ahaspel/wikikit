"""FINE_PRINT and word-spacing are STYLERS, carried — not preprocess-swept.

These two were stripped in `preprocess()` (dropping their CSS and hiding the
styler work from the leak audit).  They now ride the styler mechanism:
  * `{{word-spacing|N|X}}` → the param-styler registry (`word-spacing:N`),
  * `{{fine block/s}}…{{fine block/e}}` (+ `EB1911 fine print`, `smaller block`)
    → the CENTER paired-wrapper family, dispatched on name via the shared
    `_TEMPLATE_STYLE_WRAPPERS` registry.

Because these have 0 occurrences in the current stored segments (stripped at the
last ingest), the corpus audit can't cover them — these tests are the gate.  del
and {{nop}} STAY in preprocess (no-render / editorial), and the centring family
stays byte-identical — guarded here too.
"""
from britannica.pipeline.stages.preprocess import preprocess
from britannica.pipeline.stages.elements import ElementContext, process_elements


def _run(raw: str) -> str:
    return process_elements(
        preprocess(raw), ElementContext(volume=1, page_number=1)).strip()


def test_word_spacing_carries_as_styler():
    assert _run("{{word-spacing|3px|6 7 8}}") == (
        "«SPAN[style:word-spacing:3px]»6 7 8«/SPAN»")


def test_fine_block_paired_carries_font_size():
    assert _run("{{fine block/s}}small print here{{fine block/e}}") == (
        "«DIV[style:font-size:83%]»small print here«/DIV»")


def test_eb1911_fine_print_paired_carries_font_size():
    assert _run("{{EB1911 fine print/s}}note{{EB1911 fine print/e}}") == (
        "«DIV[style:font-size:83%]»note«/DIV»")


def test_fine_block_preserves_nested_figure_child():
    """The past failure: routing fine-print through CENTER dropped child
    figures.  The recursive walk classifies the inner FIRST, so the image
    survives as a child inside the styled block."""
    out = _run("{{fine block/s}}intro [[File:Foo.jpg]] tail{{fine block/e}}")
    assert "{{IMG:Foo.jpg" in out
    assert out.startswith("«DIV[style:font-size:83%]»")


def test_centring_paired_unchanged():
    assert _run("{{c/s}}centered line{{c/e}}") == (
        "«CTR»centered line«/CTR»")


def test_del_still_stripped_in_preprocess():
    # Janu<del>r</del>ary → January (the deleted OCR error is removed).
    assert _run("Janu<del>r</del>ary") == "January"


def test_nop_still_stripped_in_preprocess():
    assert "{{nop}}" not in _run("text{{nop}}\nmore")
