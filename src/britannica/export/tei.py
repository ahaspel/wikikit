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

_SEC_RE = re.compile(r"«(SEC|ANCHOR):([^|»]*)\|([^»]*)»")
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
    t = sub_balanced(t, _MATH_OPEN, "«/MATH»",
                     lambda m, inner: f'<formula notation="TeX">{_esc(inner)}</formula>')
    t = sub_balanced(t, _EQN_OPEN, "«/EQN»",
                     lambda m, inner: '<formula notation="TeX" rendition="#center">'
                                      f"{_esc(inner)}</formula>")
    t = _links(t)
    t = sub_balanced(t, _XL_OPEN, "«/XL»",
                     lambda m, inner: f'<ref target="{_att(m.group(1))}">{_inline(inner)}</ref>')
    t = _lists(t)
    t = _images(t)

    # bare wrappers — independent open/close, never a span regex
    for mk, el in (("B", _hi_open("bold")), ("I", "<emph>")):
        close = "</emph>" if mk == "I" else "</hi>"
        t = t.replace(f"«{mk}»", el).replace(f"«/{mk}»", close)
    t = t.replace("«MIRROR:", _hi_open("mirror")).replace("«/MIRROR»", "</hi>")
    for mk, r in (("FL", "float-left"), ("FR", "float-right")):
        t = t.replace(f"«{mk}»", _hi_open(r)).replace(f"«/{mk}»", "</hi>")

    # point markers
    t = t.replace("«BR»", "<lb/>")
    t = DHRI_RE.sub('<milestone unit="rule" rendition="#rule-inline"/>', t)
    t = DHR_RE.sub('<milestone unit="rule" rendition="#rule-block"/>', t)
    t = re.sub(r"«BAR(?:\[[^\]]*\])?»", '<milestone unit="rule" rendition="#bar"/>', t)

    t = _tables(t)
    # «P» is the PARAGRAPH and the single most common marker in the corpus
    # (199,411).  It is an OPEN-only marker: the producer emits «P» at each
    # paragraph start with no close, so it is a separator, not a span.
    t = _paragraphs(t)
    return _carried_html(t)


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
        dims = "".join(f' {k}="{_att(str(a[k]))}"' for k in ("width", "height") if a.get(k))
        head = _head(_inline(cap)) if (cap or "").strip() else ""
        return (f'<figure><graphic url="/data/images/{_att((fn or "").strip())}"{dims}/>'
                f"{head}</figure>")
    return IMG_PARTS_RE.sub(one, t)


def _tables(t: str) -> str:
    def cell(el):
        def render(m, inner):
            a = _kv(m.group(1) or "")
            at = "".join(f' {k}="{_att(a[k])}"' for k in ("cols", "rows") if k in a)
            role = ' role="label"' if el == "TH" else ""
            return f"<cell{role}{at}>{_inline(inner)}</cell>"
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
    """
    for tag, (rendition, element) in _CARRIED.items():
        open_el = f"<{element}>" if element else _hi_open(rendition)
        close_el = f"</{element}>" if element else "</hi>"
        t = re.sub(rf"&lt;{tag}&gt;", open_el, t, flags=re.I)
        t = re.sub(rf"&lt;/{tag}&gt;", close_el, t, flags=re.I)
    return t


# ── document structure ───────────────────────────────────────────────────────

def _skip_echo(chunk: str) -> str:
    """Drop the heading echo — the name is already emitted as `<head>`.  The
    SHAPE is `markers.heading_echo_end`, shared with the markdown decoder."""
    end = heading_echo_end(chunk, 0)
    return chunk[end:] if end > 0 else chunk


def _body_tei(body: str) -> str:
    """The entry's content: prose, with «SEC»/«SH» turned into nested <div>s.

    The markers are FLAT in the stream — «SEC» is a point marker and «SH» wraps
    only its own heading text — so the nesting is rebuilt here, at the one place
    that knows the document shape ([[feedback_export_owns_assembly]]).
    """
    # level 2 first: «SH:slug»heading«/SH» opens a subsection that runs to the
    # next «SH» or the end of its level-1 chunk.
    def level2(chunk: str) -> str:
        parts = list(_SH_OPEN.finditer(chunk))
        if not parts:
            return _inline(chunk)
        # the SAME straddle applies one level down — a span may cross a shoulder
        # heading exactly as it crosses a section one, so carry through here too.
        lead, carry = _reflow(chunk[:parts[0].start()], [])
        out = [_inline(lead)]
        for i, m in enumerate(parts):
            end = balanced_end(chunk, m.end(), _SH_OPEN, "«/SH»")
            if end < 0:
                out.append(_inline(chunk[m.start():]))
                break
            head = chunk[m.end():end - len("«/SH»")]
            stop = parts[i + 1].start() if i + 1 < len(parts) else len(chunk)
            piece, carry = _reflow(chunk[end:stop], carry)
            out.append(f'<div type="subsection" xml:id="{_att(m.group(1))}">'
                       f"<head>{_inline(head)}</head>{_inline(piece)}</div>")
        return "".join(out)

    marks = list(_SEC_RE.finditer(body))
    if not marks:
        return level2(body)
    lead, carry = _reflow(body[:marks[0].start()], [])
    out = [level2(lead)]
    for i, m in enumerate(marks):
        kind, slug, name = m.group(1), m.group(2), m.group(3)
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        chunk, carry = _reflow(body[m.end():stop], carry)
        if kind == "ANCHOR":
            # a link target only — never a heading, so it opens no division
            out.append(f'<anchor xml:id="{_att(slug)}"/>' + level2(chunk))
            continue
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
    cert = ('<certainty locus="value" degree="low"/>'
            if sq is not None and sq <= 1 else "")
    return f"""<teiHeader>
<fileDesc>
<titleStmt><title>{escape_body(t)}</title>{authors}</titleStmt>
<publicationStmt><publisher>britannica11.org</publisher>
<availability status="free"><licence target="https://creativecommons.org/licenses/by-sa/4.0/"/></availability>
<idno type="URL">{SITE}/article/{_att(aid)}</idno></publicationStmt>
<sourceDesc><biblStruct><monogr>
<title>Encyclopædia Britannica</title><edition>Eleventh</edition>
<imprint><pubPlace>Cambridge</pubPlace><publisher>Cambridge University Press</publisher><date>1911</date></imprint>
<biblScope unit="volume">{vol}</biblScope><biblScope unit="page">{pages}</biblScope>
</monogr></biblStruct>
<p>Transcribed by the contributors to Wikisource; encoded from that transcription.</p>
</sourceDesc>
</fileDesc>
<encodingDesc><tagsDecl>{rend}</tagsDecl></encodingDesc>
<profileDesc>{cert}</profileDesc>
<revisionDesc><change>Generated by britannica11.org export/tei.py</change></revisionDesc>
</teiHeader>"""


def article_to_tei(article: dict) -> str:
    """One article as a standalone TEI-P5 document."""
    body = escape_body(article.get("body") or "")
    body = sub_balanced(body, _TITLE_OPEN, "«/TITLE»", lambda m, inner: "")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:lang="en">\n'
            + _header(article) + "\n<text><body>\n"
            f'<div type="entry" xml:id="a{_att(article.get("stable_id") or "")}">'
            f'<head>{escape_body(article.get("title") or "")}</head>'
            + _body_tei(body)
            + "</div>\n</body></text>\n</TEI>\n")


_P_SPLIT = re.compile(r"«P»")
_BLOCK_START = re.compile(r"\s*<(table|figure|list|lg|div)\b")


def _paragraphs(t: str) -> str:
    """«P» → `<p>…</p>`.

    «P» IS OPEN-ONLY.  The producer emits it at each paragraph START and never
    closes it, because the browser closes a `<p>` for us — `render/inline.py`
    says so explicitly ("the browser closes the open-only «P»").  XML has no such
    forgiveness, so the close has to be INFERRED here, by treating «P» as the
    separator it actually is rather than the span it looks like.  Getting this
    wrong is not cosmetic: 190 «P» markers survived into the first AFRICA run and
    the leak check saw every one of them.

    A run that is only a block (a table, a figure, a list) is emitted bare — TEI
    permits those inside `<p>`, but wrapping a lone table in an empty paragraph
    asserts a paragraph the source never had.
    """
    out = []
    for part in _P_SPLIT.split(t):
        if not part.strip():
            continue
        out.append(part if _BLOCK_START.match(part) else f"<p>{part}</p>")
    return "".join(out)


_STRADDLE_OPEN = re.compile(r"«(DIV|SPAN)\[([^«»]*)\]»")


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
    text = "".join(marker_open(n, a) for n, a in carry) + chunk
    depth, open_stack, i = 0, list(carry), 0
    stack: list = []
    for m in re.finditer(r"«(DIV|SPAN)\[([^«»]*)\]»|«/(DIV|SPAN)»", text):
        if m.group(1):
            stack.append((m.group(1), m.group(2)))
        elif stack:
            stack.pop()
    text += "".join(f"«/{n}»" for n, _ in reversed(stack))
    return text, stack
