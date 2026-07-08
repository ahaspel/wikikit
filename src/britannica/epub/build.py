"""Build an EPUB from a set of article stems (thin-slice first cut).

    python -m britannica.epub.build            # builds the thin slice

Pipeline per article: render_article → XML-well-formed XHTML (ElementTree, so void elements
self-close and it validates as XML) → bundle referenced images → wrap in an XHTML doc.  Then
assemble mimetype / META-INF/container.xml / OPF (manifest+spine) / nav.xhtml over a
volume→page-ordered spine, and zip.  Navigation is a volume browse for now (the topic TOC and
EPUB-target content policies — MathML, internal links, epub footnotes — come next).
"""
import html as _html
import os
import re
import shutil
import zipfile
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import html5lib

from britannica.render.article import render_article

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
IMAGES_SRC = os.path.join(ROOT, "data", "derived", "images")
_MODIFIED = "2026-07-07T00:00:00Z"        # fixed for reproducible builds
_IMG_SRC_RE = re.compile(r'src="/data/derived/images/([^"]+)"')


def epub_css():
    """The EPUB stylesheet — content typography only, single-column, reader-controlled width."""
    return open(os.path.join(os.path.dirname(__file__), "epub.css"), encoding="utf-8").read()


def to_xhtml_body(html_str):
    """HTML5 render → XML-well-formed XHTML fragment (void elements self-closed, entities as chars)."""
    frag = html5lib.parseFragment(html_str, treebuilder="etree", namespaceHTMLElements=False)
    out = [_html.escape(frag.text, quote=False)] if frag.text else []
    for child in frag:
        out.append(ET.tostring(child, encoding="unicode", method="xml"))
    return "".join(out)


def bundle_images(body, dst_dir, seen):
    """Copy every referenced image into the EPUB and rewrite its src to a relative path."""
    def repl(m):
        enc = m.group(1)
        name = unquote(enc)                      # commons_url percent-encoded the disk name
        src = os.path.join(IMAGES_SRC, name)
        if os.path.exists(src):
            if name not in seen:
                shutil.copyfile(src, os.path.join(dst_dir, name))
                seen.add(name)
            return f'src="images/{enc}"'
        return m.group(0)                         # missing on disk: leave as-is
    return _IMG_SRC_RE.sub(repl, body)


def xhtml_doc(title, body):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        f"<head>\n<meta charset=\"utf-8\"/>\n<title>{_html.escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".svg": "image/svg+xml"}


def build_epub(stems, input_dir, out_path):
    import json
    arts = []
    for stem in stems:
        a = json.load(open(os.path.join(input_dir, stem + ".input.json"), encoding="utf-8"))
        arts.append((stem, a))
    # spine order: volume, then page
    arts.sort(key=lambda sa: (sa[1].get("volume", 0), sa[1].get("page_start", 0)))

    stage = out_path + ".stage"
    if os.path.exists(stage):
        shutil.rmtree(stage)
    oebps = os.path.join(stage, "OEBPS")
    imgdir = os.path.join(oebps, "images")
    os.makedirs(imgdir)
    os.makedirs(os.path.join(stage, "META-INF"))

    seen_imgs = set()
    manifest, spine, nav_by_vol = [], [], {}
    for stem, a in arts:
        body = bundle_images(to_xhtml_body(render_article(a, target="epub")), imgdir, seen_imgs)
        title = a.get("title") or stem
        open(os.path.join(oebps, stem + ".xhtml"), "w", encoding="utf-8").write(xhtml_doc(title, body))
        manifest.append(f'<item id="{stem}" href="{stem}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{stem}"/>')
        nav_by_vol.setdefault(a.get("volume", "?"), []).append((stem, title))

    for name in sorted(seen_imgs):
        ext = os.path.splitext(name)[1].lower()
        manifest.append(f'<item id="img-{len(manifest)}" href="images/{name}" media-type="{_MEDIA.get(ext, "image/png")}"/>')

    open(os.path.join(oebps, "style.css"), "w", encoding="utf-8").write(epub_css())
    manifest.append('<item id="css" href="style.css" media-type="text/css"/>')

    # nav.xhtml — volume browse (topic TOC comes next)
    vols = "".join(
        f'<li><span>Volume {v}</span><ol>'
        + "".join(f'<li><a href="{s}.xhtml">{_html.escape(t)}</a></li>' for s, t in items)
        + "</ol></li>"
        for v, items in sorted(nav_by_vol.items(), key=lambda kv: str(kv[0]))
    )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
        '<head><meta charset="utf-8"/><title>Contents</title></head>\n<body>\n'
        f'<nav epub:type="toc" id="toc"><h1>Browse by Volume</h1><ol>{vols}</ol></nav>\n'
        '</body>\n</html>\n'
    )
    open(os.path.join(oebps, "nav.xhtml"), "w", encoding="utf-8").write(nav)
    manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        '    <dc:identifier id="pub-id">urn:britannica11:thin-slice</dc:identifier>\n'
        '    <dc:title>Encyclopædia Britannica, 11th Edition — thin slice</dc:title>\n'
        '    <dc:language>en</dc:language>\n'
        f'    <meta property="dcterms:modified">{_MODIFIED}</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n    ' + "\n    ".join(manifest) + '\n  </manifest>\n'
        '  <spine>\n    ' + "\n    ".join(spine) + '\n  </spine>\n'
        '</package>\n'
    )
    open(os.path.join(oebps, "content.opf"), "w", encoding="utf-8").write(opf)
    open(os.path.join(stage, "META-INF", "container.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>\n'
        '</container>\n')

    # zip: mimetype first, stored (uncompressed); everything else deflated
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        for base, _dirs, files in os.walk(stage):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, stage).replace("\\", "/"))
    shutil.rmtree(stage)
    return out_path, len(arts), len(seen_imgs)


if __name__ == "__main__":
    STEMS = ["01-0032-a-A", "01-0036-s5-ABACUS", "01-0042-s5-ABBEY", "01-0127-s3-ACACIA",
             "02-0723-s2-ARTHUR", "26-0933-s2-THUCYDIDES"]
    out = os.path.join(ROOT, "britannica11-thin-slice.epub")
    path, n, imgs = build_epub(STEMS, os.path.join(ROOT, "tests", "snapshots", "render"), out)
    print(f"built {path}\n  {n} articles, {imgs} images bundled")
