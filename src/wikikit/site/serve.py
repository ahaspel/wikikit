"""Local dev server — a book's site, speaking PRODUCTION's URL space.

    uv run python -m wikikit.site.serve [port]      # from the book's root

Two jobs in one server:

1. DETACHED OPERATION: registered as a scheduled task (at logon) running under
   `pythonw.exe`, with the book's repository as its working directory.  With no
   console there is no valid stderr, so `http.server`'s per-request log write
   would raise and kill the connection mid-response — logging goes to a real
   file instead (`<book>-webserver.log` in LOCALAPPDATA), and never raises.

2. PRODUCTION URL SPACE, VERBATIM: the deployed site's URLs are shaped by the
   host's rewrites and the bucket layout (see the book's deploy script).  This
   server maps that space onto the working tree so the site's pages carry ZERO
   local/production switches — every href and fetch is written in the
   production form and works in both worlds.  Under the book's root
   (`Corpus.root`: "" at a domain's root, "/dnb" under one):

     /                        → the site's home.html
     /{page}.html, /{asset}   → the site's file of that name, when it has one:
                                produced per request by `wikikit.site.page`,
                                the function deploy assembles the site with,
                                so an edit shows without a build step
     /article/{sid}[/{slug}]  → the site's viewer.html  (the article rewrite:
                                200-serve the SPA shell; the client routes on
                                location.pathname)
     /data/articles/*         → the book's derived articles
     /data/scans/*            → the book's derived scans
     /data/{name}.json        → the book's derived file of that name; maps.json,
                                hand-curated SOURCE, from its data folder
     /data/images/*           → the book's image store
     /download/{name}         → wherever the build writes it (_download_home):
                                exactly the set deploy uploads, nothing more
     /search-api/*            → proxied to Meilisearch at 127.0.0.1:7700,
                                Authorization REWRITTEN to the local dev key
                                (clients always send the production key)

   Anything else, and anything outside the root, is served relative to the
   book's repository (dev conveniences like /data/derived/… still work).
"""
from __future__ import annotations

import http.server
import mimetypes
import os
import socketserver
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from wikikit.corpora import book_root, current_corpus
from wikikit.site.build import page

# The Greek face's fonts, typed as deploy.sh uploads them (not every Python's
# table knows the suffix).
mimetypes.add_type("font/woff2", ".woff2")

MEILI = "http://127.0.0.1:7700"
#: The key the LOCAL Meilisearch was started with — one instance serves every
#: book on this machine, each under its own index.
MEILI_LOCAL_KEY = "britannica-dev-key"


def _download_home() -> dict[str, str]:
    """/download/ in production is exactly the set deploy copies to the
    bucket's download/ — each bundle from where its builder writes it, plus
    the corpus bundle's manifest and README.  Keyed by basename; a `.sha256`
    sidecar lives beside its file.  Anything else under /download/ is a 404 in
    production and so is a 404 here.  A book with no slug has published no
    archives yet, so it has no downloads."""
    corpus = current_corpus()
    if corpus.slug is None:
        return {}
    from wikikit.export.download import _archive_name
    derived = corpus.derived().as_posix()
    home = {_archive_name(part): derived for part in ("corpus", "maps", "tei")}
    if corpus.sampler_volume is not None:
        home[f"{corpus.slug}-vol{corpus.sampler_volume:02d}.epub"] = "epub"
    home["manifest.json"] = home["README.md"] = corpus.derived("download").as_posix()
    return home


class Handler(http.server.SimpleHTTPRequestHandler):
    root: str               # the book's site root, set by `serve`
    downloads: dict[str, str]
    log: Path

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(book_root()), **kw)

    def log_message(self, fmt, *args):
        # Never raise: a logging failure must not take the response with it.
        try:
            with self.log.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} "
                         f"{self.address_string()} {fmt % args}\n")
        except Exception:
            pass

    def _site_path(self) -> tuple[str | None, str, str]:
        """(path under the book's root — None when outside it —, sep, query)."""
        path, sep, query = self.path.partition("?")
        if not self.root:
            return path, sep, query
        if path == self.root:
            return "/", sep, query
        if path.startswith(self.root + "/"):
            return path[len(self.root):], sep, query
        return None, sep, query

    # ── production-shaped routing ────────────────────────────────────────
    def _serve_site(self, head: bool) -> bool:
        """Answer with a bucket-root file of the site, if the path names one."""
        path = self._site_path()[0]
        if path is None:
            return False
        if path == "/":
            name = "home.html"
        elif path.startswith("/article/"):
            # The article rewrite: serve the SPA shell; the client routes on
            # the id in location.pathname.
            name = "viewer.html"
        else:
            name = path.lstrip("/")
        body = page(name) if name and "/" not in name else None
        if body is None:
            return False
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(name))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)
        return True

    def _rewrite_path(self) -> None:
        """Map the rest of the production URL space onto the working tree (in place)."""
        path, sep, query = self._site_path()
        if path is None:
            return
        corpus = current_corpus()
        folder, rest = None, None
        for prefix, home in (("/data/articles/", corpus.derived("articles")),
                             ("/data/scans/", corpus.derived("scans")),
                             ("/data/images/", corpus.images())):
            if path.startswith(prefix):
                folder, rest = home.as_posix(), path[len(prefix):]
                break
        else:
            if path.startswith("/data/") and path.endswith(".json") and path.count("/") == 2:
                # The viewer's map registry is hand-curated SOURCE, deployed beside
                # the derived JSON; every other /data/{name}.json is derived.
                rest = path[len("/data/"):]
                source = rest == "maps.json" and corpus.has_data(rest)
                folder = corpus.data_dir if source else corpus.derived().as_posix()
            elif path.startswith("/download/"):
                rest = path[len("/download/"):]
                folder = self.downloads.get(rest.removesuffix(".sha256"))
        if folder:
            path = f"/{folder}/{rest}"
        self.path = path + (sep + query if query else "")

    def _search_api(self) -> tuple[str, str] | None:
        """(Meilisearch subpath, query) when the request is for the search API."""
        path, _, query = self._site_path()
        if path is not None and path.startswith("/search-api/"):
            return path[len("/search-api"):], query
        return None

    # ── meilisearch proxy ────────────────────────────────────────────────
    def _proxy_meili(self, subpath: str, query: str, body: bytes | None = None):
        url = MEILI + subpath + (f"?{query}" if query else "")
        req = urllib.request.Request(url, data=body, method=self.command)
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        # Clients send the PRODUCTION search key (there is no local fork);
        # the local Meilisearch only knows the dev master key — swap it in.
        if self.headers.get("Authorization"):
            req.add_header("Authorization", f"Bearer {MEILI_LOCAL_KEY}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header(
                    "Content-Type",
                    resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            msg = b'{"message": "Meilisearch is not running locally on :7700"}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    # ── verbs ────────────────────────────────────────────────────────────
    def do_GET(self):
        search = self._search_api()
        if search:
            self._proxy_meili(*search)
            return
        if self._serve_site(head=False):
            return
        self._rewrite_path()
        super().do_GET()

    def do_HEAD(self):
        search = self._search_api()
        if search:
            self._proxy_meili(*search)
            return
        if self._serve_site(head=True):
            return
        self._rewrite_path()
        super().do_HEAD()

    def do_POST(self):
        search = self._search_api()
        if search:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            self._proxy_meili(*search, body)
            return
        self.send_error(405)


class Server(socketserver.ThreadingTCPServer):
    # Threaded so one slow article fetch can't block the viewer's next request,
    # and address-reusing so a restart doesn't hit TIME_WAIT on the port.
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int) -> None:
    corpus = current_corpus()
    Handler.root = corpus.root
    Handler.downloads = _download_home()
    Handler.log = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / \
        f"{corpus.key}-webserver.log"
    try:
        Handler.log.write_text(
            f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} serving {book_root()} "
            f"on :{port}{corpus.root}/ (production-shaped URLs)\n", encoding="utf-8")
    except Exception:
        pass
    with Server(("", port), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
