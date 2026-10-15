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
import time
from datetime import datetime, timezone
from pathlib import Path

from wikikit.corpora import current_corpus
from wikikit.scan import desk, supplied
from wikikit.scan.vocabulary import constructs
from wikikit.site import serve as site

PAGE = Path(__file__).resolve().parent / "desk_local.html"
#: The engine's code, and when this process loaded it (`Handler._save`).
ENGINE = Path(__file__).resolve().parents[1]
STARTED = time.time()


class _PageLinks:
    """The build's xref resolution (`resolve_xrefs_post.Resolution`) for one
    page: as the article the page belongs to would resolve it — the article
    whose span holds the page, the latest to begin.  Reads the book's
    database and exported index, as the build's pass does: not during a
    rebuild."""

    def __init__(self):
        from wikikit.db.models import Article
        from wikikit.db.session import SessionLocal
        from wikikit.export.article_json import register_stable_id_dedup
        from wikikit.pipeline.resolve_xrefs_post import Resolution
        self.Article = Article
        self.session = SessionLocal()
        register_stable_id_dedup(self.session.query(Article).all())
        self.resolution = Resolution(self.session)

    def link(self, body: str, vol: int, pg: int) -> str:
        from wikikit.export.article_json import _safe_filename
        A = self.Article
        art = (self.session.query(A).filter(A.volume == vol, A.page_start <= pg, A.page_end >= pg,
                                            A.article_type != "plate")
               .order_by(A.page_start.desc(), A.id.desc()).first())
        if art is None:
            raise LookupError(f"vol {vol} page {pg}: no article in the database holds this page")
        return self.resolution.link(body, art.id, _safe_filename(art, art.title).removesuffix(".json"))[0]


_LINKS: list = []


def _page_links() -> _PageLinks:
    """Built once, on the first page shown (it reads the whole corpus)."""
    with Handler.lock:
        if not _LINKS:
            _LINKS.append(_PageLinks())
    return _LINKS[0]


class Handler(site.Handler):
    queue = staticmethod(lambda: [])            # () -> [(volume, page)], set by `serve`
    label = staticmethod(desk.page_label)
    link = None                                 # (body, volume, page) -> body, set by `serve_desk`
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
        return desk.page_article(payload, self.label(vol, pg) + (" (unsaved)" if draft else ""),
                                 link=self.link)

    def _save(self, vol: int, pg: int, strips: dict) -> str:
        # A save assembles the page with the engine this process loaded: an
        # engine changed since would write the page as the old code sets it
        # (vol 26 p582, 2026-10-15: a fix to the assembler undone by a save)
        changed = [p for p in ENGINE.rglob("*.py") if p.stat().st_mtime > STARTED]
        if changed:
            raise RuntimeError(f"the engine has changed since the desk started ({changed[0].name}): "
                               "restart the desk, then save again — nothing was written")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = {desk.strip_id(vol, pg, name): {"text": s.get("text"), "status": s.get("status"),
                                               "updated_by": self.who, "updated_at": at}
                for name, s in strips.items()}
        with self.lock:
            # each strip's reading as the editor first had it must be the
            # reading on file still: else the page changed under the editor
            # (another tab, a fix written into it) and the save would undo that
            readings = supplied.read(vol, pg)["provenance"]["readings"]
            moved = [n for n, s in strips.items() if "base" in s and readings.get(n) != s["base"]]
            if moved:
                raise RuntimeError(f"the page has changed since you opened it ({', '.join(moved)}): "
                                   "reload it and make your corrections again — nothing was written")
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
        except Exception as e:                      # surfaced on the desk, never swallowed
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
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


def serve_desk(queue, label, port: int = 8020, who: str = "proofreader", links: bool = True) -> None:
    """Serve the site with the desk beside it at http://localhost:PORT/desk.
    `queue`: () -> [(volume, page)], the pages waiting; `label`: (volume, page)
    -> what the person calls the page; `who`: recorded as the checker;
    `links`: resolve each page's xrefs as the build does (`_PageLinks`: needs
    the book's database — run with its settings)."""
    corpus = current_corpus()
    site.Handler.root = corpus.root
    site.Handler.downloads = site._download_home()
    site.Handler.log = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / f"{corpus.key}-desk.log"
    Handler.queue = staticmethod(queue)
    Handler.label = staticmethod(label)
    Handler.who = who
    if links:
        Handler.link = staticmethod(lambda body, vol, pg: _page_links().link(body, vol, pg))
    Handler.strips_dir = corpus.derived("desk")
    print(f"the desk: http://localhost:{port}{corpus.root}/desk")
    with site.Server(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()
