"""Unpaired `<div>`/`<span>` stylers ride as MARKS, not containers.

A tree represents NESTING and nothing else.  When two source spans CROSS —
`{{EB1911 fine print/s}}…<div>…{{/e}}…</div>` — neither contains the other, so no
balanced matcher can bound both; and a tag whose partner is missing entirely
can't be bounded either.  Both used to fall through to body text and render as
escaped `&lt;div&gt;` / `&lt;/div&gt;` leaks, losing the styling with them.

The fix is not a bounder: it's recognizing that a styler IS a mark.  `«DIV[style:
…]»` and `«/DIV»` decode INDEPENDENTLY in the render (the viewer substitutes each
marker on its own — never as a pair), so each half carries alone and the BROWSER
pairs them, exactly as it does for the tags the source itself wrote.  The emitted
tag stream equals MediaWiki's own, which is the definition of faithful here.

Corpus (2026-07): 16 articles have a true crossing, 441 dangling halves exist;
29 articles change under this recognizer, with zero text-content loss.
"""
from __future__ import annotations

from britannica.pipeline.stages.elements import process_elements, ElementContext


def _render(text: str) -> str:
    return process_elements(text, ElementContext(volume=1))


def test_balanced_styled_wrapper_still_nests():
    """The bounding path is untouched — a styled span that DOES nest is still one
    node with a recursed inner, not two marks."""
    assert _render('a <div style="color:red">b</div> c') == \
        'a «DIV[style:color:red]»b«/DIV» c'


def test_unpaired_open_carries_its_style():
    """An open with no reachable close emits its marker ALONE (the «P» pattern:
    open-only, the browser closes it).  Dropping it would drop the styling of
    everything that follows."""
    assert _render('a <div style="color:red">b c') == \
        'a «DIV[style:color:red]»b c'
    assert _render('a <span style="color:red">b c') == \
        'a «SPAN[style:color:red]»b c'


def test_orphan_close_carries_as_a_close_mark():
    """A close whose open lives outside this span is a real tag, not text."""
    assert _render("a b</div> c") == "a b«/DIV» c"
    assert _render("a b</span> c") == "a b«/SPAN» c"


def test_bare_wrapper_is_carried_not_leaked():
    """A style-less `<div>` is still a wrapper the source wrote — carried as an
    empty-style mark pair, never as two escaped `&lt;div&gt;` leaks."""
    assert _render("a <div>b</div> c") == "a «DIV[style:]»b«/DIV» c"


def test_crossing_spans_emit_the_sources_own_tag_stream():
    """The motivating shape (EVIDENCE / GAS ENGINE / GREEK LANGUAGE): a `<div>`
    opened INSIDE a fine-print wrapper and closed AFTER it.

    `{{EB1911 fine print/s}}` expands to a literal `<div style="font-size:83%">`,
    so the faithful render is exactly `<div fp>X <div hang>Y</div> Z</div> W` —
    the tag sequence MediaWiki itself emits.  Both stylings survive; the browser
    reparents the crossing precisely as it does on Wikisource."""
    out = _render(
        "{{EB1911 fine print/s}}X <div {{Ts|it}}>Y{{EB1911 fine print/e}}"
        " Z</div> W")
    assert out == (
        "«DIV[style:font-size:83%]»X "
        "«DIV[style:padding-left:2em;text-indent:-2em]»Y«/DIV» Z«/DIV» W")
    assert "<div" not in out and "</div>" not in out


def test_mark_path_never_outranks_a_real_bounder():
    """The mark recognizer runs LAST — every bounding recognizer is offered the
    position first.  A mirrorH glyph span and a `title=` translit span both still
    win it, so the mark path claims only what nothing else can."""
    mirror = _render('<span style="{{mirrorH}}">E</span>')
    assert "«MIRROR" in mirror and "«SPAN[style:]»" not in mirror
    titled = _render('<span title="Ephemeris">Ἐφημερίς</span>')
    assert "«SPAN[title:Ephemeris]»" in titled


def test_body_fragment_cannot_close_its_own_container():
    """Containment: a stray `</div>` must not escape the body.

    `rendered_html` carries its OWN `div.card` / `div.body-text` wrappers, so an
    unmatched close inside the body closes one of them and the rest of the
    article spills out of the body styling — two of them reach the card.  The
    render balances the fragment at its own boundary instead (drop a close with
    no open, close an open with no close), which is exactly what HTML5 fragment
    parsing does."""
    from britannica.render.article import _contain
    assert _contain("a</div> b") == "a b"
    assert _contain("a</div> b</span> c") == "a b c"
    assert _contain('a<div style="x">b') == 'a<div style="x">b</div>'
    assert _contain("a<span>b") == "a<span>b</span>"
    # Balanced input is untouched — containment only ever touches the strays.
    assert _contain("a<div>b</div>c") == "a<div>b</div>c"
    assert _contain('<div><span>x</span></div>') == '<div><span>x</span></div>'


def test_noinclude_halves_are_not_transcluded():
    """A wrapper half inside `<noinclude>` stays OUT of the article.

    `<noinclude>` means "not transcluded": MediaWiki excludes it from the article,
    and ProofreadPage keeps the page header/footer there precisely so they never
    reach mainspace.  So a `{{EB1911 fine print/e}}` written into a page footer is
    NOT part of the article — Wikisource's own render leaves that block unclosed
    too, and carrying the half would emit markup MediaWiki drops.

    Pinned because the tempting misreading is expensive: `SourcePage.raw_text`
    contains the footer close, which makes 30 articles look like OUR loss.  The
    article is raw_text MINUS noinclude, so they are source defects, and the
    unpaired open is carried by WRAP_OPEN — exactly what a browser gets from
    MediaWiki.  Keeping the halves instead moved 2174 articles and then needed a
    sweeper to undo itself.

    The `{|`/`|}` table delimiters ARE still kept — not an inconsistency: an
    orphaned table opener swallows every intervening article, so that one is a
    documented structural rescue, not a rule about what noinclude means."""
    from britannica.pipeline.stages.source_cleanup import strip_noinclude_blocks
    assert strip_noinclude_blocks(
        "body<noinclude>\n{{EB1911 fine print/e}}</noinclude>") == "body"
    assert strip_noinclude_blocks(
        "<noinclude>{{EB1911 fine print/s}}</noinclude>body") == "body"
    assert strip_noinclude_blocks("a<noinclude>{{rh|1|X|2}}</noinclude>b") == "ab"
    # The table-delimiter rescue is untouched.
    assert "{|class=x" in strip_noinclude_blocks(
        "a<noinclude>\n{|class=x\n</noinclude>b")
