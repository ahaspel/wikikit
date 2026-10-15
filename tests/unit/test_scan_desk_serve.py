"""The local desk: the page rendered by the site's own chain, redrawn from the
readings being edited, and saved back into the supplied page."""
import dataclasses
import json
import threading
import urllib.request

import pytest
from PIL import Image

from test_scan_supplied import payload
from wikikit import corpora
from wikikit.scan import desk, desk_serve, supplied


def test_the_page_is_rendered_by_the_sites_own_chain():
    article = desk.page_article(payload(), "Vol. 1, p. 29")
    html = article["rendered_html"]
    assert "<h1>Vol. 1, p. 29</h1>" in html and '<div class="body-text">' in html
    assert "<b>abacus</b>" in html and "<i>n.</i>" in html
    assert '<img src="/data/images/' in html and "p0029 c1f1" in html     # the figure, set


@pytest.fixture
def desk_at(tmp_path, monkeypatch):
    """A desk server over one supplied page, on a free port."""
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    supplied.write(payload())
    book = dataclasses.replace(corpora.current_corpus(), page_scan=lambda v, p: Image.new("RGB", (600, 450), "white"),
                               page_scan_url=lambda v, p: "scan")
    monkeypatch.setattr(corpora, "current_corpus", lambda: book)
    monkeypatch.setattr(desk_serve, "current_corpus", lambda: book)
    H = desk_serve.Handler
    H.root, H.downloads, H.log = "", {}, tmp_path / "desk.log"
    H.queue, H.label = staticmethod(lambda: [(1, 29)]), staticmethod(lambda v, p: f"Vol. {v}, p. {p}")
    H.who, H.strips_dir, H.drafts = "tester", tmp_path / "desk", {}
    server = desk_serve.site.Server(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.read() if path.endswith(".png") else json.loads(r.read())
    yield call
    server.shutdown()


def test_the_desk_lists_its_pages_and_their_strips(desk_at):
    [q] = desk_at("/desk/api/queue")
    assert q == {"volume": 1, "page": 29, "label": "Vol. 1, p. 29", "level": 1, "strips": 3, "checked": 0}
    page = desk_at("/desk/api/page/1/29")
    assert [s["name"] for s in page["strips"]] == ["head", "c1s1", "c1f1", "c1s2"]
    assert any(c["open"] == "''" for c in page["constructs"])
    assert desk_at("/desk/strip/1/29/c1s1.png").startswith(b"\x89PNG")


def test_an_unsaved_edit_is_rendered_and_a_save_writes_it(desk_at):
    desk_at("/desk/api/preview", {"volume": 1, "page": 29, "readings": {"c1s1": "¶\n'''abaca''' (ab′a̤-kus)"}})
    article = desk_at("/desk/article/1-29.json")
    assert article["title"].endswith("(unsaved)") and "<b>abaca</b>" in article["rendered_html"]
    assert "(ab′ạ-kus)" in supplied.read(1, 29)["raw_text"]                     # nothing written yet
    strips = {n: {"text": None, "status": "checked"} for n in ("head", "c1s2")}
    strips["c1s1"] = {"text": "¶\n'''abaca''' (ab′a̤-kus)", "status": "checked"}
    r = desk_at("/desk/api/save", {"volume": 1, "page": 29, "strips": strips})
    assert r["report"].endswith("level 3 (now proofread)")
    page = supplied.read(1, 29)
    assert "'''abaca'''" in page["raw_text"] and page["provenance"]["checked"]["c1s1"]["by"] == "tester"
    assert not desk_at("/desk/article/1-29.json")["title"].endswith("(unsaved)")
