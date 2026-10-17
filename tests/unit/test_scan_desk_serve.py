"""The local desk: a page proofread whole, rendered by the site's own chain,
redrawn from the text being edited, and saved back into the supplied page."""
import dataclasses
import json
import threading
import urllib.error
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
    H.who, H.desk_dir, H.drafts = "tester", tmp_path / "desk", {}
    server = desk_serve.site.Server(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.read() if path.endswith(".jpg") else json.loads(r.read())
    yield call
    server.shutdown()


def _opened(call):
    page = call("/desk/api/page/1/29")
    return page["head"], page["text"]


def _save(call, head, text, base, status):
    return call("/desk/api/save", {"volume": 1, "page": 29, "head": head, "text": text,
                                   "base": base, "status": status})


def test_the_desk_lists_its_pages_and_shows_each_whole(desk_at):
    [q] = desk_at("/desk/api/queue")
    assert q == {"volume": 1, "page": 29, "label": "Vol. 1, p. 29", "level": 1}
    page = desk_at("/desk/api/page/1/29")
    head, body, _ = supplied.page_parts(supplied.read(1, 29)["raw_text"])
    assert (page["head"], page["text"], page["checked"]) == (head, body, False)
    assert any(c["open"] == "''" for c in page["constructs"])
    assert desk_at("/desk/scan/1/29.jpg").startswith(b"\xff\xd8")


def test_an_unsaved_edit_is_rendered_and_a_save_writes_it(desk_at):
    head, text = _opened(desk_at)
    edited = text.replace("'''abacus'''", "'''abaca'''")
    desk_at("/desk/api/preview", {"volume": 1, "page": 29, "head": head, "text": edited})
    article = desk_at("/desk/article/1-29.json")
    assert article["title"].endswith("(unsaved)") and "<b>abaca</b>" in article["rendered_html"]
    assert "'''abacus'''" in supplied.read(1, 29)["raw_text"]                    # nothing written yet
    r = _save(desk_at, head, edited, [head, text], "checked")
    assert r["report"] == "vol 1 p29: corrected, level 3 (now proofread)"
    page = supplied.read(1, 29)
    assert "'''abaca'''" in page["raw_text"] and page["provenance"]["page_checked"]["by"] == "tester"
    assert r["page"]["checked"] and not desk_at("/desk/article/1-29.json")["title"].endswith("(unsaved)")


def test_a_desk_older_than_the_engine_refuses_to_save(desk_at, monkeypatch):
    # its assembly would be the old code's (vol 26 p582, 2026-10-15)
    monkeypatch.setattr(desk_serve, "STARTED", 0.0)
    head, text = _opened(desk_at)
    before = supplied.read(1, 29)["raw_text"]
    with pytest.raises(urllib.error.HTTPError) as e:
        _save(desk_at, head, "changed", [head, text], "checked")
    assert "restart the desk" in json.loads(e.value.read())["error"]
    assert supplied.read(1, 29)["raw_text"] == before


def test_a_save_over_a_page_changed_since_it_was_opened_is_refused(desk_at):
    head, text = _opened(desk_at)
    page = supplied.read(1, 29)
    page["provenance"]["readings"]["c1s1"] = "¶\n'''abacus''' (ab′a̤-kus), ''n.'' fixed meanwhile"
    supplied.write(supplied.reassemble(page))                    # written under the editor
    with pytest.raises(urllib.error.HTTPError) as e:
        _save(desk_at, head, text + " edited", [head, text], "checked")
    assert "changed since you opened it" in json.loads(e.value.read())["error"]
    assert "fixed meanwhile" in supplied.read(1, 29)["raw_text"]


def test_the_page_is_linked_as_the_build_links_its_article():
    # the build's xref step, given: what it leaves is what the page shows (a
    # reference resolving to nothing is text — ALGEBRAIC FORMS's "see Gordan")
    seen = []
    article = desk.page_article(payload(), "T", link=lambda body, v, p: seen.append((v, p)) or
                                body.replace("abacus", "abaca"))
    assert seen == [(1, 29)] and "<b>abaca</b>" in article["rendered_html"]


def test_undo_puts_the_page_back_as_it_was_before_each_save(desk_at):
    first = supplied.read(1, 29)["raw_text"]
    head, text = _opened(desk_at)
    _save(desk_at, head, text + " one", [head, text], "unchecked")
    _save(desk_at, head, text + " two", [head, text + " one"], "checked")
    assert len(desk_at("/desk/api/page/1/29")["saves"]) == 2
    r = desk_at("/desk/api/undo", {"volume": 1, "page": 29})
    assert (text + " one") in supplied.read(1, 29)["raw_text"] and len(r["page"]["saves"]) == 1
    assert supplied.level(supplied.read(1, 29)) == 1
    desk_at("/desk/api/undo", {"volume": 1, "page": 29})
    assert supplied.read(1, 29)["raw_text"] == first
    with pytest.raises(urllib.error.HTTPError) as e:
        desk_at("/desk/api/undo", {"volume": 1, "page": 29})
    assert "no save of this page to undo" in json.loads(e.value.read())["error"]


def test_a_save_that_changes_nothing_leaves_nothing_to_undo(desk_at):
    head, text = _opened(desk_at)
    r = _save(desk_at, head, text, [head, text], "unchecked")
    assert r["report"] == "nothing changed" and r["page"]["saves"] == []


def test_the_page_as_saved_clears_the_draft(desk_at):
    head, text = _opened(desk_at)
    desk_at("/desk/api/preview", {"volume": 1, "page": 29, "head": head, "text": text + " edited"})
    assert desk_at("/desk/article/1-29.json")["title"].endswith("(unsaved)")
    desk_at("/desk/api/preview", {"volume": 1, "page": 29, "head": head, "text": text})
    assert not desk_at("/desk/article/1-29.json")["title"].endswith("(unsaved)")


def test_a_page_carrying_on_a_paragraph_is_shown_without_opening_one():
    opens = payload()                                  # its first reading begins ¶
    assert '<div class="body-text"><p>' in desk.page_article(opens, "T")["rendered_html"]
    runs_on = payload()
    runs_on["provenance"]["readings"]["c1s1"] = "'''abacus''' carried on"
    supplied.reassemble(runs_on)
    html = desk.page_article(runs_on, "T")["rendered_html"]
    assert '<div class="body-text" data-runs-on>' in html and "carried on" in html


def test_undo_puts_back_text_set_apart_from_the_readings(desk_at):
    # an older page whose text was set by hand, apart from its readings: a
    # save keeps the page whole, and undo puts it back as it was, never
    # rebuilt from the readings (the older level-2 pages of EB1911)
    page = supplied.read(1, 29)
    page["raw_text"] = page["raw_text"].replace("'''abacus'''", "'''abacus''' set by hand")
    supplied.write(page)
    kept = supplied.read(1, 29)["raw_text"]
    head, text = _opened(desk_at)
    _save(desk_at, head, text + " edited", [head, text], "unchecked")
    desk_at("/desk/api/undo", {"volume": 1, "page": 29})
    assert supplied.read(1, 29)["raw_text"] == kept
