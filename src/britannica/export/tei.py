"""TEI-P5 writer — the third renderer off the marker stream.

    render/article.py   marker stream -> HTML   (the site)
    export/markdown.py  marker stream -> MD     (the bundle; SHEDS presentation)
    export/tei.py       marker stream -> TEI    (this)

WHY THIS EXISTS.  Markdown cannot hold small caps, centring or a float, so
`body_to_markdown` sheds them — we carry those distinctions through the whole
walk and drop them at the last step, for every consumer of the text export.  TEI
is the first output format able to carry what we already carry, which makes this
principle 3 reaching a reader rather than a new feature
([[feedback_three_principles]]).

OFF THE MARKER STREAM, NEVER OFF OUR HTML.  A TEI writer that parsed
`rendered_html` would be a second answer to "what does this construct mean" and
would inherit every decision the site makes for screen reasons
([[feedback_shadow_path_at_the_root]]).

TOTALITY IS GROUNDED IN THE OUTPUT, NOT A MANIFEST.  `export/markdown.py` paid
for this lesson: its "TOTAL by construction over RENDERED_GUILLEMET_MARKER_NAMES"
claim was true and worthless, because the constant omitted the very names it had
no rule for, and 8,140 raw markers shipped across 560 articles.  So a marker with
no rule here SURVIVES VISIBLY into the XML and is counted, rather than being
silently dropped.  XML then gives a second, independent net that a leak scan
cannot: schema validation catches a `<cell>` outside a `<row>`, an unclosed
`<hi>`, a duplicate `@xml:id`.

NO INVENTED SEMANTIC MARKUP (user, 2026-08-22).  TEI offers `<persName>`,
`<placeName>`, `<date>`.  EB1911 does not mark its persons and places, so
emitting them means inferring them, and an inferred entity is an assertion in our
own voice about a source that made no such claim — in the one output format whose
audience would most trust it.  Encode only what the source marks.

`@rendition`, NOT `@rend` (user, 2026-08-22): every presentational distinction is
declared ONCE in `<tagsDecl>` and referenced, so the set we carry is enumerable
in the header instead of scattered across 37k files.
"""
from __future__ import annotations

import re

from britannica.render.article import dedupe_anchor_id
from britannica.render.inline import commons_url
from britannica.util.strings import page_range
from britannica.markers import (DHR_RE, DHRI_RE, FN_OPEN_RE as _FN_OPEN,
                                IMG_PARTS_RE, TR_OPEN_RE as _TR_OPEN,
                                balanced_end, marker_open,
                                heading_echo_end, parse_img_meta,
                                iter_ln_markers, strip_marker_tokens,
                                sub_balanced)

SITE = "https://britannica11.org"

# ── the declared rendition set ───────────────────────────────────────────────
# Read off `render/inline.py`'s class vocabulary, which is closed.  Ten, after
# the 2026-08-22 sweep deleted the twelve markers nothing emitted; the sizes are
# absent because the source's own value now rides in «SPAN[style:…]» instead of a
# bucket name.  Anything parametrised carries a literal @style instead.
RENDITIONS: tuple[tuple[str, str], ...] = (
    ("sc", "font-variant: small-caps"),
    ("bold", "font-weight: bold"),
    ("center", "text-align: center"),
    ("float-left", "float: left"),
    ("float-right", "float: right"),
    ("mirror", "transform: scaleX(-1)"),
    ("rule-block", "display: block; border-top: 1px solid"),
    ("rule-inline", "border-top: 1px solid"),
    ("bar", "text-decoration: overline"),
    ("verse", "display: block; white-space: pre-line"),
    ("sub", "vertical-align: sub; font-size: smaller"),
    ("sup", "vertical-align: super; font-size: smaller"),
)


# ESCAPE ONCE, AT THE DOOR.  The body is escaped whole before any marker is
# read: markers are «…», so escaping `& < >` cannot touch them, and every text
# run is then safe by construction rather than by remembering to escape at each
# of the thirty places a leaf is emitted ([[feedback_total_functions_not_cleanup_passes]]).
# Consequences, both deliberate:
#   * `_att` only has to handle the quote — its input is already escaped.
#   * the real HTML the source carries (`<sub>`) arrives here as `&lt;sub&gt;`,
#     so `_carried_html` matches THAT form.  Anything it does not know stays
#     escaped and therefore VISIBLE in the output, where the leak scan sees it.
def escape_body(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc(s: str) -> str:
    """Already-escaped text passes through; kept as a name for intent."""
    return s or ""


def _att(s: str) -> str:
    return (s or "").replace('"', "&quot;")


# ── openers ──────────────────────────────────────────────────────────────────
_TITLE_OPEN = re.compile(r"«TITLE:")
_MATH_OPEN = re.compile(r"«MATH(?:\[[^\]]*\])?:")
_EQN_OPEN = re.compile(r"«EQN(?:\[[^\]]*\])?:")
_TABLE_OPEN = re.compile(r"«TABLE\[([^\]]*)\]»")
_TD_OPEN = re.compile(r"«TD(?:\[([^\]]*)\])?»")
_TH_OPEN = re.compile(r"«TH(?:\[([^\]]*)\])?»")
_CAPTION_OPEN = re.compile(r"«CAPTION(?:\[[^\]]*\])?»")
_LI_OPEN = re.compile(r"«LI(?:\[[^\]]*\])?»")
_OL_OPEN = re.compile(r"«OL(?:\[([^\]]*)\])?»")
_UL_OPEN = re.compile(r"«UL(?:\[[^\]]*\])?»")
_SC_OPEN = re.compile(r"«SC»")
_CTR_OPEN = re.compile(r"«CTR»")
_DIV_OPEN = re.compile(r"«DIV\[([^«»]*)\]»")
_SPAN_OPEN = re.compile(r"«SPAN\[([^«»]*)\]»")
_XL_OPEN = re.compile(r"«XL:([^|«]*)\|")
_VERSE_OPEN = re.compile(r"\{\{VERSE:")

# The body is split at a SECTION only.  An «ANCHOR» is a point target INSIDE a
# division — TEI's <anchor> is a milestone, allowed anywhere — so splitting
# there orphaned the prose after it and forced an untyped wrapper <div> around
# it (25 of them over 2,000 articles, some EMPTY: a division asserting nothing).
_SEC_ONLY_RE = re.compile(r"«SEC:([^|»]*)\|([^»]*)»")
_ANCHOR_POINT_RE = re.compile(r"«ANCHOR:([^|»]*)\|[^»]*»")
_SH_OPEN = re.compile(r"«SH:([^»]*)»")
_ATTR_RE = re.compile(r"([a-zA-Z_-]+)\s*:\s*([^|]*)")


def _kv(attrs: str) -> dict:
    """`cols:8|style:…|class:…` → dict.  The one attribute reader here."""
    return {m.group(1).strip().lower(): m.group(2).strip()
            for m in _ATTR_RE.finditer(attrs or "")}


# ── inline conversion ────────────────────────────────────────────────────────

def _hi_open(rendition: str) -> str:
    """The rendition-bearing inline OPEN — one spelling.  Several markers are
    substituted open-and-close independently (the «P»/«CTR» rule), so the open
    tag is needed on its own and must not be re-spelled per site."""
    return f'<hi rendition="#{rendition}">'


def _hi(rendition: str, body: str) -> str:
    return _hi_open(rendition) + body + "</hi>"


def _head(body: str) -> str:
    return f"<head>{body}</head>"


def _wrap(name: str, rendition: str):
    def render(_m, inner):
        return _hi(rendition, _inline(inner))
    return render


def _inline(t: str) -> str:
    """Marker stream → TEI inline content.  Recursive by construction: every span
    handler calls back into `_inline` for its own inner content, so nesting is
    followed rather than enumerated ([[feedback_recursion_is_recognition]])."""
    if not t:
        return ""

    # spans whose inner content recurses
    t = sub_balanced(t, _SC_OPEN, "«/SC»", _wrap("SC", "sc"))
    t = sub_balanced(t, _CTR_OPEN, "«/CTR»", _wrap("CTR", "center"))
    t = sub_balanced(t, _VERSE_OPEN, "}VERSE}",
                     lambda m, inner: f'<lg><l>{_inline(inner)}</l></lg>')
    t = sub_balanced(t, _DIV_OPEN, "«/DIV»",
                     lambda m, inner: f'<seg style="{_att(_kv(m.group(1)).get("style", ""))}">'
                                      f"{_inline(inner)}</seg>")
    t = sub_balanced(t, _SPAN_OPEN, "«/SPAN»", _span)
    t = sub_balanced(t, _FN_OPEN, "«/FN»", _note)
    # A <formula> holds TeX and nothing else.  Passing the inner through
    # unstripped left markers in it, and the bare-wrapper substitutions further
    # down then turned an «I» INSIDE the formula into an <emph> — which the
    # schema rejects, correctly: `<formula>` has no element content.
    t = sub_balanced(t, _MATH_OPEN, "«/MATH»",
                     lambda m, inner: f'<formula notation="TeX">'
                                      f"{strip_marker_tokens(inner, '')}</formula>")
    t = sub_balanced(t, _EQN_OPEN, "«/EQN»",
                     lambda m, inner: '<formula notation="TeX" rendition="#center">'
                                      f"{strip_marker_tokens(inner, '')}</formula>")
    t = _links(t)
    t = sub_balanced(t, _XL_OPEN, "«/XL»",
                     lambda m, inner: f'<ref target="{_att(m.group(1))}">{_inline(inner)}</ref>')
    t = _lists(t)
    t = _images(t)

    # bare wrappers — independent open/close, never a span regex
    # BALANCED, not independent open/close.  The site decoder substitutes these
    # two ends separately — that is the «P»/«CTR» rule, and a browser copes — but
    # here a close with NO open became a stray `</hi>` and broke the document.
    # SPHERICAL HARMONICS has exactly that, in an OCR-damaged run.  Converting
    # only balanced pairs leaves a stray END VISIBLE as a raw marker, where the
    # leak scan counts it, instead of emitting XML that cannot be parsed
    # ([[feedback_honesty_surface_failures]]).
    for mk, open_el, close_el in (("B", _hi_open("bold"), "</hi>"),
                                  ("I", "<emph>", "</emph>"),
                                  ("FL", _hi_open("float-left"), "</hi>"),
                                  ("FR", _hi_open("float-right"), "</hi>")):
        t = sub_balanced(t, re.compile(f"«{mk}»"), f"«/{mk}»",
                         lambda m, inner, o=open_el, c=close_el: o + _inline(inner) + c)
    t = sub_balanced(t, re.compile("«MIRROR:"), "«/MIRROR»",
                     lambda m, inner: _hi("mirror", _inline(inner)))

    # point markers
    t = _ANCHOR_POINT_RE.sub(lambda m: f'<anchor xml:id="{_att(m.group(1))}"/>', t)
    t = t.replace("«BR»", "<lb/>")
    t = DHRI_RE.sub('<milestone unit="rule" rendition="#rule-inline"/>', t)
    t = DHR_RE.sub('<milestone unit="rule" rendition="#rule-block"/>', t)
    t = re.sub(r"«BAR(?:\[[^\]]*\])?»", '<milestone unit="rule" rendition="#bar"/>', t)

    t = _tables(t)
    return _carried_html(t)


def _blocks(t: str) -> str:
    """BLOCK level: inline conversion, then paragraphs.

    TEI separates block from inline, and so must we.  `_inline` recurses into
    every span, so splitting «P» there put a `<p>` inside `<hi>`, `<seg>` and
    `<ref>` — the schema rejected all three.  A `<div>` also refuses bare text
    and bare inline children, so anything that is not already a block is wrapped
    here.  This is the split the HTML renderer never needed: a browser closes an
    open `<p>` and tolerates loose text in a `<div>`, and that tolerance is
    exactly what a validator withdraws.
    """
    out, carry = [], []
    for part in _P_SPLIT.split(t):
        piece, carry = _reflow(part, carry)     # a span may straddle a «P» too
        if not piece.strip():
            continue
        h = _inline(piece)
        if not h.strip():
            continue
        out.append(_wrap_inline_runs(h))
    return "".join(out)


def _span(m, inner: str) -> str:
    """«SPAN[…]» — a style span, or a transliteration gloss.

    A `title:` payload is a TRANSLITERATION the Wikisource transcriber added; it
    is not in the printed page (a page has no hover).  It is therefore neither
    the source's nor ours, and it is attributed rather than laundered or dropped
    — see docs/tei_export.md §6a.  12,512 in the corpus.
    """
    a = _kv(m.group(1))
    body = _inline(inner)
    if "title" in a:
        return (f"{body}<note type=\"transliteration\" resp=\"#wikisource\">"
                f"{_esc(a['title'])}</note>")
    if "style" in a:
        return f'<hi style="{_att(a["style"])}">{body}</hi>'
    return body


def _note(m, inner: str) -> str:
    n = m.group(1)
    at = f' n="{_att(n)}"' if n else ""
    return f'<note place="foot"{at}>{_inline(inner)}</note>'


def _links(t: str) -> str:
    """«LN» → <ref>.  Read through the ONE «LN» reader, not a local regex."""
    out, i = [], 0
    for ln in iter_ln_markers(t):
        out.append(t[i:ln.start])
        tgt = ln.target
        # the baked 3-part form is `filename|target|display`; the file is the id
        target = f"#{tgt.rsplit('.', 1)[0]}" if tgt.endswith(".json") else ""
        at = f' target="{_att(target)}"' if target else ""
        out.append(f"<ref{at}>{_inline(ln.display)}</ref>")
        i = ln.end
    out.append(t[i:])
    return "".join(out)


def _lists(t: str) -> str:
    def item(_m, inner):
        return f"<item>{_inline(inner)}</item>"

    def lst(rend):
        def render(_m, inner):
            return f'<list rend="{rend}">{sub_balanced(inner, _LI_OPEN, "«/LI»", item)}</list>'
        return render
    t = sub_balanced(t, _OL_OPEN, "«/OL»", lst("numbered"))
    return sub_balanced(t, _UL_OPEN, "«/UL»", lst("bulleted"))


def _images(t: str) -> str:
    """`{{IMG:file|meta…|caption}}` → `<figure>`, read through the OWNED grammar.

    A local regex here got the caption wrong — it took the meta block, so a
    figure's head came out as `width=400`.  `IMG_PARTS_RE` / `img_meta` are the
    one place that knows the shape, mirrored in the viewer
    ([[feedback_tune_dont_fork]])."""
    def one(m):
        fn, meta, cap = m.group(1), m.group(2), m.group(3)
        a = parse_img_meta(meta or "")
        head = _head(_inline(cap)) if (cap or "").strip() else ""
        # `commons_url` — the SAME derivation the site and the downloader share.
        # The path must be percent-encoded: EB1911 filenames carry spaces
        # (`Karroo System.png`, `EB1911 Africa Political.jpg`), a space is not
        # legal in a URI, and TEI's @url is typed as one — so the schema rejected
        # every figure until this used the owner instead of concatenating a path
        # by hand.  Three lines of this once lived in two places and the
        # difference cost 18 articles their figures ([[feedback_tune_dont_fork]]).
        #
        # NO @width/@height: the dimension is a Wikisource display hint, not
        # something EB1911 printed.
        return (f'<figure><graphic url="{_att(commons_url((fn or "").strip()))}"/>'
                f"{head}</figure>")
    return IMG_PARTS_RE.sub(one, t)


def _tables(t: str) -> str:
    def cell(el):
        def render(m, inner):
            a = _kv(m.group(1) or "")
            at = "".join(f' {k}="{_att(a[k])}"' for k in ("cols", "rows") if k in a)
            role = ' role="label"' if el == "TH" else ""
            return f"<cell{role}{at}>{_blocks(inner)}</cell>"
        return render

    def row(_m, inner):
        inner = sub_balanced(inner, _TD_OPEN, "«/TD»", cell("TD"))
        inner = sub_balanced(inner, _TH_OPEN, "«/TH»", cell("TH"))
        return f"<row>{inner}</row>"

    def table(m, inner):
        a = _kv(m.group(1))
        cols = f' cols="{_att(a["cols"])}"' if "cols" in a else ""
        inner = sub_balanced(inner, _CAPTION_OPEN, "«/CAPTION»",
                             lambda _m, i2: _head(_inline(i2)))
        inner = sub_balanced(inner, _TR_OPEN, "«/TR»", row)
        return f"<table{cols}>{inner}</table>"
    return sub_balanced(t, _TABLE_OPEN, "«/TABLE»", table)


_CARRIED = {"sub": ("sub", None), "sup": ("sup", None), "i": (None, "emph"),
            "em": (None, "emph"), "b": ("bold", None), "strong": ("bold", None)}


def _carried_html(t: str) -> str:
    """The few real HTML tags the source carries through (`<sub>`, `<sup>`, …).

    They arrive ESCAPED (`&lt;sub&gt;`) because the body was escaped at the door,
    so that is what is matched.  Anything not listed here stays escaped and
    therefore survives VISIBLY in the XML, where the leak scan counts it — the
    totality is in the output, not in this dict.

    BALANCED PAIRS ONLY.  SPHERICAL HARMONICS carries 12 `<sub>` opens against 13
    closes — OCR damage in the Wikisource transcription, not ours — and
    substituting each end independently turned the surplus close into a stray
    `</hi>` that made the whole document unparseable.  An unmatched end now stays
    escaped and therefore VISIBLE, which is a leak the scan can count rather than
    a file nobody can open.
    """
    for tag, (rendition, element) in _CARRIED.items():
        open_el = f"<{element}>" if element else _hi_open(rendition)
        close_el = f"</{element}>" if element else "</hi>"
        t = sub_balanced(t, re.compile(rf"&lt;{tag}&gt;", re.I), f"&lt;/{tag}&gt;",
                         lambda m, inner, o=open_el, c=close_el: o + inner + c)
    return t


# ── document structure ───────────────────────────────────────────────────────

def _skip_echo(chunk: str) -> str:
    """Drop the heading echo — the name is already emitted as `<head>`.  The
    SHAPE is `markers.heading_echo_end`, shared with the markdown decoder."""
    end = heading_echo_end(chunk, 0)
    return chunk[end:] if end > 0 else chunk


_ANY_ANCHOR_RE = re.compile(r"«(SEC|ANCHOR):([^|»]*)\|([^»]*)»|«SH:([^»]*)»")


def _assign_ids(body: str) -> str:
    """Rewrite every «SEC»/«ANCHOR»/«SH» slug to its FINAL, unique id, once, in
    document order.

    Ids must be unique across all three kinds — 142 of 1,145 articles repeat a
    slug — and the rule is the renderer's `dedupe_anchor_id`, so a TEI `xml:id`
    equals the site's HTML anchor.  Doing it in one ordered pass up front means
    the converters downstream never need the counter, which is what lets an
    «ANCHOR» be converted INLINE rather than at a split.
    """
    seen: dict = {}

    def one(m):
        if m.group(1):
            kind, slug, name = m.group(1), m.group(2), m.group(3)
            return f"«{kind}:{dedupe_anchor_id(seen, f'section-{slug}')}|{name}»"
        return f"«SH:{dedupe_anchor_id(seen, f'section-{m.group(4)}')}»"
    return _ANY_ANCHOR_RE.sub(one, body)


def _body_tei(body: str) -> str:
    """The entry's content: prose, with «SEC»/«SH» turned into nested <div>s.

    The markers are FLAT in the stream — «SEC» is a point marker and «SH» wraps
    only its own heading text — so the nesting is rebuilt here, at the one place
    that knows the document shape ([[feedback_export_owns_assembly]]).

    IDS COME FROM `dedupe_anchor_id`, the renderer's own rule, NOT from the raw
    slug.  A slug is not unique within an article — 142 of 1,145 articles repeat
    one, 544 surplus corpus-wide; AFRICA has an «ANCHOR» and a «SEC» both slugged
    `history`, and two «SH» both `the-anglo-congolese-agreement-of-1894`.  XML
    forbids a duplicate `xml:id`, so this is not optional — and reusing the
    renderer's rule rather than inventing a second one means a TEI `xml:id` is
    byte-identical to the site's HTML anchor, which is what lets the two be
    cross-referenced at all ([[feedback_tune_dont_fork]]).
    """

    # level 2 first: «SH:slug»heading«/SH» opens a subsection that runs to the
    # next «SH» or the end of its level-1 chunk.
    def level2(chunk: str) -> str:
        parts = list(_SH_OPEN.finditer(chunk))
        if not parts:
            return _blocks(chunk)
        # the SAME straddle applies one level down — a span may cross a shoulder
        # heading exactly as it crosses a section one, so carry through here too.
        lead, carry = _reflow(chunk[:parts[0].start()], [])
        out = [_blocks(lead)]
        for i, m in enumerate(parts):
            end = balanced_end(chunk, m.end(), _SH_OPEN, "«/SH»")
            if end < 0:
                out.append(_blocks(chunk[m.start():]))
                break
            head = chunk[m.end():end - len("«/SH»")]
            stop = parts[i + 1].start() if i + 1 < len(parts) else len(chunk)
            piece, carry = _reflow(chunk[end:stop], carry)
            out.append(f'<div type="subsection" xml:id="{_att(m.group(1))}">'
                       f"<head>{_inline(head)}</head>{_blocks(piece)}</div>")
        return "".join(out)

    marks = list(_SEC_ONLY_RE.finditer(body))
    if not marks:
        return level2(body)
    lead, carry = _reflow(body[:marks[0].start()], [])
    out = [level2(lead)]
    for i, m in enumerate(marks):
        slug, name = m.group(1), m.group(2)
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        chunk, carry = _reflow(body[m.end():stop], carry)
        out.append(f'<div type="section" xml:id="{_att(slug)}">'
                   f"<head>{_inline(name)}</head>"
                   f"{level2(_skip_echo(chunk))}</div>")
    return "".join(out)


def _header(article: dict) -> str:
    t = article.get("title") or ""
    vol, ps, pe = article.get("volume"), article.get("page_start"), article.get("page_end")
    aid = article.get("stable_id") or ""
    pages = page_range(ps, pe)
    authors = "".join(
        f'<author ref="#{_att(c.get("slug") or "")}">{escape_body(c.get("full_name") or "")}'
        f"</author>" for c in article.get("contributors") or [])
    rend = "".join(f'<rendition xml:id="{k}" scheme="css">{v}</rendition>'
                   for k, v in RENDITIONS)
    sq = (article.get("source_quality") or {}).get("lowest_level")
    # <certainty> is not allowed in <profileDesc> — the schema said so on 20 of
    # 300 sampled articles.  The unproofread-page warning is an EDITORIAL note
    # about the transcription, so it belongs in <editorialDecl>.
    # AN EMPTY ELEMENT IS NOT A NEUTRAL ELEMENT.  Emitting <editorialDecl/> when
    # there is no notice invalidated 271 of 300 — TEI requires content, so the
    # wrapper is written only when it has something to say.
    edecl = ("<editorialDecl><p>This article spans pages whose Wikisource "
             "transcription is unproofread; mathematics in particular may be "
             "corrupt.</p></editorialDecl>"
             if sq is not None and sq <= 1 else "")
    return f"""<teiHeader>
<fileDesc>
<titleStmt><title>{escape_body(t)}</title>{authors}<respStmt xml:id="wikisource"><resp>transcription</resp><orgName>the contributors to Wikisource</orgName></respStmt></titleStmt>
<publicationStmt><publisher>britannica11.org</publisher>
<availability status="free"><licence target="https://creativecommons.org/licenses/by-sa/4.0/"/></availability>
<idno type="URL">{SITE}/article/{_att(aid)}</idno></publicationStmt>
<sourceDesc><biblStruct><monogr>
<title>Encyclopædia Britannica</title><edition>Eleventh</edition>
<imprint><pubPlace>Cambridge</pubPlace><publisher>Cambridge University Press</publisher><date>1911</date></imprint>
<biblScope unit="volume">{vol}</biblScope><biblScope unit="page">{pages}</biblScope>
</monogr></biblStruct>
</sourceDesc>
</fileDesc>
<encodingDesc><projectDesc><p>Transcribed by the contributors to Wikisource; encoded from that transcription by britannica11.org.</p></projectDesc>{edecl}<tagsDecl>{rend}</tagsDecl></encodingDesc>
<revisionDesc><change>Generated by britannica11.org export/tei.py</change></revisionDesc>
</teiHeader>"""


def article_to_tei(article: dict) -> str:
    """One article as a standalone TEI-P5 document."""
    body = escape_body(article.get("body") or "")
    body = sub_balanced(body, _TITLE_OPEN, "«/TITLE»", lambda m, inner: "")
    body = _nest_spans(_assign_ids(body))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:lang="en">\n'
            + _header(article) + "\n<text><body>\n"
            f'<div type="entry" xml:id="a{_att(article.get("stable_id") or "")}">'
            f'<head>{escape_body(article.get("title") or "")}</head>'
            + _body_tei(body)
            + "</div>\n</body></text>\n</TEI>\n")


_P_SPLIT = re.compile(r"«P»")
# The elements TEI counts as BLOCK inside a <div>; everything else needs a <p>.
_BLOCK_EL = ("table", "figure", "list", "lg", "quote")
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>")


def _wrap_inline_runs(h: str) -> str:
    """Emit block elements bare and wrap every run of loose inline/text in <p>.

    A prefix test is not enough, and the schema is what caught it: a paragraph
    whose text begins with a table — `<table>…</table> The prevailing ignorance
    may be gauged by…` — was emitted whole and bare, so the prose AFTER the table
    became a direct child of `<div>`, which TEI forbids.  Partition, don't sniff.
    """
    out, i, depth, start = [], 0, 0, None
    for m in _TAG.finditer(h):
        closing, name, selfclose = m.group(1), m.group(2), m.group(3)
        if selfclose:
            continue
        # DEPTH COUNTS EVERY ELEMENT, not just the block ones.  Counting only
        # blocks made a <table> nested inside a <seg> look top-level, and the
        # split then tore the <seg> in half — `<p><seg></p><table/><p></seg></p>`,
        # which is 9 of 300 articles not even well-formed.  A block is a BLOCK
        # only when nothing encloses it.
        if start is None and not closing and name in _BLOCK_EL and depth == 0:
            run = h[i:m.start()]
            if run.strip():
                out.append(f"<p>{run}</p>")
            start, i = m.start(), m.start()
            depth = 1
            continue
        depth += -1 if closing else 1
        if start is not None and depth == 0:
            out.append(h[start:m.end()])
            i, start = m.end(), None
    run = h[i:]
    if run.strip():
        out.append(f"<p>{run}</p>")
    return "".join(out)




# EVERY paired inline marker can straddle a boundary, not just the parametrised
# ones.  Balancing only «DIV»/«SPAN» left `<hi>`/`<seg>` torn across a «P» in 9 of
# 300 sampled articles — «SC» and «CTR» cross a paragraph break exactly as a
# fine-print «DIV» crosses a heading.
_NEST_RE = re.compile(
    r"«(DIV|SPAN)\[([^«»]*)\]»"          # 1,2  parametrised open
    r"|«(SC|CTR|B|I|FL|FR)»"             # 3    bare open
    r"|«(MIRROR):"                       # 4    split open (payload is content)
    r"|«/(DIV|SPAN|SC|CTR|B|I|FL|FR|MIRROR)»")   # 5    close


_CELL_EDGE_RE = re.compile(r"«/?(?:TD|TH|TR)(?:\[[^«»]*\])?»")


def _nest_cells(inner: str) -> str:
    """Close open spans at a CELL edge and reopen them in the next cell.

    A span may cross a cell boundary — SLAVS opens an «I» in one «TD» and closes
    it in the next, which is properly nested in the stream and torn apart the
    moment `_tables` splits at cells:
    `…ošǐ<emph></p></cell><cell><p></emph>r…`.  Same disease as a span crossing a
    «P» or a «SEC», so the same remedy: the boundary closes what is open and the
    far side reopens it.  Nothing moves; both halves keep the styling.

    The region is the cell's CONTENT, not the gap between cells: reopening in the
    gap put a `<hi>` outside any `<cell>`, which is its own violation.
    """
    carry: list = []

    def one(opener, close_tok):
        def render(m, content):
            nonlocal carry
            piece, carry = _reflow(content, carry)
            return m.group(0) + piece + close_tok
        return render

    inner = sub_balanced(inner, _TD_OPEN, "«/TD»", one(_TD_OPEN, "«/TD»"))
    return sub_balanced(inner, _TH_OPEN, "«/TH»", one(_TH_OPEN, "«/TH»"))


def _nest_spans(text: str) -> str:
    """Rewrite CROSSING spans so they nest, which XML requires and the marker
    stream does not.

    VALERIAN: `«CTR»…«DIV[…]»…«/CTR»…«/DIV»`.  The «CTR» closes while the «DIV»
    is still open.  Nothing is wrong with that in the stream — the site renderer
    substitutes open and close independently and a browser copes — but XML has no
    such thing as a partial overlap, so the writer produced `</hi>` inside a
    `<seg>` that started outside it.  3 of 37,226 articles, and NOT source damage:
    the markers balance perfectly, they simply interleave.

    The fix is the standard one, and it is the technique `_reflow` already uses at
    split points, generalised to the whole stream: when a close arrives for a span
    that is not the innermost, close the inner spans first, emit the close, then
    REOPEN the inner ones.  The rendering is identical and no content moves.

    Tables are opaque here and normalised separately: a «TABLE» is converted by
    its own balanced scan, and reopening one mid-stream would be nonsense.
    """
    def reopen(tok):
        name, attrs = tok
        if attrs is None:
            return f"«{name}»"
        return f"«{name}:" if attrs == ":" else marker_open(name, attrs)

    out: list = []
    stack: list = []
    pos = 0
    while True:
        m = _NEST_RE.search(text, pos)
        t = text.find("«TABLE[", pos)
        if t != -1 and (m is None or t < m.start()):     # step over, then recurse in
            end = balanced_end(text, t + len("«TABLE["), "«TABLE[", "«/TABLE»")
            if end < 0:
                out.append(text[pos:t + len("«TABLE[")])
                pos = t + len("«TABLE[")
                continue
            inner_start = t + len("«TABLE[")
            out.append(text[pos:inner_start])
            out.append(_nest_cells(_nest_spans(
                text[inner_start:end - len("«/TABLE»")])))
            out.append("«/TABLE»")
            pos = end
            continue
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:m.start()])
        pos = m.end()
        if m.group(1):
            stack.append((m.group(1), m.group(2)))
            out.append(m.group(0))
        elif m.group(3):
            stack.append((m.group(3), None))
            out.append(m.group(0))
        elif m.group(4):
            stack.append((m.group(4), ":"))
            out.append(m.group(0))
        else:
            name = m.group(5)
            names = [s[0] for s in stack]
            if not names or name not in names:
                out.append(m.group(0))      # a close with no open — leave it visible
                continue
            i = len(names) - 1 - names[::-1].index(name)
            inner = stack[i + 1:]
            out.extend(f"«/{n}»" for n, _ in reversed(inner))   # close the inners
            out.append(m.group(0))                              # the real close
            out.extend(reopen(s) for s in inner)                # and reopen them
            stack = stack[:i] + inner


_STRADDLE_PARAM = ("DIV", "SPAN")
_STRADDLE_BARE = ("SC", "CTR", "B", "I", "FL", "FR")
_STRADDLE_RE = re.compile(
    r"«(" + "|".join(_STRADDLE_PARAM) + r")\[([^«»]*)\]»"
    r"|«(" + "|".join(_STRADDLE_BARE) + r")»"
    r"|«/(" + "|".join(_STRADDLE_PARAM + _STRADDLE_BARE) + r")»")


def _reflow(chunk: str, carry: list) -> tuple[str, list]:
    """Close spans at a section boundary and reopen them inside — returning the
    chunk and the spans still open at its end.

    A fine-print run legitimately STRADDLES a heading: TUNICATA and ITALY open
    «DIV[style:font-size:83%]» before a «SEC» and close it after.  In the marker
    stream that is fine (the renderer substitutes open and close independently,
    and a browser copes), but XML forbids an element crossing a `<div>`
    boundary, so the only valid encoding is to close at the boundary and reopen
    on the far side.  Nothing is lost: both halves keep the same style.

    Without this the split leaves an orphan open in one chunk and an orphan
    close in the next; `sub_balanced` then leaves BOTH raw — visibly, which is
    how this was found (16 of each across a 500-article sample) rather than by
    the output quietly losing its styling ([[feedback_honesty_surface_failures]]).
    """
    def reopen(n, a):
        return marker_open(n, a) if a is not None else f"«{n}»"

    text = "".join(reopen(n, a) for n, a in carry) + chunk
    stack: list = []
    pos = 0
    while True:
        m = _STRADDLE_RE.search(text, pos)
        # STEP OVER TABLES.  A cell balances its own content (`_blocks` reflows
        # inside it), so a span left open INSIDE a table must be closed there —
        # QUEENSLAND opens a «SPAN» in a cell and never closes it, and balancing
        # it out here put the `</hi>` after `</table>`, crossing the tags.  The
        # only article in 4,000 to fail, and it failed on exactly this.
        t = text.find("«TABLE[", pos)
        if t != -1 and (m is None or t < m.start()):
            end = balanced_end(text, t + len("«TABLE["), "«TABLE[", "«/TABLE»")
            pos = end if end > 0 else t + len("«TABLE[")
            continue
        if m is None:
            break
        pos = m.end()
        if m.group(1):                       # «DIV[…]» / «SPAN[…]»
            stack.append((m.group(1), m.group(2)))
        elif m.group(3):                     # «SC» / «CTR» / «B» / «I» / «FL» / «FR»
            stack.append((m.group(3), None))
        elif stack:                          # a close — pop whatever is open
            stack.pop()
    text += "".join(f"«/{n}»" for n, _ in reversed(stack))
    return text, stack
