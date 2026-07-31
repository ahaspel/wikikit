"""Build the single-book EPUB from corpus articles — chunk-packed at any scale.

    python -m britannica.epub.build --volume 1          # the vol-1 control artifact
    python -m britannica.epub.build --all               # the whole edition
    python -m britannica.epub.build --all --target kindle

Passes (each streams; nothing holds the rendered corpus in memory):
  0. metadata + math-collect — spine order, volume/letter/contributor tables, and the
     exact (latex, display) set the render will request; then generate the math assets.
  1. stage — render each article once (link TOKENS, see epub.pack), namespace its ids,
     make it XML-well-formed, bundle its images, write it to the stage.
  2. plan — split oversized staged articles at section boundaries, greedy-pack pieces
     into ~300KB chunks, build the anchor→chunk map; pack the contributors appendix;
     generate nav/title/index pages from the map.
  3. emit — re-split (same bytes → same pieces), assemble each chunk, resolve tokens +
     cross-chunk fragments at chunk close, write.
Gates (before zipping, all hard): every article anchored exactly once · no duplicate
ids per file · every internal href resolves to a real file#id · per-chunk text
preservation against the staged articles.
"""
import argparse
import hashlib
import html as _html
import json
import os
import re
import shutil
from urllib.parse import unquote_plus
import struct
import unicodedata
import zipfile
import zlib
from collections import Counter
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import html5lib

from britannica.epub import front_matter as FM
from britannica.epub import fts as FTS
from britannica.epub import images as IMG
from britannica.epub import pack
from britannica.epub import readers_guide as RG
from britannica.epub import math_assets as MA
from britannica.markers import markers_to_text
from britannica.render.article import render_article, _section_slug

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ARTICLES_DIR = os.path.join(ROOT, "data", "derived", "articles")
IMAGES_SRC = os.path.join(ROOT, "data", "images")
MATH_PNG_SRC = os.path.join(ROOT, "data", "derived", "math_png")
# dcterms:modified is the BUILD time, not a fixed constant: readers key library
# identity on (dc:identifier, dcterms:modified) — a fixed value made every revision
# look like the same publication, so re-imports silently kept showing an OLD copy
# (the user's missing-topics/search report; the missing-glyph report earlier).
# Byte-reproducibility of the container yields to revision identity.
import datetime as _dt
_MODIFIED = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_IMG_SRC_RE = re.compile(r'src="/data/images/([^"]+)"')
_REMOTE_IMG_RE = re.compile(r'src="(https?://[^"]+)"')
_MATH_SRC_RE = re.compile(r'src="math/([0-9a-f]+\.png)"')
_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
          "gif": "image/gif", "svg": "image/svg+xml"}


def _media_type(name):
    """Media type from the LAST extension token in the name — a few disk names carry
    trailing junk after the ext (`….jpg_‎`), which defeats a bare splitext."""
    hits = re.findall(r"\.(png|jpe?g|gif|svg)", name.lower())
    return _MEDIA.get(hits[-1] if hits else "png", "image/png")


class GateFailure(RuntimeError):
    """An integrity gate failed — the container is wrong; never ship it."""


def _log(*args):
    print(*args, flush=True)      # a redirected long build must stay tailable


def epub_css(target="epub"):
    """The EPUB stylesheet — content typography only, single-column, reader-controlled width.
    Kindle drops the ONE stylesheet transform (.mirror-h scaleX(-1), ALPHABET's mirrored
    letterforms): Amazon's ET pipeline rasterizes every transformed node and its
    rasterizer dies on bare mirrored text (E00192 → the whole book "Not Supported");
    the letterforms render unmirrored there — ET cannot draw them any other way."""
    base = open(os.path.join(os.path.dirname(__file__), "epub.css"), encoding="utf-8").read()
    if target == "kindle":
        base = base.replace(".mirror-h { display: inline-block; transform: scaleX(-1); }",
                            ".mirror-h { display: inline-block; }")
        assert "transform" not in base, "kindle css must carry no transforms"
    return base + ("\n/* math */\n"
                   "svg.math-display, img.math-display { display:block; margin:1.1em auto;"
                   " max-width:100%; height:auto; }\n"
                   "svg.math-inline, img.math-inline { max-width:100%; }\n"
                   "/* A–Z index */\n"
                   ".index-ranges { line-height: 2; }\n"
                   ".index-ranges a { margin-right: 0.7em; white-space: nowrap; }\n"
                   ".index-range { font-weight: normal; font-size: 0.7em; color: inherit; }\n"
                   "/* topics */\n"
                   ".topic-crumb { font-size: 0.85em; opacity: 0.7; margin-bottom: 0.2em; }\n"
                   ".topic-children { margin-bottom: 1em; }\n"
                   "/* search key row */\n"
                   ".keyrow { line-height: 2.4; }\n"
                   ".key { display: inline-block; min-width: 1.6em; text-align: center;"
                   " border: 1px solid currentColor; border-radius: 4px; margin: 0 0.15em;"
                   " padding: 0.15em 0.25em; text-decoration: none; cursor: pointer; }\n"
                   ".key-ctl { min-width: 2.2em; }\n")


_XML_ATTR_NAME_RE = re.compile(r"[A-Za-z_][-.\w]*$")


def _drop_invalid_attrs(root):
    """html5lib tolerates attribute names XML forbids (junk in a mangled tag parses as
    an attr named `-`; ET serializes it uncritically and the container dies at read).
    Namespaced keys (`{uri}local`, e.g. epub:type) pass through."""
    for el in root.iter():
        for k in list(el.attrib):
            if not (k.startswith("{") or _XML_ATTR_NAME_RE.fullmatch(k)):
                del el.attrib[k]


def _fix_nested_lists(root):
    """A `<ul>` directly under a `<ul>` (the outline renderer's nesting form — valid in
    every browser DOM, invalid XHTML5) moves into the preceding `<li>`; renders
    identically (nested-list indent), validates.  A list that STARTS with a nested list
    gets a bullet-less carrier `<li>`."""
    for parent in [e for e in root.iter() if e.tag in ("ul", "ol")]:
        prev_li = None
        for c in list(parent):
            if c.tag in ("ul", "ol"):
                if prev_li is None:
                    prev_li = ET.Element("li", {"style": "list-style:none"})
                    parent.insert(list(parent).index(c), prev_li)
                parent.remove(c)
                prev_li.append(c)
            elif c.tag == "li":
                prev_li = c


_PHRASING_PARENTS = frozenset(
    "span sup sub a i b em strong small u s code cite q dfn abbr var samp kbd mark bdi bdo "
    "h1 h2 h3 h4 h5 h6".split())
_STRUCT_BLOCKS = frozenset("table ul ol figure blockquote dl pre h1 h2 h3 h4 h5 h6".split())


def _fix_phrasing_blocks(root):
    """Blocks inside phrasing-only content are real browser DOMs the XHTML5 schema
    rejects.  Two mechanical cures, both rendering-preserving: a `<p>`/`<div>` child
    (a cell-verse `<span>` carrying `«P»` lines) renames to a `display:block` span; a
    STRUCTURAL child (a table/list/heading inside a styled `<span>`/`<a>` wrapper)
    retags the WRAPPER to `display:inline` div — the structure must stay itself.
    Iterates to a fixpoint (a rename can expose deeper nesting)."""
    changed = True
    while changed:
        changed = False
        for parent in list(root.iter()):
            if parent.tag not in _PHRASING_PARENTS:
                continue
            for c in parent:
                if c.tag in ("p", "div"):
                    c.tag = "span"
                    c.set("style", ("display:block;" + c.get("style", "")).rstrip(";"))
                    changed = True
                elif c.tag in _STRUCT_BLOCKS and parent.tag in ("span", "a"):
                    if parent.tag == "a" and parent.get("href") is not None:
                        parent.set("data-href", parent.get("href"))   # href invalid on div
                        del parent.attrib["href"]
                    parent.tag = "div"
                    parent.set("style", ("display:inline;" + parent.get("style", "")).rstrip(";"))
                    changed = True


def _fix_overlapping_cells(root):
    """Clamp a cell whose colspan extends into a slot an earlier rowspan still owns —
    Enhanced Typesetting rejects the whole book over one overlap ("Table with
    overlapping cells… not supported"); browsers effectively render the clamped
    layout anyway.  Ten articles corpus-wide (source rowspan/colspan collisions)."""
    for table in root.iter("table"):
        pending = {}
        for tr in table.iter("tr"):
            pending = {k: v - 1 for k, v in pending.items() if v > 1}
            col = 0
            for cell in tr:
                if cell.tag not in ("td", "th"):
                    continue
                while col in pending:
                    col += 1
                try:
                    rs = max(1, int(cell.get("rowspan", "1")))
                    cs = max(1, int(cell.get("colspan", "1")))
                except ValueError:
                    rs, cs = 1, 1
                free = 1
                while free < cs and (col + free) not in pending:
                    free += 1
                if free < cs:
                    cell.set("colspan", str(free))
                    cs = free
                if rs > 1:
                    for cc in range(col, col + cs):
                        pending[cc] = rs
                col += cs


def _split_giant_tables(root, limit=18000):
    """Split a table whose serialized form exceeds ET's ~20k-character ceiling into
    consecutive sibling tables at ROW boundaries ("Tables with more than 20,000
    characters are not currently supported") — same rows, same order, text stays
    text (the faithful alternative to Amazon rasterizing it).  32 tables corpus-wide."""
    parents = {c: p for p in root.iter() for c in p}
    for table in list(root.iter("table")):
        raw = ET.tostring(table, encoding="unicode", method="xml")
        if len(raw) <= limit:
            continue
        body = next((c for c in table if c.tag == "tbody"), None)
        rows = list(body) if body is not None else [c for c in table if c.tag == "tr"]
        if len(rows) < 2:
            continue
        # cut ONLY where no rowspan is in flight — a span crossing the cut leaves a
        # dangling rowspan pointing past its table's end (a malformed table ET
        # silently rejects; three of them cost a probe round)
        groups, cur, acc, pending = [], [], 0, {}
        for r in rows:
            sz = len(ET.tostring(r, encoding="unicode", method="xml"))
            if cur and acc + sz > limit * 0.8 and not pending:
                groups.append(cur)
                cur, acc = [], 0
            pending = {k: v - 1 for k, v in pending.items() if v > 1}
            col = 0
            for cell in r:
                if cell.tag not in ("td", "th"):
                    continue
                while col in pending:
                    col += 1
                try:
                    rs = max(1, int(cell.get("rowspan", "1")))
                    cs = max(1, int(cell.get("colspan", "1")))
                except ValueError:
                    rs, cs = 1, 1
                if rs > 1:
                    for cc in range(col, col + cs):
                        pending[cc] = rs
                col += cs
            cur.append(r)
            acc += sz
        if cur:
            groups.append(cur)
        # belt: clamp any rowspan still exceeding its part's remaining rows
        for g in groups:
            for ri, r in enumerate(g):
                for cell in r:
                    if cell.tag in ("td", "th"):
                        try:
                            rs = int(cell.get("rowspan", "1") or "1")
                        except ValueError:
                            continue
                        if ri + rs > len(g):
                            cell.set("rowspan", str(len(g) - ri))
        if len(groups) < 2:
            continue
        parent = parents.get(table)
        if parent is None:
            continue
        idx = list(parent).index(table)
        tail = table.tail
        parent.remove(table)
        for gi, g in enumerate(groups):
            t = ET.Element("table", dict(table.attrib))
            holder = t
            if body is not None:
                holder = ET.SubElement(t, "tbody", dict(body.attrib))
            for r in g:
                holder.append(r)
            t.tail = "\n" if gi < len(groups) - 1 else tail
            parent.insert(idx + gi, t)


def to_xhtml_body(html_str, target="epub"):
    """HTML5 render → XML-well-formed XHTML fragment (void elements self-closed, entities
    as chars).  Inline math SVG is ALREADY valid XML (MathJax, with its own xmlns), but
    ElementTree mangles foreign-content namespaces on round-trip, so each ``<svg>`` is
    lifted out before the html5lib+ET pass and spliced back verbatim after.  The kindle
    target additionally repairs the two table classes Enhanced Typesetting hard-rejects."""
    protected, svgs = pack.protect_svgs(html_str)
    frag = html5lib.parseFragment(protected, treebuilder="etree", namespaceHTMLElements=False)
    out = [_html.escape(frag.text, quote=False)] if frag.text else []
    for child in frag:
        # a wrapper root gives every element a parent (a top-level table can split)
        w = ET.Element("w")
        w.append(child)
        _drop_invalid_attrs(w)
        _fix_nested_lists(w)
        _fix_phrasing_blocks(w)
        if target == "kindle":
            _fix_overlapping_cells(w)
            _split_giant_tables(w)
        for piece in w:
            out.append(ET.tostring(piece, encoding="unicode", method="xml"))
    result = "".join(out)
    for i, svg in enumerate(svgs):
        result = result.replace(f"MJSVGSLOT{i}ENDSLOT", svg)
    return result


# ── cover (the site mark's language: cream field, dark double-rule frame, Georgia) ──
_COVER_BG = (245, 241, 235)      # sampled from britannica11-logo.png
_COVER_INK = (44, 36, 22)
_COVER_W, _COVER_H = 1600, 2560


def _cover_font(size, bold=False, italic=False):
    from PIL import ImageFont
    name = "georgia" + ("z" if bold and italic else "b" if bold else "i" if italic else "")
    try:
        return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                               "Fonts", name + ".ttf"), size)
    except Exception:
        return ImageFont.load_default()


def _draw_tracked(draw, cx, y, text, font, tracking=0, fill=_COVER_INK):
    """Centered text with letter-tracking (PIL has none built in)."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


_TITLE_PAGE_SCAN = os.path.join(ROOT, "tools", "viewer", "title_page.jpg")
# The flat printed page inside the site's title-page photograph (crop excludes
# the book's leaf edges and the grey backdrop; fractions of the source frame).
_TITLE_PAGE_CROP = (0.106, 0.051, 0.900, 0.957)


def make_cover(path, subtitle=None):
    """The cover.  Complete edition: the site's Volume I title-page photograph,
    cropped to the flat printed page (user spec: the page, minus the book edges),
    scaled to the 1600×2560 language.  Volume builds (and a missing scan): the
    drawn site mark.  Deterministic (no timestamps), regenerated per build."""
    from PIL import Image, ImageDraw
    if subtitle is None and os.path.exists(_TITLE_PAGE_SCAN):
        im = Image.open(_TITLE_PAGE_SCAN)
        w, h = im.size
        l, t, r, b = _TITLE_PAGE_CROP
        page = im.crop((int(w * l), int(h * t), int(w * r), int(h * b)))
        cover = page.resize((int(page.width * _COVER_H / page.height), _COVER_H),
                            Image.LANCZOS)
        cover.save(path, quality=88)
        return
    im = Image.new("RGB", (_COVER_W, _COVER_H), _COVER_BG)
    d = ImageDraw.Draw(im)
    # double-rule frame, as the logo draws it
    d.rectangle([56, 56, _COVER_W - 56, _COVER_H - 56], outline=_COVER_INK, width=10)
    d.rectangle([92, 92, _COVER_W - 92, _COVER_H - 92], outline=_COVER_INK, width=4)
    # EB medallion
    cx = _COVER_W // 2
    ms = 430
    top = 330
    d.rectangle([cx - ms // 2, top, cx + ms // 2, top + ms], outline=_COVER_INK, width=8)
    d.rectangle([cx - ms // 2 + 26, top + 26, cx + ms // 2 - 26, top + ms - 26],
                outline=_COVER_INK, width=3)
    f_eb = _cover_font(240, bold=False)
    bb = d.textbbox((0, 0), "EB", font=f_eb)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], top + ms / 2 - (bb[3] - bb[1]) / 2 - bb[1]),
           "EB", font=f_eb, fill=_COVER_INK)
    # title block — the big lines auto-fit inside the frame
    def _fit(text, size, tracking, max_w=1240):
        while size > 40:
            f = _cover_font(size, bold=True)
            w = sum(d.textlength(ch, font=f) for ch in text) + tracking * (len(text) - 1)
            if w <= max_w:
                return f
            size -= 4
        return _cover_font(size, bold=True)

    _draw_tracked(d, cx, 1120, "THE", _cover_font(60), tracking=26)
    _draw_tracked(d, cx, 1235, "ENCYCLOPÆDIA", _fit("ENCYCLOPÆDIA", 150, 10), tracking=10)
    _draw_tracked(d, cx, 1425, "BRITANNICA", _fit("BRITANNICA", 150, 26), tracking=26)
    d.line([cx - 260, 1670, cx - 40, 1670], fill=_COVER_INK, width=3)
    d.line([cx + 40, 1670, cx + 260, 1670], fill=_COVER_INK, width=3)
    d.polygon([(cx, 1660), (cx + 12, 1670), (cx, 1680), (cx - 12, 1670)], fill=_COVER_INK)
    _draw_tracked(d, cx, 1730, "ELEVENTH EDITION", _cover_font(72), tracking=22)
    f_dict = _cover_font(44, italic=True)
    _draw_tracked(d, cx, 1890, "A Dictionary of Arts, Sciences, Literature", f_dict, tracking=1)
    _draw_tracked(d, cx, 1955, "and General Information", f_dict, tracking=1)
    _draw_tracked(d, cx, 2090, "1910–1911", _cover_font(56), tracking=8)
    if subtitle:
        _draw_tracked(d, cx, 2210, subtitle.upper(), _cover_font(58, bold=True), tracking=14)
    _draw_tracked(d, cx, 2380, "BRITANNICA11.ORG", _cover_font(38), tracking=16)
    im.save(path, "JPEG", quality=90, optimize=True)


_IMG_TAG_RE = re.compile(r'<img\b[^>]*>')
_IMG_LOCAL_SRC_RE = re.compile(r'src="((?:images|math)/[^"]+)"')


def stamp_img_dims(xhtml, oebps, dim_cache):
    """Inject explicit width/height attributes on every bundled <img> (real pixel
    dims, read once per file).  Kindle's Enhanced Typesetting rasterizes complex
    content, and a to-raster node whose image carries no computed dimensions kills
    the whole conversion (E00192 → "Not Supported"/"internal error"); explicit
    attrs also spare every reader a reflow when images load."""
    def tag(m):
        t = m.group(0)
        if " width=" in t or " height=" in t:
            return t
        sm = _IMG_LOCAL_SRC_RE.search(t)
        if not sm:
            return t
        rel = unquote(sm.group(1))
        if rel not in dim_cache:
            try:
                from PIL import Image
                with Image.open(os.path.join(oebps, rel)) as im:
                    dim_cache[rel] = im.size
            except Exception:
                dim_cache[rel] = None
        dims = dim_cache[rel]
        if not dims:
            return t
        return t[:-2] + f' width="{dims[0]}" height="{dims[1]}"/>' if t.endswith("/>") \
            else t[:-1] + f' width="{dims[0]}" height="{dims[1]}">'
    return _IMG_TAG_RE.sub(tag, xhtml)


def _placeholder_png():
    """A 1×1 transparent PNG, generated deterministically — the in-book stand-in for an
    image the corpus references but the image store lacks (the site shows the same
    broken figure; the container must still be valid and self-contained)."""
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")      # filter 0 + RGBA(0,0,0,0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


MISSING_IMG = "_missing.png"


_EXT_JUNK_RE = re.compile(r"(\.(?:png|jpe?g|gif|svg))[^.]*$", re.I)


def bundle_images(body, dst_dir, seen, missing, diet=True):
    """Copy every referenced image into the EPUB and rewrite its src to a relative path.
    ``seen`` maps disk name → the percent-encoded href of its BUNDLED name — the
    manifest MUST reference images in that same form (a raw ``&`` in a disk name is a
    fatal XML error in the OPF).  The bundled name drops trailing junk after the real
    extension (`….jpg_‎`, a U+200E-suffixed disk name, defeats ext-keyed reader
    checks); with ``diet`` (the default) the bundled bytes are the display-resolution
    re-encode (epub.images), whose extension may differ from the source's.  A file
    absent from the image store points at the shared placeholder and is RECORDED —
    the build log surfaces every one."""
    def repl(m):
        enc = m.group(1)
        name = unquote(enc)                      # commons_url percent-encoded the disk name
        src = os.path.join(IMAGES_SRC, name)
        if os.path.exists(src):
            if name not in seen:
                # Bundled names are strict ASCII [A-Za-z0-9._-]: an escapable char
                # anywhere (apostrophe, &, unicode dash/LRM) means the manifest href
                # must XML-escape it — legal, but Amazon's converter resolves the
                # href WITHOUT decoding the entity (looks for `…&#x27;s…` literally)
                # and its KFX stage dies on the "missing" media.  One byte-identical
                # string in body, manifest, and zip beats three encodings agreeing.
                clean = re.sub(r"[^A-Za-z0-9._-]", "_", _EXT_JUNK_RE.sub(r"\1", name))
                if diet:
                    data, ext = IMG.diet_image(src)
                    dest = os.path.splitext(clean)[0] + ext
                else:
                    data, dest = open(src, "rb").read(), clean
                if dest != name and dest in seen.values():
                    stem_, ext_ = os.path.splitext(dest)
                    dest = f"{stem_}-{hashlib.sha1(name.encode()).hexdigest()[:6]}{ext_}"
                open(os.path.join(dst_dir, dest), "wb").write(data)
                seen[name] = dest                        # ASCII-safe: needs no encoding
            return f'src="images/{seen[name]}"'
        missing.append(name)
        return f'src="images/{MISSING_IMG}"'
    body = _IMG_SRC_RE.sub(repl, body)
    # Remote image srcs (Wikimedia score renders the corpus hotlinks) violate the
    # EPUB no-remote-resources rule — placeholder + record until they're mirrored
    # into the image store.
    def remote(m):
        missing.append(m.group(1))
        return f'src="images/{MISSING_IMG}"'
    return _REMOTE_IMG_RE.sub(remote, body)


def bundle_math_png(body, dst_dir, seen):
    """Copy every referenced math PNG (Kindle target) into the EPUB."""
    for name in _MATH_SRC_RE.findall(body):
        src = os.path.join(MATH_PNG_SRC, name)
        if os.path.exists(src) and name not in seen:
            shutil.copyfile(src, os.path.join(dst_dir, name))
            seen.add(name)
    return body


_NUDGE = 0     # set per build via build_epub(nudge=); shifts every chunk's byte offsets


def xhtml_doc(title, body):
    # The nudge comment exists to dislodge a POSITIONAL Amazon-converter bug: a
    # byte-offset-dependent silent ET rejection (CASTANETS — the identical span
    # passed when shifted).  Undetectable locally; any global offset shift clears
    # a strike, so silent-fail → rebuild with nudge+1 → reconvert.
    pad = f"<!-- {'n' * _NUDGE} -->\n" if _NUDGE else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{_html.escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body>\n{pad}{body}\n</body>\n</html>\n"
    )


def _title_text(title):
    """A title as plain nav/index text (titles can carry markers — decode, don't leak)."""
    return " ".join(markers_to_text(title or "").split())


def _letter_of(title):
    t = _title_text(title)
    for ch in unicodedata.normalize("NFKD", t):
        if ch.isascii() and ch.isalpha():
            return ch.upper()
        if ch.isdigit():
            return "#"
    return "#"


# ── the build ─────────────────────────────────────────────────────────────

_STEM_RE = re.compile(r"^\d{2}-\d{4}-[0-9a-z-]+$")


def list_stems(volumes=None):
    """Corpus stems in name order (stable); volume filter by the stem's own prefix.
    The articles dir also holds index.json/contributors.json — the stem pattern
    excludes them."""
    stems = sorted(f[:-5] for f in os.listdir(ARTICLES_DIR)
                   if f.endswith(".json") and _STEM_RE.match(f[:-5]))
    if volumes:
        pre = tuple(f"{v:02d}-" for v in volumes)
        stems = [s for s in stems if s.startswith(pre)]
    return stems


def build_epub(stems, out_path, *, target="epub", articles_dir=ARTICLES_DIR,
               title="Encyclopædia Britannica, Eleventh Edition",
               ident="urn:britannica11:complete", images="diet", nav_articles=False,
               cover_subtitle=None, nudge=0, keep_stage=False, log=_log):
    global _NUDGE
    _NUDGE = nudge
    """Build a chunk-packed EPUB from the given corpus stems.  Returns a stats dict."""
    def load(stem):
        return json.load(open(os.path.join(articles_dir, stem + ".json"), encoding="utf-8"))

    # ── pass 0: metadata + math collect ──────────────────────────────────
    log(f"pass 0: metadata + math collect over {len(stems)} articles")
    meta = {}
    contribs = {}   # slug -> {name, initials, credentials, description, articles: [stem]}
    MA.start_collect()
    for stem in stems:
        a = load(stem)
        meta[stem] = {"volume": a.get("volume", 0), "page_start": a.get("page_start", 0),
                      "page_end": a.get("page_end") or 0, "title": a.get("title") or stem,
                      "article_type": a.get("article_type") or "article",
                      "ws_page_start": a.get("ws_page_start") or a.get("page_start", 0),
                      "ws_page_end": a.get("ws_page_end") or a.get("page_end") or 0}
        body = a.get("body") or ""
        if "«MATH" in body or "«EQN" in body:
            render_article(a, target=target)      # collect mode records (latex, display)
        for c in a.get("contributors") or []:
            name = c.get("full_name") or ""
            if not name:
                continue
            e = contribs.setdefault(_section_slug(name), {
                "name": name, "initials": c.get("initials") or "",
                "credentials": c.get("credentials") or "",
                "description": c.get("description") or "", "articles": []})
            e["articles"].append(stem)
    reqs = MA.take_collected()
    if reqs:
        MA.generate(reqs, svg=(target == "epub"), png=(target == "kindle"), log=log)

    # Spine order = reading order: volume, page range, title, stem (the stable-id's own
    # slug separates same-page homonyms) — the same content-derived total order the
    # export uses (article_sort_key), dict-shaped.
    spine_stems = sorted(stems, key=lambda s: (
        meta[s]["volume"], meta[s]["page_start"], meta[s]["page_end"],
        meta[s]["title"], s))

    # ── stage layout ─────────────────────────────────────────────────────
    stage = out_path + ".stage"
    if os.path.exists(stage):
        shutil.rmtree(stage)
    render_dir = os.path.join(stage, "render")
    oebps = os.path.join(stage, "OEBPS")
    imgdir, mathdir = os.path.join(oebps, "images"), os.path.join(oebps, "math")
    os.makedirs(render_dir)
    os.makedirs(imgdir)
    os.makedirs(mathdir)
    os.makedirs(os.path.join(stage, "META-INF"))

    # ── pass 1: render → namespace → XHTML → stage ───────────────────────
    log("pass 1: render + stage")
    seen_imgs, seen_math = {}, set()      # name -> body href form; math names are hex-safe
    missing_imgs = []
    img_dim_cache = {}
    # The absent-image placeholder ships unconditionally (written up front so the
    # dimension stamper can size references to it during the loop).
    open(os.path.join(imgdir, MISSING_IMG), "wb").write(_placeholder_png())
    seen_imgs[MISSING_IMG] = MISSING_IMG
    for i, stem in enumerate(spine_stems):
        a = load(stem)
        html = render_article(a, target=target, epub_bundled=pack.LINK_TOKENS)
        html = pack.lift_markers_out_of_tags(html)
        if target == "kindle":
            html = pack.kindle_style_transforms(html)
        html = pack.namespace_ids(html, stem)
        html = bundle_images(html, imgdir, seen_imgs, missing_imgs, diet=(images == "diet"))
        if target == "kindle":
            bundle_math_png(html, mathdir, seen_math)
        xhtml = pack.xhtml5_sanitize(to_xhtml_body(html, target))
        xhtml = stamp_img_dims(xhtml, oebps, img_dim_cache)
        open(os.path.join(render_dir, stem + ".xhtml"), "w", encoding="utf-8").write(xhtml)
        if (i + 1) % 2000 == 0:
            log(f"  staged {i + 1}/{len(spine_stems)}")
    if missing_imgs:
        log(f"  {len(missing_imgs)} referenced image(s) absent from the image store "
            f"(placeholder bundled): {sorted(set(missing_imgs))[:10]}")

    # ── pass 2: plan — split, assign chunks, build the anchor map ────────
    log("pass 2: plan chunks")
    def chunk_name(n):
        return f"c{n:04d}.xhtml"

    assignment = {}          # stem -> [chunk index per piece]
    anchor_map = {}          # anchor id -> chunk file (article anchors, sections, split-article ids)
    chunk_titles = {}        # chunk index -> [first title, last title] (reader-visible doc titles)
    chunk_idx, chunk_size = 0, 0
    n_pieces_total = 0
    first_chunk_of_vol, first_stem_of_vol = {}, {}
    for stem in spine_stems:
        xhtml = open(os.path.join(render_dir, stem + ".xhtml"), encoding="utf-8").read()
        pieces = pack.split_article(xhtml)
        split = len(pieces) > 1
        chunks_of = []
        for pi, piece in enumerate(pieces):
            if chunk_size and chunk_size + len(piece) > pack.TARGET_CHUNK * 1.35:
                chunk_idx, chunk_size = chunk_idx + 1, 0
            chunks_of.append(chunk_idx)
            cname = chunk_name(chunk_idx)
            t = _title_text(meta[stem]["title"])
            chunk_titles.setdefault(chunk_idx, [t, t])[1] = t
            for aid in pack.chunk_ids(piece):
                if split or aid == pack.article_anchor(stem) or "-section-" in aid:
                    anchor_map[aid] = cname
            chunk_size += len(piece)
        assignment[stem] = chunks_of
        n_pieces_total += len(pieces)
        v = meta[stem]["volume"]
        if v not in first_chunk_of_vol:
            first_chunk_of_vol[v] = chunk_name(chunks_of[0])
            first_stem_of_vol[v] = stem
    n_chunks = chunk_idx + 1
    log(f"  {n_chunks} chunks, {n_pieces_total - len(spine_stems)} split pieces beyond 1:1")

    # ── contributors appendix, packed like the articles ──────────────────
    contrib_map, contrib_files, contrib_groups = {}, [], []
    if contribs:
        # The appendix is a surname-first KEY (the print convention; user: given-name
        # order is unusable).  display_name ("Abbe, Cleveland") lives in the roster
        # export — the per-article entries carry only full_name.
        disp_of = {}
        roster_path = os.path.join(articles_dir, "contributors.json")
        if os.path.exists(roster_path):
            for r in json.load(open(roster_path, encoding="utf-8")):
                if r.get("full_name") and r.get("display_name"):
                    disp_of[r["full_name"]] = r["display_name"]
        for e in contribs.values():
            e["display"] = disp_of.get(e["name"], e["name"])
        order = sorted(contribs, key=lambda s: (contribs[s]["display"].lower(), s))
        secs = []
        for slug in order:
            e = contribs[slug]
            m = " ".join(x for x in [
                f'({_html.escape(e["initials"])})' if e["initials"] else "",
                _html.escape(e["credentials"])] if x)
            desc = (f'<p class="contrib-desc">{_html.escape(e["description"])}</p>'
                    if e["description"] else "")
            arts_li = "".join(
                f'<li><a href="{anchor_map[pack.article_anchor(st)]}#{pack.article_anchor(st)}">'
                f'{_html.escape(_title_text(meta[st]["title"]))}</a></li>'
                for st in e["articles"])
            secs.append((slug,
                         f'<section id="contrib-{slug}" class="contrib">'
                         f'<h2>{_html.escape(e["display"])}{(" " + m) if m else ""}</h2>{desc}'
                         f'<p class="contrib-articles-label">Articles</p><ul>{arts_li}</ul></section>'))
        preamble = ('<h1>Contributors</h1>\n'
                    '<p>The 1911 edition credited its authors only by initials. This edition '
                    'resolves them to full names and credentials, and lists the articles each '
                    'contributor wrote — a key the original never printed.</p>\n')
        cur, cur_size, fi = [], 0, 1
        def flush():
            nonlocal cur, cur_size, fi
            if not cur:
                return
            fname = f"contributors-{fi:02d}.xhtml"
            contrib_groups.append(
                (fname, contribs[cur[0][0]]["display"], contribs[cur[-1][0]]["display"]))
            body = (preamble if fi == 1 else "<h1>Contributors (continued)</h1>\n") \
                + "".join(h for _, h in cur)
            open(os.path.join(oebps, fname), "w", encoding="utf-8").write(
                xhtml_doc("Contributors", body))
            contrib_files.append(fname)
            for slug, _ in cur:
                contrib_map[slug] = fname
            cur, cur_size, fi = [], 0, fi + 1
        for slug, h in secs:
            if cur_size and cur_size + len(h) > pack.TARGET_CHUNK:
                flush()
            cur.append((slug, h))
            cur_size += len(h)
        flush()

    # ── A–Z index: letter → ~100-article range pages (the site's browse model:
    # PAGE_SIZE=100, range labels = first-3-chars of first/last titles) ─────
    INDEX_PAGE_SIZE = 100
    by_letter = {}
    for stem in spine_stems:
        by_letter.setdefault(_letter_of(meta[stem]["title"]), []).append(stem)
    letter_files = []           # every range page, for manifest/spine
    index_parts = []

    def _fold_title(t):
        # The index sorts ALPHABETICALLY, not by spine position — print quirks (plates
        # bound at volume ends, out-of-sequence articles, Ö collation) put e.g.
        # FRANCE, PLATE III after FYZABAD in reading order.
        t = unicodedata.normalize("NFKD", _title_text(t))
        return "".join(c for c in t if not unicodedata.combining(c)).upper()

    for letter in sorted(by_letter):
        stems_l = sorted(by_letter[letter],
                         key=lambda s: (_fold_title(meta[s]["title"]), s))
        lkey = "num" if letter == "#" else letter.lower()
        links = []
        for p in range(0, len(stems_l), INDEX_PAGE_SIZE):
            run = stems_l[p:p + INDEX_PAGE_SIZE]
            fname = f"index-{lkey}-{p // INDEX_PAGE_SIZE + 1:02d}.xhtml"
            first = _title_text(meta[run[0]]["title"])[:3].upper()
            last = _title_text(meta[run[-1]]["title"])[:3].upper()
            label = _html.escape(first if first == last else f"{first}–{last}")
            lis = "".join(
                f'<li><a href="{anchor_map[pack.article_anchor(s)]}#{pack.article_anchor(s)}">'
                f'{_html.escape(_title_text(meta[s]["title"]))}</a></li>'
                for s in run)
            open(os.path.join(oebps, fname), "w", encoding="utf-8").write(xhtml_doc(
                f"Index — {_html.escape(letter)} · {label}",
                f"<h1>{_html.escape(letter)} <span class=\"index-range\">{label}</span></h1>"
                f"<ul>{lis}</ul>"))
            letter_files.append((letter, fname))
            links.append(f'<a href="{fname}">{label}</a>')
        index_parts.append(f"<h2>{_html.escape(letter)}</h2>"
                           f'<p class="index-ranges">{" ".join(links)}</p>')
    index_body = "<h1>A–Z Index</h1>" + "".join(index_parts)
    open(os.path.join(oebps, "index.xhtml"), "w", encoding="utf-8").write(
        xhtml_doc("A–Z Index", index_body))

    # ── Search: a SCRIPTED title-search page (EPUB3 scripted content; Thorium runs
    # it).  The reader's own search linearly scans ~500MB of body text — the format
    # has no index.  Titles DO fit in-book: the embedded [title, href] table (~1.7MB)
    # ranks with the site's exact fold + titleRank tiers (ported from search-api.js,
    # one ordering spec), so title lookup is instant and Meilisearch-ordered.  Full-
    # text ranked search stays the site's job.  Script-stripping readers (Kindle)
    # see the fallback pointing at the A–Z index. ────────────────────────────
    # Kindle: NO scripted page at all — Enhanced Typesetting rejects the whole book
    # ("Not Supported" → the GUI's "internal error") when a scripted content doc is
    # present, Kindle strips JS anyway, and its conversion-built index already gives
    # fast native search.  The nav's lookup branch points at the A–Z index instead.
    with_search = target != "kindle"
    # Plain articles only (user: plate pages OUT of search — a "plate" query
    # was a wall of captions); plates stay reachable from their articles.
    rows = [[_title_text(meta[s]["title"]),
             f"{anchor_map[pack.article_anchor(s)]}#{pack.article_anchor(s)}"]
            for s in spine_stems
            if meta[s]["article_type"] == "article"] if with_search else []
    data_js = json.dumps(rows, ensure_ascii=False).replace("]]>", "]]\\u003e")
    search_script = (
        "\n//<![CDATA[\n"
        f"var DATA={data_js};\n"
        'function fold(s){return String(s||"").toLowerCase().normalize("NFD").replace(/[\\u0300-\\u036f]/g,"");}\n'
        "var F=DATA.map(function(r){return fold(r[0]);});\n"
        "var W=F.map(function(t){return t.split(/[\\s,.'\\u2019()\\-]+/).filter(Boolean);});\n"
        # Single-token queries keep the site's exact tiers; multi-token queries go
        # token-set: every query word must match a title word (whole-word, else
        # prefix), ANY ORDER — so "henry james" finds "JAMES, HENRY" (the print
        # convention inverts names; readers type them straight).
        "function rank(t,w,q,qt){if(t===q)return 0;\n"
        "if(qt.length>1){if(t.indexOf(q)===0)return 1;\n"
        " var all=true,allp=true;\n"
        " for(var k=0;k<qt.length;k++){\n"
        "  if(w.indexOf(qt[k])===-1)all=false;\n"
        "  var p=false;for(var m=0;m<w.length;m++)if(w[m].indexOf(qt[k])===0){p=true;break;}\n"
        "  if(!p){allp=false;break;}}\n"
        " if(all)return 2;if(allp)return 3;\n"
        " if(t.indexOf(q)!==-1)return 4;return 5;}\n"
        "if(w[0]===q)return 1;"
        "if(w.indexOf(q)!==-1)return 2;if(t.indexOf(q)===0)return 3;"
        "if(t.indexOf(q)!==-1)return 4;return 5;}\n"
        'function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;");}\n'
        "function run(){\n"
        ' var q=fold(document.getElementById("q").value.trim());\n'
        ' var out=document.getElementById("results");\n'
        ' if(!q){out.innerHTML="";return;}\n'
        " var qt=q.split(/[\\s,.'\\u2019()\\-]+/).filter(Boolean);\n"
        " var hits=[];\n"
        " for(var i=0;i<DATA.length;i++){var r=rank(F[i],W[i],q,qt);"
        "if(r<5)hits.push([r,DATA[i][0],DATA[i][1]]);}\n"
        " hits.sort(function(a,b){return a[0]-b[0]||a[1].length-b[1].length"
        "||(a[1]<b[1]?-1:a[1]>b[1]?1:0);});\n"
        ' var html="";\n'
        " for(var j=0;j<Math.min(hits.length,100);j++)"
        "html+='<li><a href=\"'+hits[j][2]+'\">'+esc(hits[j][1])+'</a></li>';\n"
        " if(hits.length>100)html+='<li>\\u2026 '+(hits.length-100)+' more \\u2014 keep typing</li>';\n"
        " out.innerHTML=html;\n"
        "}\n"
        'document.getElementById("q").addEventListener("input",run);\n'
        "// Click-driven query building: some readers (Thorium) capture keyboard input\n"
        "// for app shortcuts, so typing into content forms fails — buttons always work.\n"
        "function press(ch){\n"
        ' var el=document.getElementById("q");\n'
        ' if(ch==="BK")el.value=el.value.slice(0,-1);\n'
        ' else if(ch==="CLR")el.value="";\n'
        ' else el.value+=ch;\n'
        " run();\n"
        "}\n"
        'var row=document.getElementById("keys");\n'
        'var chars="ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").concat([" "]);\n'
        "for(var k=0;k<chars.length;k++){(function(ch){\n"
        ' var b=document.createElement("a");\n'
        ' b.textContent=(ch===" "?"\\u2423":ch);\n'
        ' b.className="key";\n'
        " b.addEventListener(\"click\",function(e){e.preventDefault();press(ch);});\n"
        " row.appendChild(b);\n"
        "})(chars[k]);}\n"
        "[[\"BK\",\"\\u232b\"],[\"CLR\",\"\\u2715\"]].forEach(function(p){\n"
        ' var b=document.createElement("a");\n'
        " b.textContent=p[1];b.className=\"key key-ctl\";\n"
        " b.addEventListener(\"click\",function(e){e.preventDefault();press(p[0]);});\n"
        " row.appendChild(b);\n"
        "});\n"
        'document.getElementById("fallback").style.display="none";\n'
        'document.getElementById("box").style.display="block";\n'
        "//]]>\n")
    if with_search:
        open(os.path.join(oebps, "search.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
            "Title Search",
            '<h1>Title Search</h1>'
            '<p id="fallback">This page needs a reader that runs scripts (Thorium, most '
            'desktop readers).  Without scripts, use the <a href="index.xhtml">A–Z Index'
            '</a> to find articles by title.</p>'
            '<div id="box" style="display:none">'
            '<p><input type="text" id="q" placeholder="Type or tap letters below…" '
            'style="width:100%;font-size:1.1em;padding:0.3em"/></p>'
            '<p id="keys" class="keyrow"></p>'
            '<p><a href="fulltext.xhtml">Full-Text Search →</a></p>'
            '<ul id="results"></ul></div>'
            f'<script>{search_script}</script>'))

    # ── Full-Text Search: the whole corpus as an embedded inverted index
    # (epub/fts.py — AND-of-words, article granularity, no phrases/snippets).
    # Its own page so Title Search stays instant; only opening this page pays
    # the ~30MB asset load.  Kindle: absent with the rest of the scripted UI. ──
    if with_search:
        coll = FTS.Collector()
        fts_docs = [s for s in spine_stems if meta[s]["article_type"] == "article"]
        for s in fts_docs:
            coll.add(_title_text(meta[s]["title"]),
                     f"{anchor_map[pack.article_anchor(s)]}#{pack.article_anchor(s)}",
                     load(s).get("body") or "")
        asset = coll.asset_js()
        open(os.path.join(oebps, "fts-data.js"), "w", encoding="utf-8").write(asset)
        log(f"fts: {len(fts_docs)} docs, {len(coll.by_term):,} terms, "
            f"asset {len(asset)/1e6:.1f}MB")
        ft_script = (
            "\n//<![CDATA[\n" + FTS.DECODER_JS +
            'function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;");}\n'
            "function run(){\n"
            ' var q=document.getElementById("q").value.trim();\n'
            ' var out=document.getElementById("results");\n'
            ' var note=document.getElementById("note");\n'
            ' if(!q){out.innerHTML="";note.textContent="";return;}\n'
            " var r=ftsQuery(q);\n"
            ' var msg=[];\n'
            ' if(r.ignored&&r.ignored.length)msg.push("ignored (too common): "+r.ignored.join(", "));\n'
            ' if(r.miss)msg.push("no article contains \\u201c"+r.miss+"\\u201d");\n'
            " if(r.docs.length)msg.push(r.docs.length+\" article\"+(r.docs.length===1?\"\":\"s\"));\n"
            ' note.textContent=msg.join(" \\u00b7 ");\n'
            # title-boosted order: articles whose title carries a query token first
            " var hits=[];\n"
            " for(var i=0;i<r.docs.length;i++){\n"
            "  var d=FTS_DOCS[r.docs[i]];\n"
            "  var tf=ftsFold(d[0]);var boost=0;\n"
            "  for(var k=0;k<(r.terms||[]).length;k++)if(tf.indexOf(r.terms[k])!==-1){boost=1;break;}\n"
            "  hits.push([boost?0:1,d[0],d[1]]);\n"
            " }\n"
            " hits.sort(function(a,b){return a[0]-b[0]||(a[1]<b[1]?-1:a[1]>b[1]?1:0);});\n"
            ' var html="";\n'
            " for(var j=0;j<Math.min(hits.length,200);j++)"
            "html+='<li><a href=\"'+hits[j][2]+'\">'+esc(hits[j][1])+'</a></li>';\n"
            " if(hits.length>200)html+='<li>\\u2026 '+(hits.length-200)+' more \\u2014 add a word</li>';\n"
            " out.innerHTML=html;\n"
            "}\n"
            'document.getElementById("q").addEventListener("input",run);\n'
            'document.getElementById("go").addEventListener("click",function(e){e.preventDefault();run();});\n'
            "function press(ch){\n"
            ' var el=document.getElementById("q");\n'
            ' if(ch==="BK")el.value=el.value.slice(0,-1);\n'
            ' else if(ch==="CLR")el.value="";\n'
            ' else el.value+=ch;\n'
            " run();\n"
            "}\n"
            'var row=document.getElementById("keys");\n'
            'var chars="ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").concat([" "]);\n'
            "for(var k=0;k<chars.length;k++){(function(ch){\n"
            ' var b=document.createElement("a");\n'
            ' b.textContent=(ch===" "?"\\u2423":ch);\n'
            ' b.className="key";\n'
            " b.addEventListener(\"click\",function(e){e.preventDefault();press(ch);});\n"
            " row.appendChild(b);\n"
            "})(chars[k]);}\n"
            "[[\"BK\",\"\\u232b\"],[\"CLR\",\"\\u2715\"]].forEach(function(p){\n"
            ' var b=document.createElement("a");\n'
            " b.textContent=p[1];b.className=\"key key-ctl\";\n"
            " b.addEventListener(\"click\",function(e){e.preventDefault();press(p[0]);});\n"
            " row.appendChild(b);\n"
            "});\n"
            'document.getElementById("fallback").style.display="none";\n'
            'document.getElementById("box").style.display="block";\n'
            "//]]>\n")
        open(os.path.join(oebps, "fulltext.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
            "Full-Text Search",
            '<h1>Full-Text Search</h1>'
            '<p id="fallback">This page needs a reader that runs scripts (Thorium, most '
            'desktop readers).  Without scripts, use your reader\u2019s built-in search.</p>'
            '<div id="box" style="display:none">'
            '<p>Every article containing <i>all</i> of the words you type '
            '(word order and phrases are not considered).</p>'
            '<p><input type="text" id="q" placeholder="e.g. waterloo cavalry" '
            'style="width:80%;font-size:1.1em;padding:0.3em"/> '
            '<a href="#" id="go" class="key key-ctl">Search</a></p>'
            '<p id="keys" class="keyrow"></p>'
            '<p id="note" style="color:#6b5e4f"></p>'
            '<ul id="results"></ul></div>'
            '<script src="fts-data.js"></script>'
            f'<script>{ft_script}</script>'))

    # ── Topics: the classified TOC (vol-29) as nested hub pages, arbitrary depth.
    # One page per node in DFS preorder (nav targets stay monotone in spine order);
    # each page = breadcrumb + notes + child links + the node's article links.  A
    # note's cross-link names another NODE (`anchor`) — resolved via a first-wins
    # name→page map, the title-map discipline. ──────────────────────────────
    topic_files, topics_nav = [], ""
    ct_path = os.path.join(ROOT, "data", "derived", "classified_toc.json")
    if os.path.exists(ct_path):
        ct = json.load(open(ct_path, encoding="utf-8"))

        def _tkids(n):
            return (n.get("subsections") or []) + (n.get("children") or [])

        fname_of, name_to_file = {}, {}
        def _assign(n):
            fname = f"topic-{len(topic_files) + 1:03d}.xhtml"
            topic_files.append(fname)
            fname_of[id(n)] = fname
            name_to_file.setdefault(n.get("name") or "", fname)
            for c in _tkids(n):
                _assign(c)
        for c in ct["categories"]:
            _assign(c)

        def _art_li(a):
            disp = _html.escape(a.get("display") or a.get("target") or "")
            if a.get("emphasized") in (True, "True", "true"):
                disp = f"<b>{disp}</b>"
            fn = a.get("filename")
            if not fn:
                return f"<li>{disp}</li>"          # unresolved index entry: faithful, unlinked
            stem = re.sub(r"\.json$", "", fn)
            anch = pack.article_anchor(stem)
            href = (f"{anchor_map[anch]}#{anch}" if anch in anchor_map
                    else f"{pack.SITE_BASE}/article/{stem}")
            return f'<li><a href="{href}">{disp}</a></li>'

        def _note_html(note):
            txt = note.get("text") or ""
            out, pos = [], 0
            for l in sorted(note.get("links") or [], key=lambda x: x["start"]):
                out.append(_html.escape(txt[pos:l["start"]]))
                dsp = _html.escape(txt[l["start"]:l["end"]])
                tgt = name_to_file.get(l.get("anchor") or "")
                out.append(f'<a href="{tgt}">{dsp}</a>' if tgt else dsp)
                pos = l["end"]
            out.append(_html.escape(txt[pos:]))
            return f'<p class="topic-note"><i>{"".join(out)}</i></p>'

        def _emit_topic(n, crumb):
            name = n.get("name") or "?"
            body = []
            if crumb:
                body.append(f'<p class="topic-crumb">{_html.escape(" › ".join(crumb))}</p>')
            body.append(f"<h1>{_html.escape(name)}</h1>")
            for note in n.get("notes") or []:
                body.append(_note_html(note))
            ch = _tkids(n)
            if ch:
                body.append("<ul class=\"topic-children\">" + "".join(
                    f'<li><a href="{fname_of[id(c)]}">{_html.escape(c.get("name") or "?")}</a></li>'
                    for c in ch) + "</ul>")
            arts = n.get("articles") or []
            if arts:
                body.append("<ul>" + "".join(_art_li(a) for a in arts) + "</ul>")
            open(os.path.join(oebps, fname_of[id(n)]), "w", encoding="utf-8").write(
                xhtml_doc(" › ".join(crumb + [name]) if crumb else name, "".join(body)))
            for c in ch:
                _emit_topic(c, crumb + [name])

        for c in ct["categories"]:
            _emit_topic(c, [])
        open(os.path.join(oebps, "topics.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
            "Topics", "<h1>Topics</h1><ul>" + "".join(
                f'<li><a href="{fname_of[id(c)]}">{_html.escape(c.get("name") or "?")}</a></li>'
                for c in ct["categories"]) + "</ul>"))

        # TOC shows the top categories ONLY (user spec); the deeper tree lives on
        # the topic pages themselves, one click in.
        topics_nav = "".join(
            f'<li><a href="{fname_of[id(c)]}">{_html.escape(c.get("name") or "?")}</a></li>'
            for c in ct["categories"])

    # ── Reader's Guide (back matter): hub → 6 parts → 71 chapters, extracted from
    # the static site pages; article citations resolve presence-aware against the
    # anchor map (absent stem keeps its site URL — the packer's standing policy) ──
    _GUIDE_ART_RE = re.compile(
        r'href="' + re.escape(pack.SITE_BASE) + r'/article/([0-9a-z-]+)"')
    _SITE_CONTRIB_RE = re.compile(
        r'href="' + re.escape(pack.SITE_BASE) + r'/contributors\.html\?q=([^"]+)"')
    _name_to_slug = {e["name"]: s for s, e in contribs.items()}

    def _guide_art_href(m):
        anch = pack.article_anchor(m.group(1))
        if anch in anchor_map:
            return f'href="{anchor_map[anch]}#{anch}"'
        return m.group(0)

    def _site_contrib_href(m):
        # The q= value is the resolver's CANONICAL name — exact-match against the
        # appendix roster only (zero false positives); a miss keeps the site link.
        name = unquote_plus(m.group(1))
        slug = _name_to_slug.get(name)
        if slug is None and _section_slug(name) in contrib_map:
            slug = _section_slug(name)
        if slug and slug in contrib_map:
            return f'href="{contrib_map[slug]}#contrib-{slug}"'
        return m.group(0)

    def _internalize_site_links(body):
        return _SITE_CONTRIB_RE.sub(_site_contrib_href,
                                    _GUIDE_ART_RE.sub(_guide_art_href, body))

    guide_pages, guide_imgs = RG.pages()
    if FM.DROPPED_HREFS:
        log(f"front-matter/guide: dropped {len(FM.DROPPED_HREFS)} malformed source href(s) "
            "(site-side generator bug, queued)")
    for b, src_path in sorted(guide_imgs.items()):
        shutil.copyfile(src_path, os.path.join(imgdir, b))
    for fname, g_title, g_body, _part in guide_pages:
        g_body = _internalize_site_links(g_body)
        open(os.path.join(oebps, fname), "w", encoding="utf-8").write(
            xhtml_doc(g_title, '<div class="frontmatter">'
                      + to_xhtml_body(g_body, target) + "</div>"))
    guide_by_part = {}
    for fname, g_title, _b, part in guide_pages:
        guide_by_part.setdefault(part, []).append((fname, g_title))
    guide_branch = ""
    if guide_pages:
        # TOC: the six parts only — chapters live on the part pages, one click in.
        part_lis = [f'<li><a href="{guide_by_part[n][0][0]}">'
                    f"{_html.escape(guide_by_part[n][0][1])}</a></li>"
                    for n in range(1, 7)]
        stray = [(f, t) for f, t in guide_by_part.get(None, []) if f != "guide.xhtml"]
        if stray:
            log(f"readers-guide: {len(stray)} chapter(s) not listed on any part page")
        guide_branch = ('<li><a href="guide.xhtml">Reader’s Guide</a><ol>'
                        + "".join(part_lis) + "</ol></li>")

    # ── nav: TOP-LEVELS ONLY (user spec) — 28 flat volume entries; each opens a
    # VOLUME HUB page listing the ~100-article ranges (detail lives behind the
    # click, never in the TOC panel) ─────────────────────────────────────────
    def _vol_label(v):
        # MIRROR THE SITE (index.html volArticles/volRangeWords — user: 100%
        # correct): plain articles only (no plates), ordered ws_page_start with
        # ws_page_end tiebreak, label = first word of the FIRST and LAST titles
        # (comma-clipped).  No folding, no min/max — the site's rule verbatim.
        arts = sorted(
            (s for s in vol_stems[v] if meta[s]["article_type"] == "article"),
            key=lambda s: (meta[s]["ws_page_start"], meta[s]["ws_page_end"]))
        if not arts:
            return f"Volume {v}"
        def word(s):
            return _title_text(meta[s]["title"]).split(",")[0].split(" ")[0]
        return f"Volume {v} · {word(arts[0])} – {word(arts[-1])}"

    vol_stems = {}
    for s in spine_stems:                      # spine order = the volume's page order
        vol_stems.setdefault(meta[s]["volume"], []).append(s)

    def _r3(stem):
        return _title_text(meta[stem]["title"])[:3].upper()

    # Each range is a HUB PAGE listing its ~100 article links — the site model: a
    # sub-volume click shows the TITLE LIST, never a reading position (a range entry
    # that jumps into the text leaves ~100 articles of page-turning to the target).
    browse_files = []
    def _range_li(v, n, run):
        label = _r3(run[0]) if _r3(run[0]) == _r3(run[-1]) else f"{_r3(run[0])}–{_r3(run[-1])}"
        fname = f"browse-{v:02d}-{n:02d}.xhtml"
        lis = "".join(
            f'<li><a href="{anchor_map[pack.article_anchor(s)]}#{pack.article_anchor(s)}">'
            f'{_html.escape(_title_text(meta[s]["title"]))}</a></li>'
            for s in run)
        open(os.path.join(oebps, fname), "w", encoding="utf-8").write(xhtml_doc(
            f"Volume {v} · {label}",
            f'<h1>Volume {v} <span class="index-range">{_html.escape(label)}</span></h1><ul>{lis}</ul>'))
        browse_files.append(fname)
        if nav_articles:
            # third nav level: every article — the reader's own TOC-panel search then
            # matches titles natively.  Targets must be chunk anchors (spine-ordered),
            # not the late-spine hub pages (NAV-011).
            a0 = pack.article_anchor(run[0])
            return (f'<li><a href="{anchor_map[a0]}#{a0}">{_html.escape(label)}</a>'
                    f"<ol>{lis}</ol></li>")
        return f'<li><a href="{fname}">{_html.escape(label)}</a></li>'

    # The volume link opens a VOLUME HUB listing the volume's range hubs — the
    # site model both levels down: volume → ranges → title list, detail always
    # behind a click, the TOC itself stays 28 flat lines.
    def _vol_href(v):
        if nav_articles:
            a = pack.article_anchor(first_stem_of_vol[v])
            return f"{anchor_map[a]}#{a}"
        return f"volume-{v:02d}.xhtml"

    vol_lis_parts = []
    for v in sorted(first_chunk_of_vol):
        range_lis = "".join(
            _range_li(v, i // INDEX_PAGE_SIZE + 1, vol_stems[v][i:i + INDEX_PAGE_SIZE])
            for i in range(0, len(vol_stems[v]), INDEX_PAGE_SIZE))
        if nav_articles:
            vol_lis_parts.append(
                f'<li><a href="{_vol_href(v)}">{_html.escape(_vol_label(v))}</a>'
                f"<ol>{range_lis}</ol></li>")
            continue
        hub = f"volume-{v:02d}.xhtml"
        open(os.path.join(oebps, hub), "w", encoding="utf-8").write(xhtml_doc(
            _vol_label(v),
            f"<h1>{_html.escape(_vol_label(v))}</h1><ul>{range_lis}</ul>"))
        n_ranges = (len(vol_stems[v]) + INDEX_PAGE_SIZE - 1) // INDEX_PAGE_SIZE
        browse_files.insert(len(browse_files) - n_ranges, hub)   # hub precedes its ranges
        vol_lis_parts.append(
            f'<li><a href="{hub}">{_html.escape(_vol_label(v))}</a></li>')
    vol_lis = "".join(vol_lis_parts)
    # FOUR top-level branches (user's spec): Volumes · Topics · Title Search ·
    # Contributors.  "Volumes" is a span group header (nav li may be a span when it
    # nests an ol); the A–Z Index rides under Title Search as its no-script twin.
    # Branch order follows spine order (NAV-011 monotone): the fullnav variant's
    # volume links target CHUNK anchors, so its Volumes branch comes after the
    # front-matter branches instead of first.
    vols_branch = f"<li><span>Volumes</span><ol>{vol_lis}</ol></li>"
    topics_branch = (f'<li><a href="topics.xhtml">Topics</a><ol>{topics_nav}</ol></li>'
                     if topic_files else "")
    # A–Z stays reachable from the title page and the search page's no-script
    # fallback; the TOC keeps only the top-level entries.
    search_branch = ('<li><a href="search.xhtml">Title Search</a></li>'
                     '<li><a href="fulltext.xhtml">Full-Text Search</a></li>'
                     if with_search else
                     '<li><a href="index.xhtml">A–Z Index</a></li>')
    contrib_branch = ""
    if contrib_files:
        def _cg_label(a, b):
            return a[:1].upper() if a[:1].upper() == b[:1].upper() else f"{a[:1].upper()}–{b[:1].upper()}"
        subs = "".join(f'<li><a href="{f}">{_html.escape(_cg_label(a, b))}</a></li>'
                       for f, a, b in contrib_groups)
        contrib_branch = (f'<li><a href="{contrib_files[0]}">Contributors</a>'
                          + (f"<ol>{subs}</ol>" if len(contrib_groups) > 1 else "") + "</li>")
    # Front matter (introduction when written · the 1910 Editorial Preface · the
    # Index Preface) rides directly after the title page; ONE compact nav group so
    # Title Search keeps its visual top slot while nav stays spine-monotone.
    fm_pages = FM.pages()
    for fname, fm_title, fm_body in fm_pages:
        open(os.path.join(oebps, fname), "w", encoding="utf-8").write(
            xhtml_doc(fm_title, '<div class="frontmatter">'
                      + to_xhtml_body(_internalize_site_links(fm_body), target)
                      + "</div>"))
    # Plain top-level entries, directly after Title Search (user spec — no group).
    fm_branch = "".join(f'<li><a href="{f}">{_html.escape(t)}</a></li>'
                        for f, t, _ in fm_pages)
    # Title Search leads (user: the most important tool by far — it was buried);
    # the prefaces follow it directly, the Reader's Guide closes the book (user spec).
    if nav_articles:
        nav_top = search_branch + fm_branch + topics_branch + vols_branch + contrib_branch + guide_branch
    else:
        nav_top = search_branch + fm_branch + vols_branch + topics_branch + contrib_branch + guide_branch
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        '<head><meta charset="utf-8"/><title>Contents</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n'
        f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{nav_top}</ol></nav>\n'
        '</body>\n</html>\n')
    open(os.path.join(oebps, "nav.xhtml"), "w", encoding="utf-8").write(nav)

    # The tools ride on the FIRST page every reader sees (user: the Contents-panel
    # entries alone were too easy to miss).
    tool_links = (['<a href="search.xhtml">Title Search</a>',
                   '<a href="fulltext.xhtml">Full-Text Search</a>']
                  if with_search else [])
    if topic_files:
        tool_links.append('<a href="topics.xhtml">Topics</a>')
    tool_links.append('<a href="index.xhtml">A–Z Index</a>')
    if contrib_files:
        tool_links.append(f'<a href="{contrib_files[0]}">Contributors</a>')
    if guide_pages:
        tool_links.append('<a href="guide.xhtml">Reader’s Guide</a>')
    open(os.path.join(oebps, "titlepage.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
        title,
        f'<div class="titlepage"><h1>{_html.escape(title)}</h1>'
        '<p>A Dictionary of Arts, Sciences, Literature and General Information</p>'
        f'<p>{len(spine_stems):,} articles · 28 volumes (1910–1911)</p>'
        f'<p class="titlepage-tools">{" · ".join(tool_links)}</p>'
        + (('<p class="titlepage-tools">' + " · ".join(
            f'<a href="{f}">{_html.escape(t)}</a>' for f, t, _ in fm_pages) + "</p>")
           if fm_pages else "")
        + '<p><a href="https://britannica11.org">britannica11.org</a></p></div>'))

    open(os.path.join(oebps, "style.css"), "w", encoding="utf-8").write(epub_css(target))

    # Cover: the site mark's language at 1600×2560; cover-image manifest property
    # (EPUB3) + the legacy meta (Kindle keys its thumbnails on it).  KINDLE gets the
    # IMAGE ONLY — Amazon generates its own cover page, and shipping an HTML cover
    # page silently disqualifies Enhanced Typesetting (vol-1 regressed "Supported" →
    # "Not Supported" on exactly this delta, with zero logged errors).
    make_cover(os.path.join(oebps, "cover.jpg"), subtitle=cover_subtitle)
    if target != "kindle":
        open(os.path.join(oebps, "cover.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
            title,
            '<div style="text-align:center;margin:0;padding:0">'
            f'<img src="cover.jpg" alt="{_html.escape(title)}" width="{_COVER_W}" height="{_COVER_H}" '
            'style="max-width:100%;height:auto"/></div>'))

    # ── pass 3: emit chunks (same staged bytes → same pieces), resolve at close ──
    log("pass 3: emit chunks")
    gate_errors = []
    buffers = {}             # chunk index -> [piece html]
    for stem in spine_stems:
        xhtml = open(os.path.join(render_dir, stem + ".xhtml"), encoding="utf-8").read()
        pieces = pack.split_article(xhtml)
        if len(pieces) != len(assignment[stem]):
            raise GateFailure(f"{stem}: plan/emit split divergence "
                              f"({len(assignment[stem])} vs {len(pieces)} pieces)")
        if len(pieces) > 1 and pack.split_invariant("".join(pieces)) != pack.split_invariant(xhtml):
            raise GateFailure(f"{stem}: split lost content")
        for piece, ci in zip(pieces, assignment[stem]):
            buffers.setdefault(ci, []).append(piece)

    # Spine assembles in FOUR runs: head (titlepage) · front matter (the lookup
    # surfaces — search/topics/A–Z — FIRST, like a print encyclopedia's guide, and
    # so their nav entries can precede the volumes under NAV-011's monotone rule) ·
    # the article chunks · back matter (volume browse hubs, contributors).
    manifest, spine, spine_front, spine_back = [], [], [], []
    manifest.append('<item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
    if target != "kindle":
        manifest.append('<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="coverpage"/>')
    manifest.append('<item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="titlepage"/>')
    manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    dangling_total = []
    for ci in sorted(buffers):
        cname = chunk_name(ci)
        raw = "".join(buffers[ci])
        own = pack.chunk_ids(raw)
        resolved, dangling = pack.resolve_chunk(raw, own, anchor_map, contrib_map)
        dangling_total.extend((cname, d) for d in dangling)
        # Resolution touches only hrefs — text equality against the raw chunk is exact.
        if pack.text_of(resolved) != pack.text_of(raw):
            raise GateFailure(f"{cname}: resolution changed chunk text")
        has_svg = "<svg" in resolved
        ct = chunk_titles.get(ci, ["", ""])
        ctitle = ct[0] if ct[0] == ct[1] else f"{ct[0]} – {ct[1]}"
        open(os.path.join(oebps, cname), "w", encoding="utf-8").write(
            xhtml_doc(ctitle or cname[:-6], resolved))
        props = ' properties="svg"' if has_svg else ""
        manifest.append(f'<item id="id-{cname[:-6]}" href="{cname}" '
                        f'media-type="application/xhtml+xml"{props}/>')
        spine.append(f'<itemref idref="id-{cname[:-6]}"/>')

    # Hub/ancillary pages ride in the nav's branch order (NAV-011): standard =
    # browse hubs, topics, search, A–Z before the chunks; contributors after.  The
    # fullnav variant's volume links target chunks, so its browse hubs move to the
    # back and search/topics/A–Z lead.
    def _mspine(lst, items):
        for iid, fname, extra in items:
            manifest.append(f'<item id="{iid}" href="{fname}" media-type="application/xhtml+xml"{extra}/>')
            lst.append(f'<itemref idref="{iid}"/>')

    browse_items = [("bro-" + f[7:-6], f, "") for f in browse_files]
    topic_items = (([("topics", "topics.xhtml", "")]
                    + [("top-" + f[6:-6], f, "") for f in topic_files]) if topic_files else [])
    search_items = ([("searchpage", "search.xhtml", ' properties="scripted"'),
                     ("fulltextpage", "fulltext.xhtml", ' properties="scripted"')]
                    if with_search else [])
    index_items = ([("azindex", "index.xhtml", "")]
                   + [("idx-" + f[6:-6], f, "") for _l, f in letter_files])
    contrib_items = [("ctb-" + f[13:-6], f, "") for f in contrib_files]
    fm_items = [(f"fm-{i}", fname, "") for i, (fname, _t, _b) in enumerate(fm_pages)]
    guide_items = [(f"gd-{i}", fname, "")
                   for i, (fname, _t, _b, _p) in enumerate(guide_pages)]
    if nav_articles:
        for grp in (search_items, index_items, fm_items, topic_items):
            _mspine(spine_front, grp)
        for grp in (browse_items, contrib_items, guide_items):
            _mspine(spine_back, grp)
    else:
        for grp in (search_items, index_items, fm_items, browse_items, topic_items):
            _mspine(spine_front, grp)
        for grp in (contrib_items, guide_items):
            _mspine(spine_back, grp)
    head_n = 1 if target == "kindle" else 2      # kindle: no cover page (titlepage only)
    spine = spine[:head_n] + spine_front + spine[head_n:] + spine_back
    for n, name in enumerate(sorted(seen_imgs)):
        manifest.append(f'<item id="img-{n}" href="images/{_html.escape(seen_imgs[name])}" '
                        f'media-type="{_media_type(seen_imgs[name])}"/>')
    for n, name in enumerate(sorted(guide_imgs)):
        manifest.append(f'<item id="gimg-{n}" href="images/{_html.escape(name)}" '
                        f'media-type="{_media_type(name)}"/>')
    for n, name in enumerate(sorted(seen_math)):
        manifest.append(f'<item id="mpng-{n}" href="math/{name}" media-type="image/png"/>')
    if with_search:
        manifest.append('<item id="fts-data" href="fts-data.js" '
                        'media-type="application/javascript"/>')
    manifest.append('<item id="css" href="style.css" media-type="text/css"/>')

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="pub-id">{ident}</dc:identifier>\n'
        f'    <dc:title>{_html.escape(title)}</dc:title>\n'
        '    <dc:language>en</dc:language>\n'
        '    <dc:source>https://en.wikisource.org/wiki/1911_Encyclop%C3%A6dia_Britannica</dc:source>\n'
        f'    <meta property="dcterms:modified">{_MODIFIED}</meta>\n'
        + ('    <meta name="cover" content="cover-image"/>\n' if target == "kindle" else "")
        + '  </metadata>\n'
        '  <manifest>\n    ' + "\n    ".join(manifest) + '\n  </manifest>\n'
        '  <spine>\n    ' + "\n    ".join(spine) + '\n  </spine>\n'
        '</package>\n')
    open(os.path.join(oebps, "content.opf"), "w", encoding="utf-8").write(opf)
    open(os.path.join(stage, "META-INF", "container.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n')

    # ── integrity gates over the WRITTEN container (measure the artifact) ──
    log("gates: id census + href resolution")
    _SCRIPT_RE = re.compile(r"<script>.*?</script>", re.S)
    ids_by_file = {}
    for f in sorted(os.listdir(oebps)):
        if not f.endswith(".xhtml"):
            continue
        # script bodies (the search page's JS builds href= strings) are not markup
        content = _SCRIPT_RE.sub("", open(os.path.join(oebps, f), encoding="utf-8").read())
        found = pack._ID_RE.findall(content)
        dupes = [i for i, n in Counter(found).items() if n > 1]
        if dupes:
            gate_errors.append(f"{f}: duplicate ids {dupes[:5]}")
        ids_by_file[f] = set(found)
    all_ids = Counter()
    for s in ids_by_file.values():
        all_ids.update(s)
    missing = [s for s in spine_stems if all_ids[pack.article_anchor(s)] != 1]
    if missing:
        gate_errors.append(f"article anchors not exactly-once: {len(missing)} "
                           f"(first: {missing[:5]})")
    href_re = re.compile(r'href="([^"]+)"')
    bad_hrefs = 0
    for f, _ids in ids_by_file.items():
        content = _SCRIPT_RE.sub("", open(os.path.join(oebps, f), encoding="utf-8").read())
        for h in href_re.findall(content):
            if h.startswith(("http://", "https://", "mailto:")):
                continue
            if h.startswith("epublink:") or h.startswith("epubcontrib:") or h.startswith("/"):
                gate_errors.append(f"{f}: unresolved token/root href {h[:80]}")
                continue
            tgt, _, frag = h.partition("#")
            tgt = tgt or f
            if tgt not in ids_by_file:
                # non-XHTML target (style.css, …): existence on disk is the contract
                if not os.path.exists(os.path.join(oebps, tgt)):
                    bad_hrefs += 1
                    if bad_hrefs <= 5:
                        gate_errors.append(f"{f}: href to missing file {h[:80]}")
            elif frag and frag not in ids_by_file[tgt]:
                bad_hrefs += 1
                if bad_hrefs <= 5:
                    gate_errors.append(f"{f}: dangling fragment {h[:80]}")
    if bad_hrefs > 5:
        gate_errors.append(f"...{bad_hrefs} bad hrefs total")
    for cname, d in dangling_total[:5]:
        gate_errors.append(f"{cname}: unresolvable internal fragment {d}")
    if len(dangling_total) > 5:
        gate_errors.append(f"...{len(dangling_total)} unresolvable fragments total")
    if gate_errors:
        raise GateFailure("integrity gates failed:\n  " + "\n  ".join(gate_errors))

    # ── zip (mimetype first, stored; deterministic file order) ───────────
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        for base, _dirs, files in sorted(os.walk(stage)):
            if os.path.abspath(base).startswith(os.path.abspath(render_dir)):
                continue
            for f in sorted(files):
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, stage).replace("\\", "/"))
    if not keep_stage:
        shutil.rmtree(stage)
    stats = {"path": out_path, "articles": len(spine_stems), "chunks": n_chunks,
             "images": len(seen_imgs), "math_png": len(seen_math),
             "size_mb": round(os.path.getsize(out_path) / 1e6, 1)}
    log(f"built {out_path}: {stats['articles']} articles in {stats['chunks']} chunks, "
        f"{stats['images']} images, {stats['size_mb']} MB")
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the chunk-packed EPUB.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--volume", type=int, action="append", help="volume(s) to include")
    g.add_argument("--all", action="store_true", help="the whole edition")
    ap.add_argument("--target", choices=("epub", "kindle"), default="epub")
    ap.add_argument("--images", choices=("diet", "full"), default="diet",
                    help="diet = display-resolution re-encodes (default); full = source bytes")
    ap.add_argument("--nav-articles", action="store_true",
                    help="third nav level: every article (reader TOC-panel search matches titles)")
    ap.add_argument("--limit", type=int, default=None,
                    help="probe builds: only the first N stems (converter bisection)")
    ap.add_argument("--nudge", type=int, default=0,
                    help="shift every chunk's byte offsets (dislodges Amazon's positional ET bug)")
    ap.add_argument("--exclude", action="append", default=[],
                    help="probe builds: drop specific stem(s)")
    ap.add_argument("--chunk-target", type=int, default=None,
                    help="soft chunk budget in bytes (default pack.TARGET_CHUNK; "
                         "hard split scales 1.5x)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-stage", action="store_true")
    args = ap.parse_args(argv)
    if args.chunk_target:
        pack.TARGET_CHUNK = args.chunk_target
        pack.HARD_SPLIT = int(args.chunk_target * 1.5)
    stems = list_stems(None if args.all else args.volume)
    if args.limit:
        stems = stems[:args.limit]
    if args.exclude:
        stems = [s for s in stems if s not in set(args.exclude)]
    if args.all:
        name, ident = "eb1911", "urn:britannica11:complete"
        title = "Encyclopædia Britannica, Eleventh Edition"
    else:
        vols = "-".join(f"{v:02d}" for v in sorted(args.volume))
        name = f"eb1911-vol{vols}"
        ident = f"urn:britannica11:vol-{vols}"
        title = f"Encyclopædia Britannica, Eleventh Edition — Volume {', '.join(map(str, sorted(args.volume)))}"
    if args.nav_articles:
        ident += ":fullnav"
    suffix = ("-kindle" if args.target == "kindle" else "") + \
        ("-fullnav" if args.nav_articles else "")
    out = args.out or os.path.join(ROOT, f"{name}{suffix}.epub")
    cover_sub = (None if args.all
                 else "Volume " + ", ".join(map(str, sorted(args.volume))))
    build_epub(stems, out, target=args.target, title=title, ident=ident,
               images=args.images, nav_articles=args.nav_articles,
               cover_subtitle=cover_sub, nudge=args.nudge, keep_stage=args.keep_stage)


if __name__ == "__main__":
    main()
