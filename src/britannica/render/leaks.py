"""Render-leak detection — the honest oracle.

It reads the ACTUAL rendered HTML and reports everything that survived into it
raw: guillemet markers, `{{templates}}`, `[[wikilinks]]`, leaked HTML/wikitable
attribute residue (`style=`/`align=`/`colspan=`… in visible text), control
sentinels.  It
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
# A template's `{{` open OR a `}}` close surviving into visible text — both are
# brace-delimiter residue.  Checked on MATH-stripped text, so a TeX `}}` group
# (`{{1 \over 2}}`, exempt via `_TEXMATH_RE`) can't false-match; only a real
# leaked close (a producer consumed the `{{` open but dumped its `}}` — COBALT
# `solution}}`, POLYHEDRON `width:400px}}"`, contributor-sig `…</span>}}`, 18
# articles) survives.  Checking only `{{` was a blind spot: an unmatched close
# has no open to catch it.
_TEMPLATE_RE = re.compile(r"\{\{|\}\}")
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

# Raw HTML/wikitable ATTRIBUTE residue surviving into VISIBLE text — a producer
# consumed a template/table but dumped its `style=`/`align=`/`colspan=`… arg as
# text (ALGEBRAIC FORMS' `{{dual line|A|B|style=…}}` leaked 446 of these), or an
# escaped-and-shown tag (`&lt;br style=…&gt;`).  The `=` is the tell — a CSS
# PROPERTY (`text-align:`, `vertical-align:`) ends in `:`, so it can't false-match.
# Checked against TAG-STRIPPED text: a valid attribute lives inside a real `<tag …>`
# (removed here); only leaked residue survives.  This is the class the marker /
# template / wikilink checks were structurally blind to.
_TAG_RE = re.compile(r"<[^>]+>")
_ATTR_RE = re.compile(
    r"\b(?:style|align|valign|colspan|rowspan|bgcolor|scope|cellpadding|cellspacing"
    r"|width|height)=")

# An ESCAPED-and-shown HTML tag surviving into visible text (`&lt;/span&gt;`,
# `&lt;p&gt;`, `&lt;div &gt;`) — a producer consumed the tag's OPEN into a marker
# but dumped its bare close (or an attribute-less open) as escaped text.  The attr
# check above only catches escaped tags that still carry a `style=`/`align=`; a
# BARE `&lt;/div&gt;` has no attribute to trip it, so it needs its own check.
# Keyed on the KNOWN HTML/wiki tag-name set — the same real-tag-vs-garbage
# discrimination the walker uses: `&lt;span&gt;` is a leaked tag, but a math
# `a&lt;e&gt;`, an OCR `&lt;t`, or PLINY's prose `&lt;Secundus&gt;` are a literal
# `<` and correctly ignored.  The trailing `&gt;` is required, so a bare `&lt;`
# (less-than) never matches.
# The attribute run tolerates ENTITIES but not raw `<`/`>`: the render escapes an
# attribute's quotes to `&#39;`/`&quot;`, so a flat `[^&]*?` stopped dead at the
# first one and went blind to every escaped tag that CARRIES an attribute
# (`&lt;a href=&#39;x&#39;&gt;` leaked visibly and counted clean).  Capped at 200
# chars so a lone `&lt;` in prose can't scan half the article for a `&gt;`.
_ESC_TAG_RE = re.compile(
    r"&lt;/?(?:a|abbr|b|big|blockquote|br|chem|cite|code|div|em|hr|i|includeonly"
    r"|ins|li|mark|ol|p|poem|pre|q|ref|s|score|small|span|strike|strong|sub|sup"
    r"|table|tbody|td|th|thead|tr|u|ul|var)\b"
    r"(?:[^&<>]|&#?\w{1,8};){0,200}?&gt;", re.IGNORECASE)


def find_render_leaks(rendered_html):
    """Return a list of ``(category, snippet)`` for every raw survivor; empty = clean.

    Categories: ``marker`` / ``template`` / ``wikilink`` / ``attr`` / ``tag`` /
    ``sentinel``.
    """
    rh = rendered_html or ""
    no_math = _TEXMATH_RE.sub("", rh)
    no_tags = _TAG_RE.sub(" ", no_math)      # attribute-residue check: real tags gone
    leaks = []
    for cat, rx, text in (
        ("marker", _MARKER_RE, rh),
        ("template", _TEMPLATE_RE, no_math),
        ("wikilink", _WIKILINK_RE, no_math),
        ("attr", _ATTR_RE, no_tags),
        ("tag", _ESC_TAG_RE, no_math),
        ("sentinel", _SENTINEL_RE, rh),
    ):
        for m in rx.finditer(text):
            i = m.start()
            leaks.append((cat, text[max(0, i - 20):i + 30]))
    return leaks
