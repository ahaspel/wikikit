"""Output-leak detection — the honest oracle.

It reads a marker-stream consumer's ACTUAL output and reports everything that
survived into it raw: guillemet markers, `{{templates}}`, `[[wikilinks]]`, leaked
HTML/wikitable attribute residue (`style=`/`align=`/`colspan=`… in visible text),
control sentinels.  It
consults **no** "known marker" manifest — a known marker in the output is a
recursion failure, not an exemption.  This is the deliberate replacement for the
body-level `unhandled_marker_in_htmltable` shadow, which trusted the
handled-marker list and thereby went blind to exactly the markers that leak
(`«FN»`/`«MATH»`/`«I»` — all "known", all leaking).

The rule is one line: if it came out of a converter looking like markup, it's a
leak.  The question stops being "is this marker handled?" and becomes "did it come
out clean?"

EVERY consumer, not just the render.  The body is one marker stream with several
converters over it (HTML, Markdown, search text, titles), and the rule above is
indifferent to which one ran: a `«` in Markdown is a recursion failure by the same
definition as a `«` in HTML.  Scanning only `rendered_html` is what let
`markdown.py`'s `_OUTLINE_RE` match nothing for five weeks while 151 records
shipped a raw `«OUTLINE»` — the same blindness as the handled-marker list, one
output over.  ``fmt`` names the output's format so the checks that need rendered
tags to anchor stay off the outputs that have none; nothing else varies.
"""
import re

# The marker lexicon lives in `britannica.markers` — the module that owns the
# marker vocabulary.  Imported, never retyped: this file's whole argument is that
# a second copy of a rule drifts from the rule.
from britannica.markers import MARKER_TOKEN_RE as _MARKER_RE  # noqa: E402
from britannica.util.strings import HTML_TAG_RE
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

# Each output carries its math in its own wrapper, so each masks it its own way.
# A format whose converter DROPS math whole (plain text) masks nothing — and a raw
# «MATH» surviving there is a leak the marker check must still see.  An unknown
# format KeyErrors rather than defaulting: a new output has to say how its math is
# carried before it can be scanned.
_MATH_MASK = {
    "html": _TEXMATH_RE,
    "markdown": re.compile(r"\$\$.*?\$\$|\$[^$\n]*\$", re.DOTALL),
    "text": None,
}


def mask_math(text: str, fmt: str) -> str:
    """``text`` with its math carrier removed, per that format's wrapper.

    Public so a second scanner cannot grow a second idea of where math lives:
    ``tools/diagnostics/mangled_markers.py`` compares a guillemet's neighbours
    across formats, and rendered math rewrites those neighbours wholesale.
    """
    mask = _MATH_MASK[fmt]
    return mask.sub("", text or "") if mask else (text or "")

# Raw HTML/wikitable ATTRIBUTE residue surviving into VISIBLE text — a producer
# consumed a template/table but dumped its `style=`/`align=`/`colspan=`… arg as
# text (ALGEBRAIC FORMS' `{{dual line|A|B|style=…}}` leaked 446 of these), or an
# escaped-and-shown tag (`&lt;br style=…&gt;`).  The `=` is the tell — a CSS
# PROPERTY (`text-align:`, `vertical-align:`) ends in `:`, so it can't false-match.
# Checked against TAG-STRIPPED text: a valid attribute lives inside a real `<tag …>`
# (removed here); only leaked residue survives.  This is the class the marker /
# template / wikilink checks were structurally blind to.
_TAG_RE = HTML_TAG_RE
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


# A wiki INDENT mark (`:`/`::`/`:::`) surviving into visible text.  `:` is
# MediaWiki's indent (`<dd>`), and where the outline recognizer declines a line —
# it needs a `:`-anchored LINE, and at a page break the position sentinel occupies
# the line start — the mark renders as literal text: BIRD p.978 shows `:::` above
# "Sub-order 7. Pici".  This class was structurally invisible to every check
# above: it is not a marker, a template, a wikilink, an attribute or a tag, so the
# audit reported these articles CLEAN.
#
# ONE colon is the threshold, set by corpus measurement, not caution: 1+ finds 17
# occurrences in 14 articles, 2+ finds only 5 — and all 12 it would hide are real
# (GASTROPODA's `:Fam. 29.— Vermetidae`, FUNGI's `: Class II.—Zygomycetes`,
# PATAGONIA's `:2. Patagonian Molasse`).  Requiring two would rebuild the very
# blind spot this check exists to close.
#
# The discriminator is POSITION, not count: a prose colon is TRAILING ("as
# follows:"), so it can never open a text node.  Anchoring to a block boundary —
# the tag that ends or starts a block, or the `</a>` of a page marker — matches
# only a colon that begins rendered content, and the `(?=[^\s:])` tail requires
# real content after it, so a decorative `::` alone on a line is not claimed.
_INDENT_RE = re.compile(
    r"(?:</(?:p|div|li|ul|ol|td|th|tr|h[1-6]|blockquote)>"
    r"|<(?:p|div|li|ul|ol|td|th|tr|h[1-6]|blockquote)\b[^>]*>"
    r"|</a>)\s*:+(?=[^\s:])")


_ALL = frozenset(("html", "markdown", "text"))

# (category, pattern, which text it scans, formats it holds for).  Applicability
# lives HERE, beside the pattern, so a new check cannot be added without stating
# where it holds — the omission that lets a manifest go stale has nowhere to
# happen.  `tag` and `indent` are HTML-only for the same reason: both need a
# rendered tag to anchor on.  `tag` matches an ESCAPED tag (`&lt;span&gt;`) and the
# escaping happens inside decode_inline, so no other output can carry one; `indent`
# needs a block boundary to tell a leaked `:` from prose punctuation.  Their
# non-HTML equivalents (a RAW `<span>` in Markdown, a line-initial `:` in text) are
# real classes with no check yet — absent, not exempt.
_CHECKS = (
    ("marker", _MARKER_RE, "raw", _ALL),
    ("template", _TEMPLATE_RE, "no_math", _ALL),
    ("wikilink", _WIKILINK_RE, "no_math", _ALL),
    ("attr", _ATTR_RE, "no_tags", _ALL),
    ("tag", _ESC_TAG_RE, "no_math", frozenset(("html",))),
    ("indent", _INDENT_RE, "no_math", frozenset(("html",))),
    ("sentinel", _SENTINEL_RE, "raw", _ALL),
)


def find_leaks(output, fmt="html"):
    """Return a list of ``(category, snippet)`` for every raw survivor; empty = clean.

    ``output`` is ONE consumer's finished text; ``fmt`` names its format
    (``html`` / ``markdown`` / ``text``), which selects the checks that can fire
    without false positives and how its math is masked.  Which converter produced
    the text changes nothing else — the oracle is the same for all of them.

    Categories: ``marker`` / ``template`` / ``wikilink`` / ``attr`` / ``sentinel``
    on every format; ``tag`` / ``indent`` on HTML only (see ``_CHECKS``).
    """
    raw = output or ""
    no_math = mask_math(raw, fmt)
    # The attribute check runs on TAG-STRIPPED text wherever real tags are legal,
    # because an attribute INSIDE a tag is not visible text — only a dumped one is.
    # GFM allows inline HTML and the Markdown emitter uses it deliberately (`<sub>`,
    # and the nested-table fallback GFM has no syntax for), so Markdown strips too;
    # PLAIN TEXT has no legitimate tag, so nothing is stripped there and a leaked
    # attribute has nowhere to hide.
    no_tags = _TAG_RE.sub(" ", no_math) if fmt in ("html", "markdown") else no_math
    src = {"raw": raw, "no_math": no_math, "no_tags": no_tags}
    leaks = []
    for cat, rx, which, formats in _CHECKS:
        if fmt not in formats:
            continue
        text = src[which]
        for m in rx.finditer(text):
            i = m.start()
            leaks.append((cat, text[max(0, i - 20):i + 30]))
    return leaks
