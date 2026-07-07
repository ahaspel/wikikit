"""Article marker-stream → HTML: the Python port of the viewer's renderArticle/renderParagraph.

Builds the SAME open-only template the viewer builds (mechanical marker decode); a browser
(or normalize_html) does the tag fixup.  Verified by diffing normalize_html(this) against the
jsdom golden (tests/snapshots/render/<stem>.html), per project_render_to_python.

Scope of this brick: the shell (card / metadata / contributors / xref), the body \\n\\n
pipeline (balanced-block collapse, split, continuation + SH merge), and renderParagraph's
mixed-block split, TITLE, and prose paths (page markers, inline decode, MATH→«MATHPH» stub).

DEFERRED to later bricks (the diff flags any seed that needs them): IMG, OUTLINE, VERSE,
LEGEND, HTMLTABLE/CHEM tables, EQNGROUP math systems, footnotes, section detection + TOC,
hieroglyph, the renderTitleMarkers fallback.
"""
import html as _html
import re

from britannica.render.inline import (
    decode_inline,
    escape_html,
    _encode_uri_component as _enc,
)

# ── marker constants ──
TITLE_OPEN, TITLE_CLOSE = "«TITLE:", "«/TITLE»"
HTMLTABLE_OPEN, HTMLTABLE_CLOSE = "«HTMLTABLE:", "«/HTMLTABLE»"
CHEM_OPEN, CHEM_CLOSE = "«CHEM:", "«/CHEM»"
DIV_OPEN, DIV_CLOSE = "«DIV[", "«/DIV»"
EQNGROUP_OPEN, EQNGROUP_CLOSE = "«EQNGROUP»", "«/EQNGROUP»"

_PAGE_RE = re.compile("\x01PAGE:(\\d+)\x01")
_MATH_RE = re.compile(r"«MATH(?:\[([^\]]*)\])?:(.*?)«/MATH»", re.S)
_SH_RE = re.compile(r"«SH:([^»]*)»(.*?)«/SH»", re.S)
_SH_STRIP_RE = re.compile(r"«/?[A-Za-z]+(?:\[[^\]]*\])?»")

BLOCK_MARKER_SCAN_RE = re.compile(
    r"\{\{TABLEH?(?:\[style:[^\]]*\])?:[\s\S]*?\}TABLE\}"
    r"|\{\{VERSE(?:\[style:[^\]]*\])?:[\s\S]*?\}VERSE\}"
    r"|\{\{LEGEND:[\s\S]*?\}LEGEND\}"
    r"|«OUTLINE:[\s\S]*?«/OUTLINE»"
    r"|«PLATE_OUTLINE:[\s\S]*?«/PLATE_OUTLINE»"
    r"|«HTMLTABLE:[\s\S]*?«/HTMLTABLE»"
    r"|«CHEM:[\s\S]*?«/CHEM»"
    r"|«TITLE:[\s\S]*?«/TITLE»"
)

# Block markers rendered by a paragraph-anchored regex: collapse internal \n\n so the
# paragraph split can't fragment them.  (HTMLTABLE/DIV use the balanced walker instead.)
_BLOCK_MARKER_RES = [
    re.compile(r"\{\{TABLEH?(?:\[style:[^\]]*\])?:[\s\S]*?\}TABLE\}"),
    re.compile(r"\{\{VERSE(?:\[style:[^\]]*\])?:[\s\S]*?\}VERSE\}"),
    re.compile(r"\{\{LEGEND:[\s\S]*?\}LEGEND\}"),
    re.compile(r"«OUTLINE:[\s\S]*?«/OUTLINE»"),
    re.compile(r"«PLATE_OUTLINE:[\s\S]*?«/PLATE_OUTLINE»"),
    re.compile(r"«CHEM:[\s\S]*?«/CHEM»"),
]

# A structural block keeps a paragraph from the plain-text continuation merge.
_NON_TEXT_PREFIX = re.compile(
    r"^(?:\{\{(?:TABLE|LEGEND|IMG|VERSE)|«HTMLTABLE|«SEC:|«SH:|«ANCHOR:|«OUTLINE:|«PLATE_OUTLINE:|«TITLE:)"
)


def _section_slug(name):
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"^-|-$", "", s)


def _article_url(filename):
    # Matches the jsdom reference stub (BritannicaUrls.filenameToUrl).
    return "/article/" + filename


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


def _collapse_balanced(s, open_m, close_m):
    """Collapse internal \\n\\n+ → \\n inside every balanced `open_m…close_m` block."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        oi = s.find(open_m, i)
        if oi == -1:
            out.append(s[i:])
            break
        out.append(s[i:oi])
        end = find_marker_end(s, oi, open_m, close_m)
        if end == -1:
            out.append(s[oi:])
            break
        out.append(re.sub(r"\n\n+", "\n", s[oi:end]))
        i = end
    return "".join(out)


class RenderContext:
    """Per-article render state (mirrors renderArticle's module-level counters)."""

    def __init__(self, volume, scan_url, unproofed_pages):
        self.volume = volume
        self.scan_url = scan_url
        self.unproofed_pages = unproofed_pages
        self.math_popout_counter = 0
        self.math_popout_latex = {}
        self.footnote_counter = 0
        self.named_fn_numbers = {}
        self.collected_footnotes = []
        self.collected_sections = []
        self.wide_table_counter = 0


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


def _render_math_markers(html, ctx):
    def repl(m):
        hint, latex = m.group(1), m.group(2)
        ph = parse_math_hint(hint)
        if ph["popout"]:
            return render_math_popout(_process_latex(latex), ph["display"], ctx)
        result = "«MATHPH»"  # katex stub
        fs = ph["fsPct"]
        if fs and 0 < fs < 100:
            return f'<span class="math-scaled" style="font-size: {fs}%;">{result}</span>'
        return result
    return _MATH_RE.sub(repl, html)


def render_page_markers(s, ctx):
    def repl(m):
        page = m.group(1)
        unproofed = page in ctx.unproofed_pages
        cls = "page-marker unproofed" if unproofed else "page-marker"
        title = (f"Volume {ctx.volume}, page {page} (unproofed source) — click to view scan"
                 if unproofed else f"Volume {ctx.volume}, page {page} — click to view scan")
        href = f"{ctx.scan_url}&pinit={page}"
        return (f'<a class="{cls}" data-page="{page}" title="{title}" href="{href}">'
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
    blocks = []
    guard = 0
    for m in matches:
        if m.start() < guard:
            continue
        text = m.group(0)
        if text.startswith(HTMLTABLE_OPEN) or text.startswith(CHEM_OPEN):
            if text.startswith(CHEM_OPEN):
                end = find_marker_end(p, m.start(), CHEM_OPEN, CHEM_CLOSE)
            else:
                end = find_marker_end(p, m.start(), HTMLTABLE_OPEN, HTMLTABLE_CLOSE)
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
        h = decode_inline(escape_html(inner))
        dc = re.match(r"^((?:<[^>]+>)*)([\s\S])([\s\S]*)$", h, re.S)
        if dc:
            h = (f"{dc.group(1)}<span style=\"font-size:1.6em; line-height:1; "
                 f"vertical-align:baseline;\">{dc.group(2)}</span>{dc.group(3)}")
        return f"<h1>{h}</h1>"

    # DEFERRED block paths (EQNGROUP, IMG, OUTLINE, VERSE, LEGEND, HTMLTABLE/CHEM):
    # fall through to prose so the marker leaks visibly and the diff flags the gap.

    # Prose → <p> with left-margin page markers + inline decode + MATH stub.
    html = escape_html(p)
    html = render_page_markers(html, ctx)
    html = _render_sh(html)
    html = decode_inline(html, skip_math=True)
    html = _render_math_markers(html, ctx)
    return f"<p>{html}</p>"


def _merge_paras(raw_paras):
    """The \\n\\n merge pass: plain-text continuation + shoulder-heading absorb.

    (EQNGROUP bundling is DEFERRED with the math-system brick.)
    """
    def strip_margin(s):
        s = re.sub(r"\x01PAGE:\d+\x01", "", s)
        s = re.sub(r"«SH:[^»]*».*?«/SH»", "", s, flags=re.S)
        return s.strip()

    merged = []
    i = 0
    n = len(raw_paras)
    while i < n:
        cur = raw_paras[i]
        if merged and not _NON_TEXT_PREFIX.match(cur):
            prev = merged[-1]
            if not _NON_TEXT_PREFIX.match(prev):
                pt, ct = strip_margin(prev), strip_margin(cur)
                if pt and ct and re.search(r"[,;]$", pt) and re.match(r"^[a-z]", ct):
                    merged[-1] = prev + " " + cur
                    i += 1
                    continue
        if cur.startswith("«SH:"):
            if merged:
                merged[-1] += " " + cur
            else:
                merged.append(cur)
            if i + 1 < n and not raw_paras[i + 1].startswith("«SH:"):
                merged[-1] += " " + raw_paras[i + 1]
                i += 1
        else:
            merged.append(cur)
        i += 1
    return merged


def _render_body(article, ctx):
    body = article.get("body") or ""
    marked = re.sub(r"^«TITLE:[\s\S]*?«/TITLE»", "", body, count=1)
    if "\x01PAGE:" not in marked and article.get("page_start"):
        marked = f"\x01PAGE:{article['page_start']}\x01" + marked
    marked = _collapse_balanced(marked, HTMLTABLE_OPEN, HTMLTABLE_CLOSE)
    marked = _collapse_balanced(marked, DIV_OPEN, DIV_CLOSE)
    for rx in _BLOCK_MARKER_RES:
        marked = rx.sub(lambda m: re.sub(r"\n\n+", "\n", m.group(0)), marked)
    merged = _merge_paras(marked.split("\n\n"))
    # detect_sections(merged, ctx) — DEFERRED (section/TOC brick)
    body_html = "".join(
        render_paragraph(pp, merged[idx + 1] if idx + 1 < len(merged) else None, ctx)
        for idx, pp in enumerate(merged)
    )
    toc_html = ""  # DEFERRED
    return toc_html + f'<div class="body-text">{body_html}</div>'


def _scan_url(article, is_local, back_href):
    base = "scans.html" if is_local else "/scans.html"
    return (f"{base}?vol={article.get('volume')}&start={article.get('leaf_start')}"
            f"&end={article.get('leaf_end')}&label={_enc(str(article.get('title') or ''))}"
            f"&back={_enc(back_href)}")


def _build_xref_href(xref, is_local):
    if xref.get("target_filename"):
        base = _article_url(xref["target_filename"])
    elif xref.get("normalized_target"):
        base = _article_url(str(xref["normalized_target"]).strip().lower() + ".json")
    else:
        return "#"
    if xref.get("target_section"):
        slug = _section_slug(xref["target_section"])
        if slug:
            base = base + "#section-" + slug
    return base


def render_article(article, *, is_local=True, back_href="http://localhost/"):
    """Render an article JSON to the viewer's (open-only) HTML template."""
    ctx = RenderContext(
        volume=article.get("volume", "?"),
        scan_url=_scan_url(article, is_local, back_href),
        unproofed_pages=(article.get("source_quality") or {}).get("unproofed_pages") or {},
    )
    xrefs = sorted(article.get("xrefs") or [], key=lambda x: x.get("normalized_target") or "")
    contributors = article.get("contributors") or []

    xref_html = ""
    if xrefs:
        items = []
        for xref in xrefs:
            normalized = xref.get("normalized_target") or ""
            resolved = (xref.get("status") or "") == "resolved"
            inner = (f'<a href="{_build_xref_href(xref, is_local)}">{escape_html(normalized)}</a>'
                     if resolved else escape_html(normalized))
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
        h1 = f'<h1>{escape_html(article.get("title") or "Untitled")}</h1>'  # renderTitleMarkers DEFERRED

    vol = escape_html(article.get("volume", "?"))
    ps, pe = article.get("page_start"), article.get("page_end")
    pages = (f'pp. {escape_html(ps if ps is not None else "")}–{escape_html(pe if pe is not None else "")}'
             if ps != pe else f'p. {escape_html(ps if ps is not None else "")}')
    wc = f'&middot; {article["word_count"]:,} words' if article.get("word_count") else ""

    contrib_html = ""
    if contributors:
        parts = [
            f'<a href="/contributors.html?q={_enc(c.get("full_name", ""))}" style="color: var(--muted);">'
            f'{escape_html(c.get("full_name", ""))}</a> '
            f'<span style="color: var(--muted); font-size: 0.85em;">({escape_html(c.get("initials", ""))})</span>'
            for c in contributors
        ]
        contrib_html = f'<div class="contributors">By {", ".join(parts)}</div>'

    parent_html = ""   # DEFERRED (plate parent line)
    topics_html = ""   # always empty in the golden (topicMap unloaded)
    plates_html = ""   # DEFERRED

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
    footnotes_html = ""  # DEFERRED (footnotes card)

    xref_card = (f'<div class="card">\n          <h2>Cross-references</h2>\n'
                 f"          {xref_html}\n        </div>") if xref_html else ""

    return (
        f"\n        <div class=\"card\">"
        f"\n          {h1}"
        f"\n          <div style=\"font-size: 0.85rem; color: #6b5e4f; font-style: italic; margin-bottom: 6px;\">"
        f"\n            <a href=\"{ctx.scan_url}\" style=\"color: #6b5e4f;\">vol. {vol}, {pages}</a>"
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
