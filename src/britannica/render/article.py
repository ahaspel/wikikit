"""Article marker-stream → HTML: the Python port of the viewer's renderArticle/renderParagraph.

Builds the SAME open-only template the viewer builds (mechanical marker decode); a browser
(or normalize_html) does the tag fixup.  Verified by diffing normalize_html(this) against the
jsdom golden (tests/snapshots/render/<stem>.html), per project_render_to_python.

Scope: the shell (card / metadata / contributors / xref) and renderParagraph's block-in-place
render — the mixed-block split peels every block («TABLE»/«EQN»/VERSE/LEGEND/OUTLINE) in place,
then TITLE, the self-rendering «EQN» (its own `math-system` grid + right-margin label), and the
prose path (page markers, inline decode, «P»→<p>, MATH→«MATHPH» stub).

Paragraph structure is CARRIED — prose breaks ride as «P», each numbered equation is a
self-delimiting «EQN» block — never re-inferred at render: no `\\n\\n` split, no merge pass,
no EQNGROUP wrapper.
"""
import html as _html
import re
import unicodedata

from britannica.render.inline import (
    build_outline_ul,
    decode_inline,
    escape_html,
    format_footnote_text,
    parse_img_meta,
    render_img,
    _article_url,
    _encode_uri_component as _enc,
)
# ── marker constants ──
TITLE_OPEN, TITLE_CLOSE = "«TITLE:", "«/TITLE»"
# The table rides as recursive markers now (`«TABLE[…]»…«TR»…«TD[…]»…«/TABLE»`),
# decoded mechanically by `decode_inline` — no `render_table`, no html5lib
# re-parse.  `«TABLE[` is the open (the `cols`/attr payload always follows).
TABLE_OPEN, TABLE_CLOSE = "«TABLE[", "«/TABLE»"

_PAGE_RE = re.compile("\x01PAGE:(\\d+)\x01")
_TABLE_COLS_RE = re.compile(r"«TABLE\[cols:(\d+)")
_MATH_RE = re.compile(r"«MATH(?:\[([^\]]*)\])?:(.*?)«/MATH»", re.S)
_EQN_PARA_RE = re.compile(
    r"^\s*((?:\x01PAGE:\d+\x01\s*)*)«EQN:([^»]*)»([\s\S]*?)«/EQN»\s*$", re.S)
# A genuine single-«MATH» EQN: the content group must NOT cross an internal «/MATH», so a
# multi-equation row («MATH:…«/MATH»  «MATH:…«/MATH») fails to match here (otherwise its
# close backtracks to the LAST «/MATH», lumping every equation into one display span and
# leaking the interior «/MATH»«MATH: markers) and falls to the inline decode instead — the
# same clean path the non-leaking multi-equation rows already took.
_MATH_ONLY_RE = re.compile(
    r"^«MATH(?:\[([^\]]*)\])?:((?:(?!«/MATH»)[\s\S])*?)«/MATH»\s*[.,;:]?\s*$", re.S)
_SH_RE = re.compile(r"«SH:([^»]*)»(.*?)«/SH»", re.S)
_SH_STRIP_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")
_ANCHOR_RE = re.compile(r"«SEC:([^|»]*)\|([^»]*)»|«SH:([^»]*)»([\s\S]*?)«/SH»")
_SECTION_ID_RE = re.compile(r'id="(section-[^"]+)"')
_VERSE_BLOCK_RE = re.compile(r"^\{\{VERSE(?:\[style:([^\]]*)\])?:([\s\S]*)\}VERSE\}$")
_IMG_ANCHORED_RE = re.compile(
    r"^\{\{IMG:([^|}]+)"
    r"((?:\|(?:align=(?:center|left|right)|width=\d+|height=\d+))*)"
    r"(?:\|([^{}]*))?\}\}$"
)


# ── Single-outline render: parse the flat «OLI:depth» items and hand them to the
# ONE owner (build_outline_ul), which stamps class="outline" (bullet-free) and
# densifies the sparse depths into nested <ul>s.  No forked renderer, no bullets.
def _render_outline_block(marker: str, ctx) -> str:
    """Parse the flat depth-tagged `«OLI:depth»…«/OLI»` items out of an `«OUTLINE»`
    block and render them through `build_outline_ul`.  Item content renders block-aware
    (a `:<math>` item's math, etc.) via `render_paragraph`."""
    inner = marker[len("«OUTLINE»"):-len("«/OUTLINE»")]
    items, i = [], 0
    while True:
        a = inner.find("«OLI:", i)
        if a == -1:
            break
        colon = inner.find("»", a)
        end = inner.find("«/OLI»", colon)
        items.append((int(inner[a + len("«OLI:"):colon]), inner[colon + 1:end]))
        i = end + len("«/OLI»")
    return build_outline_ul(items, None, lambda c: render_paragraph(c, None, ctx))
_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV"]

BLOCK_MARKER_SCAN_RE = re.compile(
    r"\{\{VERSE(?:\[style:[^\]]*\])?:[\s\S]*?\}VERSE\}"
    r"|«OUTLINE»[\s\S]*?«/OUTLINE»"
    r"|«TABLE\[[\s\S]*?«/TABLE»"
    r"|«EQN:[^»]*»[\s\S]*?«/EQN»"
    r"|«TITLE:[\s\S]*?«/TITLE»"
)


# Latin ligatures / letters NFKD leaves whole — expand to their base sequence for the
# primary collation level (Æ≈AE, Œ≈OE, …), matching localeCompare.
_LIGATURES = {"æ": "ae", "œ": "oe", "ø": "o", "ß": "ss", "þ": "th", "ð": "d", "đ": "d", "ł": "l"}


def _xref_sort_key(s):
    """Approximate the viewer's ``localeCompare`` xref ordering (UCA): symbols < digits <
    letters, case- and accent-insensitive at the primary level, with the original string as a
    secondary tiebreak (accented forms after their base).  Matches localeCompare across the
    corpus; full UCA (pyuca) would be exact for the rarest tailorings.
    """
    s = s or ""
    primary = []
    for c in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(c):        # drop accents → base letter (primary fold)
            continue
        lc = c.lower()
        if lc in _LIGATURES:
            primary.extend((2, b) for b in _LIGATURES[lc])
            continue
        cls = 0 if not c.isalnum() else (1 if c.isdigit() else 2)  # symbols < digits < letters
        primary.append((cls, lc))
    return (primary, s)


def _render_title_markers(value, ctx):
    """Inline title markers → HTML (no drop-cap): the H1 fallback and the parent-plate link."""
    return decode_inline(escape_html(value or ""), ctx=ctx)


def _section_slug(name):
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"^-|-$", "", s)


def find_marker_end(s, start, open_m, close_m):
    """Index just past the balanced close of the marker opening at `start` (-1 if none)."""
    depth = 1
    i = start + len(open_m)
    n = len(s)
    while i < n:
        if s.startswith(open_m, i):
            depth += 1
            i += len(open_m)
        elif s.startswith(close_m, i):
            depth -= 1
            i += len(close_m)
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _fn_span_ranges(s):
    """Balanced (start, end) ranges of every «FN:…«/FN» / «FN[name]:…«/FN» span in `s`.

    A footnote is an INLINE ref whose CONTENT may be body-level (a bibliography table,
    a verse quotation).  Its block markers belong to the footnote — the inline «FN»
    handler decodes the whole span as a unit — so the body block-scan must NOT split
    them out (that tears the «FN» span across block boundaries and leaks «FN:»/«/FN»).
    """
    ranges = []
    i = s.find("«FN")
    while i != -1:
        if s.startswith("«FN:", i) or s.startswith("«FN[", i):   # opener, not the «/FN» close
            end = find_marker_end(s, i, "«FN", "«/FN»")
            if end != -1:
                ranges.append((i, end))
                i = s.find("«FN", end)
                continue
        i = s.find("«FN", i + 3)
    return ranges


def _outline_span_ranges(s):
    """Balanced (start, end) ranges of every «OUTLINE»…«/OUTLINE» span in `s` — depth-matched, so a
    nested outline doesn't tear.  Parallel to `_fn_span_ranges`: keeps the body block-scan from
    splitting an outline whose items are newline-delimited across a line boundary."""
    ranges = []
    i = s.find("«OUTLINE»")
    while i != -1:
        end = find_marker_end(s, i, "«OUTLINE»", "«/OUTLINE»")
        if end == -1:
            break
        ranges.append((i, end))
        i = s.find("«OUTLINE»", end)
    return ranges


def _split_lines_keep_spans(text):
    """`text.split("\\n")`, except a "\\n" inside an «FN:…«/FN» or «OUTLINE:…«/OUTLINE»
    span is not a split point.

    A block renderer (verse / legend / outline) decodes its content line-by-line and
    joins with <br>.  A footnote whose body carries its own line break (a verse
    translation quoted in a note), or an OUTLINE whose items ARE newline-delimited, would
    otherwise be split across lines — each line's inline decode then sees a lone «FN:» /
    «/FN» / «OUTLINE:» / «/OUTLINE», a torn span that leaks.  Identical to ``str.split``
    whenever no such span straddles a newline.
    """
    ranges = _fn_span_ranges(text) + _outline_span_ranges(text)
    if not ranges:
        return text.split("\n")
    parts, last, i = [], 0, text.find("\n")
    while i != -1:
        if not any(a <= i < b for a, b in ranges):
            parts.append(text[last:i])
            last = i + 1
        i = text.find("\n", i + 1)
    parts.append(text[last:])
    return parts



class RenderContext:
    """Per-article render state (mirrors renderArticle's module-level counters)."""

    def __init__(self, volume, scan_url, unproofed_pages, target="site", is_local=True, epub_bundled=None):
        self.volume = volume
        self.scan_url = scan_url
        self.unproofed_pages = unproofed_pages
        self.target = target            # "site" (byte-identical to the viewer) | "epub"
        self.is_local = is_local        # article-link URL scheme (stub form vs production clean URL)
        self.epub_bundled = epub_bundled  # None on site; a set of in-book stems → the EPUB link policy
        self.math_popout_counter = 0
        self.math_popout_latex = {}
        self.footnote_counter = 0
        self.named_fn_numbers = {}
        self.fn_anchor_instance = 0
        self.collected_footnotes = []
        self.collected_sections = []
        self.wide_table_counter = 0
        # Image placeholders are article-scoped, not per-decode_inline call: a footnote's
        # image is shielded by the outer paragraph decode but restored inside the nested
        # footnote decode (or vice-versa).  A shared list keeps the «\x00IMGn\x00» → HTML
        # restore nesting-safe; indices stay globally unique via append.
        self.img_html = []


# ── math (KaTeX is stubbed to «MATHPH» in the golden; reproduce the structure) ──
def parse_math_hint(hint):
    toks = hint.split(",") if hint else []
    fs_pct = None
    for t in toks:
        m = re.fullmatch(r"fs=(\d+)", t)
        if m:
            fs_pct = int(m.group(1))
    return {"display": "display" in toks, "popout": "popout" in toks, "fsPct": fs_pct}


def _process_latex(latex):
    raw = _html.unescape(latex)
    raw = re.sub(r"\\mbox\b", r"\\text", raw)
    raw = re.sub(r"[          ​]", " ", raw)
    raw = re.sub(r"\\overset\{([^}]*)\}\s*\\underset\{([^}]*)\}\s*\{\\Sigma\s*", r"\\sum_{\2}^{\1}{", raw)
    return raw


def render_math_popout(latex, is_display, ctx):
    mid = "mp-" + str(ctx.math_popout_counter)
    ctx.math_popout_counter += 1
    ctx.math_popout_latex[mid] = latex
    cls = "math-popout-link popout-display" if is_display else "math-popout-link"
    return (f'<a class="{cls}" data-mp="{mid}" '
            f"onclick=\"openMathPopout('{mid}');return false;\" href=\"#\">"
            "[equation ⤢ click to view]</a>")


def _tex_math(latex, display, ctx):
    """Site target → a KaTeX-hydration placeholder carrying the (HTML-escaped) LaTeX and its
    display mode; the viewer runs KaTeX over `.tex-math` after inserting the page.  Other targets
    keep the `«MATHPH»` stub (the byte-identical comparison stub / EPUB path)."""
    if ctx.target != "site":
        return "«MATHPH»"
    return f'<span class="tex-math" data-display="{"1" if display else "0"}">{latex}</span>'


def _render_math_markers(html, ctx):
    def repl(m):
        hint, latex = m.group(1), m.group(2)
        ph = parse_math_hint(hint)
        if ph["popout"]:
            return render_math_popout(_process_latex(latex), ph["display"], ctx)
        result = _tex_math(latex, ph["display"], ctx)
        fs = ph["fsPct"]
        if fs and 0 < fs < 100:
            return f'<span class="math-scaled" style="font-size: {fs}%;">{result}</span>'
        return result
    return _MATH_RE.sub(repl, html)


def _render_display_math(latex, hint, ctx):
    """A display-mode equation («EQN» row) → a KaTeX-hydration placeholder (or popout / fs-scaled)."""
    ph = parse_math_hint(hint)
    if ph["popout"]:
        return render_math_popout(_process_latex(latex), True, ctx)
    result = _tex_math(latex, True, ctx)
    fs = ph["fsPct"]
    if fs and 0 < fs < 100:
        return f'<span class="math-scaled" style="font-size: {fs}%;">{result}</span>'
    return result


def render_page_markers(s, ctx):
    def repl(m):
        page = m.group(1)
        unproofed = page in ctx.unproofed_pages
        cls = "page-marker unproofed" if unproofed else "page-marker"
        if ctx.epub_bundled is not None:
            # EPUB drops scans (can't bundle page images; readers don't want the viewer
            # link).  Keep the printed-page boundary as a non-linked indicator — the
            # bibliographic reference survives without the scan.
            return f'<span class="{cls}" data-page="{page}">{ctx.volume}:{page}</span>'
        title = (f"Volume {ctx.volume}, page {page} (unproofed source) — click to view scan"
                 if unproofed else f"Volume {ctx.volume}, page {page} — click to view scan")
        # Bare `scans.html` anchor: fixScanHrefs rebuilds the real URL at load and adds
        # &pinit from data-page, so we bake neither the query string nor &pinit here.
        return (f'<a class="{cls}" data-page="{page}" title="{title}" href="{ctx.scan_url}">'
                f"{ctx.volume}:{page}</a>")
    return _PAGE_RE.sub(repl, s)


def _render_sh(html):
    def repl(m):
        slug, content = m.group(1), m.group(2)
        display = _SH_STRIP_RE.sub("", content).strip()
        return f'<span class="shoulder-heading" id="section-{slug}">{display}</span>'
    return _SH_RE.sub(repl, html)


def render_paragraph(p, next_para, ctx):
    """Render one merged paragraph to (open-only) HTML, mirroring renderParagraph."""
    # Mixed-paragraph split: block marker(s) + surrounding prose → split and recurse.
    matches = list(BLOCK_MARKER_SCAN_RE.finditer(p))
    fn_ranges = _fn_span_ranges(p)
    blocks = []
    guard = 0
    for m in matches:
        if m.start() < guard:
            continue
        if any(a <= m.start() < b for a, b in fn_ranges):
            continue   # block inside a footnote: kept whole, decoded by the «FN» handler
        text = m.group(0)
        if text.startswith(TABLE_OPEN):
            end = find_marker_end(p, m.start(), TABLE_OPEN, TABLE_CLOSE)
            if end != -1:
                text = p[m.start():end]
                guard = end
        blocks.append((m.start(), text))
    if blocks:
        only_block = len(blocks) == 1 and blocks[0][1] == p.strip()
        if not only_block:
            pieces = []
            cursor = 0
            for idx, text in blocks:
                if idx > cursor:
                    before = p[cursor:idx]
                    if before.strip():
                        pieces.append(before)
                pieces.append(text)
                cursor = idx + len(text)
            if cursor < len(p):
                after = p[cursor:]
                if after.strip():
                    pieces.append(after)
            return "".join(render_paragraph(piece.strip(), None, ctx) for piece in pieces)

    # Article title — «TITLE» heading element: escape, decode, drop-cap the first char.
    if p.startswith(TITLE_OPEN) and p.endswith(TITLE_CLOSE):
        inner = p[len(TITLE_OPEN):len(p) - len(TITLE_CLOSE)]
        h = decode_inline(escape_html(inner), ctx=ctx)
        dc = re.match(r"^((?:<[^>]+>)*)([\s\S])([\s\S]*)$", h, re.S)
        if dc:
            h = (f"{dc.group(1)}<span style=\"font-size:1.6em; line-height:1; "
                 f"vertical-align:baseline;\">{dc.group(2)}</span>{dc.group(3)}")
        return f"<h1>{h}</h1>"

    # Numbered display-math block — a self-contained «EQN:label»content«/EQN».  It renders its
    # own math plus its label in the right margin (the `math-system` CSS grid), the SAME HTML
    # the render-time «EQNGROUP» wrapper produced for a lone equation — which is every equation:
    # a group never carried a second numbered row (a 2nd label breaks the old loop), so there is
    # nothing to bundle.  The label rides inside the marker; no wrapper, no `\n\n` grouping.
    eqm = _EQN_PARA_RE.match(p)
    if eqm:
        label_text = eqm.group(2)
        content = eqm.group(3).strip()
        # A genuine single-«MATH» row renders in display mode; anything else — a multi-
        # equation row (Y_z=Z_y,  Z_x=X_z,  …), or prose+math — decodes inline, exactly as
        # the non-leaking multi-«MATH» rows (MOLECULE) already did.  The tightened
        # _MATH_ONLY_RE is what stops a multi-equation row from being lumped into one span
        # (which leaked the interior «/MATH»«MATH: markers); it now falls here, clean.
        mo = _MATH_ONLY_RE.match(content)
        content_html = (_render_display_math(mo.group(2), mo.group(1), ctx) if mo
                        else decode_inline(content, escape=True, dhr_inline=True, ctx=ctx))
        row_html = (f'<div class="math-system-row">'
                    f'{render_page_markers(eqm.group(1) or "", ctx)}{content_html}</div>')
        label_html = (f'<div class="math-system-label">({escape_html(label_text)})</div>'
                      if label_text else "")
        return (f'<div class="math-system"><div class="math-system-rows">'
                f'{row_html}</div>{label_html}</div>')

    # Standalone block image (a paragraph that IS one image) → the <img>, no <p> wrap.
    im = _IMG_ANCHORED_RE.match(p)
    if im:
        return render_img(im.group(1), parse_img_meta(im.group(2)), im.group(3) or "")

    # Hierarchical outline → a single recursive «OUTLINE»…«/OUTLINE» of nested «OLI:depth»
    # items, rendered through the shared owner build_outline_ul.  The same owner serves an
    # outline nested in a cell/verse via decode_inline's «OUTLINE» handler.
    if p.startswith("«OUTLINE»") and p.endswith("«/OUTLINE»"):
        return _render_outline_block(p, ctx)

    # Verse → blockquote (lines joined by <br>).
    vm = _VERSE_BLOCK_RE.match(p)
    if vm:
        style_attr = f' style="{vm.group(1).replace(chr(34), "&quot;")}"' if vm.group(1) else ""
        lines = [decode_inline(escape_html(s), ctx=ctx) for s in _split_lines_keep_spans(vm.group(2)) if s.strip()]
        return f'<blockquote class="verse"{style_attr}>{"<br>".join(lines)}</blockquote>'

    # Complex table (rowspan/colspan/nested/chem preserved) — «TABLE[…]».  It rides
    # as recursive markers, so it decodes through the SAME sequence as prose (escape
    # → page markers → inline decode → math): `decode_inline` owns the marker→tag
    # substitution for the table structure AND any nested table, in one pass, with
    # no html5lib re-parse.  Balanced-match the close so a table whose cell holds
    # another table isn't truncated; `cols` (off the opener, computed at classify)
    # drives the wide-table wrap.
    if p.startswith(TABLE_OPEN):
        end = find_marker_end(p, 0, TABLE_OPEN, TABLE_CLOSE)
        if end != -1:
            cm = _TABLE_COLS_RE.match(p)
            cols = int(cm.group(1)) if cm else 0
            html = escape_html(p[:end])
            html = render_page_markers(html, ctx)
            # Cell context: `dhr_inline=True`, and `skip_math` default False so a
            # cell's «MATH» decodes INLINE to the «MATHPH» stub — exactly as the old
            # per-cell decode did (the golden's katex stub returns «MATHPH» too).
            html = decode_inline(html, dhr_inline=True, ctx=ctx)
            trailing = p[end:].strip()
            trailing_html = render_paragraph(trailing, None, ctx) if trailing else ""
            if cols >= 10:
                # Wide table: renders inline but gains an "Expand" button to a full-width modal.
                ctx.wide_table_counter += 1
                rendered = (f'<figure class="wide-table-wrap"><button class="expand-table-btn" '
                            f'data-wt="wt-{ctx.wide_table_counter}" title="Open full-width view">'
                            f'⤢ Expand ({cols} columns)</button>'
                            f'<div class="wide-table-inline">{html}</div></figure>')
            else:
                rendered = html
            return rendered + trailing_html

    # Prose → <p> with left-margin page markers + inline decode + MATH stub.
    html = escape_html(p)
    html = render_page_markers(html, ctx)
    html = _render_sh(html)
    html = decode_inline(html, skip_math=True, ctx=ctx)
    html = _render_math_markers(html, ctx)
    return f"<p>{html}</p>"



def dedupe_anchor_id(seen, id_):
    seen[id_] = seen.get(id_, 0) + 1
    return id_ if seen[id_] == 1 else f"{id_}-{seen[id_]}"


def detect_sections(paragraphs, ctx):
    """Walk «SEC» (L1) and «SH» (L2) anchors in document order into ctx.collected_sections."""
    ctx.collected_sections = []
    seen = {}
    for p in paragraphs:
        for m in _ANCHOR_RE.finditer(p):
            if m.group(1) is not None:  # «SEC:slug|name» — major section
                ctx.collected_sections.append(
                    {"id": dedupe_anchor_id(seen, f"section-{m.group(1)}"),
                     "title": m.group(2), "level": 1})
            else:                       # «SH:slug»…«/SH» — shoulder heading
                display = _SH_STRIP_RE.sub("", m.group(4)).strip()
                ctx.collected_sections.append(
                    {"id": dedupe_anchor_id(seen, f"section-{m.group(3)}"),
                     "title": display, "level": 2})


def _toc_link(s):
    return f'<li><a href="#{s["id"]}">{escape_html(s["title"])}</a></li>'


def _build_toc(sections):
    level1 = [s for s in sections if s["level"] == 1]
    level2 = [s for s in sections if s["level"] == 2]
    if len(level1) >= 1 and len(level2) >= 1:
        # Interleaved: major sections with shoulder headings nested below; level-2
        # orphans (before any level-1) render standalone so the HTML stays valid.
        # A SINGLE major section counts (>= 1): a lone «SEC» among shoulders (AFGHANISTAN's
        # `History`, POLAND's `Polish Literature`) must appear in the TOC, not be dropped
        # to the shoulder-only branch which lists only level-2.
        inner = ""
        in_sub = False
        seen_level1 = False
        for s in sections:
            if s["level"] == 1:
                if in_sub:
                    inner += "</ul></li>"
                    in_sub = False
                elif seen_level1:
                    inner += "</li>"
                inner += f'<li><a href="#{s["id"]}">{escape_html(s["title"])}</a>'
                seen_level1 = True
            elif not seen_level1:
                inner += _toc_link(s)
            else:
                if not in_sub:
                    inner += "<ul>"
                    in_sub = True
                inner += _toc_link(s)
        if in_sub:
            inner += "</ul></li>"
        elif seen_level1:
            inner += "</li>"
        return f'<div class="toc"><h3>Contents</h3><ul class="toc-contents">{inner}</ul></div>'
    if len(level1) >= 2:
        return (f'<div class="toc"><h3>Contents</h3><ul class="toc-contents">'
                f'{"".join(_toc_link(s) for s in level1)}</ul></div>')
    if len(level2) >= 3:
        return (f'<div class="toc"><h3>Sections</h3><ol>'
                f'{"".join(_toc_link(s) for s in level2)}</ol></div>')
    return ""


def _render_body(article, ctx):
    body = article.get("body") or ""
    marked = re.sub(r"^«TITLE:[\s\S]*?«/TITLE»", "", body, count=1)
    if "\x01PAGE:" not in marked and article.get("page_start"):
        marked = f"\x01PAGE:{article['page_start']}\x01" + marked
    # Paragraph structure is CARRIED, not re-inferred: prose breaks ride as «P» (→<p>, the
    # browser auto-closes) and each numbered equation is a self-delimiting «EQN» block.  So there
    # is no `\n\n` split and no merge pass — render the whole body once; render_paragraph's block
    # scan peels every block («TABLE»/«EQN»/VERSE/…) in place and the prose runs through «P»→<p>.
    detect_sections([marked], ctx)
    body_html = render_paragraph(marked, None, ctx)
    # De-dup colliding section ids in the SAME document order detect_sections used, so
    # each TOC link resolves to its own anchor (first keeps section-<slug>, reuses get -N).
    id_seen = {}
    body_html = _SECTION_ID_RE.sub(
        lambda m: f'id="{dedupe_anchor_id(id_seen, m.group(1))}"', body_html)
    toc_html = _build_toc(ctx.collected_sections)
    return toc_html + f'<div class="body-text">{body_html}</div>'


def _build_xref_href(xref, is_local, bundled=None):
    if xref.get("target_filename"):
        base = _article_url(xref["target_filename"], is_local, bundled)
    elif xref.get("normalized_target"):
        base = _article_url(str(xref["normalized_target"]).strip().lower() + ".json", is_local, bundled)
    else:
        return "#"
    if xref.get("target_section"):
        slug = _section_slug(xref["target_section"])
        if slug:
            base = base + "#section-" + slug
    return base


def render_article(article, *, is_local=True, target="site", epub_bundled=None):
    """Render an article JSON to HTML.  target="site" is byte-identical to the viewer
    (corpus-proven); target="epub" swaps the per-target policies (footnotes, contributor
    links → appendix, scans dropped, …).  ``epub_bundled`` — a set of in-book stems —
    selects the EPUB link policy (see ``_article_url``); leave None for site."""
    ctx = RenderContext(
        volume=article.get("volume", "?"),
        scan_url="scans.html",   # bare anchor; fixScanHrefs rebuilds the real URL at runtime
        unproofed_pages=(article.get("source_quality") or {}).get("unproofed_pages") or {},
        target=target,
        is_local=is_local,
        epub_bundled=epub_bundled,
    )
    xrefs = sorted(article.get("xrefs") or [],
                   key=lambda x: _xref_sort_key(x.get("normalized_target") or ""))
    contributors = article.get("contributors") or []

    xref_html = ""
    if xrefs:
        items = []
        for xref in xrefs:
            normalized = xref.get("normalized_target") or ""
            resolved = (xref.get("status") or "") == "resolved"
            # Recurse the display through the decoder like all display text — the
            # target may carry style markers («I»/«SC») that rode in from the source;
            # printing it raw leaked them into the panel.  Clean targets are unchanged.
            disp = decode_inline(normalized, escape=True, ctx=ctx)
            inner = (f'<a href="{_build_xref_href(xref, is_local, epub_bundled)}">{disp}</a>'
                     if resolved else disp)
            items.append(
                f'\n                <li class="xref-item {"resolved" if resolved else "unresolved"}">'
                f"\n                  {inner}"
                f"\n                </li>")
        xref_html = (f'\n          <ul class="xref-list">'
                     f'\n            {"".join(items)}'
                     f"\n          </ul>")

    # h1 — the «TITLE» element at the head of the body, rendered through renderParagraph.
    body = article.get("body") or ""
    tm = re.match(r"^«TITLE:[\s\S]*?«/TITLE»", body)
    if tm:
        h1 = render_paragraph(tm.group(0), None, ctx)
    else:
        h1 = f'<h1>{_render_title_markers(article.get("title") or "Untitled", ctx)}</h1>'

    vol = escape_html(article.get("volume", "?"))
    ps, pe = article.get("page_start"), article.get("page_end")
    pages = (f'pp. {escape_html(ps if ps is not None else "")}–{escape_html(pe if pe is not None else "")}'
             if ps != pe else f'p. {escape_html(ps if ps is not None else "")}')
    wc = f'&middot; {article["word_count"]:,} words' if article.get("word_count") else ""
    # Site links the citation to the page scan; EPUB drops scans, so it's plain text.
    citation = (f"vol. {vol}, {pages}" if epub_bundled is not None
                else f'<a href="{ctx.scan_url}" style="color: #6b5e4f;">vol. {vol}, {pages}</a>')

    contrib_html = ""
    if contributors:
        def _contrib_link(c):
            name = c.get("full_name", "")
            # EPUB: link to the in-book contributors appendix anchor; site: the search page.
            href = ("contributors.xhtml#contrib-" + _section_slug(name) if epub_bundled is not None
                    else "/contributors.html?q=" + _enc(name))
            return (f'<a href="{href}" style="color: var(--muted);">{escape_html(name)}</a> '
                    f'<span style="color: var(--muted); font-size: 0.85em;">({escape_html(c.get("initials", ""))})</span>')
        parts = [_contrib_link(c) for c in contributors]
        contrib_html = f'<div class="contributors">By {", ".join(parts)}</div>'

    parent = article.get("parent_article")
    parent_html = ""
    if parent:
        parent_html = ('<div style="margin-bottom: 8px; font-size: 0.95rem;">Plate for '
                       f'<a href="{_article_url(parent["filename"], is_local, epub_bundled)}">'
                       f'{_render_title_markers(parent.get("title") or "", ctx)}</a></div>')
    topics_html = ""   # always empty in the golden (topicMap unloaded)
    plates = article.get("plates") or []
    plates_html = ""
    if plates and article.get("article_type") != "plate":
        links = ", ".join(
            f'<a href="{_article_url(p["filename"], is_local, epub_bundled)}">Plate {_ROMAN[i] if i < len(_ROMAN) else i + 1}</a>'
            for i, p in enumerate(plates))
        plates_html = f'<div class="contributors">Plates: {links}</div>'

    sq = article.get("source_quality") or {}
    source_notice = ""
    if sq.get("lowest_level") is not None and sq["lowest_level"] <= 1:
        source_notice = (
            '<div style="text-align: center; margin: 10px 0;">\n'
            '                 <div style="background: #fff3cd; border: 1px solid #ffc107; '
            'border-radius: 6px; padding: 6px 14px; font-size: 0.9rem; display: inline-block;">\n'
            "                   <strong>Source quality notice:</strong> This article spans "
            "pages with unproofread transcriptions.\n"
            "                 </div>\n"
            "               </div>")

    body_section = _render_body(article, ctx)
    footnotes_html = ""
    if ctx.collected_footnotes:
        if ctx.target == "epub":
            # Popup footnotes: the reader hides these asides and pops each up from its noteref.
            # No visible "Notes" section — the reader hides the aside content, so a heading over
            # them just renders empty.  The asides live at the end of the body as popup targets.
            footnotes_html = "".join(
                f'<aside epub:type="footnote" role="doc-footnote" id="fn-{fn["num"]}"><p>'
                f'<a epub:type="backlink" href="#fnref-{fn["num"]}">{fn["num"]}.</a> '
                f'{format_footnote_text(fn["text"], ctx)}</p></aside>'
                for fn in ctx.collected_footnotes
            )
        else:
            lis = "".join(
                f'<li id="fn-{fn["num"]}" value="{fn["num"]}">'
                f'<a onclick="var el=document.getElementById(\'fnref-{fn["num"]}\');'
                f"if(el)el.scrollIntoView({{behavior:'instant',block:'start'}});return false;\" "
                f'href="#">{fn["num"]}.</a> {format_footnote_text(fn["text"], ctx)}</li>'
                for fn in ctx.collected_footnotes
            )
            footnotes_html = f'<div class="footnotes"><h3>Notes</h3><ol>{lis}</ol></div>'

    xref_card = (f'<div class="card">\n          <h2>Cross-references</h2>\n'
                 f"          {xref_html}\n        </div>") if xref_html else ""

    return (
        f"\n        <div class=\"card\">"
        f"\n          {h1}"
        f"\n          <div style=\"font-size: 0.85rem; color: #6b5e4f; font-style: italic; margin-bottom: 6px;\">"
        f"\n            {citation}"
        f"\n            {wc}"
        f"\n          </div>"
        f"\n          {parent_html}"
        f"\n          {contrib_html}"
        f"\n          {topics_html}"
        f"\n          {plates_html}"
        f"\n          {source_notice}"
        f"\n          {body_section}"
        f"\n          {footnotes_html}"
        f"\n        </div>"
        f"\n"
        f"\n        {xref_card}"
        f"\n"
        f"\n      "
    )
