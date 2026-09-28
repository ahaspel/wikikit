"""The book's pages beyond its articles — front matter and a guide — and the
extraction that turns them into book bodies.

WHICH pages a book has is the book's (``Corpus.ancillary``, returning an
``Ancillary``); EB1911's — its introduction, the 1910 Editorial Preface, the
Historical Preface and the Reader's Guide — are listed in
eb1911/front_matter.py.  The EPUB and the dictionary both read
them through ``book_pages()``.  This module keeps what any book's pages need:

  * ``text_page_html`` — a plain-text page (blank-line paragraphs, ``#`` comment
    lines, ``[Header.]`` shoulder headings) rendered to HTML;
  * ``_extract`` — one content div lifted out of one of OUR static site pages.

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
from dataclasses import dataclass, field

from wikikit.corpora import brand, current_corpus
from wikikit.util.strings import section_slug
import xml.etree.ElementTree as ET

import html5lib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
        return brand("site") + href
    return brand("site") + "/" + href


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
                if a.get("href") in ("#", brand("site") + "/#", None):
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


@dataclass
class Ancillary:
    """A book's pages beyond its articles, as ``Corpus.ancillary`` returns them.

    front    [(file, nav title, body HTML)] — the Introduction group, in order.
    guide    [(file, title, body HTML, part | None)] — a guide tree: its HUB is
             the first row (part None); a part's first row is that part's page,
             and its other rows are the chapters under it.  Empty = no guide.
    images   {bundled basename: source path} — images the guide bodies use.
    sources  the files these pages are built FROM, for provenance hashes.
    """
    front: list = field(default_factory=list)
    guide: list = field(default_factory=list)
    images: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)


def book_pages() -> Ancillary:
    """The current book's pages beyond its articles; none when it names none."""
    hook = current_corpus().ancillary
    return hook() if hook is not None else Ancillary()


def text_page_html(path, title):
    """A plain-text page → body HTML, or None while there is nothing to show.

    The file is REQUIRED: an empty one (or all comments) is a page not yet
    written, and omitted; a missing one is an error, never a quietly absent page.

    Notation: plain text, blank-line paragraphs, ``#`` comment lines; a paragraph
    opening with ``[Some Header.]`` renders the bracketed text as a shoulder
    heading (the About page's device), anchored by its slug."""
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
        slug = section_slug(head)
        return (f'<p><span class="shoulder-heading" id="intro-{slug}">'
                f"{_h.escape(head)}</span> {_h.escape(p[m.end():])}</p>")

    return f"<h1>{_h.escape(title)}</h1>" + "".join(_para(p) for p in paras)
