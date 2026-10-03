"""The proofreading desk's data: out to the desk, and corrections back.

The desk is a page outside this engine (a claude.ai artifact with its own
store), so the engine meets it with files:

- OUT: `bundle` writes, for chosen supplied pages, every item's image (cut
  from the scan by the recorded boxes) and `desk.json` — the pages, their
  strips and readings, the render table the engine measured
  (`vocabulary.render_table`), and the book's notation for the palette.  The
  desk applies the table with `desk_render.js` and knows no markup of its own.
- BACK: `apply` takes the desk's saved strips and writes them into the
  supplied pages' readings, records who checked what, and reassembles each
  page — level 3 (proofread) once every text strip of it is checked.
- PARITY: `parity` renders every strip with desk_render.js (under Node) and
  with the engine's own chain, and reports any difference: the desk's view is
  a second renderer, tolerated only because it is held to the first.

    uv run python -m wikikit.scan.desk parity        # from the book's root
"""
from __future__ import annotations

import html
import re
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from wikikit.corpora import current_corpus
from wikikit.scan import cut, supplied
from wikikit.scan.supplied import page_id
from wikikit.scan.transcribe import PARA
from wikikit.scan.vocabulary import _render, render_table

DESK_RENDER_JS = Path(__file__).resolve().parent / "desk_render.js"
_LINK_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S)


def strip_id(volume: int, page: int, name: str) -> str:
    return f"{page_id(volume, page)}-{name}"


def notation() -> list:
    book = current_corpus()
    if not book.has_data("scan_notation.json"):
        return []
    return json.loads(book.data("scan_notation.json").read_text(encoding="utf-8"))["groups"]


def bundle(pages: list[tuple[int, int]], scans: dict[tuple[int, int], Image.Image],
           out: Path) -> Path:
    """Write the desk's files for these supplied pages into `out`."""
    out.mkdir(parents=True, exist_ok=True)
    desk_pages = []
    for vol, pg in pages:
        payload = supplied.read(vol, pg)
        p = payload["provenance"]
        level = cut.deskew(scans[(vol, pg)].convert("L"), p["angle"])
        strips = []
        for it in supplied.items_of(payload):
            rel = f"strips/{page_id(vol, pg)}/{it.name}.png"
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            level.crop(it.box).save(out / rel, optimize=True)
            strips.append({"id": strip_id(vol, pg, it.name), "name": it.name, "kind": it.kind,
                           "column": it.column, "img": rel,
                           "text": p["readings"].get(it.name, "") if it.kind != "figure" else None,
                           "refused": it.name in p.get("refused", []) or it.name in p.get("unread", {}),
                           "unread": p.get("unread", {}).get(it.name),
                           "checked": it.name in p.get("checked", {})})
        head = (p["readings"].get("head") or "").strip()
        desk_pages.append({"id": page_id(vol, pg), "volume": vol, "page": pg,
                           "label": f"Vol. {vol}, page {pg}", "running_head": head,
                           "disagreements": p.get("disagreements", {}), "strips": strips})
    (out / "desk.json").write_text(json.dumps({
        "table": render_table(), "notation": notation(), "paragraph_signal": PARA,
        "pages": desk_pages}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out / "desk.json"


def apply(rows: dict[str, dict]) -> list[str]:
    """The desk's saved strips -> the supplied pages.  `rows` maps a strip id to
    {"text", "status", "updated_by", "updated_at"} as the desk stores them.
    Returns one line per page changed."""
    by_page: dict[tuple[int, int], dict[str, dict]] = {}
    for sid, row in rows.items():
        vol, pg, name = sid[1:3], sid[5:9], sid[10:]
        by_page.setdefault((int(vol), int(pg)), {})[name] = row
    report = []
    for (vol, pg), strips in sorted(by_page.items()):
        payload = supplied.read(vol, pg)
        before = supplied.level(payload)
        p = payload["provenance"]
        checked = p.setdefault("checked", {})
        changed = 0
        for name, row in strips.items():
            if name not in p["readings"]:
                raise KeyError(f"vol {vol} p{pg}: the desk has a strip {name!r} the page does not")
            if row.get("text") is not None and row["text"] != p["readings"][name]:
                p["readings"][name] = row["text"]
                changed += 1
            if row.get("status") == "checked":
                checked[name] = {"by": row.get("updated_by"), "at": row.get("updated_at")}
            else:
                checked.pop(name, None)
        supplied.reassemble(payload)
        supplied.write(payload)
        report.append(f"vol {vol} p{pg}: {changed} reading(s) corrected, "
                      f"{len(checked)} strip(s) checked, level {supplied.level(payload)}"
                      + (" (now proofread)" if supplied.level(payload) > before else ""))
    return report


def desk_render(texts: list[str], table: dict) -> list[dict]:
    """desk_render.js on each text, under Node: what the desk shows."""
    runner = ("const r=require(process.argv[1]);let s='';process.stdin.on('data',d=>s+=d);"
              "process.stdin.on('end',()=>{const a=JSON.parse(s);"
              "process.stdout.write(JSON.stringify(a.texts.map(t=>r.render(t,a.table))))});")
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
        engine = _LINK_RE.sub(r"\1", _render(text)).strip()
        desk = html.unescape(d["html"])
        if d["leaks"] or desk != engine:
            out.append((text, desk, engine))
    return out


def _book_readings() -> list[str]:
    from wikikit.corpora import book_root
    from wikikit.source_pages import SUPPLIED_DIR
    texts = []
    for f in sorted((book_root() / SUPPLIED_DIR).glob("vol_*/*.json")):
        p = json.loads(f.read_text(encoding="utf-8"))["provenance"]
        heads = {i["name"] for i in p["items"] if i["kind"] == "head"}
        texts += [t for n, t in p["readings"].items() if n not in heads and t]
    return texts


def main() -> int:
    if sys.argv[1:] != ["parity"]:
        print(__doc__)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    texts = _book_readings()
    bad = parity(texts)
    for text, desk, engine in bad:
        print(f"DIFFER  {text[:90]!r}\n  desk   {desk[:160]!r}\n  engine {engine[:160]!r}")
    print(f"parity: {len(texts) - len(bad)} of {len(texts)} readings render the same on the desk and in the engine")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
