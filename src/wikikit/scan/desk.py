"""The proofreading desk's data: out to the desk, and corrections back.

Two desks, one model: a page is proofread WHOLE, as one text — its running
head and body beside its scan, one check for the page (user, 2026-10-16: the
strips it was cut into for the model to read are not the proofreader's
concern), saved by `apply_pages`.  The LOCAL desk (EB1911's, `desk_serve`)
runs on the book's own site server, where the engine is: its rendered pane is
the site's viewer showing `page_article`, so it has no renderer of its own.  The REMOTE desk (the
Century's) is a page outside this engine (a claude.ai artifact with its own
store), so the engine meets it with files:

- OUT: `bundle` writes, for chosen supplied pages, each page WHOLE: its scan
  (deskewed), its running head and text as they will enter, copies of its
  figures, and `desk.json` — the pages, the render table the engine measured
  (`vocabulary.render_table`), and the book's notation for the palette.  The
  desk applies the table with `desk_render.js` and knows no markup of its own.
- BACK: `apply_pages` takes the desk's saved pages and gives each its text as
  the proofreader left it (`supplied.set_page_text`) — level 3 (proofread)
  once checked.
- PARITY: `parity` renders every reading — `parity pages`, every page — with
  desk_render.js (under Node) and with the engine's own chain, and reports any
  difference: the desk's view is a second renderer, tolerated only because it
  is held to the first.

    uv run python -m wikikit.scan.desk parity [pages]   # from the book's root
"""
from __future__ import annotations

import html
import re
import shutil
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from wikikit.corpora import current_corpus
from wikikit.pipeline.stages.preprocess import PARAGRAPH_ENDS_WITH_PAGE
from wikikit.scan import cut, supplied
from wikikit.scan.supplied import page_id
from wikikit.scan.transcribe import PARA
from wikikit.scan.vocabulary import IMAGE_SRC, _render, render_table

DESK_RENDER_JS = Path(__file__).resolve().parent / "desk_render.js"
#: A desk's text face, Junicode, whole (Peter S. Baker, OFL: its licence
#: beside it), copied beside a published desk as fonts/: polytonic Greek, the
#: transliterations' letters, true small capitals, marks placed (the Anthon's,
#: user's choice, 2026-10-18).  Never a font host's subsets: Google Fonts cuts
#: a face by script and leaves out the rarer combining marks (the breve of y̆),
#: so a marked letter came from another font, its mark unplaced.  The site's
#: Greek face (GFS Didot, `epub.build.GREEK_FACE`) goes beside it, for a desk
#: that keeps the site's Georgia (the Century's).
DESK_FONTS = Path(__file__).resolve().parent / "desk_fonts"
_LINK_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S)
#: A formula as desk_render.js leaves it for KaTeX (its LaTeX escaped, so no "<").
_TEX_RE = re.compile(r'<span class="tex">[^<]*</span>')
#: An anchor the engine makes FROM the content (a margin heading's
#: id="section-<its words>") is a link target, not the construct's look:
#: render_table leaves it out of the desk, so parity leaves it out of the engine.
_ANCHOR_RE = re.compile(r'\s+id="section-[^"]*"')
#: what a report says of a page the desk has just made proofread
NOW_PROOFREAD = " (now proofread)"


def keep_version(out: Path, volume: int, page: int) -> Path:
    """The supplied page as it is now, kept under `out`/history before the
    desk writes it: what an undo puts back."""
    from datetime import datetime, timezone
    keep = out / "history" / page_id(volume, page) / f"{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%S-%fZ}.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(supplied.path_for(volume, page).read_bytes())
    return keep


def saved_versions(out: Path, volume: int, page: int) -> list[Path]:
    """The page as it was before each of its desk saves, oldest first."""
    return sorted((out / "history" / page_id(volume, page)).glob("*.json"))


def page_label(volume: int, page: int) -> str:
    """What the desk calls a page when the book names no label of its own."""
    return f"Vol. {volume}, page {page}"


def notation() -> list:
    book = current_corpus()
    if not book.has_data("scan_notation.json"):
        return []
    return json.loads(book.data("scan_notation.json").read_text(encoding="utf-8"))["groups"]


def levelled_scan(payload: dict, scan: Image.Image) -> Image.Image:
    """The page's scan as the desk shows it: levelled by the page's recorded
    deskew, the angle its items' boxes were measured at."""
    return cut.deskew(scan.convert("L"), payload["provenance"]["angle"])


def page_notes(payload: dict) -> list[str]:
    """What the desk tells the proofreader of a page before they read it: the
    columns the model could not read, until the page is edited."""
    p = payload["provenance"]
    columns = {it.name: it.column for it in supplied.items_of(payload)}
    gaps = sorted({columns[n] for n in [*p.get("refused", []), *p.get("unread", {})]
                   if n in columns and n not in p.get("checked", {})})
    if not gaps or p.get("page_edited"):
        return []
    return [f"The model could not read part of column {', '.join(map(str, gaps))}: "
            "transcribe what is missing from the scan."]


def page_article(payload: dict, title: str, link=None) -> dict:
    """The page as it would enter, as an article the site's viewer shows: its
    text through the engine's chain for an article body (preprocess, elements,
    `render_article`) — the site's own renderer, not a copy of it.

    `link(body, volume, page) -> body`: the build's xref resolution for the
    article the page belongs to (`resolve_xrefs_post.Resolution`); without
    it, a reference shows as a link to nothing — the build would drop it (ALGEBRAIC
    FORMS's "see Gordan").  What the whole build adds beyond that is not
    here: contributors bound, the article cut at its headword."""
    from wikikit.db.models import SourcePage
    from wikikit.pipeline.stages.elements import ElementContext, process_elements
    from wikikit.pipeline.stages.preprocess import stream_with_keys
    from wikikit.render.article import render_article
    vol, pg = payload["volume"], payload["page_number"]
    stream, _, _ = stream_with_keys([SourcePage(volume=vol, page_number=pg,
                                                wikitext=payload["raw_text"])], vol)
    body = process_elements(stream, ElementContext(volume=vol))
    article = {"id": page_id(vol, pg), "title": title, "volume": vol,
               "body": link(body, vol, pg) if link else body}
    html = render_article(article, target="site")
    # An article opens with a paragraph; a page need not.  One that carries
    # on the paragraph of the page before is shown so — no opening paragraph,
    # no indent — as it will run on in its article (`stream_with_keys`): the
    # proofreader sees what the reading's ¶ says (SOLUTION p. 375, 2026-10-15)
    page_text = payload["raw_text"].split("</noinclude>", 1)[-1].lstrip()
    if not page_text.startswith(PARAGRAPH_ENDS_WITH_PAGE):
        # (the desk's page sets its first block unindented: data-runs-on)
        html = html.replace('<div class="body-text"><p>', '<div class="body-text" data-runs-on>', 1)
    article["rendered_html"] = html
    return article


def bundle(pages: list[tuple[int, int]], scans: dict[tuple[int, int], Image.Image],
           out: Path, flags=None, label=None, people=None) -> Path:
    """Write the desk's files for these supplied pages into `out`.

    `flags`: the book's own checks, `payload -> {strip name: [{"text",
    "start", "end", "reason"}]}` — readings worth a second look, found from
    what only the book knows (a dictionary's order, its notation).  The desk
    marks each and says why; the engine does not judge them.

    `label`: `(volume, page) -> str`, what the person at the desk calls the
    page — the PRINTED page, where a book's file numbers are not the print's
    (EB1911's are Wikisource's scan indices).  None: the file's number.

    `people`: everyone a page may be given to at the desk — each page shows
    to its owner (`supplied.owner`).  None: the owners of these pages.

    The desk's fonts go beside them, as fonts/ (`DESK_FONTS`, and the site's
    Greek face)."""
    from wikikit.epub.build import GREEK_FACE
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(DESK_FONTS, out / "fonts", dirs_exist_ok=True)
    for font in GREEK_FACE:
        shutil.copyfile(font, out / "fonts" / Path(font).name)
    desk_pages = []
    for vol, pg in pages:
        payload = supplied.read(vol, pg)
        pid = page_id(vol, pg)
        level = levelled_scan(payload, scans[(vol, pg)])
        img = f"pages/{pid}.jpg"
        (out / img).parent.mkdir(parents=True, exist_ok=True)
        level.save(out / img, quality=85, optimize=True)
        head, body, _ = supplied.page_parts(payload["raw_text"])
        desk_pages.append({"id": pid, "volume": vol, "page": pg,
                           "label": (label or page_label)(vol, pg), "img": img,
                           "head": head, "text": body,
                           "figures": figure_copies(payload, level, out),
                           # the book's checks, each found in the page's text by its words
                           "flags": [{"text": f["text"], "reason": f["reason"]}
                                     for fs in (flags(payload) if flags else {}).values() for f in fs],
                           "notes": page_notes(payload), "checked": supplied.level(payload) >= 3,
                           "owner": supplied.owner(payload)})
    (out / "desk.json").write_text(json.dumps({
        "table": render_table(), "notation": notation(), "pages": desk_pages,
        "people": sorted(set(people or ()) | {d["owner"] for d in desk_pages if d["owner"]})},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return out / "desk.json"


def figure_copies(payload: dict, level: Image.Image, out: Path) -> dict[str, str]:
    """The page's figures, cut from the deskewed scan for the desk to show in
    the page as it will enter -> {file name as the page names it: path
    relative to `out`}."""
    p = payload["provenance"]
    pid = page_id(payload["volume"], payload["page_number"])
    copies = {}
    for it in supplied.items_of(payload):
        name = p["figure_files"].get(it.name) if it.kind == "figure" else None
        if name:
            rel = f"figures/{pid}/{it.name}.png"
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            level.crop(it.box).save(out / rel, optimize=True)
            copies[name] = rel
    return copies


def apply_pages(rows: dict[str, dict]) -> list[str]:
    """The desk's saved pages -> the supplied pages.  `rows` maps a page id to
    {"head", "text", "status", "owner", "updated_by", "updated_at"} as the
    desk stores them: each page takes its text as the person left it, and is
    proofread once they checked it (`supplied.set_page_text`); a page given to
    someone else at the desk is theirs from now on (`owner`).  Returns one
    line per page changed."""
    report = []
    for pid, row in sorted(rows.items()):
        vol, pg = int(pid[1:3]), int(pid[5:9])
        if page_id(vol, pg) != pid:
            raise KeyError(f"not a page id: {pid!r}")
        payload = supplied.read(vol, pg)
        before = supplied.level(payload)
        head, body, _ = supplied.page_parts(payload["raw_text"])
        new_head, new_body = row.get("head", head), row.get("text", body)
        checked = row.get("status") == "checked"
        edited = (new_head, new_body) != (head, body)
        given = row.get("owner") or supplied.owner(payload)
        regiven = given != supplied.owner(payload)
        if not edited and checked == (before >= 3) and not regiven:
            continue
        if regiven:
            p = payload["provenance"]
            p["owner"] = given
            if given == p.get("fetched_by"):
                del p["owner"]
        if edited or checked != (before >= 3):
            supplied.set_page_text(payload, new_head, new_body, checked,
                                   by=row.get("updated_by"), at=row.get("updated_at"))
        supplied.write(payload)
        after = supplied.level(payload)
        report.append(f"vol {vol} p{pg}: {'corrected' if edited else 'text unchanged'}, level {after}"
                      + (NOW_PROOFREAD if after > before else "")
                      + (f", given to {given}" if regiven else ""))
    return report


def desk_render(texts: list[str], table: dict) -> list[dict]:
    """desk_render.js on each text, under Node: what the desk shows."""
    runner = ("const r=require(process.argv[1]);let s='';process.stdin.on('data',d=>s+=d);"
              "process.stdin.on('end',()=>{const a=JSON.parse(s);"
              "process.stdout.write(JSON.stringify(a.texts.map(t=>r.render(t,a.table,{}))))});")
    done = subprocess.run(["node", "-e", runner, str(DESK_RENDER_JS)],
                          input=json.dumps({"texts": texts, "table": table}),
                          capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(done.stdout)


def _strip_signal(text: str) -> str:
    """A reading without its joining signals: the ¶ line (between strips, not
    text) and a trailing hyphen awaiting the next strip's word."""
    t = text.strip()
    if t.startswith(PARA):
        t = t[len(PARA):].lstrip()
    return t


def parity(texts: list[str]) -> list[tuple[str, str, str]]:
    """(text, desk html, engine html) for every reading the desk would render
    differently from the engine — or would flag as a leak."""
    table = render_table()
    cleaned = [_strip_signal(t) for t in texts]
    shown = desk_render(cleaned, table)
    out = []
    for text, d in zip(cleaned, shown):
        # A link is not typography: links are resolved at export, from the
        # whole corpus, and the desk shows the text a reader sees.
        engine = _ANCHOR_RE.sub("", _LINK_RE.sub(r"\1", _render(text))).strip()
        # A formula is typeset by the reader — KaTeX in the viewer and on the
        # desk — so both sides hold the engine's stand-in for it.  Both sides
        # are compared as characters: &nbsp; and \xa0 are one space.
        # A figure's image is the site's file there and the desk's copy here.
        desk = html.unescape(IMAGE_SRC.sub('src=""', _TEX_RE.sub(table["math"][0], d["html"])))
        engine = html.unescape(IMAGE_SRC.sub('src=""', engine))
        if d["leaks"] or desk != engine:
            out.append((text, desk, engine))
    return out


def _book_payloads() -> list[dict]:
    from wikikit.corpora import book_root
    from wikikit.source_pages import SUPPLIED_DIR
    return [json.loads(f.read_text(encoding="utf-8"))
            for f in sorted((book_root() / SUPPLIED_DIR).glob("vol_*/*.json"))]


def _book_readings() -> list[str]:
    texts = []
    for payload in _book_payloads():
        p = payload["provenance"]
        heads = {i["name"] for i in p["items"] if i["kind"] == "head"}
        texts += [t for n, t in p["readings"].items() if n not in heads and t]
    return texts


def _book_pages() -> list[str]:
    """Every supplied page's body, as the desk shows it to proofread."""
    return [supplied.page_parts(payload["raw_text"])[1] for payload in _book_payloads()]


def main() -> int:
    if sys.argv[1:] not in (["parity"], ["parity", "pages"]):
        print(__doc__)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    texts = _book_pages() if sys.argv[2:] == ["pages"] else _book_readings()
    bad = parity(texts)
    for text, desk, engine in bad:
        print(f"DIFFER  {text[:90]!r}\n  desk   {desk[:160]!r}\n  engine {engine[:160]!r}")
    print(f"parity: {len(texts) - len(bad)} of {len(texts)} readings render the same on the desk and in the engine")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
