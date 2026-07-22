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


def _article_url(filename, is_local=True, bundled=None):
    """``{stable_id}.json`` filename → article link URL.  The ONE URL builder for every
    article link (inline «LN», xref panel, plate/parent).  Production URLs route on the
    stable_id ALONE — `/article/{stable_id}` — so a renamed article keeps its URL (the title
    is not in the key); the viewer canonicalises a readable slug into the address bar after
    load.  is_local returns the jsdom BritannicaUrls stub the byte-identical golden uses;
    ``bundled`` (a set of in-book stems) selects the EPUB policy (in-book → relative
    `{stem}.xhtml`, else the absolute live-site URL)."""
    stem = re.sub(r"\.json$", "", str(filename or ""))
    if bundled is not None:
        return (f"{stem}.xhtml" if stem in bundled
                else "https://britannica11.org" + _article_url(filename, is_local=False))
    if is_local:
        return "/article/" + filename           # jsdom BritannicaUrls stub
    return "/article/" + stem


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


def commons_url(filename):
    if filename.startswith("http://") or filename.startswith("https://"):
        return filename
    name = filename.replace(" ", "_")             # Commons: spaces ↔ underscores
    name = re.sub(r'["<>|?*]', "_", name)          # match download_images.py disk sanitization
    if name.lower().endswith(".svg"):              # SVGs rasterized to .svg.png
        name += ".png"
    # Images are location-agnostic: the files sit in data/images/ locally and deploy to the
    # same /data/images/ on S3, so one path serves both — no local-vs-web branch to bake in
    # (the imageless-local bug was this path frozen to the web form at export while the files
    # lived under data/derived/images/).  `is_local` still steers LINKS and scans, not images.
    return "/data/images/" + _encode_uri_component(name)


def parse_img_meta(meta_block):
    out = {}
    if meta_block:
        for m in re.finditer(r"(align|width|height)=([^|]+)", meta_block):
            out[m.group(1)] = m.group(2) if m.group(1) == "align" else int(m.group(2))
    return out


def render_img(filename, meta, caption):
    fn = unescape_html(filename)
    url = fn if fn.startswith("http") else commons_url(fn)
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
_FN_OPEN_RE = re.compile(r"«FN(?:\[([^\]]+)\])?:")
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
# The INLINE verse form — `{{IVERSE:…}IVERSE}`, stamped by the producer when the poem sits
# inside a TABLE/REF.  It decodes to a `cell-verse` span (its «BR» line breaks decode to <br>
# further down).  The BLOCK form `{{VERSE:…}VERSE}` is a top-level blockquote, owned elsewhere.
_VERSE_RE = re.compile(r"\{\{IVERSE(?:\[style:[^\]]*\])?:([\s\S]*?)\}IVERSE\}")
_BAR_RE = re.compile(r"«BAR(?:\[(\d+)\])?»")
_DIV_RE = re.compile(r"«DIV\[style:([^\]]*)\]»")
_SPAN_STYLE_RE = re.compile(r"«SPAN\[style:([^\]]*)\]»")
_SPAN_TITLE_RE = re.compile(r"«SPAN\[title:([^\]]*)\]»")
_BRACE2_RE = re.compile(r"«BRACE2\[(\d+)\|([lrud])\]»")
# «LN» decodes as INDEPENDENT open/close: the opener «LN:filename?|target|» → <a …>, «/LN» → </a>.
# The display rides through and finishes decoding in the later passes (order-invariant), unlike the old
# span-match whose display group excluded « and so leaked whenever a link held a marker decoded AFTER
# this pass (XL/SEC/TABLE).  A trailing second capture marks the 3-part (resolved) form.
_LN_OPEN_RE = re.compile(r"«LN:([^|«]*)\|(?:([^|«]*)\|)?")
_XL_OPEN_RE = re.compile(r"«XL:([^|«]*)(\|)?")
_SEC_RE = re.compile(r"«(?:SEC|ANCHOR):([^|»]*)\|[^»]*»")

_BRACE2_GLYPH = {"l": "⎧", "r": "⎫", "u": "⏞", "d": "⏟"}  # ⎧ ⎫ ⏞ ⏟


def _verse(m):
    # Verse line breaks ride as «BR» in the body (decoded to <br> further down); there is
    # no «\n» to split.  The span is the inline (in-cell / in-footnote) verse form.
    return '<span class="cell-verse">' + m.group(1) + "</span>"


def build_outline_ul(items, plate, render_item):
    """Nested ``<ul>`` from ``(depth, content)`` items — sparse source depths densified
    to dense levels.  The ONE owner of the outline structure; ``render_item(content)``
    styles each item body: a ``<p>``-wrap for the standalone BLOCK outline, identity for one
    inside a table cell / verse / footnote (where ``decode_inline`` finishes the item's inline
    markers in place — the wrap and the item markers alike decode in the passes below)."""
    if not items:
        return ""
    rank = {d: i for i, d in enumerate(sorted({d for d, _ in items}))}
    root = "outline plate-outline" if plate else "outline"
    # Fully-indented outline — every item carries ≥1 level of indent, no margin-level
    # heading — renders as an indented block, keeping the indentation the source states.
    if min(d for d, _ in items) >= 1:
        root += " outline-indent"
    out = [f'<ul class="{root}">']
    cur = 0
    for depth, content in items:
        lvl = rank[depth]
        while cur < lvl:
            out.append("<ul>")
            cur += 1
        while cur > lvl:
            out.append("</ul>")
            cur -= 1
        out.append(f"<li>{render_item(content)}</li>")
    while cur > 0:
        out.append("</ul>")
        cur -= 1
    out.append("</ul>")
    return "".join(out)


def _outline_body(inner, open_m, close_m, render_item):
    """One outline body → its nested <ul>: parse the flat «OLI:depth»…«/OLI» items and render.
    ``render_item`` styles each item — identity for the INLINE form (item markers finish in
    ``decode_inline``'s later passes), a `<p>`-wrap for the BLOCK form (top-level outline).  No
    items ⇒ hand the raw span back unchanged."""
    items, i = [], 0
    while True:
        a = inner.find("«OLI:", i)
        if a == -1:
            break
        colon = inner.find("»", a)
        end = inner.find("«/OLI»", colon)
        items.append((int(inner[a + len("«OLI:"):colon]), inner[colon + 1:end]))
        i = end + len("«/OLI»")
    return build_outline_ul(items, None, render_item) if items else f"{open_m}{inner}{close_m}"


def _render_outlines(h, open_m="«IOUTLINE»", close_m="«/IOUTLINE»", render_item=lambda c: c):
    """Render each ``open_m``…``close_m`` outline in place, matching its close by DEPTH — a
    balanced scan, not a span-match that would mis-pair on a nested outline.  Serves BOTH forms:
    the INLINE «IOUTLINE» (in a cell / footnote — plain <li> items) and the BLOCK «OUTLINE»
    (top-level — `<p>`-wrapped items), selected by the caller via ``open_m``/``render_item``."""
    out, i = [], 0
    while True:
        a = h.find(open_m, i)
        if a == -1:
            out.append(h[i:])
            break
        out.append(h[i:a])
        depth, j, close_end = 1, a + len(open_m), None
        while depth:
            no, nc = h.find(open_m, j), h.find(close_m, j)
            if nc == -1:
                break                                  # unbalanced (a non-case)
            if no != -1 and no < nc:
                depth, j = depth + 1, no + len(open_m)
            else:
                depth, j = depth - 1, nc + len(close_m)
                if depth == 0:
                    close_end = j
        if close_end is None:                          # unbalanced: leave the marker raw, move on
            out.append(open_m)
            i = a + len(open_m)
            continue
        out.append(_outline_body(h[a + len(open_m):close_end - len(close_m)], open_m, close_m, render_item))
        i = close_end
    return "".join(out)


def _span_title(m):
    return f'<span class="xlit" title="{m.group(1).replace(chr(34), "&quot;")}">'


def _brace2(m):
    side = m.group(2)
    return f'<span class="brace2 brace2-{side}">{_BRACE2_GLYPH[side]}</span>'


def _ln_open_factory(article_url):
    def _ln_open(m):
        # «LN:filename|target|» (3-part, resolved) or «LN:target|» (2-part, unresolved).  A second
        # capture => the 3-part form: g1=filename, g2=target; else g1=target and there is no file.
        g1, g2 = m.group(1), m.group(2)
        has_file = g2 is not None
        filename = g1 if has_file else None
        target = g2 if has_file else g1
        href = article_url(filename) if filename else "/search.html?q=" + _encode_uri_component(target)
        return f'<a href="{href}" class="article-link" title="{target}">'
    return _ln_open


def _xl_open(m):
    # «XL:url|display«/XL» → <a>…</a> as INDEPENDENT open/close (open here, «/XL»→</a> below).
    # No pipe ⇒ no display, so mirror the old `disp or url` fallback and emit the url as the link
    # text at the opener.  Order-invariant; a display finishes decoding in the later passes.
    url, pipe = m.group(1), m.group(2)
    tag = f'<a href="{url}" class="external-link" target="_blank" rel="noopener">'
    return tag if pipe else tag + url


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


# ── block-level forms — render_paragraph's former job, folded into the ONE decoder ──────────
# These fire only for the article BODY (``decode_inline(..., body_blocks=True)``); a cell /
# footnote never carries them (a verse / outline there is the IVERSE / IOUTLINE inline form).
# The browser closes the open-only «P» — so there is no paragraph re-inference and no block
# re-scan: every construct is one balanced marker decoded in place, exactly like the inline ones.
_VERSE_BLOCK_OPEN_RE = re.compile(r"\{\{VERSE(?:\[style:([^\]]*)\])?:")
_TABLE_COLS_RE = re.compile(r"«TABLE\[cols:(\d+)")


def _verse_block_open(m):
    style = f' style="{m.group(1).replace(chr(34), "&quot;")}"' if m.group(1) else ""
    return f'<blockquote class="verse"{style}>'


def _balanced_end(h, a, open_m, close_m):
    """Index just past the depth-balanced close of the marker opening at ``a`` (None if none)."""
    depth, j = 1, a + len(open_m)
    while depth:
        no, nc = h.find(open_m, j), h.find(close_m, j)
        if nc == -1:
            return None
        if no != -1 and no < nc:
            depth, j = depth + 1, no + len(open_m)
        else:
            depth, j = depth - 1, nc + len(close_m)
            if depth == 0:
                return j


def _render_eqn(h, ctx):
    """Each «EQN:label»content«/EQN» → the math-system grid (content row + right-margin label).
    A lone «MATH» content renders in forced display mode; other content decodes inline in the
    passes below.  Close matched by DEPTH (uniform with FN/OUTLINE; EQN itself does not nest)."""
    from britannica.render.article import _MATH_ONLY_RE, _render_display_math
    OPEN, CLOSE = "«EQN:", "«/EQN»"
    out, i = [], 0
    while True:
        a = h.find(OPEN, i)
        if a == -1:
            out.append(h[i:]); break
        out.append(h[i:a])
        lbl_end = h.find("»", a + len(OPEN))
        label = h[a + len(OPEN):lbl_end]
        close_end = _balanced_end(h, a, OPEN, CLOSE) if lbl_end != -1 else None
        if close_end is None:
            out.append(h[a:lbl_end + 1] if lbl_end != -1 else h[a:]); i = (lbl_end + 1) if lbl_end != -1 else len(h); continue
        content = h[lbl_end + 1:close_end - len(CLOSE)].strip()
        mo = _MATH_ONLY_RE.match(content)
        content_html = _render_display_math(mo.group(2), mo.group(1), ctx) if mo else content
        label_html = f'<div class="math-system-label">({label})</div>' if label else ""
        out.append(f'<div class="math-system"><div class="math-system-rows">'
                   f'<div class="math-system-row">{content_html}</div></div>{label_html}</div>')
        i = close_end
    return "".join(out)


def _wrap_wide_tables(h, ctx):
    """Wrap each cols≥10 «TABLE[…]…«/TABLE» in a `wide-table-wrap` figure + Expand button
    (balanced close, so a nested table isn't torn).  The corpus has NO wide table nested inside
    another table, so this left-to-right scan wraps exactly the top-level wide ones."""
    OPEN, CLOSE = "«TABLE[", "«/TABLE»"
    out, i = [], 0
    while True:
        a = h.find(OPEN, i)
        if a == -1:
            out.append(h[i:]); break
        out.append(h[i:a])
        close_end = _balanced_end(h, a, OPEN, CLOSE)
        if close_end is None:
            out.append(h[a:a + len(OPEN)]); i = a + len(OPEN); continue
        span = h[a:close_end]
        cm = _TABLE_COLS_RE.match(span)
        cols = int(cm.group(1)) if cm else 0
        if cols >= 10:
            ctx.wide_table_counter += 1
            out.append(f'<figure class="wide-table-wrap"><button class="expand-table-btn" '
                       f'data-wt="wt-{ctx.wide_table_counter}" title="Open full-width view">'
                       f'⤢ Expand ({cols} columns)</button>'
                       f'<div class="wide-table-inline">{span}</div></figure>')
        else:
            out.append(span)
        i = close_end
    return "".join(out)


def decode_inline(h, *, escape=False, skip_math=False, article_url=None,
                  is_local=True, body_blocks=False, ctx=None):
    """Decode an inline marker string to HTML, reproducing ``decodeInlineMarkers``.

    ``ctx`` carries the per-article footnote state (counter / named numbers / collected
    list); it must be provided wherever «FN» can appear (title, prose, cells).  Without
    it FN markers are left untouched (the inline unit battery passes none).

    ``body_blocks=True`` (the article body only) additionally decodes the block-level forms
    render_paragraph used to own — page markers, «SH», «EQN» grids, top-level «VERSE» /
    «OUTLINE», and the cols≥10 wide-table wrap — as balanced markers in place, letting the
    browser close the open «P».  A cell / footnote leaves it False (no block form appears there).
    """
    # Inline «LN» links honor the render target's URL scheme via ctx.is_local, so the
    # body's links match the panel/plate links (production clean URLs off-golden, the
    # jsdom-stub form in the golden where is_local defaults True).
    article_url = article_url or (
        lambda fn: _article_url(fn, getattr(ctx, "is_local", True), getattr(ctx, "epub_bundled", None)))

    # IMG — shielded from escapeHtml (the filename is a URL, not display text) and
    # restored last, exactly as the viewer does.  FN / MATH stay deferred (block+shell
    # layers own them); the prose path passes skip_math and leaves «MATH» here.
    # The placeholder list is ARTICLE-scoped (on ctx) so a footnote's image — shielded
    # in the outer paragraph decode, restored inside the nested footnote decode — isn't
    # lost when the inner call has no images of its own.  Indices stay unique via append.
    img_html = ctx.img_html if ctx is not None and hasattr(ctx, "img_html") else []

    def _shield_img(m):
        # Images resolve to one location-agnostic path (/data/images/, local and web), so —
        # unlike article links — they no longer key on is_local.  Just render and shield.
        img_html.append(render_img(m.group(1), parse_img_meta(m.group(2)), m.group(3) or ""))
        return f"\x00IMG{len(img_html) - 1}\x00"

    h = _IMG_PARTS_RE.sub(_shield_img, h)

    if escape:
        h = escape_html(h)

    if body_blocks:
        # BODY-only block forms.  Page markers + shoulder headings go first: «SH» derives its
        # display text by STRIPPING its inner markers, so it must consume the span before the
        # footnote / styler passes decode those same markers.
        from britannica.render.article import render_page_markers, _render_sh
        h = render_page_markers(h, ctx)
        h = _render_sh(h)

    # Footnotes decode the same in every context (title, prose, cell) — numbered and
    # collected through the shared ctx so a title footnote is #1.
    if ctx is not None:
        h = _render_footnotes(h, ctx)

    if body_blocks:
        # «EQN» display-equation grids — BEFORE the math pass, so a lone-«MATH» equation is
        # forced into display mode (and its «MATH» consumed) here rather than decoded inline.
        h = _render_eqn(h, ctx)

    if not skip_math:
        if getattr(ctx, "target", None) in ("site", "epub", "kindle"):
            # Rendering targets → the SAME renderer the prose path uses (ONE owner), so a cell's
            # «MATH» honors its [display]/popout/fs hint instead of being forced inline.  A
            # «MATH[display]» inside a cell — ALGEBRA's detached-coefficients tableau holds
            # \begin{align} — MUST emit display mode, or KaTeX rejects the align environment and the
            # raw LaTeX leaks.  The old inline stub hardcoded display=0 and dropped the hint: that WAS
            # the leak.  Site hydrates KaTeX client-side; the EPUB targets read the pre-rendered
            # SVG/PNG cache (`_tex_math` → `_epub_math_el`) — same owner, same hint handling.
            from britannica.render.article import _render_math_markers  # deferred: article imports inline
            h = _render_math_markers(h, ctx)
        else:
            h = _MATH_RE.sub("«MATHPH»", h)

    h = _VERSE_RE.sub(_verse, h)   # IVERSE (inline) → cell-verse span
    if body_blocks:
        # Top-level «VERSE» → blockquote (independent open/close subs; «BR» → <br> below).
        h = _VERSE_BLOCK_OPEN_RE.sub(_verse_block_open, h).replace("}VERSE}", "</blockquote>")
    # An «IOUTLINE» in a cell/footnote — the item markers decode in the passes below (identity).
    h = _render_outlines(h)
    if body_blocks:
        # Top-level «OUTLINE» → the same nested <ul>, its items <p>-wrapped (block-level).
        h = _render_outlines(h, "«OUTLINE»", "«/OUTLINE»", lambda c: f"<p>{c}</p>")

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

    # «BAR[N]» = a fixed N-em rule; a bare «BAR» ({{bar}} with no width) is a
    # sum-line rule under a figures column (MANCHURIA) → span the whole cell.
    h = _BAR_RE.sub(
        lambda m: (f'<span class="inline-bar" style="width:{m.group(1)}em">&nbsp;</span>'
                   if m.group(1) else
                   '<span class="inline-bar" style="display:block;width:100%">&nbsp;</span>'), h)
    # DHR divider — the block-vs-inline choice rides in the marker («DHR» vs «DHRI», stamped by the
    # producer off ctx.inline), so the render is a plain token sub with no per-caller flag.  «DHRI»
    # first (it is not a prefix of «DHR», but resolve it before «DHR» to keep the two independent).
    h = re.sub(r"«DHRI(?:\[[^\]]*\])?»", '<span class="dhr-inline"></span>', h)
    h = re.sub(r"«DHR(?:\[[^\]]*\])?»", '<span class="dhr-block"></span>', h)

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

    h = _LN_OPEN_RE.sub(_ln_open_factory(article_url), h).replace("«/LN»", "</a>")
    h = _XL_OPEN_RE.sub(_xl_open, h).replace("«/XL»", "</a>")
    h = _SEC_RE.sub(r'<span id="section-\1" class="section-anchor"></span>', h)

    # Wide-table wrap (BODY only): a cols≥10 table gains its `wide-table-wrap` figure + Expand
    # button BEFORE the markers decode, so the balanced «TABLE…«/TABLE» inside becomes the
    # wrapped <table>.  (A cell / footnote leaves body_blocks False — no wrap, as before.)
    if body_blocks:
        h = _wrap_wide_tables(h, ctx)

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


def _render_footnotes(h, ctx):
    """Replace each «FN[name]?:body«/FN» with its numbered superscript + popup (via
    ``render_fn_marker``, the one owner of numbering/collection), matching its close by DEPTH —
    a balanced scan, not a span-match that would mis-pair a footnote-in-footnote."""
    out, i = [], 0
    while True:
        m = _FN_OPEN_RE.search(h, i)
        if m is None:
            out.append(h[i:])
            break
        out.append(h[i:m.start()])
        name = m.group(1)
        depth, j, close_end = 1, m.end(), None
        while depth:
            no, nc = _FN_OPEN_RE.search(h, j), h.find("«/FN»", j)
            if nc == -1:
                break                                   # unbalanced (a non-case)
            if no is not None and no.start() < nc:
                depth, j = depth + 1, no.end()
            else:
                depth, j = depth - 1, nc + len("«/FN»")
                if depth == 0:
                    close_end = j
        if close_end is None:                           # unbalanced: leave the opener raw, move on
            out.append(h[m.start():m.end()])
            i = m.end()
            continue
        out.append(render_fn_marker(name, h[m.end():close_end - len("«/FN»")], ctx))
        i = close_end
    return "".join(out)


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
    # Popup content rides in an INERT <template>, not a live inline <span>.  A
    # footnote whose body is a <table> (block content) inside an inline <span>
    # inside the body <p> made the parser close the <p> at the <table> start
    # tag — emptying the popup AND foster-parenting the table loose into the
    # body (AGRICULTURE fn5).  <template> content is parsed into its own inert
    # fragment, so it never closes the <p>; toggleFnPopup clones it into a
    # positioned popup on demand.  The Notes section renders the same content in
    # a valid <li> block, unaffected.
    return (
        f'<sup class="footnote-ref" id="{ref_id}">'
        f'<a onclick="toggleFnPopup(event,\'{popup_id}\');return false;" href="#">{num}</a>'
        f'<template class="fn-popup-tpl" data-popup-id="{popup_id}">'
        f'<span class="fn-popup-num">{num}.</span>'
        f'{format_footnote_text(content, ctx)}</template></sup>'
    )


def format_footnote_text(text, ctx):
    """Footnote body → HTML.  Input is already escaped (title/prose/cell), so no re-escape —
    just the shared inline decode (matching formatFootnoteText)."""
    return decode_inline(text, ctx=ctx)
