"""The site's served files: `page(name)` makes one, `assemble_site()` all.

    uv run python -m wikikit.site.build          # writes <derived>/site/

A served name is either an ENGINE page (`pages/`, its `@@name@@` tokens filled
from the book's profile) or a file in the book's `site_dir`, copied verbatim.
The two sets may not overlap: a book page shadowing an engine page would be a
fork of the viewer, not a customisation of it.
"""
from __future__ import annotations

import html
import json
import re
import sys
from html.entities import codepoint2name
from pathlib import Path

from wikikit.corpora import book_root, current_corpus

PAGES = Path(__file__).resolve().parent / "pages"

#: The book's names a page may ask for — the only tokens `fill` accepts.
FIELDS = frozenset({"work_name", "edition_label", "monogram", "search_key",
                    "search_index", "analytics_code", "volume_scans_url",
                    "contributor_example", "root"})

#: What the engine's pages link to that only the book can supply.
BOOK_PAGES = frozenset({"home.html", "ancillary.html", "download.html", "topics.html",
                        "favicon.svg"})

#: The kinds of file a book's site folder serves; its builders stay behind.
SERVED = frozenset({".html", ".js", ".css", ".svg", ".jpg", ".png"})

#: The one served file neither side writes by hand — `wikikit.site.stamp`
#: generates it from the corpus, into the assembled folder.
STAMP = "build-stamp.js"

# `@@name@@` in HTML text or an attribute; `@@name|js@@` inside a JS string.
_TOKEN = re.compile(r"@@([a-z_]+)(\|js)?@@")


def out_dir() -> Path:
    """Where the assembled site goes — what deploy uploads."""
    return book_root() / current_corpus().derived("site")


def _html(value: str) -> str:
    # The pages spell non-ASCII as named entities (`&aelig;`), and a filled
    # value follows suit, so a page's bytes never depend on its charset.
    text = html.escape(value)
    return "".join(ch if ord(ch) < 128
                   else f"&{codepoint2name[ord(ch)]};" if ord(ch) in codepoint2name
                   else f"&#{ord(ch)};" for ch in text)


def _js(value: str) -> str:
    # The body of a double-quoted JS string, safe inside an inline <script>.
    return json.dumps(value)[1:-1].replace("</", "<\\/")


def fill(text: str, source: str) -> str:
    """One engine page with the book's names in place of its tokens.

    Strict both ways: a token naming no site field, a field the book lacks
    (`Corpus.need`), or a stray `@@` raises — never a hole in a served page."""
    corpus = current_corpus()

    def one(m: re.Match) -> str:
        name, js = m.groups()
        if name not in FIELDS:
            raise KeyError(f"{source}: @@{name}@@ is not a site field")
        value = corpus.need(name)
        return _js(value) if js else _html(value)

    if "@@" in _TOKEN.sub("", text):
        raise ValueError(f"{source}: a malformed @@…@@ token")
    return _TOKEN.sub(one, text)


def _sources() -> dict[str, Path]:
    engine = {p.name: p for p in PAGES.iterdir() if p.is_file()}
    folder = book_root() / current_corpus().need("site_dir")
    book = {p.name: p for p in folder.iterdir()
            if p.is_file() and p.suffix in SERVED}
    clash = sorted(engine.keys() & book.keys())
    if clash:
        raise ValueError(f"{folder} shadows the engine's pages: {clash}")
    missing = sorted(BOOK_PAGES - book.keys())
    if missing:
        raise FileNotFoundError(f"{folder} lacks {missing}, which the engine's pages link to")
    return engine | book


def _bytes(src: Path) -> bytes:
    data = src.read_bytes()
    if src.parent == PAGES:
        data = fill(data.decode("utf-8"), src.name).encode("utf-8")
    return data


def page(name: str) -> bytes | None:
    """The served bytes of one site file, or None when the site has no such file."""
    if name == STAMP:
        path = out_dir() / STAMP
        return path.read_bytes() if path.is_file() else None
    src = _sources().get(name)
    return None if src is None else _bytes(src)


def assemble_site() -> Path:
    """Write every served file to `out_dir()`, and remove any the site no
    longer has (the stamp excepted: it is written separately, from the corpus)."""
    out = out_dir()
    out.mkdir(parents=True, exist_ok=True)
    sources = _sources()
    for old in out.iterdir():
        if old.name not in sources and old.name != STAMP:
            old.unlink()
    for name, src in sorted(sources.items()):
        (out / name).write_bytes(_bytes(src))
    return out


def main() -> int:
    out = assemble_site()
    print(f"site: {sum(1 for _ in out.iterdir())} files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
