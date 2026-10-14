"""Transcribing pages through the Batch API: the same calls as reading them
now, at half the price of every token, answered within a day.

A page is a chain — find its figures, refine each, cut, read each strip, read
again what failed the guard — and a batch cannot run a chain, so pages advance
in STAGES, every page's next stage sent together:

    locate    one call per page: are there figures, roughly where
    refine    one call per figure: exact edges
    (cut)     local: the page cut into items with its figures
    read      one call per text item; an answer failing `settle` is
              sent once more, then recorded as unread
    (done)    local: the supplied page built, written, its figures cropped

`advance` is run again and again (`--wait` polls): it collects every ended
batch, moves each page as far as it can, sends the next stage, and saves.  All
of it is kept in one state file, so a run stops and resumes at will.  Each
page's tokens are kept as the API counted them, as `transcribe_page` keeps them.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from PIL import Image

from wikikit.corpora import book_root
from wikikit.scan import cut, figures, supplied
from wikikit.scan.layout import layout_for
from wikikit.scan.cut import Item
from wikikit.scan.supplied import page_id
from wikikit.scan.model import USAGE_FIELDS, batch_status, collect, request, submit
from wikikit.scan.transcribe import figures_of, read_request, settle, small_type_of

#: Requests per batch are bounded by size: base64 images, ~0.5 MB per strip.
MAX_BATCH_BYTES = 200 * 1024 * 1024
MAX_BATCH_REQUESTS = 10_000
CACHE_TTL = "1h"


def state_path() -> Path:
    return book_root() / "data" / "derived" / "scan_batch" / "state.json"


def load() -> dict:
    p = state_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"pages": {}, "batches": []}


def save(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")



def _empty_usage() -> dict:
    return {"batch": True, "calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)}


def _add_usage(rec: dict, usage) -> None:
    rec["calls"] += 1
    for f in USAGE_FIELDS:
        rec[f] += getattr(usage, f, 0) or 0


def _items(ps: dict) -> list[Item]:
    return [Item(i["kind"], tuple(i["box"]), i["column"], i["name"]) for i in ps["items"]]


def _apply(state: dict, results: dict, scans: Callable[[int, int], Image.Image]) -> None:
    """A batch's answers, into the pages they belong to."""
    for cid, (status, text, usage) in results.items():
        pid, kind, key = cid.split("_", 2)
        ps = state["pages"][pid]
        if kind in ("loc", "ref", "gut") and usage is not None:
            _add_usage(ps["usage"]["figures"], usage)
        if kind == "read" and usage is not None:
            _add_usage(ps["usage"]["text"], usage)
        if kind == "gut":
            if status != "ok":
                ps["error"] = f"gutter look: {text}"
                continue
            try:
                ps["gutter"] = figures.gutter_answer(text)
            except ValueError as e:
                ps["error"] = f"gutter look, unreadable answer: {e}"
        elif kind == "loc":
            if status != "ok":
                ps["error"] = f"figure finding: {text}"
                continue
            # an answer that cannot be read sets its page aside, reported, like
            # a failed call: one page's answer never stops the run
            try:
                ps["rough"], ps["across"] = figures.locate_answer(text, ps["loc_scale"])
            except ValueError as e:
                ps["error"] = f"figure finding, unreadable answer: {e}"
                continue
            ps["refined"] = {}
        elif kind == "ref":
            i = int(key)
            if status != "ok":
                ps["error"] = f"figure {i}: {text}"
                continue
            try:
                ps["refined"][str(i)] = figures.refine_answer(text, ps["rough"][i], ps["windows"][str(i)])
            except ValueError as e:
                ps["error"] = f"figure {i}, unreadable answer: {e}"
        elif kind == "read":
            if status == "refused":
                ps["refused"].append(key)
                continue
            if status != "ok":
                ps["tries"][key] = ps["tries"].get(key, 0) + 1
                if ps["tries"][key] >= 2:
                    ps["unread"][key] = text
                continue
            level = _level(ps, scans)
            items = _items(ps)
            item = next(it for it in items if it.name == key)
            text, why = settle(level, item, text, figures_of(items, item))
            if why is None:
                ps["readings"][key] = text
            else:
                ps["tries"][key] = ps["tries"].get(key, 0) + 1
                if ps["tries"][key] >= 2:
                    ps["unread"][key] = why
                    if text.strip():
                        ps.setdefault("rejected", {})[key] = text

#: Levelled pages, the last few only.  A full-resolution leaf levelled is
#: ~23 MB (EB1911: 2340 x 3334 in colour); kept for every page of a 406-page
#: run that was ~9.5 GB, and the run was killed for want of memory.  A run's
#: passes walk the pages in turn, so a larger cache would never hit anyway:
#: these cover a page used twice in a row, and the rest are levelled again.
_LEVELS: OrderedDict[str, Image.Image] = OrderedDict()
LEVELS_KEPT = 4


def _level(ps: dict, scans) -> Image.Image:
    pid = ps["id"]
    if pid in _LEVELS:
        _LEVELS.move_to_end(pid)
    else:
        _LEVELS[pid] = cut.deskew(scans(ps["volume"], ps["page"]), ps["angle"])
        while len(_LEVELS) > LEVELS_KEPT:
            _LEVELS.popitem(last=False)
    return _LEVELS[pid]


def _page_requests(pid: str, ps: dict, scans) -> dict[str, dict]:
    """One page moved as far as local work allows, and the calls its next
    stage needs."""
    reqs: dict[str, dict] = {}
    level = _level(ps, scans)
    layout = layout_for(ps["volume"], ps["page"])
    # a book reading only pages set in columns asks the gutter look beside
    # the figures' (`figures.gutter`), once
    if layout.columns_only and ps.get("gutter") is None and not ps.get("gut_sent"):
        img, prompt, max_tokens = figures.gutter_request(level)
        reqs[f"{pid}_gut_0"] = request(img, prompt, max_tokens)
        ps["gut_sent"] = True
    if ps["rough"] is None:
        img, prompt, max_tokens, scale = figures.locate_request(level)
        ps["loc_scale"] = scale
        reqs[f"{pid}_loc_0"] = request(img, prompt, max_tokens)
        return reqs
    if len(ps["refined"]) < len(ps["rough"]):
        cols = cut.page_columns(level, layout.columns)
        for i, f in enumerate(ps["rough"]):
            if str(i) not in ps["refined"]:
                img, prompt, max_tokens, window = figures.refine_request(level, f, figures.column_of(f, cols))
                ps["windows"][str(i)] = window
                reqs[f"{pid}_ref_{i}"] = request(img, prompt, max_tokens)
        return reqs
    if layout.columns_only and ps.get("gutter") is None:
        return reqs                                  # the gutter look still out
    if ps["items"] is None:                          # the cut, once the figures are known
        figs = [ps["refined"][str(i)] for i in range(len(ps["rough"]))]
        plan = cut.plan(level, figs, max_h=layout.max_h, angle=0.0, across=ps.get("across") or [],
                        n_columns=layout.columns, gutter=ps.get("gutter") or [])
        ps["figures"] = figs
        ps["items"] = [{"name": it.name, "kind": it.kind, "column": it.column, "box": list(it.box)}
                       for it in plan.items]
        ps["disagreements"] = {str(k): list(v) for k, v in plan.disagreements.items()}
        # a page the book reads only in columns, with anything across them:
        # filed for special handling, never read (`layout.columns_only`)
        ps["special"] = plan.crossing if layout.columns_only else []
        ps["stage"] = "read"
    if ps.get("special"):
        return reqs
    items = _items(ps)
    pending = [it for it in items if not (it.kind == "figure" or it.name in ps["readings"]
                                          or it.name in ps["unread"] or it.name in ps["refused"])]
    small = small_type_of(level, items, layout) if pending else {}
    for it in pending:
        reqs[f"{pid}_read_{it.name}"] = read_request(level, it, cache_ttl=CACHE_TTL, small=small.get(it.name))
    return reqs


def _next_requests(state: dict, scans) -> dict[str, dict]:
    """Every page's next calls.  A page whose calls cannot be prepared is set
    aside with its error — reported, never silently dropped — and the rest
    go on: one bad page does not hold up a run."""
    reqs: dict[str, dict] = {}
    for pid, ps in state["pages"].items():
        if ps["stage"] == "done" or ps.get("error"):
            continue
        try:
            reqs.update(_page_requests(pid, ps, scans))
        except Exception as e:
            ps["error"] = f"preparing its calls: {type(e).__name__}: {e}"
    return reqs


def _finish(state: dict, scans) -> list[str]:
    """Pages whose every text item is settled, written as supplied pages."""
    done = []
    for pid, ps in state["pages"].items():
        if ps["stage"] != "read":
            continue
        items = _items(ps)
        texts = [it for it in items if it.kind != "figure"]
        if not ps.get("special") and not all(
                it.name in ps["readings"] or it.name in ps["unread"] or it.name in ps["refused"]
                for it in texts):
            continue
        level = _level(ps, scans)
        plan = cut.Plan(ps["angle"], items, {int(k): tuple(v) for k, v in ps["disagreements"].items()})
        readings = {it.name: ps["readings"].get(it.name, "") for it in texts}
        payload = supplied.build_payload(ps["volume"], ps["page"], ps["scan"], level, plan,
                                         ps["figures"], readings, ps["refused"], ps["unread"],
                                         ps["usage"], ps.get("rejected", {}), ps.get("replaces"),
                                         ps.get("across") or [], ps.get("special"), ps.get("gutter"))
        if ps.get("out"):
            # a reading for another use (a book's patches): written to its own
            # place, never the supplied pages a build reads, and no figure
            # crops — those would overwrite the book's images
            from wikikit.source_pages import page_filename, volume_dir
            f = volume_dir(ps["volume"], ps["out"]) / page_filename(ps["volume"], ps["page"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            supplied.write(payload)
            supplied.materialize_figures(payload, scans(ps["volume"], ps["page"]))
        ps["stage"] = "done"
        done.append(pid)
    return done


def _chunks(reqs: dict[str, dict]) -> list[dict[str, dict]]:
    out, cur, size = [], {}, 0
    for cid, params in reqs.items():
        n = len(json.dumps(params))
        if cur and (size + n > MAX_BATCH_BYTES or len(cur) >= MAX_BATCH_REQUESTS):
            out.append(cur)
            cur, size = {}, 0
        cur[cid], size = params, size + n
    if cur:
        out.append(cur)
    return out


def add_pages(pages: list[tuple[int, int, str]], scans, replaces: str | None = None,
              located: dict[str, dict] | None = None, out: str | Path | None = None) -> None:
    """Put pages (volume, page, scan name) into the run; a page still in
    flight is left as it is, and a finished page asked for again is read
    afresh (the caller decided to overwrite its supplied page).  `replaces`:
    why the fetched pages were found bad (`supplied.build_payload`).  A page's
    layout is the book's to declare (`wikikit.scan.layout`), asked when the
    page is cut.

    `located`: page id -> its supplied page's provenance, for a page read
    again whose figures were located before: its deskew, figures and type
    across are taken as they stand (the boxes are in that deskew's pixels),
    and only its text is read — what locating cost is kept, not paid twice.
    Its "gutter", if given, is the gutter look's answer (`figures.gutter`).

    `out`: a directory the finished pages are written to (out/vol_NN/ID.json)
    INSTEAD of the supplied pages, with no figure crops — a reading for another
    use, e.g. a book re-reading pages for their tables.  Kept with each page, so
    a resumed run writes where it was asked to."""
    state = load()
    for vol, pg, scan in pages:
        pid = page_id(vol, pg)
        if pid in state["pages"] and state["pages"][pid]["stage"] != "done":
            continue
        ps = state["pages"][pid] = {
            "id": pid, "volume": vol, "page": pg, "scan": scan, "stage": "figures",
            "replaces": replaces,
            "angle": None, "rough": None, "across": [], "refined": {},
            "windows": {},
            "figures": None, "items": None, "disagreements": {}, "readings": {}, "tries": {},
            "refused": [], "unread": {},
            "usage": {"figures": _empty_usage(), "text": _empty_usage()},
            "out": str(Path(out).resolve()) if out else None}
        known = (located or {}).get(pid)
        if known:
            figs = known.get("figures") or []
            ps.update(angle=known["angle"], rough=figs, refined={str(i): f for i, f in enumerate(figs)},
                      across=known.get("across") or [])
            if "gutter" in known:                    # the gutter look, asked before
                ps["gutter"] = known["gutter"]
            ps["usage"]["figures"] = known["usage"]["figures"]
        else:
            ps["angle"] = cut.skew_angle(scans(vol, pg))
    save(state)


def advance(scans) -> dict:
    """One step of the run: collect ended batches, move pages on, send the next
    stage.  Returns a summary; call again until nothing is waiting."""
    state = load()
    waiting = []
    for bid in list(state["batches"]):
        status, counts = batch_status(bid)
        if status != "ended":
            waiting.append((bid, counts))
            continue
        _apply(state, collect(bid), scans)
        state["batches"].remove(bid)
    finished = _finish(state, scans)
    sent = []
    if not waiting:
        reqs = _next_requests(state, scans)
        for chunk in _chunks(reqs):
            bid = submit(chunk)
            state["batches"].append(bid)
            sent.append((bid, len(chunk)))
        finished += _finish(state, scans)
    save(state)
    stages = {}
    for ps in state["pages"].values():
        key = "error" if ps.get("error") else ps["stage"]
        stages[key] = stages.get(key, 0) + 1
    return {"waiting": waiting, "sent": sent, "finished": finished, "pages": stages,
            "errors": {pid: ps["error"] for pid, ps in state["pages"].items() if ps.get("error")}}


def run_until_done(scans, poll_seconds: int = 300, log=print) -> None:
    """`advance` until every page is done or stopped on an error."""
    while True:
        s = advance(scans)
        log(json.dumps({k: v for k, v in s.items() if v}, default=str))
        if not s["waiting"] and not s["sent"]:
            return
        time.sleep(poll_seconds)
