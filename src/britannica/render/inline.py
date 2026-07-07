"""Inline marker → HTML: the Python port of the viewer's ``decodeInlineMarkers``.

THE one inline decoder (project_render_to_python).  Reproduces the viewer verbatim —
same order, same strings, same INDEPENDENT open→tag / close→tag substitution for the
styler/wrapper markers (the «P»/«CTR» rule: a marker that nests or spans render chunks
pairs in the browser, so there is no `«X»(.*?)«/X»` span-match to mis-pair).  The
parametrized markers (SPAN/DIV/LN/XL/BAR/BRACE2/…) key their close on the `»`-anchored
opener.  Verified against tools/render/inline_ref.json (the real viewer output).

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


def _default_article_url(filename):
    # Matches the jsdom reference stub (BritannicaUrls.filenameToUrl); production
    # emitters inject their own target-specific resolver.
    return "/article/" + filename


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
        s += "float:right;margin:.3em 0 .4em 1.2em;"
    elif meta.get("align") == "left":
        s += "float:left;margin:.3em 1.2em .4em 0;"
    elif meta.get("align") == "center":
        s += "display:block;margin:0 auto;"
    return f'<img src="{url}" alt="{escape_html(alt)}" loading="lazy" style="{s}" />'


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


def decode_inline(h, *, escape=False, dhr_inline=False, skip_math=False, article_url=None, is_local=True):
    """Decode an inline marker string to HTML, reproducing ``decodeInlineMarkers``."""
    article_url = article_url or _default_article_url

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

    h = _VERSE_RE.sub(_verse, h)

    # Un-escape the fixed safe set of carried presentational HTML (CHEM/MATH signals).
    h = _SAFE_HTML_RE.sub(r"<\1>", h)

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

    # hieroglyph — deferred (Gardiner tables).

    h = _LN_RE.sub(_ln_factory(article_url), h)
    h = _XL_RE.sub(_xl, h)
    h = _SEC_RE.sub(r'<span id="section-\1" class="section-anchor"></span>', h)

    # Restore the shielded inline images (last, like the viewer).
    if img_html:
        h = re.sub(r"\x00IMG(\d+)\x00", lambda m: img_html[int(m.group(1))], h)

    return h
