"""EPUB chunk packing — the assembly layer between the render and the container.

A 37k-article book can't ship one XHTML file per article (37k manifest entries choke
readers), so articles pack in spine order into ~300KB chunk files, each article opening
at an anchor derived from its stable id.  The seam is a token contract:

  * The RENDER never learns chunk names.  Its EPUB link policy (``LINK_TOKENS``) emits
    canonical tokens — ``href="epublink:{stem}"`` / ``epublink:{stem}#section-{slug}`` /
    ``epubcontrib:{slug}`` — pure functions of content.
  * The PACKER — the only stage that knows chunk assignment — materializes real hrefs
    at chunk close (``resolve_chunk``).  Presence-awareness lives there too: a stem
    absent from the book resolves to the live-site URL.

Ids are namespaced per article (``a{stem}-``) so articles can share a file without
collisions; an oversized article (FRANCE, 1.7MB) splits at section boundaries
(``split_article``), a pure function of the staged bytes so plan and emit passes agree
by construction.
"""
import html as _html
import re
from xml.etree import ElementTree as ET

ET.register_namespace("epub", "http://www.idpf.org/2007/ops")
_EPUB_NS = "{http://www.idpf.org/2007/ops}"

SITE_BASE = "https://britannica11.org"
TARGET_CHUNK = 300_000     # soft chunk budget (bytes of XHTML)
HARD_SPLIT = 450_000       # an article bigger than this splits at section boundaries


class _LinkTokens:
    """The render-time EPUB link policy: emit tokens, never real chunk hrefs."""

    def url_for(self, stem, section_slug=None):
        return f"epublink:{stem}" + (f"#section-{section_slug}" if section_slug else "")

    def contrib_url(self, slug):
        return f"epubcontrib:{slug}"


LINK_TOKENS = _LinkTokens()


def article_anchor(stem):
    """The article's opening anchor id.  Prefixed (`a`) because stems start with a
    digit and the OPF side of the container already treats ids as NCNames."""
    return "a" + stem


# `(?<![-\w])` so `data-popup-id="…"` (or any *-id attr) is not an `id` match.
_ID_RE = re.compile(r'(?<![-\w])id="([^"]+)"')
_FRAG_HREF_RE = re.compile(r'href="#([^"]+)"')


def protect_svgs(html_str):
    """Lift each top-level ``<svg>…</svg>`` out with a BALANCED scan (MathJax nests
    ``<svg>`` for some structures, so a non-greedy regex splits the outer element).
    Returns (html_with_slots, svgs)."""
    out, svgs, i, n = [], [], 0, len(html_str)
    while i < n:
        j = html_str.find("<svg", i)
        if j < 0:
            out.append(html_str[i:])
            break
        out.append(html_str[i:j])
        depth, k = 1, j + 4
        while k < n and depth > 0:
            nxt, close = html_str.find("<svg", k), html_str.find("</svg", k)
            if close < 0:
                k = n
                break
            if nxt != -1 and nxt < close:
                depth, k = depth + 1, nxt + 4
            else:
                depth, k = depth - 1, close + 5
        end = html_str.find(">", k)
        k = end + 1 if end != -1 else n
        svgs.append(html_str[j:k])
        out.append(f"MJSVGSLOT{len(svgs) - 1}ENDSLOT")
        i = k
    return "".join(out), svgs


def _splice_svgs(text, svgs):
    for i, svg in enumerate(svgs):
        slot = f"MJSVGSLOT{i}ENDSLOT"
        if slot in text:
            text = text.replace(slot, svg)
    return text


def namespace_ids(xhtml, stem):
    """Prefix every id and fragment href with the article's anchor, and stamp the
    anchor itself on the article card.  SVG internals are shielded (MathJax ids and
    ``url(#…)`` refs are not fragment hrefs and must not shift)."""
    protected, svgs = protect_svgs(xhtml)
    p = article_anchor(stem) + "-"
    protected = _ID_RE.sub(lambda m: f'id="{p}{m.group(1)}"', protected)
    protected = _FRAG_HREF_RE.sub(lambda m: f'href="#{p}{m.group(1)}"', protected)
    protected = protected.replace(
        '<div class="card"', f'<div class="card" id="{article_anchor(stem)}"', 1)
    return _splice_svgs(protected, svgs)


# ── splitting an oversized article at section boundaries ──────────────────

def _open_tag(el):
    parts = [el.tag]
    for k, v in el.items():
        if k.startswith(_EPUB_NS):
            k = "epub:" + k[len(_EPUB_NS):]
        parts.append(f'{k}="{_html.escape(v)}"')
    return "<" + " ".join(parts) + ">"


def _ser(els):
    return "".join(ET.tostring(e, encoding="unicode", method="xml") for e in els)


def _is_sec_head(el):
    return el.tag == "h3" and "section-head" in (el.get("class") or "")


def split_article(xhtml, target=TARGET_CHUNK, hard=HARD_SPLIT):
    """Split a staged (namespaced, XML-well-formed) article into pieces at section
    boundaries.  A pure function of the input bytes — the plan and emit passes call it
    on the same staged file and get identical pieces.  Piece 1 keeps the head matter
    and the article anchor; each footnote aside travels with the piece holding its
    noteref; the xref card rides last.  Returns [xhtml] unchanged when small enough
    or structurally unexpected (the size gate reports oversized chunks)."""
    if len(xhtml) <= hard:
        return [xhtml]
    protected, svgs = protect_svgs(xhtml)
    try:
        root = ET.fromstring(
            '<r xmlns:epub="http://www.idpf.org/2007/ops">' + protected + "</r>")
    except ET.ParseError:
        return [xhtml]
    kids = list(root)
    if not kids or kids[0].get("class") != "card":
        return [xhtml]
    card = kids[0]
    card_kids = list(card)
    body = next((c for c in card_kids
                 if c.tag == "div" and c.get("class") == "body-text"), None)
    if body is None:
        return [xhtml]
    bi = card_kids.index(body)
    head_els, after_els = card_kids[:bi], card_kids[bi + 1:]
    asides = [e for e in after_els if e.tag == "aside"]
    other_after = [e for e in after_els if e.tag != "aside"]

    children = list(body)
    if len(children) < 2:
        return [xhtml]
    sizes = [len(ET.tostring(c, encoding="unicode", method="xml")) for c in children]

    groups, cur, acc = [], [], 0
    for el, sz in zip(children, sizes):
        if cur and ((acc >= target * 0.6 and _is_sec_head(el)) or acc + sz > hard):
            groups.append(cur)
            cur, acc = [], 0
        cur.append(el)
        acc += sz
    if cur:
        groups.append(cur)
    if len(groups) == 1:
        return [xhtml]

    group_html = [_ser(g) for g in groups]
    # Each footnote aside travels with the piece that holds its noteref, so the
    # reader's popup stays same-document.
    aside_for_group = [[] for _ in groups]
    for a in asides:
        aid = a.get("id") or ""
        rid = "-fnref-".join(aid.rsplit("-fn-", 1)) if "-fn-" in aid else ""
        tgt = len(groups) - 1
        if rid:
            for gi, gh in enumerate(group_html):
                if f'id="{rid}"' in gh:
                    tgt = gi
                    break
        aside_for_group[tgt].append(a)

    card_open = _open_tag(card)
    # Continuation pieces drop the article-anchor id (one anchor per article).
    cont_attrs = {k: v for k, v in card.items() if k != "id"}
    cont_el = ET.Element(card.tag, cont_attrs)
    card_open_cont = _open_tag(cont_el)
    body_open = _open_tag(body)

    pieces = []
    for gi, gh in enumerate(group_html):
        first, last = gi == 0, gi == len(group_html) - 1
        parts = [root.text or "" if first else "",
                 card_open if first else card_open_cont,
                 (card.text or "") + _ser(head_els) if first else "",
                 body_open, (body.text or "") if first else "", gh, "</div>",
                 _ser(aside_for_group[gi]),
                 _ser(other_after) if last else "",
                 "</div>",
                 (card.tail or "") + _ser(kids[1:]) if last else ""]
        pieces.append(_splice_svgs("".join(parts), svgs))
    return pieces


# ── XHTML5 conformance for already-baked bodies ───────────────────────────
# The corpus carries source presentation faithfully; two carried forms are valid
# HTML-in-browsers but invalid XHTML5: legacy table attributes and empty CSS
# declarations (`width:;`, from an empty source `width=` — producer now guards,
# baked bodies keep it until the next rebuild).  Both convert mechanically:
# attrs → data-* (carry preserved, validity restored), empty decls → dropped
# (they declare nothing; browsers already ignored them).

_LEGACY_TABLE_ATTRS = r"cellpadding|cellspacing|rules|summary|frame|hspace|vspace|bordercolor"
_TABLE_TAG_RE = re.compile(r"<(table|tr|td|th|caption|colgroup|col)\b[^>]*>")
_LEGACY_ATTR_RE = re.compile(rf'\b({_LEGACY_TABLE_ATTRS})=')
_SCOPE_ATTR_RE = re.compile(r"\b(scope)=")          # valid on th only
_SPAN_ATTRS_RE = re.compile(r"\b(colspan|rowspan)=")  # valid on td/th only
_BAD_BORDER_RE = re.compile(r'\b(border)="(?!1?")')   # XHTML5 allows only "" or "1"
_STYLE_ATTR_RE = re.compile(r'style="([^"]*)"')
# direction/unicode-bidi are FORBIDDEN in EPUB CSS (CSS-001); the HTML dir attribute
# carries the same semantics and is valid — convert before the style filter strips them.
_RTL_TAG_RE = re.compile(r'<([a-z]+)\b((?:(?!\bdir=)[^<>])*style="[^"]*direction:\s*rtl[^"]*"[^<>]*)>')
_EPUB_CSS_BANNED = ("direction", "unicode-bidi")
# `style="font-size:92%&quot;"` — a stray source quote rides into the value and the
# CSS parser dies at EOF; an ODD &quot; count is junk (an even count is a real quoted
# string, e.g. font-family:&quot;Times&quot; — untouched).
_QUOT = "&quot;"
# Split style declarations on `;` — but the escaped-attr entities (&quot; &amp; &lt;
# &gt;) END with a semicolon; a naive split cuts inside them and emits an unterminated
# entity (the RSC-016 fatal class).
_DECL_SPLIT_RE = re.compile(r"(?<!&quot)(?<!&amp)(?<!&lt)(?<!&gt);")
_DECL_OK_RE = re.compile(r"^\s*-?[A-Za-z][A-Za-z-]*\s*:\s*\S")


def xhtml5_sanitize(xhtml):
    def _table_tag(m):
        tag = m.group(0)
        out = _LEGACY_ATTR_RE.sub(r"data-\1=", tag)
        out = _BAD_BORDER_RE.sub(r'data-\1="', out)
        if m.group(1) == "td":
            out = _SCOPE_ATTR_RE.sub(r"data-\1=", out)
        elif m.group(1) not in ("td", "th"):
            out = _SPAN_ATTRS_RE.sub(r"data-\1=", out)
        return out

    xhtml = _TABLE_TAG_RE.sub(_table_tag, xhtml)
    xhtml = xhtml.replace(' max-width="', ' data-max-width="')  # never a valid attribute
    xhtml = _RTL_TAG_RE.sub(r'<\1 dir="rtl"\2>', xhtml)

    def _style(m):
        # Keep only well-formed declarations (`prop: value`); carried junk (`width=5%`,
        # bare Ts codes, empty values) declares nothing — browsers already drop it.
        kept = []
        for d in _DECL_SPLIT_RE.split(m.group(1)):
            if not _DECL_OK_RE.match(d) or "{" in d or "}" in d:
                continue                          # leaked template braces: invalid, browsers drop it
            d = d.strip()
            if d.split(":", 1)[0].strip() in _EPUB_CSS_BANNED:
                continue
            if d.count(_QUOT) % 2:
                d = d.replace(_QUOT, "")
                if not _DECL_OK_RE.match(d):
                    continue
            kept.append(d)
        return f'style="{";".join(kept)}"'

    return _STYLE_ATTR_RE.sub(_style, xhtml)


# A page boundary baked INSIDE a table tag's attr slot decodes its marker span into
# the middle of the open tag (`<tr style="" <span class="page-marker"…></span> …>`),
# which html5lib mangles into junk attributes (`page-marker"=""` — a fatal XML entity
# downstream).  Lift the span out to just after the open tag BEFORE the HTML5 parse.
# (Pipeline-side: an attr-slot page marker is misplaced content — queued; the site
# carries the same junk attrs silently.)
_MARKER_IN_TAG_RE = re.compile(
    r'(<(?:table|tbody|tr|td|th)\b[^<>]*?)\s*(<span class="page-marker"[^<>]*></span>)\s*([^<>]*>)')


def lift_markers_out_of_tags(html):
    return _MARKER_IN_TAG_RE.sub(r"\1\3 \2", html)


# ── chunk-close resolution: tokens + cross-file fragments → real hrefs ────

_EPUBLINK_RE = re.compile(r'href="epublink:([^"#]+)(?:#([^"]*))?"')
_EPUBCONTRIB_RE = re.compile(r'href="epubcontrib:([^"]+)"')
_ROOT_HREF_RE = re.compile(r'href="(/[^"]*)"')
_HTTP_HREF_RE = re.compile(r'href="(https?://[^"]*&quot;[^"]*)"')


def chunk_ids(xhtml):
    return set(_ID_RE.findall(xhtml))


def resolve_chunk(xhtml, own_ids, anchor_map, contrib_map):
    """Materialize every link in a finished chunk: ``epublink:`` tokens against the
    anchor→chunk map (absent → live site), ``epubcontrib:`` against the appendix map,
    fragment hrefs whose target lives in another chunk get file-qualified, and any
    root-relative site href goes absolute.  Returns (xhtml, dangling) — dangling
    fragments feed the integrity gate."""
    dangling = []

    def link(m):
        stem, suffix = m.group(1), m.group(2)
        art = article_anchor(stem)
        if suffix:
            anch = f"{art}-{suffix}"
            if anch in own_ids:
                return f'href="#{anch}"'
            if anch in anchor_map:
                return f'href="{anchor_map[anch]}#{anch}"'
        if art in own_ids:
            return f'href="#{art}"'
        if art in anchor_map:
            return f'href="{anchor_map[art]}#{art}"'
        tail = f"#{suffix}" if suffix else ""
        return f'href="{SITE_BASE}/article/{stem}{tail}"'

    xhtml = _EPUBLINK_RE.sub(link, xhtml)

    def contrib(m):
        slug = m.group(1)
        f = contrib_map.get(slug)
        if f is None:
            dangling.append("contrib:" + slug)
            return f'href="{SITE_BASE}/contributors.html"'
        return f'href="{f}#contrib-{slug}"'

    xhtml = _EPUBCONTRIB_RE.sub(contrib, xhtml)

    def frag(m):
        t = m.group(1)
        if t in own_ids:
            return m.group(0)
        if t in anchor_map:
            return f'href="{anchor_map[t]}#{t}"'
        dangling.append("#" + t)
        return m.group(0)

    xhtml = _FRAG_HREF_RE.sub(frag, xhtml)
    xhtml = _ROOT_HREF_RE.sub(lambda m: f'href="{SITE_BASE}{m.group(1)}"', xhtml)
    # A source URL carrying literal quotes (WS: The Nigger of the "Narcissus") is an
    # invalid URL character — percent-encode it in place.
    xhtml = _HTTP_HREF_RE.sub(lambda m: 'href="' + m.group(1).replace("&quot;", "%22") + '"', xhtml)
    return xhtml, dangling


# ── the text-preservation strip (the bank gate) ───────────────────────────

_TAG_RE = re.compile(r"<[^>]*>")


def text_of(xhtml):
    """Tag-stripped, whitespace-collapsed text — the per-chunk preservation gate
    compares this across the pack (resolution touches only attrs, so equality holds
    exactly when no content moved or vanished)."""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", xhtml))).strip()


_ASIDE_RE = re.compile(r"<aside\b.*?</aside>", re.S)


def split_invariant(xhtml):
    """The split gate: (ordered non-aside text, sorted aside texts).  A split
    intentionally MOVES each footnote aside to its noteref's piece, so ordered
    whole-text equality is the wrong invariant — main content must keep its order,
    asides must survive as a multiset."""
    return (text_of(_ASIDE_RE.sub(" ", xhtml)),
            sorted(text_of(a) for a in _ASIDE_RE.findall(xhtml)))
