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
import struct
import unicodedata
import zipfile
import zlib
from collections import Counter
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

import html5lib

from britannica.epub import images as IMG
from britannica.epub import pack
from britannica.epub import math_assets as MA
from britannica.markers import markers_to_text
from britannica.render.article import render_article, _section_slug

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ARTICLES_DIR = os.path.join(ROOT, "data", "derived", "articles")
IMAGES_SRC = os.path.join(ROOT, "data", "images")
MATH_PNG_SRC = os.path.join(ROOT, "data", "derived", "math_png")
_MODIFIED = "2026-07-07T00:00:00Z"        # fixed for reproducible builds
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


def epub_css():
    """The EPUB stylesheet — content typography only, single-column, reader-controlled width."""
    base = open(os.path.join(os.path.dirname(__file__), "epub.css"), encoding="utf-8").read()
    return base + ("\n/* math */\n"
                   "svg.math-display, img.math-display { display:block; margin:1.1em auto;"
                   " max-width:100%; height:auto; }\n"
                   "svg.math-inline, img.math-inline { max-width:100%; }\n")


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


def to_xhtml_body(html_str):
    """HTML5 render → XML-well-formed XHTML fragment (void elements self-closed, entities
    as chars).  Inline math SVG is ALREADY valid XML (MathJax, with its own xmlns), but
    ElementTree mangles foreign-content namespaces on round-trip, so each ``<svg>`` is
    lifted out before the html5lib+ET pass and spliced back verbatim after."""
    protected, svgs = pack.protect_svgs(html_str)
    frag = html5lib.parseFragment(protected, treebuilder="etree", namespaceHTMLElements=False)
    out = [_html.escape(frag.text, quote=False)] if frag.text else []
    for child in frag:
        _drop_invalid_attrs(child)
        _fix_nested_lists(child)
        _fix_phrasing_blocks(child)
        out.append(ET.tostring(child, encoding="unicode", method="xml"))
    result = "".join(out)
    for i, svg in enumerate(svgs):
        result = result.replace(f"MJSVGSLOT{i}ENDSLOT", svg)
    return result


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
                clean = _EXT_JUNK_RE.sub(r"\1", name)
                if diet:
                    data, ext = IMG.diet_image(src)
                    dest = os.path.splitext(clean)[0] + ext
                else:
                    data, dest = open(src, "rb").read(), clean
                if dest != name and any(unquote(e) == dest for e in seen.values()):
                    stem_, ext_ = os.path.splitext(dest)
                    dest = f"{stem_}-{hashlib.sha1(name.encode()).hexdigest()[:6]}{ext_}"
                open(os.path.join(dst_dir, dest), "wb").write(data)
                seen[name] = quote(dest, safe="!*'()")   # encodeURIComponent, the body's form
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


def xhtml_doc(title, body):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{_html.escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
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
               ident="urn:britannica11:complete", images="diet", keep_stage=False, log=_log):
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
                      "page_end": a.get("page_end") or 0, "title": a.get("title") or stem}
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
    for i, stem in enumerate(spine_stems):
        a = load(stem)
        html = render_article(a, target=target, epub_bundled=pack.LINK_TOKENS)
        html = pack.lift_markers_out_of_tags(html)
        html = pack.namespace_ids(html, stem)
        html = bundle_images(html, imgdir, seen_imgs, missing_imgs, diet=(images == "diet"))
        if target == "kindle":
            bundle_math_png(html, mathdir, seen_math)
        xhtml = pack.xhtml5_sanitize(to_xhtml_body(html))
        open(os.path.join(render_dir, stem + ".xhtml"), "w", encoding="utf-8").write(xhtml)
        if (i + 1) % 2000 == 0:
            log(f"  staged {i + 1}/{len(spine_stems)}")
    if missing_imgs:
        open(os.path.join(imgdir, MISSING_IMG), "wb").write(_placeholder_png())
        seen_imgs[MISSING_IMG] = MISSING_IMG
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
    contrib_map, contrib_files = {}, []
    if contribs:
        order = sorted(contribs, key=lambda s: contribs[s]["name"].lower())
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
                         f'<h2>{_html.escape(e["name"])}{(" " + m) if m else ""}</h2>{desc}'
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

    # ── A–Z letter index ─────────────────────────────────────────────────
    by_letter = {}
    for stem in spine_stems:
        by_letter.setdefault(_letter_of(meta[stem]["title"]), []).append(stem)
    letter_files = []
    for letter in sorted(by_letter):
        fname = f"index-{'num' if letter == '#' else letter.lower()}.xhtml"
        lis = "".join(
            f'<li><a href="{anchor_map[pack.article_anchor(s)]}#{pack.article_anchor(s)}">'
            f'{_html.escape(_title_text(meta[s]["title"]))}</a></li>'
            for s in by_letter[letter])
        open(os.path.join(oebps, fname), "w", encoding="utf-8").write(xhtml_doc(
            f"Index — {letter}", f"<h1>{_html.escape(letter)}</h1><ul>{lis}</ul>"))
        letter_files.append((letter, fname))
    index_body = "<h1>A–Z Index</h1><p class=\"index-letters\">" + " ".join(
        f'<a href="{f}">{_html.escape(letter)}</a>' for letter, f in letter_files) + "</p>"
    open(os.path.join(oebps, "index.xhtml"), "w", encoding="utf-8").write(
        xhtml_doc("A–Z Index", index_body))

    # ── nav (two-level: volumes; fine-grain lives in the A–Z index + reader search) ──
    def _vol_label(v):
        first = _title_text(meta[first_stem_of_vol[v]]["title"]).split()
        last_stem = max((s for s in spine_stems if meta[s]["volume"] == v),
                        key=lambda s: (meta[s]["page_start"], meta[s]["page_end"], s))
        last = _title_text(meta[last_stem]["title"]).split()
        rng = f" · {first[0]} – {last[0]}" if first and last else ""
        return f"Volume {v}{rng}"

    vol_lis = "".join(
        f'<li><a href="{first_chunk_of_vol[v]}#{pack.article_anchor(first_stem_of_vol[v])}">'
        f"{_html.escape(_vol_label(v))}</a></li>"
        for v in sorted(first_chunk_of_vol))
    nav_extra = '<li><a href="index.xhtml">A–Z Index</a></li>'
    if contrib_files:
        nav_extra += f'<li><a href="{contrib_files[0]}">Contributors</a></li>'
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        '<head><meta charset="utf-8"/><title>Contents</title>'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n<body>\n'
        f'<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>{vol_lis}{nav_extra}</ol></nav>\n'
        '</body>\n</html>\n')
    open(os.path.join(oebps, "nav.xhtml"), "w", encoding="utf-8").write(nav)

    open(os.path.join(oebps, "titlepage.xhtml"), "w", encoding="utf-8").write(xhtml_doc(
        title,
        f'<div class="titlepage"><h1>{_html.escape(title)}</h1>'
        '<p>A Dictionary of Arts, Sciences, Literature and General Information</p>'
        f'<p>{len(spine_stems):,} articles · 28 volumes (1910–1911)</p>'
        '<p><a href="https://britannica11.org">britannica11.org</a></p></div>'))

    open(os.path.join(oebps, "style.css"), "w", encoding="utf-8").write(epub_css())

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

    manifest, spine = [], []
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

    manifest.append('<item id="azindex" href="index.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="azindex"/>')
    for letter, fname in letter_files:
        iid = "idx-" + fname[6:-6]
        manifest.append(f'<item id="{iid}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{iid}"/>')
    for fname in contrib_files:
        iid = "ctb-" + fname[13:-6]
        manifest.append(f'<item id="{iid}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{iid}"/>')
    for n, name in enumerate(sorted(seen_imgs)):
        manifest.append(f'<item id="img-{n}" href="images/{_html.escape(seen_imgs[name])}" '
                        f'media-type="{_media_type(unquote(seen_imgs[name]))}"/>')
    for n, name in enumerate(sorted(seen_math)):
        manifest.append(f'<item id="mpng-{n}" href="math/{name}" media-type="image/png"/>')
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
        '  </metadata>\n'
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
    ids_by_file = {}
    for f in sorted(os.listdir(oebps)):
        if not f.endswith(".xhtml"):
            continue
        content = open(os.path.join(oebps, f), encoding="utf-8").read()
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
        content = open(os.path.join(oebps, f), encoding="utf-8").read()
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
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-stage", action="store_true")
    args = ap.parse_args(argv)
    stems = list_stems(None if args.all else args.volume)
    if args.all:
        name, ident = "eb1911", "urn:britannica11:complete"
        title = "Encyclopædia Britannica, Eleventh Edition"
    else:
        vols = "-".join(f"{v:02d}" for v in sorted(args.volume))
        name = f"eb1911-vol{vols}"
        ident = f"urn:britannica11:vol-{vols}"
        title = f"Encyclopædia Britannica, Eleventh Edition — Volume {', '.join(map(str, sorted(args.volume)))}"
    suffix = "-kindle" if args.target == "kindle" else ""
    out = args.out or os.path.join(ROOT, f"{name}{suffix}.epub")
    build_epub(stems, out, target=args.target, title=title, ident=ident,
               images=args.images, keep_stage=args.keep_stage)


if __name__ == "__main__":
    main()
