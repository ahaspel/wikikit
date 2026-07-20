"""Render-leak detection — the honest oracle.

It reads the ACTUAL rendered HTML and reports everything that survived into it
raw: guillemet markers, `{{templates}}`, `[[wikilinks]]`, control sentinels.  It
consults **no** "known marker" manifest — a known marker in the output is a
recursion failure, not an exemption.  This is the deliberate replacement for the
body-level `unhandled_marker_in_htmltable` shadow, which trusted the
handled-marker list and thereby went blind to exactly the markers that leak
(`«FN»`/`«MATH»`/`«I»` — all "known", all leaking).

The rule is one line: if it came out of the renderer looking like markup, it's a
leak.  The question stops being "is this marker handled?" and becomes "did it come
out clean?"
"""
import re

# Markers are UPPERCASE-named (FN, MATH, SPAN, I, B…); this avoids matching a
# lowercase French « » quotation that is legitimate content.
_MARKER_RE = re.compile(r"«/?[A-Z][A-Za-z0-9_]*(?:\[[^\]]*\])?[:»]")
_TEMPLATE_RE = re.compile(r"\{\{")
# A leaked wikilink is `[[Target…`, whose target is page-title TEXT.  A `[[` sitting
# immediately before a tag (`[[</span>`, `[[<i>`) is a literal double-bracket GLYPH at
# a markup boundary — e.g. MENSURATION's large-font `[[V_{x,y}.u]]` cubature operator,
# authored verbatim in the source — never a link.  Excluding `[[<` sharpens what
# "wikilink" means; it is NOT an article/marker exemption list.
_WIKILINK_RE = re.compile(r"\[\[(?!<)")
_SENTINEL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# Rendered math is KaTeX-bound LaTeX, which legitimately carries `{…}`/`[…]`; so
# brace/bracket checks run with the math spans removed.  Marker and sentinel
# checks run over the WHOLE render — a raw `«MATH»` *inside* a math span is the
# display-grouping leak (it over-captured an adjacent marker), and must be caught.
# `data-latex` is the SAME content in a different carrier (the wide-math popout
# link, whose LaTeX must ride in the DOM for the click handler), so it takes the
# same exemption — `{{1 \over 2}}` is a TeX brace group (`\over` needs the
# enclosing group), not a template.  Both are carriers of LaTeX, not of markup.
_TEXMATH_RE = re.compile(
    r'<span class="tex-math"[^>]*>.*?</span>|data-latex="[^"]*"', re.DOTALL)


def find_render_leaks(rendered_html):
    """Return a list of ``(category, snippet)`` for every raw survivor; empty = clean.

    Categories: ``marker`` / ``template`` / ``wikilink`` / ``sentinel``.
    """
    rh = rendered_html or ""
    no_math = _TEXMATH_RE.sub("", rh)
    leaks = []
    for cat, rx, text in (
        ("marker", _MARKER_RE, rh),
        ("template", _TEMPLATE_RE, no_math),
        ("wikilink", _WIKILINK_RE, no_math),
        ("sentinel", _SENTINEL_RE, rh),
    ):
        for m in rx.finditer(text):
            i = m.start()
            leaks.append((cat, text[max(0, i - 20):i + 30]))
    return leaks
