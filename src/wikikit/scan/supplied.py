"""A page transcribed from its scan, as a supplied page the book commits.

The file has the shape of a fetched page (`wikikit.source_pages`), so it enters
by the same door, plus `source: "scan"` and its provenance: the deskew, every
item's box, every reading, the figures' file names, the model, the date, and
the items the API refused.  The READINGS are the source a proofreader corrects;
`raw_text` is assembled from them (`transcribe.assemble_page`) and re-assembled
whenever one changes — never edited on its own.

Figure images are not committed: they are crops of the scan, regenerated into
the book's image store from the scan and the boxes recorded here
(`materialize_figures`).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from PIL import Image

from wikikit.corpora import book_root, current_corpus
from wikikit.image_assets import local_image_filename
from wikikit.scan import carry, cut
from wikikit.scan.cut import Item
from wikikit.scan.layout import layout_for
from wikikit.scan.model import MODEL, Refused, metered
from wikikit.scan.page import cut_page
from wikikit.scan.transcribe import Unread, assemble_page, captions, read_item
from wikikit.source_pages import SUPPLIED_DIR, fetched_text, page_filename, volume_dir


def page_id(volume: int, page: int) -> str:
    """A page's id wherever pages are tracked (the desk, a batch run)."""
    return f"v{volume:02d}-p{page:04d}"


def figure_file(volume: int, page: int, item_name: str) -> str:
    """A figure's image name: the book's key, volume, page and item — stable
    while the page's cut is."""
    return f"{current_corpus().key} v{volume:02d} p{page:04d} {item_name}.png"


def path_for(volume: int, page: int) -> Path:
    return book_root() / volume_dir(volume, SUPPLIED_DIR) / page_filename(volume, page)


def transcribe_page(img: Image.Image, volume: int, page: int, scan: str,
                    replaces: str | None = None) -> dict:
    """Scan -> a supplied page: cut, read every text item, assemble.  `scan`
    names the image it came from (e.g. an Internet Archive item and leaf).  A
    refused item is recorded and its reading left empty — the page is still
    written, and the refusal is listed for a person to transcribe.  The page
    is cut as the book declares its layout (`wikikit.scan.layout`).
    `replaces`: as `build_payload`."""
    with metered() as finding:
        cp = cut_page(img, n_columns=layout_for(volume, page).columns)
    caps = captions(cp.plan.items)
    readings, refused, unread, rejected = {}, [], {}, {}
    with metered() as reading:
        for it in cp.plan.items:
            if it.kind == "figure":
                continue
            try:
                readings[it.name] = read_item(cp.level, it, is_caption=it.name in caps)
            except Refused:
                readings[it.name], refused = "", refused + [it.name]
            except Unread as e:
                readings[it.name], unread[it.name] = "", e.reason
                if e.answer:
                    rejected[it.name] = e.answer
    return build_payload(volume, page, scan, cp.level, cp.plan, cp.figures, readings,
                         refused, unread, {"figures": finding.record(), "text": reading.record()},
                         rejected, replaces, cp.across)


def build_payload(volume: int, page: int, scan: str, level: Image.Image, plan: cut.Plan,
                  figures: list[dict], readings: dict, refused: list, unread: dict,
                  usage: dict, rejected: dict | None = None,
                  replaces: str | None = None, across: list | None = None) -> dict:
    """The supplied page from its cut and readings — the one builder, whether
    the page was read now (`transcribe_page`) or in batches
    (`wikikit.scan.batch`).  `replaces`: why the fetched page, proofread or
    not, was found bad as data — the page then stands in for it at any level
    (`wikikit.source_pages.prefer`)."""
    files = {it.name: figure_file(volume, page, it.name)
             for it in plan.items if it.kind == "figure"}
    payload = {
        "volume": volume, "page_number": page, "source": "scan", "title": scan,
        "provenance": {
            "scan": scan, "model": MODEL, "transcribed": date.today().isoformat(),
            "angle": plan.angle,
            "column_width": max(b - a for a, b in cut.page_columns(level)),
            "items": [{"name": it.name, "kind": it.kind, "column": it.column, "box": list(it.box)}
                      for it in plan.items],
            "readings": readings, "figure_files": files, "figures": figures,
            # the type set across the columns, as located: with `figures`, all
            # a re-cut needs to cut the page again the same way
            "across": across or [],
            "refused": refused, "unread": unread,
            # what the model answered for an unread item, kept for whoever
            # settles it — never page text
            "rejected": rejected or {},
            "disagreements": {str(k): list(v) for k, v in plan.disagreements.items()},
            # what the page cost, as the API counted it: finding its figures
            # and reading its text
            "usage": usage,
            # its captions are the cutter's caption items, not guessed by
            # position (`transcribe.captions`)
            "captions_marked": True,
        },
    }
    if replaces:
        payload["provenance"]["replaces"] = replaces
    carry_from_fetched(payload)
    reassemble(payload)
    return payload


def carry_from_fetched(payload: dict) -> None:
    """Record the editorial markup of the fetched page this one replaces —
    its section tags and links (`wikikit.scan.carry`) — to be laid on at
    every assembly.  A page the book never fetched carries nothing."""
    fetched = fetched_text(payload["volume"], payload["page_number"])
    if fetched is not None:
        payload["provenance"]["carried"] = carry.marks(fetched)


def items_of(payload: dict) -> list[Item]:
    return [Item(i["kind"], tuple(i["box"]), i["column"], i["name"])
            for i in payload["provenance"]["items"]]


def reassemble(payload: dict) -> dict:
    """`raw_text` from the readings — after transcription, and after every
    correction a proofreader makes to a reading."""
    p = payload["provenance"]
    raw = assemble_page(items_of(payload), p["readings"], p["figure_files"],
                        p["column_width"], level=level(payload),
                        legacy_captions=not p.get("captions_marked"))
    if p.get("carried") is not None:
        raw, unplaced = carry.lay_on(raw, p["carried"])
        p["carried_unplaced"] = unplaced          # reported, never dropped silently
    payload["raw_text"] = raw
    return payload


def level(payload: dict) -> int:
    """Wikisource's proofreading level for the page: 3 (proofread) once a
    person has checked every text item; 2 (problematic: Wikisource's word for
    a page with text missing) while any text item the model refused or left
    unread is unchecked — the reader is told the page is incomplete; else 1
    (not proofread)."""
    p = payload["provenance"]
    checked = p.get("checked", {})
    items = [it for it in items_of(payload) if it.kind != "figure"]
    texts = [it.name for it in items]
    if texts and all(n in checked for n in texts):
        return 3
    body = {it.name for it in items if it.kind != "head"}       # a lost running head loses no text
    gaps = [n for n in [*p.get("refused", []), *p.get("unread", {})] if n in body and n not in checked]
    return 2 if gaps else 1


def write(payload: dict) -> Path:
    path = path_for(payload["volume"], payload["page_number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def read(volume: int, page: int) -> dict:
    return json.loads(path_for(volume, page).read_text(encoding="utf-8"))


def materialize_figures(payload: dict, img: Image.Image) -> list[Path]:
    """The page's figure crops, regenerated from its scan into the book's image
    store under the names its wikitext links to."""
    p = payload["provenance"]
    level = cut.deskew(img, p["angle"])
    store = book_root() / current_corpus().images()
    store.mkdir(parents=True, exist_ok=True)
    out = []
    for it in items_of(payload):
        if it.kind == "figure":
            dest = store / local_image_filename(p["figure_files"][it.name])
            level.crop(it.box).save(dest)
            out.append(dest)
    return out
