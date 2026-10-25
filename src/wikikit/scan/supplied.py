"""A page transcribed from its scan, as a supplied page the book commits.

The file has the shape of a fetched page (`wikikit.source_pages`), so it enters
by the same door, plus `source: "scan"` and its provenance: the deskew, every
item's box, every reading, the figures' file names, the model, the date, and
the items the API refused, and who fetched it (`fetched_by`: whose key read
it; it is theirs to proofread until given to someone else, `owner`).  The READINGS are the source a proofreader corrects;
`raw_text` is assembled from them (`transcribe.assemble_page`) and re-assembled
whenever one changes — until a person proofreads the PAGE as one text at the
desk (`set_page_text`): from then on its text is theirs, kept as they left it,
and the readings are the record of how the model read it.

Figure images are not committed: they are crops of the scan, regenerated into
the book's image store from the scan and the boxes recorded here
(`materialize_figures`).
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image

from wikikit.corpora import book_root, current_corpus
from wikikit.image_assets import local_image_filename
from wikikit.scan import cut
from wikikit.scan.cut import Item
from wikikit.scan.layout import layout_for
from wikikit.scan.model import MODEL, Refused, metered
from wikikit.scan.page import cut_page
from wikikit.scan.transcribe import (Unread, assemble_page, drop_neighbour_marks, figures_of, fit_figures,
                                     read_item, settle, small_captions, small_type_of)
from wikikit.source_pages import SUPPLIED_DIR, page_filename, volume_dir


def page_id(volume: int, page: int) -> str:
    """A page's id wherever pages are tracked (the desk, a batch run)."""
    return f"v{volume:02d}-p{page:04d}"


def fetcher() -> str | None:
    """Who is reading pages here, on their own key: the checkout's git name
    (`git config user.name`), recorded on each page they fetch."""
    import subprocess
    try:
        out = subprocess.run(["git", "config", "user.name"], cwd=book_root(),
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    return out or None


def owner(payload: dict) -> str | None:
    """Who proofreads the page: whoever it was given to at the desk, else
    whoever fetched it."""
    p = payload["provenance"]
    return p.get("owner") or p.get("fetched_by")


#: An apostrophe between two letters (don’t, o’clock): never markup, which is
#: two or more; beside a digit it is a prime (5' 6"), left as it is.
_APOSTROPHE = re.compile(r"(?<=[^\W\d_])'(?=[^\W\d_])")


def curly_marks(text: str) -> str:
    """A reading's marks as a book that prints them curly sets them
    (`Corpus.curly_readings`): its quotation marks by the engine's own rule
    (`preprocess.curly_quotes`), and an apostrophe within a word."""
    from wikikit.pipeline.stages.preprocess import curly_quotes
    return _APOSTROPHE.sub("’", curly_quotes(text))


def book_rules(text: str) -> str:
    """A page's body with the book's own rules applied (`Corpus.text_rules`):
    at every assembly and every save, so a text written before a rule came,
    or a save of one, is set as the rule says."""
    for rule in current_corpus().text_rules:
        text = rule(text)
    return text


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
    is cut as the book declares its layout (`wikikit.scan.layout`); a page the
    book reads only in columns, with anything across them, is filed — written
    unread, held for special handling (`special`) — never read.
    `replaces`: as `build_payload`."""
    layout = layout_for(volume, page)
    with metered() as finding:
        cp = cut_page(img, layout)
    special = cp.plan.crossing if layout.columns_only else []
    readings, refused, unread, rejected = {}, [], {}, {}
    small = small_type_of(cp.level, cp.plan.items, layout)
    with metered() as reading:
        for it in list(cp.plan.items):           # a split figure adds an item
            if it.kind == "figure" or special:
                continue
            try:
                readings[it.name] = read_item(cp.level, it, figures_of(cp.plan.items, it), small.get(it.name))
            except Refused:
                readings[it.name], refused = "", refused + [it.name]
            except Unread as e:
                # more marks than figures: the figure the locator found as one
                # split to match, if blank paper parts it (`fit_figures`)
                fitted = fit_figures(cp.level, cp.plan.items, it, e.answer or "")
                if fitted is not None:
                    text, why = settle(cp.level, it, e.answer, figures_of(fitted, it))
                    if why is None:
                        cp.plan.items[:] = fitted
                        readings[it.name] = text
                        continue
                # a mark for the next column's figure, seen across the cut
                bare = drop_neighbour_marks(cp.plan.items, it, e.answer or "")
                if bare is not None:
                    text, why = settle(cp.level, it, bare, figures_of(cp.plan.items, it))
                    if why is None:
                        readings[it.name] = text
                        continue
                readings[it.name], unread[it.name] = "", e.reason
                if e.answer:
                    rejected[it.name] = e.answer
    return build_payload(volume, page, scan, cp.level, cp.plan, cp.figures, readings,
                         refused, unread, {"figures": finding.record(), "text": reading.record()},
                         rejected, replaces, cp.across, special, cp.gutter if layout.columns_only else None)


def build_payload(volume: int, page: int, scan: str, level: Image.Image, plan: cut.Plan,
                  figures: list[dict], readings: dict, refused: list, unread: dict,
                  usage: dict, rejected: dict | None = None,
                  replaces: str | None = None, across: list | None = None,
                  special: list[str] | None = None, gutter: list | None = None) -> dict:
    """The supplied page from its cut and readings — the one builder, whether
    the page was read now (`transcribe_page`) or in batches
    (`wikikit.scan.batch`).  `replaces`: why the fetched page, proofread or
    not, was found bad as data — the page then stands in for it at any level
    (`wikikit.source_pages.prefer`).  `special`: why the page was filed for
    special handling instead of read (`cut.Plan.crossing`).  `gutter`: the
    gutter look's answer, when it was asked (`figures.gutter`)."""
    files = {it.name: figure_file(volume, page, it.name)
             for it in plan.items if it.kind == "figure"}
    if current_corpus().curly_readings:
        readings = {name: curly_marks(r) for name, r in readings.items()}
    if current_corpus().captions_small:
        readings = {name: small_captions(r) for name, r in readings.items()}
    payload = {
        "volume": volume, "page_number": page, "source": "scan", "title": scan,
        "provenance": {
            "scan": scan, "model": MODEL, "transcribed": date.today().isoformat(),
            "fetched_by": fetcher(),
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
    if special:
        payload["provenance"]["special"] = list(special)
    if gutter is not None:
        # with `figures` and `across`, what a re-cut needs to file the page
        # again the same way
        payload["provenance"]["gutter"] = list(gutter)
    if special:
        reassemble(payload)
    else:
        measure_type(payload, level)
        measure_insets(payload, level)
        name_sections(payload)
        carry_images(payload, scan_width=level.size[0])
    return payload


def items_of(payload: dict) -> list[Item]:
    return [Item(i["kind"], tuple(i["box"]), i["column"], i["name"])
            for i in payload["provenance"]["items"]]


def reassemble(payload: dict, carried: bool = True, fetched: str | None = None) -> dict:
    """`raw_text` from the readings — after transcription, and after every
    correction a proofreader makes to a reading — with the replaced page's
    tables and links carried onto it when the page carries them
    (`carry_markup`; `carried=False`: the readings alone).  `fetched`: the
    replaced page's text (default: as filed)."""
    p = payload["provenance"]
    if p.get("page_edited"):
        # proofread as a page: its text is the person's, never re-made from
        # the readings — only its level said again, and the book's rules
        head, body, furniture = page_parts(payload["raw_text"])
        payload["raw_text"] = framed(head, book_rules(body), furniture, level(payload))
        return payload
    raw = assemble_page(items_of(payload), p["readings"], p["figure_files"],
                        p["column_width"], level=level(payload),
                        legacy_captions=not p.get("captions_marked"),
                        sections=p.get("sections"),
                        source_images=p.get("images"), regions=p.get("regions"),
                        insets=p.get("insets"))
    if carried and "carried" in p:
        from wikikit.scan.carry import carry
        from wikikit.source_pages import fetched_text
        if fetched is None:
            fetched = fetched_text(payload["volume"], payload["page_number"]) or ""
        raw, rep = carry(raw, fetched)
        p["carried"] = {"tables": rep["tables"],     # kept: spans in the fetched page
                        "tables kept": {k: [list(s) for s in v] for k, v in rep["tables kept"].items()},
                        "links": rep["links"], "unplaced": rep["unplaced"]}
    if current_corpus().text_rules:
        head, body, furniture = page_parts(raw)
        raw = framed(head, book_rules(body), furniture, level(payload))
    payload["raw_text"] = raw
    return payload


def carry_markup(payload: dict, fetched: str | None = None) -> dict:
    """The replaced page's tables and links carried onto ours
    (`wikikit.scan.carry`), from now on at every assembly: what a person built
    on Wikisource's page — borders, spans, headers, links — that a reading
    cannot make.  Recorded (`carried`: what was carried, what ours keeps, and
    what could not be placed); the supplied-page gate fails a page with
    anything unplaced."""
    payload["provenance"]["carried"] = {}
    return reassemble(payload, fetched=fetched)


def carry_images(payload: dict, fetched: str | None = None, scan_width: int | None = None) -> dict:
    """The replaced page's own images set where our figures are
    (`wikikit.scan.images`): decided once, stored (`images`: our figure ->
    the markup set in its place), and the page reassembled.  A page whose
    images cannot all be placed keeps none of the pairing and lists why
    (`images_unplaced`) — the supplied-page gate fails it until resolved:
    never a guess, and never our crop silently standing for a wiki image.
    `fetched`: the replaced page's text (default: as filed); `scan_width`:
    the scan's width in pixels, needed only for an editor's region (default:
    measured from the book's scan)."""
    from wikikit.scan import images
    from wikikit.source_pages import fetched_text
    p = payload["provenance"]
    if p.get("images_decided"):
        # placed by a person or by looking at the scan, the reason recorded:
        # kept, never recomputed (as a checked reading is never reread)
        p.pop("images_unplaced", None)
        return reassemble(payload)
    p.pop("images", None)
    p.pop("images_unplaced", None)
    # our figures as read: no table carried over them yet, and no Commons
    # region set for one — a region stands only for a figure Wikisource's
    # page has no image of (vol 27 p. 401's Fig. 2, set before its
    # `{{img float}}` was seen)
    bare = {**payload, "provenance": {**p, "regions": None}}
    reassemble(bare, carried=False)
    if fetched is None:
        fetched = fetched_text(payload["volume"], payload["page_number"]) or ""
    source = images.source_images(fetched)
    if source:
        if scan_width is None and any(s["region"] for s in source):
            scan_width = current_corpus().page_scan(payload["volume"], payload["page_number"]).size[0]
        boxes = {i["name"]: i["box"] for i in p["items"] if i["kind"] == "figure"}
        ours = images.our_figures(bare["raw_text"], p["figure_files"], boxes, scan_width)
        placed, problems = images.pair(source, ours)
        if problems:
            p["images_unplaced"] = problems
        else:
            p["images"] = placed
            if p.get("regions"):
                p["regions"] = {k: v for k, v in p["regions"].items() if k not in placed}
    return reassemble(payload, fetched=fetched)


def measure_type(payload: dict, level: Image.Image) -> dict:
    """Each recorded paragraph's type, MEASURED (`cut.paragraph_type`), set
    into its reading (`apply_type`) — for a book whose layout marks small
    type.  Set once, into the reading: from then on the reading is the only
    truth of the page's sizes, and what a proofreader corrects there stands
    (2026-10-15: stored apart and laid over the reading at every assembly,
    the measurement undid the desk's corrections, and the desk showed a
    reading whose sizes were not the page's — vol 1 p634's "Taking the first
    polar", small in the reading, body in print and on the page).
    Unmeasurable, only by the book's rules: a footnote is small; a column's
    first paragraph, unless it opens with a title, continues the column
    before and keeps its size (a display needs no rule: it is inside its
    paragraph).  Any other paragraph not measured is NOT inherited — "§ 14"
    (vol 11 p317), a new paragraph in body type, came out small — but keeps
    the size the reading gave it (`apply_type`), and is listed
    (`type_unmeasured`).  Type size never holds a page: a wrong size loses no
    text.  Stored once: a reassembly never measures again."""
    from wikikit.scan.transcribe import POSITION, _is_note, apply_type, positions
    p = payload["provenance"]
    if not layout_for(payload["volume"], payload["page_number"]).marks_small_type:
        return payload
    texts = [it for it in items_of(payload) if it.kind == "text" and it.column > 0]
    pitches = cut.type_pitches(level, [it.box for it in texts])
    prev, unmeasured = None, []
    for it in texts:
        reading = p["readings"].get(it.name, "")
        starts = positions(reading)
        sizes = cut.paragraph_type(level, it.box, [y for _, y in starts], pitches)
        for k, (off, y) in enumerate(starts):
            para = POSITION.sub("", reading[off:].split("\n\n", 1)[0]).strip()
            if _is_note(para):
                sizes[k] = "small"
            elif sizes[k] is None and k == 0 and not para.startswith("'''") and prev:
                sizes[k] = prev
            elif sizes[k] is None:
                unmeasured.append(f"{it.name} y{y}")
            prev = sizes[k] or prev
        p["readings"][it.name] = apply_type(reading, sizes)
    p["type_unmeasured"] = unmeasured      # kept as the reading set them; never a hold
    return reassemble(payload)

def measure_insets(payload: dict, level: Image.Image) -> dict:
    """Each figure the print sets with type beside it, and the side of its
    column it stands on (`cut.inset_side`), MEASURED once and stored
    (`insets`: figure item -> "left" | "right"): assembly floats it there, in
    its place in the text (`transcribe.assemble_page`).  The vision read's own
    `inset` is not the answer — it named some figures standing alone
    (SOUND p. 454's Fig. 36) and missed one with type beside it (vol 25
    p. 845's Fig. 59).  An item holding several figures is inset only when
    every one stands on the same side with type beside it (SIGNAL p. 72's
    semaphore plate is one figure, not three)."""
    p = payload["provenance"]
    dark = np.asarray(level.convert("L")) < cut.INK
    columns = {it.column: it.box for it in items_of(payload) if it.kind == "text"}
    insets = {}
    for it in items_of(payload):
        column = columns.get(it.column)
        if it.kind != "figure" or column is None:
            continue
        inside = [f["page_box"] for f in p.get("figures", []) if f.get("page_box")
                  and f.get("column") == it.column and _centre_in(f["page_box"], it.box)]
        sides = {cut.inset_side(dark, column, box) for box in inside or [it.box]}
        if len(sides) == 1 and None not in sides:
            insets[it.name] = sides.pop()
    p["insets"] = insets
    return reassemble(payload)


def _centre_in(box, outer) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def name_sections(payload: dict) -> dict:
    """The page's section addresses, from the book (`Corpus.page_sections`):
    where its articles start, read off the page as assembled, and the name
    each address must keep so its article keeps its id.  Stored with the
    page, so a reassembly never asks again.  A page the book cannot name — or
    whose names do not all land on their articles — is filed for special
    handling (`special`), never shipped with an article misaddressed."""
    hook = current_corpus().page_sections
    p = payload["provenance"]
    if hook is None:
        return reassemble(payload)
    p.pop("sections", None)
    reassemble(payload)
    secs, why = hook(payload["volume"], payload["page_number"], payload["raw_text"])
    if secs is None:
        p["special"] = sorted(set(p.get("special") or []) | {f"sections: {why}"})
    else:
        p["sections"] = secs
    reassemble(payload)
    placed = payload["raw_text"].count("<section begin=")
    if secs is not None and placed != len(secs):
        p["special"] = sorted(set(p.get("special") or [])
                              | {f"sections: {placed} of {len(secs)} placed at their articles"})
        reassemble(payload)
    return payload


def level(payload: dict) -> int:
    """Wikisource's proofreading level for the page: 3 (proofread) once a
    person has checked every text item; 2 (problematic: Wikisource's word for
    a page with text missing) while any text item the model refused or left
    unread is unchecked, or while the page is filed for special handling
    (`special`: set across its columns) — the reader is told the page is
    incomplete; else 1 (not proofread).  A page edited whole is checked whole
    (`page_checked`): its items' checks were of a text it no longer has."""
    p = payload["provenance"]
    if p.get("page_checked"):
        return 3
    checked = {} if p.get("page_edited") else p.get("checked", {})
    items = [it for it in items_of(payload) if it.kind != "figure"]
    texts = [it.name for it in items]
    if texts and all(n in checked for n in texts):
        return 3
    if p.get("special"):
        return 2
    body = {it.name for it in items if it.kind != "head"}       # a lost running head loses no text
    gaps = [n for n in [*p.get("refused", []), *p.get("unread", {})] if n in body and n not in checked]
    return 2 if gaps else 1


#: A page's frame as assembly writes it (`transcribe.assemble_page`): its
#: quality and running head above the body, its furniture and notes below.
_FRAME = re.compile(r'^<noinclude><pagequality level="\d" user="[^"]*" />(.*?)</noinclude>'
                    r'(.*)<noinclude>(.*?)<references/></noinclude>$', re.S)


def page_parts(raw: str) -> tuple[str, str, str]:
    """(running head, body, furniture) of an assembled page."""
    m = _FRAME.match(raw)
    if not m:
        raise ValueError(f"not a page as assembly frames it: {raw[:80]!r}")
    return m.group(1), m.group(2), m.group(3)


def framed(head: str, body: str, furniture: str, level: int) -> str:
    who = "proofreader" if level >= 3 else "transcriber"
    return (f'<noinclude><pagequality level="{level}" user="{who}" />{head}</noinclude>'
            f"{body}<noinclude>{furniture}<references/></noinclude>")


def set_page_text(payload: dict, head: str, body: str, checked: bool,
                  by: str | None = None, at: str | None = None) -> dict:
    """The page as a person left it at the desk: its running head and body,
    and whether they checked it — proofread (level 3) once checked.  From then
    on the page keeps this text: `reassemble` no longer makes it from the
    readings (`page_edited`: who last saved it, and when)."""
    p = payload["provenance"]
    _, _, furniture = page_parts(payload["raw_text"])
    body = book_rules(body)
    p["page_edited"] = {"by": by, "at": at}
    if checked:
        p["page_checked"] = {"by": by, "at": at}
    else:
        p.pop("page_checked", None)
    payload["raw_text"] = framed(head, body, furniture, level(payload))
    return payload


def write(payload: dict) -> Path:
    path = path_for(payload["volume"], payload["page_number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # one line ending on every machine: a page fetched on Windows and one
    # fetched elsewhere differ only where their content does
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
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
    carried = {**(p.get("images") or {}), **(p.get("regions") or {})}
    for it in items_of(payload):
        # a figure the replaced page has its own image for is never cropped
        if it.kind == "figure" and it.name not in carried:
            dest = store / local_image_filename(p["figure_files"][it.name])
            level.crop(it.box).save(dest)
            out.append(dest)
    return out
