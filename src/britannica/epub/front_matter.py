"""Front-matter pages for the EPUB.

Three pages, in book order after the title page:

  * ``introduction.xhtml`` — the editor's own introduction, from
    ``docs/introduction.txt`` (plain text, blank-line paragraphs; ``#`` lines are
    comments).  Page omitted while the file is absent or has no content lines.
  * ``preface.xhtml`` — the 1910 Editorial Preface, extracted from the static site
    page ``tools/viewer/preface.html`` (built once from raw Wikisource by
    ``tools/viewer/build_preface.py``; frozen content).
  * ``index-preface.xhtml`` — the vol-29 Preface to the Index, extracted from
    ``tools/viewer/ancillary-index-preface.html``.

The site pages are OUR static renders — extraction takes the one content div and
adapts it to the book context: ``on*`` script attributes dropped (an EPUB content
doc with script hooks must declare itself scripted), footnote back-links retargeted
from JS to real ``#fnref-N`` anchors, site-relative hrefs made absolute to
britannica11.org (the packer's policy for content the book doesn't carry), and the
leading wiki-indent colons of the signature lines stripped.  The result is fed
through build.to_xhtml_body for XHTML conformance like every other baked body.
"""
import os
import re
import xml.etree.ElementTree as ET

import html5lib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SITE = "https://britannica11.org"

_COLON_RUN_RE = re.compile(r"^\s*:+\s*")
DROPPED_HREFS = []          # malformed source hrefs dropped at extraction (logged)


def _find_div(root, cls):
    for el in root.iter("div"):
        if cls in (el.get("class") or "").split():
            return el
    return None


def _absolutize(href):
    if not href or href.startswith("#") or "://" in href or href.startswith("mailto:"):
        return href
    if href.split("#")[0].endswith(".xhtml"):     # already retargeted book-internal
        return href
    if href.startswith("/"):
        return SITE + href
    return SITE + "/" + href


def _adapt(el):
    """In-place: drop script-hook attrs, retarget footnote back-links, absolutize
    site links, strip leading wiki-indent colons."""
    for node in el.iter():
        for k in list(node.attrib):
            if k.lower().startswith("on"):
                del node.attrib[k]
        if node.tag == "a" and node.get("href"):
            h = node.get("href")
            if "<" in h or '"' in h:
                # Source-side generator bug (a nested tag mangled into the URL,
                # live on the site too — build_readers_guide.py, queued).  A
                # broken href is not a link; keep the text.
                del node.attrib["href"]
                DROPPED_HREFS.append(h)
            else:
                node.set("href", _absolutize(h))
    # Footnote back-links: the site page scrolls via JS from a bare "#" href; the
    # book links back to the noteref anchor.
    for li in el.iter("li"):
        lid = li.get("id") or ""
        if lid.startswith("fn-"):
            for a in li.iter("a"):
                if a.get("href") in ("#", SITE + "/#", None):
                    a.set("href", "#fnref-" + lid[3:])
    for p in el.iter("p"):
        if p.text and _COLON_RUN_RE.match(p.text) and p.text.strip().startswith(":"):
            p.text = _COLON_RUN_RE.sub("", p.text)
        for child in p:
            if child.tail and child.tail.strip().startswith(":"):
                child.tail = re.sub(r"\s*:+\s*", " ", child.tail, count=1)


def _inner_html(el):
    out = [el.text or ""]
    for child in el:
        out.append(ET.tostring(child, encoding="unicode", method="html"))
    return "".join(out)


def _extract(path, cls):
    doc = html5lib.parse(open(path, encoding="utf-8").read(),
                         treebuilder="etree", namespaceHTMLElements=False)
    body = _find_div(doc, cls)
    if body is None:
        raise RuntimeError(f"{path}: no <div class={cls!r}> content block")
    _adapt(body)
    return _inner_html(body)


_SHOULDER_RE = re.compile(r"^\[([^\]]+)\]\s*")


def introduction_html():
    """docs/introduction.txt → body HTML, or None while there is nothing to show.

    Notation: plain text, blank-line paragraphs, ``#`` comment lines; a paragraph
    opening with ``[Some Header.]`` renders the bracketed text as a shoulder
    heading (the About page's device), anchored by its slug."""
    path = os.path.join(ROOT, "docs", "introduction.txt")
    if not os.path.exists(path):
        return None
    lines = [l for l in open(path, encoding="utf-8").read().splitlines()
             if not l.lstrip().startswith("#")]
    paras = [p.strip() for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
    if not paras:
        return None
    import html as _h

    def _para(p):
        m = _SHOULDER_RE.match(p)
        if not m:
            return f"<p>{_h.escape(p)}</p>"
        head = m.group(1).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", head.lower()).strip("-")
        return (f'<p><span class="shoulder-heading" id="intro-{slug}">'
                f"{_h.escape(head)}</span> {_h.escape(p[m.end():])}</p>")

    return "<h1>Introduction</h1>" + "".join(_para(p) for p in paras)


def preface_html():
    body = _extract(os.path.join(ROOT, "tools", "viewer", "preface.html"),
                    "preface-body")
    return ("<h1>Editorial Preface</h1>"
            '<p class="fm-meta"><i>By Hugh Chisholm · London, December 10, 1910</i></p>'
            + body)


def index_preface_html():
    body = _extract(os.path.join(ROOT, "tools", "viewer",
                                 "ancillary-index-preface.html"), "body")
    return "<h1>Preface to the Index</h1>" + body


def pages():
    """[(fname, nav title, body HTML)] in book order; introduction only when written."""
    out = []
    intro = introduction_html()
    if intro:
        out.append(("introduction.xhtml", "Introduction", intro))
    out.append(("preface.xhtml", "Editorial Preface", preface_html()))
    out.append(("index-preface.xhtml", "Preface to the Index", index_preface_html()))
    return out
