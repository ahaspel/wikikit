"""The proofreading desk, served locally on the book's own site server.

The desk shows three things side by side for each page: the ORIGINAL (each
strip cut from the scan), the page RENDERED as it would enter, and the SOURCE
(each strip's reading, to correct).  The rendered pane is the site's own
viewer showing `desk.page_article` — the engine's chain, so the desk has no
renderer of its own and nothing to hold in parity.  It is redrawn from the
readings as they stand in the editor, saved or not.

Saving writes the corrections and the checks into the supplied page
(`desk.apply`): a page whose every text strip is checked is proofread (level
3), and enters the book at the next rebuild.  Do not save while a rebuild runs.

Routes, beside everything the site server answers (`wikikit.site.serve`):

    /desk                           the desk page
    /desk/api/queue                 the pages waiting, with their progress
    /desk/api/page/V/P              one page: its strips and their readings
    /desk/strip/V/P/NAME.png        a strip's image, cut from the scan
    /desk/article/V-P.json          the page as it would enter, for the viewer
    POST /desk/api/preview          {volume, page, readings}: redraw from these
    POST /desk/api/save             {volume, page, strips: {name: {text, status}}}
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from wikikit.corpora import current_corpus
from wikikit.scan import desk, supplied
from wikikit.scan.vocabulary import constructs
from wikikit.site import serve as site

PAGE = Path(__file__).resolve().parent / "desk_local.html"


class Handler(site.Handler):
    queue = staticmethod(lambda: [])            # () -> [(volume, page)], set by `serve`
    label = staticmethod(desk.page_label)
    who = "proofreader"
    strips_dir: Path
    drafts: dict = {}                           # (volume, page) -> readings being edited
    lock = threading.Lock()

    # ── answers ──────────────────────────────────────────────────────────
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, code: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _page(self, vol: int, pg: int) -> dict:
        payload = supplied.read(vol, pg)
        p = payload["provenance"]
        from wikikit.source_pages import RAW_DIR, page_filename, volume_dir
        fetched = volume_dir(vol, RAW_DIR) / page_filename(vol, pg)
        title = json.loads(fetched.read_text(encoding="utf-8")).get("title") if fetched.exists() else None
        return {"volume": vol, "page": pg, "label": self.label(vol, pg), "level": supplied.level(payload),
                "wikisource": ("https://en.wikisource.org/wiki/" + title.replace(" ", "_")) if title else None,
                "strips": [{"name": it.name, "kind": it.kind, "column": it.column,
                            "img": f"/desk/strip/{vol}/{pg}/{it.name}.png",
                            "text": p["readings"].get(it.name) if it.kind != "figure" else None,
                            "checked": it.name in p.get("checked", {})}
                           for it in supplied.items_of(payload)],
                "constructs": [{"name": c.name, "open": c.open, "close": c.close, "says": c.says,
                                "args": list(c.args)} for c in constructs()]}

    def _queue(self) -> list[dict]:
        out = []
        for vol, pg in self.queue():
            payload = supplied.read(vol, pg)
            p = payload["provenance"]
            texts = [it.name for it in supplied.items_of(payload) if it.kind != "figure"]
            out.append({"volume": vol, "page": pg, "label": self.label(vol, pg),
                        "level": supplied.level(payload), "strips": len(texts),
                        "checked": sum(n in p.get("checked", {}) for n in texts)})
        return out

    def _strip(self, vol: int, pg: int, name: str) -> Path:
        path = self.strips_dir / "strips" / supplied.page_id(vol, pg) / f"{name}.png"
        with self.lock:
            if not path.exists():
                desk.cut_strips(supplied.read(vol, pg), current_corpus().page_scan(vol, pg),
                                self.strips_dir)
        return path

    def _article(self, vol: int, pg: int) -> dict:
        payload = supplied.read(vol, pg)
        draft = self.drafts.get((vol, pg))
        if draft:
            payload = copy.deepcopy(payload)
            payload["provenance"]["readings"].update(draft)
            supplied.reassemble(payload)
        return desk.page_article(payload, self.label(vol, pg) + (" (unsaved)" if draft else ""))

    def _save(self, vol: int, pg: int, strips: dict) -> str:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = {desk.strip_id(vol, pg, name): {"text": s.get("text"), "status": s.get("status"),
                                               "updated_by": self.who, "updated_at": at}
                for name, s in strips.items()}
        with self.lock:
            [line] = desk.apply(rows)
            self.drafts.pop((vol, pg), None)
        return line

    # ── routes ───────────────────────────────────────────────────────────
    def _desk_get(self) -> bool:
        path = self._site_path()[0] or ""
        parts = path.strip("/").split("/")
        if parts[0] != "desk":
            return False
        try:
            if parts == ["desk"]:
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif parts[1:] == ["api", "queue"]:
                self._json(self._queue())
            elif parts[1:3] == ["api", "page"] and len(parts) == 5:
                self._json(self._page(int(parts[3]), int(parts[4])))
            elif parts[1] == "strip" and len(parts) == 5:
                self._send(self._strip(int(parts[2]), int(parts[3]), parts[4].removesuffix(".png"))
                           .read_bytes(), "image/png")
            elif parts[1] == "article" and len(parts) == 3:
                vol, pg = parts[2].removesuffix(".json").split("-")
                self._json(self._article(int(vol), int(pg)))
            else:
                self.send_error(404)
        except FileNotFoundError as e:
            self._json({"error": str(e)}, 404)
        return True

    def do_GET(self):
        if not self._desk_get():
            super().do_GET()

    def do_POST(self):
        path = self._site_path()[0] or ""
        if not path.startswith("/desk/api/"):
            return super().do_POST()
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        vol, pg = int(body["volume"]), int(body["page"])
        try:
            if path == "/desk/api/preview":
                self.drafts[(vol, pg)] = body["readings"]
                self._json({"ok": True})
            elif path == "/desk/api/save":
                self._json({"ok": True, "report": self._save(vol, pg, body["strips"]),
                            "page": self._page(vol, pg)})
            else:
                self.send_error(404)
        except Exception as e:                      # surfaced on the desk, never swallowed
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def serve_desk(queue, label, port: int = 8020, who: str = "proofreader") -> None:
    """Serve the site with the desk beside it at http://localhost:PORT/desk.
    `queue`: () -> [(volume, page)], the pages waiting; `label`: (volume, page)
    -> what the person calls the page; `who`: recorded as the checker."""
    corpus = current_corpus()
    site.Handler.root = corpus.root
    site.Handler.downloads = site._download_home()
    site.Handler.log = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / f"{corpus.key}-desk.log"
    Handler.queue = staticmethod(queue)
    Handler.label = staticmethod(label)
    Handler.who = who
    Handler.strips_dir = corpus.derived("desk")
    print(f"the desk: http://localhost:{port}{corpus.root}/desk")
    with site.Server(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()
