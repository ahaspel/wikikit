"""Article marker-stream → HTML: the Python port of the viewer's renderArticle.

Builds the SAME open-only template the viewer builds (mechanical marker decode); a browser
(or normalize_html) does the tag fixup.  Verified by diffing normalize_html(this) against the
jsdom golden (tests/snapshots/render/<stem>.html), per project_render_to_python.

Scope: the shell (card / metadata / contributors / xref / TITLE h1) only.  The BODY is decoded
by the ONE mechanical decoder — ``decode_inline(..., body_blocks=True)`` (inline.py) — which
substitutes every marker in place: prose breaks (open-only «P»→<p>, browser closes at the next
block), «SH» shoulder headings, «EQN» display-math grids, «VERSE»/«OUTLINE» blocks, the cols≥10
wide-table wrap, and every inline styler.  There is no render_paragraph, no block re-scan, and
no span-match regex.

Paragraph structure is CARRIED — prose breaks ride as «P», each numbered equation is a
self-delimiting «EQN» block — never re-inferred at render: no `\\n\\n` split, no merge pass,
no EQNGROUP wrapper.
"""
import html as _html
import re
import unicodedata

from britannica.render.inline import (
    decode_inline,
    escape_html,
    format_footnote_text,
    _article_url,
    _encode_uri_component as _enc,
)
from britannica.markers import markers_to_text
# ── marker constants ──
TITLE_OPEN, TITLE_CLOSE = "«TITLE:", "«/TITLE»"

_PAGE_RE = re.compile("\x01PAGE:(\\d+)\x01")
_MATH_RE = re.compile(r"«MATH(?:\[([^\]]*)\])?:(.*?)«/MATH»", re.S)
# A genuine single-«MATH» EQN row: the content group must NOT cross an internal «/MATH», so a
# multi-equation row («MATH:…«/MATH»  «MATH:…«/MATH») fails to match here (otherwise its close
# backtracks to the LAST «/MATH», lumping every equation into one display span and leaking the
# interior «/MATH»«MATH: markers) and each equation decodes inline instead.  The «EQN» grid owner
# (inline._render_eqn) imports this to force a lone-«MATH» row into display mode.
_MATH_ONLY_RE = re.compile(
    r"^«MATH(?:\[([^\]]*)\])?:((?:(?!«/MATH»)[\s\S])*?)«/MATH»\s*[.,;:]?\s*$", re.S)
_SH_RE = re.compile(r"«SH:([^»]*)»(.*?)«/SH»", re.S)
_SH_STRIP_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")
_ANCHOR_RE = re.compile(r"«SEC:([^|»]*)\|([^»]*)»|«SH:([^»]*)»([\s\S]*?)«/SH»")
_SECTION_ID_RE = re.compile(r'id="(section-[^"]+)"')

_ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV"]


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


def _render_title_h1(marker, ctx):
    """The head-of-body «TITLE:…«/TITLE» element → an <h1> with a drop-cap first character.
    The one block form the shell renders directly (it sits above the body, not in the «P» flow),
    so it stays here rather than in the body's mechanical decode."""
    inner = marker[len(TITLE_OPEN):len(marker) - len(TITLE_CLOSE)]
    h = decode_inline(escape_html(inner), ctx=ctx)
    dc = re.match(r"^((?:<[^>]+>)*)([\s\S])([\s\S]*)$", h, re.S)
    if dc:
        h = (f"{dc.group(1)}<span style=\"font-size:1.6em; line-height:1; "
             f"vertical-align:baseline;\">{dc.group(2)}</span>{dc.group(3)}")
    return f"<h1>{h}</h1>"


def _section_slug(name):
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"^-|-$", "", s)


class RenderContext:
    """Per-article render state (mirrors renderArticle's module-level counters)."""

    def __init__(self, volume, scan_url, unproofed_pages, target="site", is_local=True, epub_bundled=None):
        self.volume = volume
        self.scan_url = scan_url
        self.unproofed_pages = unproofed_pages
        self.target = target            # "site" (byte-identical to the viewer) | "epub"
        self.is_local = is_local        # article-link URL scheme (stub form vs production clean URL)
        self.epub_bundled = epub_bundled  # None on site; a set of in-book stems → the EPUB link policy
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
    # The LaTeX must ride IN the markup: the render runs in Python, the click
    # handler in the browser — an id into a render-side dict is a carry into a
    # void.  Attribute-escape it into data-latex; the handler reads the DOM.
    cls = "math-popout-link popout-display" if is_display else "math-popout-link"
    esc = _html.escape(latex, quote=True)
    return (f'<a class="{cls}" data-latex="{esc}" '
            "onclick=\"openMathPopout(this);return false;\" href=\"#\">"
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
        # popout is a SITE policy (click-to-modal needs JS + a viewport that
        # can't fit the equation).  Every other target renders the equation
        # itself — a popout-hinted expression is display math there.
        if ph["popout"] and ctx.target == "site":
            return render_math_popout(_process_latex(latex), ph["display"], ctx)
        result = _tex_math(latex, ph["display"] or ph["popout"], ctx)
        fs = ph["fsPct"]
        if fs and 0 < fs < 100:
            return f'<span class="math-scaled" style="font-size: {fs}%;">{result}</span>'
        return result
    return _MATH_RE.sub(repl, html)


def _render_display_math(latex, hint, ctx):
    """A display-mode equation («EQN» row) → a KaTeX-hydration placeholder (or popout / fs-scaled)."""
    ph = parse_math_hint(hint)
    if ph["popout"] and ctx.target == "site":
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
        # The page break IS a word separator; the marker floats to the margin (out
        # of flow), so FLANK it with a space or 'an⟨p.121⟩oath' collapses to
        # 'anoath'.  The 'vol:page' label lives in a `::after` pseudo-element
        # (data-vol/data-page), NOT a text node — generated content is excluded from
        # in-article/browser find and `textContent`, so the marker stays furniture
        # and a search for 'an oath' matches.
        if ctx.epub_bundled is not None:
            # EPUB drops scans (can't bundle page images; readers don't want the viewer
            # link).  Keep the printed-page boundary as a non-linked indicator — the
            # bibliographic reference survives without the scan.
            return f' <span class="{cls}" data-page="{page}" data-vol="{ctx.volume}"></span> '
        title = (f"Volume {ctx.volume}, page {page} (unproofed source) — click to view scan"
                 if unproofed else f"Volume {ctx.volume}, page {page} — click to view scan")
        # Bare `scans.html` anchor: fixScanHrefs rebuilds the real URL at load and adds
        # &pinit from data-page, so we bake neither the query string nor &pinit here.
        return (f' <a class="{cls}" data-page="{page}" data-vol="{ctx.volume}" '
                f'title="{title}" href="{ctx.scan_url}"></a> ')
    return _PAGE_RE.sub(repl, s)


def _render_sh(html):
    def repl(m):
        slug, content = m.group(1), m.group(2)
        display = _SH_STRIP_RE.sub("", content).strip()
        return f'<span class="shoulder-heading" id="section-{slug}">{display}</span>'
    return _SH_RE.sub(repl, html)


def dedupe_anchor_id(seen, id_):
    seen[id_] = seen.get(id_, 0) + 1
    return id_ if seen[id_] == 1 else f"{id_}-{seen[id_]}"


def _section_title_text(raw):
    """A section title reduced to plain text for the TOC.  The title can itself be
    a link (`«LN:Iron|Iron»`) or carry inline markers; the TOC entry is ALREADY a
    link to the section anchor, and an `<a>` can't nest the link's own `<a>`, so
    the marker interior collapses to its display text (recurse, don't read flat —
    else the marker escapes straight into the TOC: SOMALILAND, UNITED KINGDOM).

    `markers_to_text` SUPERSEDES the old `_SH_STRIP_RE` here: that regex matched
    the closing `«/LN»` but not the opening `«LN:…`, so it mangled a linked title
    into a broken half-marker.  Whitespace is NOT collapsed — same spacing the old
    strip produced, so only link-bearing titles change."""
    return markers_to_text(raw or "").strip()


def detect_sections(paragraphs, ctx):
    """Walk «SEC» (L1) and «SH» (L2) anchors in document order into ctx.collected_sections."""
    ctx.collected_sections = []
    seen = {}
    for p in paragraphs:
        for m in _ANCHOR_RE.finditer(p):
            if m.group(1) is not None:  # «SEC:slug|name» — major section
                ctx.collected_sections.append(
                    {"id": dedupe_anchor_id(seen, f"section-{m.group(1)}"),
                     "title": _section_title_text(m.group(2)), "level": 1})
            else:                       # «SH:slug»…«/SH» — shoulder heading
                display = _section_title_text(m.group(4))
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
    # browser auto-closes at the next block) and each numbered equation is a self-delimiting «EQN».
    # A leading «P» opens the first paragraph — the body has no separator before its first prose
    # run.  decode_inline(body_blocks=True) is the ONE mechanical decoder: it owns every block form
    # in place (page markers, «SH», «EQN» grids, «VERSE»/«OUTLINE», the cols≥10 wide-table wrap)
    # as balanced markers — no render_paragraph, no `\n\n` split, no block re-scan.
    detect_sections([marked], ctx)
    body_html = decode_inline("«P»" + marked, escape=True, body_blocks=True, ctx=ctx)
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
    # Sort by the SAME name the panel shows — the canonical title for a resolved
    # xref, the normalized reference otherwise — so the alphabetical order matches
    # the displayed labels (was sorting by the source phrasing, `normalized_target`).
    xrefs = sorted(article.get("xrefs") or [],
                   key=lambda x: _xref_sort_key(
                       x.get("target_title") or x.get("normalized_target") or ""))
    # Dedupe by target: two source refs to one article (ALGEBRA's "Continued
    # Fraction" + "Continued Fractions" → CONTINUED FRACTIONS) collapse to one
    # panel entry.  The old source-phrasing display masked this (the labels read
    # differently); the canonical display exposes it.
    _seen: set = set()
    _deduped = []
    for x in xrefs:
        key = x.get("target_filename") or x.get("normalized_target") or ""
        if key in _seen:
            continue
        _seen.add(key)
        _deduped.append(x)
    xrefs = _deduped
    contributors = article.get("contributors") or []

    xref_html = ""
    if xrefs:
        items = []
        for xref in xrefs:
            normalized = xref.get("normalized_target") or ""
            resolved = (xref.get("status") or "") == "resolved"
            # The panel is OUR index, so a resolved xref shows OUR canonical title
            # (DESCARTES, RENÉ), not the source's phrasing — the inline prose keeps
            # the original text ([[project_resolver_consolidation]] display policy).
            # Unresolved falls back to the normalized reference.  Recurse the display
            # through the decoder like all display text — the target may carry style
            # markers («I»/«SC») that rode in from the source; printing it raw leaked
            # them into the panel.
            disp = decode_inline(xref.get("target_title") or normalized,
                                 escape=True, ctx=ctx)
            inner = (f'<a href="{_build_xref_href(xref, is_local, epub_bundled)}">{disp}</a>'
                     if resolved else disp)
            items.append(
                f'\n                <li class="xref-item {"resolved" if resolved else "unresolved"}">'
                f"\n                  {inner}"
                f"\n                </li>")
        xref_html = (f'\n          <ul class="xref-list">'
                     f'\n            {"".join(items)}'
                     f"\n          </ul>")

    # h1 — the «TITLE» element at the head of the body (drop-cap first char); if the body
    # carries no «TITLE», fall back to the article's title field.
    body = article.get("body") or ""
    tm = re.match(r"^«TITLE:[\s\S]*?«/TITLE»", body)
    if tm:
        h1 = _render_title_h1(tm.group(0), ctx)
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
