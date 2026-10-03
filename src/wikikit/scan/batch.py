"""Transcribing pages through the Batch API: the same calls as reading them
now, at half the price of every token, answered within a day.

A page is a chain — find its figures, refine each, cut, read each strip, read
again what failed the guard — and a batch cannot run a chain, so pages advance
in STAGES, every page's next stage sent together:

    locate    one call per page: are there figures, roughly where
    refine    one call per figure: exact edges
    (cut)     local: the page cut into items with its figures
    read      one call per text item; an answer failing `check_reading` is
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
from pathlib import Path
from typing import Callable

from PIL import Image

from wikikit.corpora import book_root
from wikikit.scan import cut, figures, supplied
from wikikit.scan.cut import Item
from wikikit.scan.supplied import page_id
from wikikit.scan.model import USAGE_FIELDS, batch_status, collect, request, submit
from wikikit.scan.transcribe import captions, check_reading, read_request

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
        if kind in ("loc", "ref") and usage is not None:
            _add_usage(ps["usage"]["figures"], usage)
        if kind == "read" and usage is not None:
            _add_usage(ps["usage"]["text"], usage)
        if kind == "loc":
            if status != "ok":
                ps["error"] = f"figure finding: {text}"
                continue
            ps["rough"] = figures.locate_answer(text, ps["loc_scale"])
            ps["refined"] = {}
        elif kind == "ref":
            i = int(key)
            if status != "ok":
                ps["error"] = f"figure {i}: {text}"
                continue
            ps["refined"][str(i)] = figures.refine_answer(text, ps["rough"][i], ps["windows"][str(i)])
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
            item = next(it for it in _items(ps) if it.name == key)
            why = check_reading(level, item, text)
            if why is None:
                ps["readings"][key] = text
            else:
                ps["tries"][key] = ps["tries"].get(key, 0) + 1
                if ps["tries"][key] >= 2:
                    ps["unread"][key] = why
                    if text.strip():
                        ps.setdefault("rejected", {})[key] = text

_LEVELS: dict[str, Image.Image] = {}


def _level(ps: dict, scans) -> Image.Image:
    pid = ps["id"]
    if pid not in _LEVELS:
        _LEVELS[pid] = cut.deskew(scans(ps["volume"], ps["page"]), ps["angle"])
    return _LEVELS[pid]


def _page_requests(pid: str, ps: dict, scans) -> dict[str, dict]:
    """One page moved as far as local work allows, and the calls its next
    stage needs."""
    reqs: dict[str, dict] = {}
    level = _level(ps, scans)
    if ps["rough"] is None:
        img, prompt, max_tokens, scale = figures.locate_request(level)
        ps["loc_scale"] = scale
        reqs[f"{pid}_loc_0"] = request(img, prompt, max_tokens)
        return reqs
    if len(ps["refined"]) < len(ps["rough"]):
        cols = cut.page_columns(level, ps.get("n_columns"))
        for i, f in enumerate(ps["rough"]):
            if str(i) not in ps["refined"]:
                img, prompt, max_tokens, window = figures.refine_request(level, f, figures.column_of(f, cols))
                ps["windows"][str(i)] = window
                reqs[f"{pid}_ref_{i}"] = request(img, prompt, max_tokens)
        return reqs
    if ps["items"] is None:                          # the cut, once the figures are known
        figs = [ps["refined"][str(i)] for i in range(len(ps["rough"]))]
        plan = cut.plan(level, figs, angle=0.0, n_columns=ps.get("n_columns"))
        ps["figures"] = figs
        ps["items"] = [{"name": it.name, "kind": it.kind, "column": it.column, "box": list(it.box)}
                       for it in plan.items]
        ps["disagreements"] = {str(k): list(v) for k, v in plan.disagreements.items()}
        ps["stage"] = "read"
    items = _items(ps)
    caps = captions(items)
    for it in items:
        if it.kind == "figure" or it.name in ps["readings"] or it.name in ps["unread"] \
                or it.name in ps["refused"]:
            continue
        reqs[f"{pid}_read_{it.name}"] = read_request(level, it, it.name in caps, cache_ttl=CACHE_TTL)
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
        if not all(it.name in ps["readings"] or it.name in ps["unread"] or it.name in ps["refused"]
                   for it in texts):
            continue
        level = _level(ps, scans)
        plan = cut.Plan(ps["angle"], items, {int(k): tuple(v) for k, v in ps["disagreements"].items()})
        readings = {it.name: ps["readings"].get(it.name, "") for it in texts}
        payload = supplied.build_payload(ps["volume"], ps["page"], ps["scan"], level, plan,
                                         ps["figures"], readings, ps["refused"], ps["unread"],
                                         ps["usage"], ps.get("rejected", {}))
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


def add_pages(pages: list[tuple[int, int, str]], scans, n_columns: int | None = None) -> None:
    """Put pages (volume, page, scan name) into the run; already-present pages
    are left as they are."""
    state = load()
    for vol, pg, scan in pages:
        pid = page_id(vol, pg)
        if pid in state["pages"]:
            continue
        state["pages"][pid] = {
            "id": pid, "volume": vol, "page": pg, "scan": scan, "stage": "figures",
            "n_columns": n_columns,
            "angle": cut.skew_angle(scans(vol, pg)), "rough": None, "refined": {}, "windows": {},
            "figures": None, "items": None, "disagreements": {}, "readings": {}, "tries": {},
            "refused": [], "unread": {},
            "usage": {"figures": _empty_usage(), "text": _empty_usage()}}
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
