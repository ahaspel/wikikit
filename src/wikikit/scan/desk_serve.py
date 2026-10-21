"""The proofreading desk, served locally on the book's own site server.

The desk shows three things side by side for each page, the page WHOLE: the
ORIGINAL (its scan, levelled), the page RENDERED as it would enter, and the
SOURCE (its running head and text, one text to correct, one check for the
page).  The rendered pane is the site's own viewer showing `desk.page_article`
— the engine's chain, so the desk has no renderer of its own and nothing to
hold in parity.  It is redrawn from the text as it stands in the editor, saved
or not.

Saving writes the text and the check into the supplied page
(`desk.apply_pages`): from then on the page keeps that text, and once checked
it is proofread (level 3) and enters the book at the next rebuild.  Do not
save while a rebuild runs.

Routes, beside everything the site server answers (`wikikit.site.serve`):

    /desk                           the desk page
    /desk/api/queue                 the pages waiting, with their progress
    /desk/api/page/V/P              one page: its head, text and check
    /desk/scan/V/P.jpg              its scan, levelled
    /desk/article/V-P.json          the page as it would enter, for the viewer
    POST /desk/api/preview          {volume, page, head, text}: redraw from these
    POST /desk/api/save             {volume, page, head, text, base, status}
    POST /desk/api/undo             {volume, page}: the page as it was before its last save

Every save first keeps the page as it was (`history/<page>/<time>.json` under
the desk's folder): undo puts the latest back, one save at a time, back to the
page as it came to the desk.

THE PATCH SCREEN (when the book gives `serve_desk` its patches): each patch
laid on a fetched page (`wikikit.scan.patches`) beside the scan, Wikisource's
markup as it stands and as the patch leaves it, for a person to rule KEEP,
DROP or EDIT (the person's own text laid in the patch's place) — the ruling
is final, over every check that flagged the patch or passed it.  The book
owns the patches and the rulings (`PatchSource`):

    /desk/patches                   the patch screen
    /desk/api/patch-queue           the pages with patches, flagged ones counted
    /desk/api/patches/V/P           one page's patches, each with its markup and place
    /desk/pscan/V/P.jpg             the page's scan, levelled by our reading
    POST /desk/api/patch-ruling     {volume, page, old, new, ruling: keep|drop|edit|null, note,
                                     text: the edit's own new text}
"""
from __future__ import annotations

import copy
import json
import os
import re
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
PATCH_PAGE = Path(__file__).resolve().parent / "desk_patches.html"
#: characters of Wikisource's markup shown either side of a patch
PATCH_CONTEXT = 360
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


class PatchSource:
    """What a book gives the patch screen.  A patch is named by its page and
    its exact `old` and `new` text, as the book's own checks name it."""

    def queue(self) -> list[tuple[int, int]]:
        """The pages with patches, in the order the screen offers them."""
        raise NotImplementedError

    def patches(self, volume: int, page: int) -> list[dict]:
        """The page's patches: {kind, start, end, old, new} on the fetched
        page, `flags` (what the book's checks say of it, [] if nothing),
        `chain` ("install" | "drop": what the checks alone would do) and
        `ruling` (a person's {ruling, note, by, at, text}, or None; `text`
        is an edit's new text)."""
        raise NotImplementedError

    def reading(self, volume: int, page: int) -> dict:
        """Our reading of the page (a supplied-page payload): its deskew and
        items place the patch on the scan."""
        raise NotImplementedError

    def rule(self, volume: int, page: int, old: str, new: str, ruling: str | None,
             note: str, by: str, text: str | None = None) -> str:
        """Record a person's ruling (None withdraws it; "edit" lays `text` in
        the patch's place, refused if the book's checks fail it); a report."""
        raise NotImplementedError


_WORD = re.compile(r"[^\W\d_]{3,}")


def patch_place(reading: dict, raw: str, x: dict) -> dict | None:
    """Where a patch stands on the scan: the item of our reading holding its
    new text, found by the real words Wikisource's page sets either side of
    it (OCR spoils some: fewest words first, until exactly one place in our
    reading holds the new text among them), and how far down that item it
    stands.  None when no single place holds it."""
    texts = reading["provenance"].get("readings") or {}
    boxes = {it["name"]: it["box"] for it in reading["provenance"]["items"]}
    plain = lambda s: _WORD.findall(re.sub(r"\{\{[^{}]*\}\}|<[^>]*>|''+", " ", s))
    before = plain(raw[max(0, x["start"] - 160):x["start"]])
    after = plain(raw[x["end"]:x["end"] + 160])
    new = x["new"].strip()[:40]                 # our text: it stands in our reading as is
    if not new:
        return None
    for k, j in ((0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (0, 2), (2, 1), (1, 2), (2, 2)):
        words = [re.escape(w) for w in before[len(before) - k:]] if k else []
        pat = r"\W+".join(words + [re.escape(new)] + [re.escape(w) for w in after[:j]])
        hits = [(name, m.start() / max(1, len(text))) for name, text in texts.items()
                if name in boxes and text for m in re.finditer(pat, text)]
        if len(hits) == 1:
            name, at = hits[0]
            return {"item": name, "box": boxes[name], "at": round(at, 4)}
    return None


def _head_and_text(payload: dict) -> tuple[str, str]:
    head, body, _ = supplied.page_parts(payload["raw_text"])
    return head, body


class Handler(site.Handler):
    queue = staticmethod(lambda: [])            # () -> [(volume, page)], set by `serve`
    label = staticmethod(desk.page_label)
    link = None                                 # (body, volume, page) -> body, set by `serve_desk`
    patch_source: PatchSource | None = None     # the book's patches, set by `serve_desk`
    who = "proofreader"
    desk_dir: Path
    drafts: dict = {}                           # (volume, page) -> (head, text) being edited
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
        head, text = _head_and_text(payload)
        from wikikit.source_pages import RAW_DIR, page_filename, volume_dir
        fetched = volume_dir(vol, RAW_DIR) / page_filename(vol, pg)
        title = json.loads(fetched.read_text(encoding="utf-8")).get("title") if fetched.exists() else None
        return {"volume": vol, "page": pg, "label": self.label(vol, pg), "level": supplied.level(payload),
                "wikisource": ("https://en.wikisource.org/wiki/" + title.replace(" ", "_")) if title else None,
                "scan": f"/desk/scan/{vol}/{pg}.jpg", "head": head, "text": text,
                "checked": supplied.level(payload) >= 3, "notes": desk.page_notes(payload),
                "constructs": [{"name": c.name, "open": c.open, "close": c.close, "says": c.says,
                                "args": list(c.args)} for c in constructs()],
                "saves": [h.stem for h in self._history(vol, pg)]}

    def _queue(self) -> list[dict]:
        return [{"volume": vol, "page": pg, "label": self.label(vol, pg),
                 "level": supplied.level(supplied.read(vol, pg))} for vol, pg in self.queue()]

    def _scan(self, vol: int, pg: int) -> Path:
        path = self.desk_dir / "pages" / f"{supplied.page_id(vol, pg)}.jpg"
        with self.lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                desk.levelled_scan(supplied.read(vol, pg), current_corpus().page_scan(vol, pg)) \
                    .save(path, quality=85, optimize=True)
        return path

    def _article(self, vol: int, pg: int) -> dict:
        payload = supplied.read(vol, pg)
        draft = self.drafts.get((vol, pg))
        if draft:
            payload = copy.deepcopy(payload)
            supplied.set_page_text(payload, *draft, checked=supplied.level(payload) >= 3)
        return desk.page_article(payload, self.label(vol, pg) + (" (unsaved)" if draft else ""),
                                 link=self.link)

    # ── the patch screen ─────────────────────────────────────────────────
    def _patch_queue(self) -> list[dict]:
        out = []
        for vol, pg in self.patch_source.queue():
            ps = self.patch_source.patches(vol, pg)
            out.append({"volume": vol, "page": pg, "label": self.label(vol, pg), "patches": len(ps),
                        "flagged": sum(1 for x in ps if x["flags"]),
                        "ruled": sum(1 for x in ps if x["ruling"])})
        return out

    def _patch_page(self, vol: int, pg: int) -> dict:
        from wikikit.source_pages import fetched_text
        raw = fetched_text(vol, pg) or ""
        reading = self.patch_source.reading(vol, pg)
        rows = []
        for x in self.patch_source.patches(vol, pg):
            a, b = max(0, x["start"] - PATCH_CONTEXT), min(len(raw), x["end"] + PATCH_CONTEXT)
            rows.append({**x, "before": raw[a:x["start"]], "after": raw[x["end"]:b],
                         "stands": raw[x["start"]:x["end"]] == x["old"],
                         "place": patch_place(reading, raw, x)})
        return {"volume": vol, "page": pg, "label": self.label(vol, pg),
                "scan": f"/desk/pscan/{vol}/{pg}.jpg", "patches": rows}

    def _patch_scan(self, vol: int, pg: int) -> Path:
        path = self.desk_dir / "patch_pages" / f"{supplied.page_id(vol, pg)}.jpg"
        with self.lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                desk.levelled_scan(self.patch_source.reading(vol, pg), current_corpus().page_scan(vol, pg)) \
                    .save(path, quality=85, optimize=True)
        return path

    def _history(self, vol: int, pg: int) -> list[Path]:
        return desk.saved_versions(self.desk_dir, vol, pg)

    @staticmethod
    def _engine_unchanged() -> None:
        # A write assembles the page with the engine this process loaded: an
        # engine changed since would write the page as the old code sets it
        # (vol 26 p582, 2026-10-15: a fix to the assembler undone by a save)
        changed = [p for p in ENGINE.rglob("*.py") if p.stat().st_mtime > STARTED]
        if changed:
            raise RuntimeError(f"the engine has changed since the desk started ({changed[0].name}): "
                               "restart the desk, then try again — nothing was written")

    def _undo(self, vol: int, pg: int) -> str:
        """The page as it was before its last save, put back as it was kept:
        never rebuilt from its readings, which would lose text set apart from
        them (the older level-2 pages')."""
        with self.lock:
            history = self._history(vol, pg)
            if not history:
                raise RuntimeError("no save of this page to undo")
            last = history[-1]
            payload = json.loads(last.read_text(encoding="utf-8"))
            supplied.write(payload)
            last.unlink()
            self.drafts.pop((vol, pg), None)
        return (f"restored the page as it was before the save of {last.stem} "
                f"(level {supplied.level(payload)})")

    def _save(self, vol: int, pg: int, body: dict) -> str:
        self._engine_unchanged()
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            # the page as the editor first had it must be the page on file
            # still: else it changed under the editor (another tab, a fix
            # written into it) and the save would undo that
            if "base" in body and list(_head_and_text(supplied.read(vol, pg))) != body["base"]:
                raise RuntimeError("the page has changed since you opened it: "
                                   "reload it and make your corrections again — nothing was written")
            # the page as it was, kept before it is written: undo puts it back
            desk.keep_version(self.desk_dir, vol, pg)
            lines = desk.apply_pages({supplied.page_id(vol, pg): {
                "head": body["head"], "text": body["text"], "status": body.get("status"),
                "updated_by": self.who, "updated_at": at}})
            self.drafts.pop((vol, pg), None)
        if not lines:
            self._history(vol, pg)[-1].unlink()          # nothing written: nothing to undo
            return "nothing changed"
        return lines[0]

    # ── routes ───────────────────────────────────────────────────────────
    def _desk_get(self) -> bool:
        path = self._site_path()[0] or ""
        parts = path.strip("/").split("/")
        if parts[0] != "desk":
            return False
        try:
            if parts == ["desk"]:
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif parts[1:2] in (["patches"], ["pscan"]) or parts[1:3] in (["api", "patch-queue"],
                                                                         ["api", "patches"]):
                if self.patch_source is None:
                    self._json({"error": "this book gives the desk no patches"}, 404)
                elif parts[1:] == ["patches"]:
                    self._send(PATCH_PAGE.read_bytes(), "text/html; charset=utf-8")
                elif parts[1:] == ["api", "patch-queue"]:
                    self._json(self._patch_queue())
                elif parts[1:3] == ["api", "patches"] and len(parts) == 5:
                    self._json(self._patch_page(int(parts[3]), int(parts[4])))
                elif parts[1] == "pscan" and len(parts) == 4:
                    self._send(self._patch_scan(int(parts[2]), int(parts[3].removesuffix(".jpg"))).read_bytes(),
                               "image/jpeg")
                else:
                    self.send_error(404)
            elif parts[1:] == ["api", "queue"]:
                self._json(self._queue())
            elif parts[1:3] == ["api", "page"] and len(parts) == 5:
                self._json(self._page(int(parts[3]), int(parts[4])))
            elif parts[1] == "scan" and len(parts) == 4:
                self._send(self._scan(int(parts[2]), int(parts[3].removesuffix(".jpg"))).read_bytes(),
                           "image/jpeg")
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
                if [body["head"], body["text"]] == list(_head_and_text(supplied.read(vol, pg))):
                    self.drafts.pop((vol, pg), None)        # back to the page as saved
                else:
                    self.drafts[(vol, pg)] = (body["head"], body["text"])
                self._json({"ok": True})
            elif path == "/desk/api/undo":
                self._json({"ok": True, "report": self._undo(vol, pg), "page": self._page(vol, pg)})
            elif path == "/desk/api/patch-ruling" and self.patch_source is not None:
                if body.get("ruling") not in ("keep", "drop", "edit", None):
                    raise ValueError(f"a ruling is keep, drop, edit or none, not {body.get('ruling')!r}")
                if body.get("ruling") == "edit" and not isinstance(body.get("text"), str):
                    raise ValueError("an edit needs its text")
                with self.lock:
                    report = self.patch_source.rule(vol, pg, body["old"], body["new"], body.get("ruling"),
                                                    body.get("note") or "", self.who,
                                                    body.get("text") if body.get("ruling") == "edit" else None)
                self._json({"ok": True, "report": report, "page": self._patch_page(vol, pg)})
            elif path == "/desk/api/save":
                self._json({"ok": True, "report": self._save(vol, pg, body), "page": self._page(vol, pg)})
            else:
                self.send_error(404)
        except Exception as e:                      # surfaced on the desk, never swallowed
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


def serve_desk(queue, label, port: int = 8020, who: str = "proofreader", links: bool = True,
               patches: PatchSource | None = None) -> None:
    """Serve the site with the desk beside it at http://localhost:PORT/desk.
    `queue`: () -> [(volume, page)], the pages waiting; `label`: (volume, page)
    -> what the person calls the page; `who`: recorded as the checker;
    `links`: resolve each page's xrefs as the build does (`_PageLinks`: needs
    the book's database — run with its settings); `patches`: the book's
    patches, for the patch screen at /desk/patches."""
    corpus = current_corpus()
    site.Handler.root = corpus.root
    site.Handler.downloads = site._download_home()
    site.Handler.log = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / f"{corpus.key}-desk.log"
    Handler.queue = staticmethod(queue)
    Handler.label = staticmethod(label)
    Handler.who = who
    if links:
        Handler.link = staticmethod(lambda body, vol, pg: _page_links().link(body, vol, pg))
    Handler.desk_dir = corpus.derived("desk")
    Handler.patch_source = patches
    print(f"the desk: http://localhost:{port}{corpus.root}/desk")
    if patches is not None:
        print(f"the patch screen: http://localhost:{port}{corpus.root}/desk/patches")
    with site.Server(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()
