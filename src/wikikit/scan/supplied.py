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
from wikikit.scan import cut
from wikikit.scan.cut import Item
from wikikit.scan.model import MODEL, Refused
from wikikit.scan.page import cut_page
from wikikit.scan.transcribe import assemble_page, captions, read_item
from wikikit.source_pages import SUPPLIED_DIR, page_filename, volume_dir


def figure_file(volume: int, page: int, item_name: str) -> str:
    """A figure's image name: the book's key, volume, page and item — stable
    while the page's cut is."""
    return f"{current_corpus().key} v{volume:02d} p{page:04d} {item_name}.png"


def path_for(volume: int, page: int) -> Path:
    return book_root() / volume_dir(volume, SUPPLIED_DIR) / page_filename(volume, page)


def transcribe_page(img: Image.Image, volume: int, page: int, scan: str) -> dict:
    """Scan -> a supplied page: cut, read every text item, assemble.  `scan`
    names the image it came from (e.g. an Internet Archive item and leaf).  A
    refused item is recorded and its reading left empty — the page is still
    written, and the refusal is listed for a person to transcribe."""
    cp = cut_page(img)
    caps = captions(cp.plan.items)
    readings, refused = {}, []
    for it in cp.plan.items:
        if it.kind == "figure":
            continue
        try:
            readings[it.name] = read_item(cp.level, it, is_caption=it.name in caps)
        except Refused:
            readings[it.name], refused = "", refused + [it.name]
    files = {it.name: figure_file(volume, page, it.name)
             for it in cp.plan.items if it.kind == "figure"}
    payload = {
        "volume": volume, "page_number": page, "source": "scan", "title": scan,
        "provenance": {
            "scan": scan, "model": MODEL, "transcribed": date.today().isoformat(),
            "angle": cp.plan.angle,
            "column_width": max(b - a for a, b in cut.page_columns(cp.level)),
            "items": [{"name": it.name, "kind": it.kind, "column": it.column, "box": list(it.box)}
                      for it in cp.plan.items],
            "readings": readings, "figure_files": files, "figures": cp.figures,
            "refused": refused,
            "disagreements": {str(k): list(v) for k, v in cp.plan.disagreements.items()},
        },
    }
    reassemble(payload)
    return payload


def items_of(payload: dict) -> list[Item]:
    return [Item(i["kind"], tuple(i["box"]), i["column"], i["name"])
            for i in payload["provenance"]["items"]]


def reassemble(payload: dict) -> dict:
    """`raw_text` from the readings — after transcription, and after every
    correction a proofreader makes to a reading."""
    p = payload["provenance"]
    payload["raw_text"] = assemble_page(items_of(payload), p["readings"], p["figure_files"],
                                        p["column_width"], level=level(payload))
    return payload


def level(payload: dict) -> int:
    """Wikisource's proofreading level for the page: 3 (proofread) once a
    person has checked every text item, else 1 (not proofread)."""
    checked = payload["provenance"].get("checked", {})
    texts = [it.name for it in items_of(payload) if it.kind != "figure"]
    return 3 if texts and all(n in checked for n in texts) else 1


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
