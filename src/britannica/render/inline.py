"""Inline marker → HTML: the Python port of the viewer's ``decodeInlineMarkers``.

THE one inline decoder (project_render_to_python).  Reproduces the viewer verbatim —
same order, same strings, same INDEPENDENT open→tag / close→tag substitution for the
styler/wrapper markers (the «P»/«CTR» rule: a marker that nests or spans render chunks
pairs in the browser, so there is no `«X»(.*?)«/X»` span-match to mis-pair).  The
parametrized markers (SPAN/DIV/LN/XL/BAR/BRACE2/…) key their close on the `»`-anchored
opener.  Regression-snapshotted against tests/snapshots/inline/inline_ref.json (once
the viewer's decodeInlineMarkers output; now this decoder's own golden).

Deferred to later bricks (frame-dependent / need helpers the block+shell layers own):
IMG (renderImg), FN (footnote numbering), MATH (KaTeX / display-mode), hieroglyph
(Gardiner tables).  The prose path passes ``skip_math`` and leaves «MATH» for the block
layer, exactly as the viewer does.

The article-link URL is the one seam onto the URL layer (the viewer's ``articleUrl`` →
BritannicaUrls); it is injected via ``article_url`` so an emitter supplies its own
target-specific resolver.  The default matches the jsdom reference stub.
"""
import re
from urllib.parse import quote

# ── escapeHtml — the {escape:true} boundary (plain marker string vs DOM innerHTML) ──
_ESCAPE = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;"))


def escape_html(value):
    if value is None:
        return ""
    s = str(value)
    for a, b in _ESCAPE:
        s = s.replace(a, b)
    return s


_FILENAME_RE = re.compile(r"^(\d{2}-\d{4}-[a-z0-9][a-z0-9-]*?)-([^a-z0-9-].*)$")


def _article_url(filename, is_local=True):
    """filename → article link URL, mirroring article-urls.js `filenameToUrl`.  The
    ONE URL builder for every article link (inline «LN», xref panel, plate/parent).
    is_local=True is the jsdom-stub form the byte-identical golden uses; production is
    the clean `/article/{id}/{slug}`."""
    if is_local:
        return "/article/" + filename           # matches the jsdom BritannicaUrls stub
    base = re.sub(r"\.json$", "", str(filename or ""))
    m = _FILENAME_RE.match(base)
    if m:
        return f"/article/{m.group(1)}/{m.group(2).lower()}"
    page = re.sub(r"^0+", "", base).split("-")[0]        # legacy numeric-id fallback
    slug = base[base.index("-") + 1:].lower() if "-" in base else base.lower()
    return f"/article/{page}/{slug}"


def _encode_uri_component(s):
    # encodeURIComponent: leave A-Za-z0-9 -_.!~*'() unescaped, %-escape the rest.
    return quote(s, safe="!*'()")


# ── unescapeHtml — exact inverse of escape_html (amp LAST) ──
def unescape_html(value):
    if value is None:
        return ""
    s = str(value)
    for a, b in (("&#39;", "'"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        s = s.replace(a, b)
    return s


# ── IMG — {{IMG:filename[|align=…|width=N|height=N][|caption]}} → <img> ──
_IMG_PARTS_RE = re.compile(
    r"\{\{IMG:([^|}]+)"
    r"((?:\|(?:align=(?:center|left|right)|width=\d+|height=\d+))*)"
    r"(?:\|([^{}]*))?\}\}"
)


def commons_url(filename, is_local=True):
    if filename.startswith("http://") or filename.startswith("https://"):
        return filename
    name = filename.replace(" ", "_")             # Commons: spaces ↔ underscores
    name = re.sub(r'["<>|?*]', "_", name)          # match download_images.py disk sanitization
    if name.lower().endswith(".svg"):              # SVGs rasterized to .svg.png
        name += ".png"
    base = "/data/derived/images/" if is_local else "/data/images/"
    return base + _encode_uri_component(name)


def parse_img_meta(meta_block):
    out = {}
    if meta_block:
        for m in re.finditer(r"(align|width|height)=([^|]+)", meta_block):
            out[m.group(1)] = m.group(2) if m.group(1) == "align" else int(m.group(2))
    return out


def render_img(filename, meta, caption, is_local=True):
    fn = unescape_html(filename)
    url = fn if fn.startswith("http") else commons_url(fn, is_local)
    alt = re.sub(r"«[^»]*»", "", caption or fn)
    s = "max-width:100%;height:auto;vertical-align:middle;"
    if meta.get("width"):
        s += f"width:{meta['width']}px;"
    if meta.get("height"):
        s += f"height:{meta['height']}px;"
    if meta.get("align") == "right":
        s += "float:right;margin:.4em 0 .5em 1.8em;"
    elif meta.get("align") == "left":
        s += "float:left;margin:.4em 1.8em .5em 0;"
    elif meta.get("align") == "center":
        s += "display:block;margin:0 auto;"
    return f'<img src="{url}" alt="{escape_html(alt)}" loading="lazy" style="{s}" />'


# ── hieroglyphs — [hieroglyph: <Gardiner/MdC codes>] → Egyptian Hieroglyph block chars ──
_GARDINER_BASES = {
    "A": 0x13000, "B": 0x13032, "C": 0x13037, "D": 0x13044, "E": 0x130A4, "F": 0x130C6,
    "G": 0x130F9, "H": 0x13131, "I": 0x1313B, "K": 0x1314D, "L": 0x13153, "M": 0x1315C,
    "N": 0x13189, "O": 0x131B3, "P": 0x131E7, "Q": 0x131F0, "R": 0x131F7, "S": 0x13210,
    "T": 0x1323D, "U": 0x13260, "V": 0x13286, "W": 0x132A9, "X": 0x132C2, "Y": 0x132CA,
    "Z": 0x132CF, "Aa": 0x132DB,
}
_MDC_TO_GARDINER = {
    "A": "G53", "H": "O4", "S": "S29", "s": "S29", "t": "X1", "q": "Q3", "sw": "M23",
    "pr": "O1", "anx": "S34", "nH": "S3", "zA": "V17", "aA": "G25", "xpr": "L1",
    "nDs": "N33", "Hm": "U36", "Xrd": "A17", "DA": "I10",
    "N35A": "N35", "N35B": "N35", "V10A": "V10",
}
_GARDINER_CODE_RE = re.compile(r"^([A-Z]a?)(\d+)$")
_HIEROGLYPH_RE = re.compile(r"\[hieroglyph:\s*([^\]]+)\]")


def _gardiner_to_unicode(code):
    if "*" in code:                          # MdC ligature — take the leading glyph
        code = code.split("*")[0]
    code = _MDC_TO_GARDINER.get(code, code)
    m = _GARDINER_CODE_RE.match(code)
    if not m:
        return None
    base = _GARDINER_BASES.get(m.group(1))
    if base is None:
        return None
    cp = base + int(m.group(2)) - 1
    return cp if 0x13000 <= cp <= 0x1342F else None


def _render_hieroglyph(m):
    parts = []
    for code in re.split(r"[:\s\-]+", m.group(1).strip()):
        code = code.replace("\\", "")
        if not code or code in ("<", ">", "!", "-"):
            continue
        cp = _gardiner_to_unicode(code)
        if cp:
            parts.append(f'<span class="hieroglyph">&#x{cp:x};</span>')
        else:
            parts.append(f'<span class="hieroglyph-fallback" title="Gardiner {code}">{code}</span>')
    return "".join(parts) or m.group(0)


# ── size markers (applySizeMarkers) — longest openers first; no «XL» size line ──
_SIZE_NAMED = r"xx-small|x-small|small|medium|large|x-large|xx-large|smaller|larger"
_FS_RE = re.compile(r"«FS\[([^\]]*)\]»")
_LH_RE = re.compile(r"«LH\[([^\]]*)\]»")


def _fs(m):
    sz = m.group(1).strip()
    if re.fullmatch(r"[\d.]+", sz):
        sz += "%"
    ok = re.fullmatch(rf"[\d.]+(%|em|rem|px|pt)|(?:{_SIZE_NAMED})", sz, re.I)
    return f'<span style="font-size:{sz}">' if ok else "<span>"


def _lh(m):
    lh = m.group(1).strip()
    ok = re.fullmatch(r"[\d.]+(%|em|rem|px|pt)?", lh)
    return f'<span style="display:inline-block;line-height:{lh}">' if ok else "<span>"


def _apply_size_markers(h):
    h = h.replace("«XXL»", '<span class="size-xxl">').replace("«/XXL»", "</span>")
    h = h.replace("«LG»", '<span class="size-lg">').replace("«/LG»", "</span>")
    h = h.replace("«XXS»", '<span class="size-xxs">').replace("«/XXS»", "</span>")
    h = h.replace("«XS»", '<span class="size-xs">').replace("«/XS»", "</span>")
    h = h.replace("«SM»", '<span class="size-sm">').replace("«/SM»", "</span>")
    h = _FS_RE.sub(_fs, h)
    h = h.replace("«/FS»", "</span>")
    h = _LH_RE.sub(_lh, h)
    h = h.replace("«/LH»", "</span>")
    return h


# ── parametrized markers ──
_FN_RE = re.compile(r"«FN(?:\[([^\]]+)\])?:([\s\S]*?)«/FN»")
# Inline MATH (cell/caption/verse default): katex is stubbed to «MATHPH» and the cell path
# does NOT do popout / fs-scaling (that's the prose path, which passes skip_math and handles
# «MATH» itself).  So here MATH is just the placeholder.
_MATH_RE = re.compile(r"«MATH(?:\[[^\]]*\])?:[\s\S]*?«/MATH»")
# EPUB only: a centered small-caps that is preceded by a «SEC» anchor is a main-section
# heading — promote «SEC:slug|…»«CTR»«SC»X«/SC»«/CTR» to a real <h3> carrying the anchor id.
# The «SEC» requirement is what distinguishes it from an identically-marked figure label
# ("Fig. 3." is also a pure centered small-caps, but has no «SEC» before it).
_EPUB_SEC_HEAD_RE = re.compile(r"«SEC:([^|»]*)\|[^»]*»\s*«CTR»«SC»([\s\S]*?)«/SC»«/CTR»")
_SAFE_HTML_RE = re.compile(r"&lt;(/?(?:sub|sup|small|big|br)\s*/?)&gt;", re.I)
_VERSE_RE = re.compile(r"\{\{VERSE(?:\[style:[^\]]*\])?:([\s\S]*?)\}VERSE\}")
_BAR_RE = re.compile(r"«BAR\[(\d+)\]»")
_DIV_RE = re.compile(r"«DIV\[style:([^\]]*)\]»")
_SPAN_STYLE_RE = re.compile(r"«SPAN\[style:([^\]]*)\]»")
_SPAN_TITLE_RE = re.compile(r"«SPAN\[title:([^\]]*)\]»")
_BRACE2_RE = re.compile(r"«BRACE2\[(\d+)\|([lrud])\]»")
_LN_RE = re.compile(r"«LN:([^|]*)\|([^|«]*?)(?:\|([^«]*))?«/LN»")
_XL_RE = re.compile(r"«XL:([^|«]*)(?:\|((?:(?!«/XL»)[\s\S])*?))?«/XL»")
_SEC_RE = re.compile(r"«(?:SEC|ANCHOR):([^|»]*)\|[^»]*»")

_BRACE2_GLYPH = {"l": "⎧", "r": "⎫", "u": "⏞", "d": "⏟"}  # ⎧ ⎫ ⏞ ⏟


def _verse(m):
    lines = [ln for ln in m.group(1).split("\n") if ln.strip()]
    return '<span class="cell-verse">' + "<br>".join(lines) + "</span>"


def _span_title(m):
    return f'<span class="xlit" title="{m.group(1).replace(chr(34), "&quot;")}">'


def _brace2(m):
    side = m.group(2)
    return f'<span class="brace2 brace2-{side}">{_BRACE2_GLYPH[side]}</span>'


def _ln_factory(article_url):
    def _ln(m):
        g1, g2, g3 = m.group(1), m.group(2), m.group(3)
        has_file = g3 is not None
        target = g2 if has_file else g1
        display = g3 if has_file else g2
        filename = g1 if has_file else None
        href = article_url(filename) if filename else "/search.html?q=" + _encode_uri_component(target)
        return f'<a href="{href}" class="article-link" title="{target}">{display}</a>'
    return _ln


def _xl(m):
    url, disp = m.group(1), m.group(2)
    return f'<a href="{url}" class="external-link" target="_blank" rel="noopener">{disp or url}</a>'


_TABLE_OPEN_RE = re.compile(r"«(TABLE|TR|TD|TH)\[([^\]]*)\]»")


def _table_open(m):
    """`«TD[colspan:2|style:text-align:right]»` → `<td colspan="2" style="text-align:right">`.

    The quote-free `key:value` payload (the SAME wire `«SPAN[style:…]»` rides)
    re-gains its quotes here — split on `|`, each field on its FIRST `:`.  `cols`
    is the wide-table metadata the block layer already read off the opener, not an
    HTML attribute, so it is dropped."""
    tag = m.group(1).lower()
    attrs = ""
    for field in m.group(2).split("|"):
        k, _, v = field.partition(":")
        if k == "cols":
            continue
        attrs += f' {k}="{v}"'
    return f"<{tag}{attrs}>"


def decode_inline(h, *, escape=False, dhr_inline=False, skip_math=False, article_url=None,
                  is_local=True, ctx=None):
    """Decode an inline marker string to HTML, reproducing ``decodeInlineMarkers``.

    ``ctx`` carries the per-article footnote state (counter / named numbers / collected
    list); it must be provided wherever «FN» can appear (title, prose, cells).  Without
    it FN markers are left untouched (the inline unit battery passes none).
    """
    # Inline «LN» links honor the render target's URL scheme via ctx.is_local, so the
    # body's links match the panel/plate links (production clean URLs off-golden, the
    # jsdom-stub form in the golden where is_local defaults True).
    article_url = article_url or (lambda fn: _article_url(fn, getattr(ctx, "is_local", True)))

    # IMG — shielded from escapeHtml (the filename is a URL, not display text) and
    # restored last, exactly as the viewer does.  FN / MATH stay deferred (block+shell
    # layers own them); the prose path passes skip_math and leaves «MATH» here.
    img_html = []

    def _shield_img(m):
        img_html.append(render_img(m.group(1), parse_img_meta(m.group(2)), m.group(3) or "", is_local))
        return f"\x00IMG{len(img_html) - 1}\x00"

    h = _IMG_PARTS_RE.sub(_shield_img, h)

    if escape:
        h = escape_html(h)

    # Footnotes decode the same in every context (title, prose, cell) — numbered and
    # collected through the shared ctx so a title footnote is #1.
    if ctx is not None:
        h = _FN_RE.sub(lambda m: render_fn_marker(m.group(1), m.group(2), ctx), h)

    if not skip_math:
        if getattr(ctx, "target", None) == "site":
            # Site target → the same KaTeX-hydration placeholder the prose path emits.  Inline
            # (cell/caption/verse) MATH is display=0; the viewer runs KaTeX over `.tex-math`.
            def _math_ph(m):
                inner = re.match(r"«MATH(?:\[[^\]]*\])?:([\s\S]*?)«/MATH»", m.group(0))
                return f'<span class="tex-math" data-display="0">{inner.group(1) if inner else ""}</span>'
            h = _MATH_RE.sub(_math_ph, h)
        else:
            h = _MATH_RE.sub("«MATHPH»", h)

    h = _VERSE_RE.sub(_verse, h)

    # Un-escape the fixed safe set of carried presentational HTML (CHEM/MATH signals).
    h = _SAFE_HTML_RE.sub(r"<\1>", h)

    if getattr(ctx, "target", "site") == "epub":
        # main-section heading: «SEC:slug|…»«CTR»«SC»name«/SC»«/CTR» → <h3> carrying the anchor
        # id (\1=slug) with the name (\2) as text, BEFORE «SC»/«CTR» decode (afterwards the
        # pattern is already <span>/<div>).  Inner markers in the name still decode below.
        h = _EPUB_SEC_HEAD_RE.sub(r'<h3 class="section-head" id="section-\1">\2</h3>', h)

    # Position-invariant styler markers — independent open/close (the «P»/«CTR» rule).
    h = h.replace("«MIRROR:", '<span class="mirror-h">').replace("«/MIRROR»", "</span>")
    h = h.replace("«B»", "<b>").replace("«/B»", "</b>")
    h = h.replace("«I»", "<i>").replace("«/I»", "</i>")
    h = h.replace("«SC»", '<span class="small-caps">').replace("«/SC»", "</span>")
    h = h.replace("«SS»", '<span class="sans-serif">').replace("«/SS»", "</span>")
    h = h.replace("«SR»", '<span class="explicit-serif">').replace("«/SR»", "</span>")
    h = h.replace("«U»", '<span class="underline">').replace("«/U»", "</span>")
    h = h.replace("«STK»", "<s>").replace("«/STK»", "</s>")
    h = h.replace("«BR»", "<br>")

    h = _apply_size_markers(h)

    h = _BAR_RE.sub(lambda m: f'<span class="inline-bar" style="width:{m.group(1)}em">&nbsp;</span>', h)
    h = re.sub(r"«DHR(?:\[[^\]]*\])?»",
               '<span class="dhr-inline"></span>' if dhr_inline else '<span class="dhr-block"></span>', h)

    # Paragraph + wrapper markers — open/close substituted independently.
    h = h.replace("«P»", "<p>")
    h = h.replace("«CTR»", '<div class="centered">').replace("«/CTR»", "</div>")
    h = _DIV_RE.sub(r'<div style="\1">', h).replace("«/DIV»", "</div>")
    h = _SPAN_STYLE_RE.sub(r'<span style="\1">', h)
    h = _SPAN_TITLE_RE.sub(_span_title, h)
    h = h.replace("«/SPAN»", "</span>")
    h = h.replace("«FR»", '<span class="float-right">').replace("«/FR»", "</span>")
    h = h.replace("«FL»", '<span class="float-left">').replace("«/FL»", "</span>")
    h = _BRACE2_RE.sub(_brace2, h)
    h = _HIEROGLYPH_RE.sub(_render_hieroglyph, h)

    h = _LN_RE.sub(_ln_factory(article_url), h)
    h = _XL_RE.sub(_xl, h)
    h = _SEC_RE.sub(r'<span id="section-\1" class="section-anchor"></span>', h)

    # Recursive table markers — «TABLE[…]»/«TR[…]»/«TD[…]»/«TH[…]»/«CAPTION».  The
    # producer carried the table AS markers (no HTML on the wire), so this is pure
    # independent token substitution: the cell text between the markers was escaped
    # above and its carried <sub>/<br> restored, the inner stylers already decoded,
    # and a nested table is just more markers in a cell — decoded in this same pass,
    # no re-parse.  The attr-bearing opens (with the quote-free payload) go first,
    # then the bare opens/closes.
    h = _TABLE_OPEN_RE.sub(_table_open, h)
    h = (h.replace("«TR»", "<tr>").replace("«/TR»", "</tr>")
          .replace("«TD»", "<td>").replace("«/TD»", "</td>")
          .replace("«TH»", "<th>").replace("«/TH»", "</th>")
          .replace("«CAPTION»", "<caption>").replace("«/CAPTION»", "</caption>")
          .replace("«/TABLE»", "</table>"))

    # Restore the shielded inline images (last, like the viewer).
    if img_html:
        h = re.sub(r"\x00IMG(\d+)\x00", lambda m: img_html[int(m.group(1))], h)

    return h


def render_fn_marker(name, content, ctx):
    """«FN[name]?:content«/FN» → an inline superscript + popup; number + collect via ctx.

    Named refs share a number across anchors (one Notes entry, N superscripts); the first
    anchor uses unsuffixed ids, reuses get a -instance suffix.
    """
    if name and name in ctx.named_fn_numbers:
        num = ctx.named_fn_numbers[name]
        ctx.fn_anchor_instance += 1
        ref_id = f"fnref-{num}-{ctx.fn_anchor_instance}"
        popup_id = f"fnpop-{num}-{ctx.fn_anchor_instance}"
    else:
        ctx.footnote_counter += 1
        num = ctx.footnote_counter
        if name:
            ctx.named_fn_numbers[name] = num
        ctx.collected_footnotes.append({"num": num, "text": content})
        ref_id = f"fnref-{num}"
        popup_id = f"fnpop-{num}"
    if getattr(ctx, "target", "site") == "epub":
        # Native EPUB footnote: a noteref to the collected <aside epub:type="footnote">
        # (render_article emits the notes section) — the reader pops it up, no JS.  Both
        # epub:type and the ARIA role, since readers vary on which they key.
        return (f'<sup class="footnote-ref" id="{ref_id}">'
                f'<a epub:type="noteref" role="doc-noteref" href="#fn-{num}">{num}</a></sup>')
    return (
        f'<sup class="footnote-ref" id="{ref_id}">'
        f'<a onclick="toggleFnPopup(event,\'{popup_id}\');return false;" href="#">{num}</a>'
        f'<span class="fn-popup" id="{popup_id}"><span class="fn-popup-num">{num}.</span>'
        f'{format_footnote_text(content, ctx)}</span></sup>'
    )


def format_footnote_text(text, ctx):
    """Footnote body → HTML.  Input is already escaped (title/prose/cell), so no re-escape —
    just the shared inline decode (matching formatFootnoteText)."""
    return decode_inline(text, ctx=ctx)
