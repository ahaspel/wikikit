"""The staged batch run, with the Batch API replaced: pages advance stage by
stage, an answer failing the guard is sent once more in the next batch, and a
finished page is written as a supplied page — the same page `transcribe_page`
would write.  No model is called."""
import json

from wikikit.scan import batch, supplied

import test_scan_cut


class FakeAPI:
    """Batches answered at once: figure finding says "none", each strip reads
    as plenty of text — except one strip whose FIRST answer is empty."""

    def __init__(self):
        self.batches, self.sent, self.empty_once = {}, [], {"c1s2"}

    def submit(self, reqs):
        bid = f"b{len(self.batches)}"
        self.batches[bid] = reqs
        self.sent.append(sorted(reqs))
        return bid

    def status(self, bid):
        return "ended", {}

    def collect(self, bid):
        out = {}
        for cid in self.batches[bid]:
            kind, key = cid.split("_", 2)[1:]
            if kind == "loc":
                out[cid] = ("ok", '{"figures": []}', None)
            elif kind == "read" and key == "head":
                out[cid] = ("ok", "{{RunningHeader|abacus|4|abannition}}", None)
            elif kind == "read" and key in self.empty_once:
                self.empty_once.discard(key)
                out[cid] = ("ok", "", None)                      # not a reading
            else:
                out[cid] = ("ok", "¶\n" + " ".join(["word"] * 900), None)
        return out


def test_a_page_runs_through_the_stages_to_a_supplied_page(tmp_path, monkeypatch):
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    monkeypatch.setattr(batch, "submit", api.submit)
    monkeypatch.setattr(batch, "batch_status", api.status)
    monkeypatch.setattr(batch, "collect", api.collect)
    batch._LEVELS.clear()
    img = test_scan_cut.page()
    scans = lambda v, p: img

    batch.add_pages([(1, 7, "scan of page 7")], scans)
    s1 = batch.advance(scans)                       # sends: find figures
    assert [n for _, n in s1["sent"]] == [1]
    s2 = batch.advance(scans)                       # collects; cuts; sends every strip
    assert len(api.sent[1]) > 3 and all("_read_" in c for c in api.sent[1])
    s3 = batch.advance(scans)                       # collects; one strip was empty: sent again
    assert api.sent[2] == ["v01-p0007_read_c1s2"]
    s4 = batch.advance(scans)                       # collects; the page is finished
    assert s4["finished"] == ["v01-p0007"] or "v01-p0007" in s3["finished"] + s4["finished"]
    assert batch.advance(scans)["pages"] == {"done": 1}

    page = supplied.read(1, 7)
    p = page["provenance"]
    assert p["unread"] == {} and p["refused"] == []
    assert all(p["readings"][i["name"]] for i in p["items"] if i["kind"] != "figure")
    assert p["usage"]["text"]["batch"] is True
    assert page["raw_text"].startswith('<noinclude><pagequality level="1" user="transcriber" />'
                                       "{{RunningHeader|abacus|4|abannition}}")


def test_a_strip_empty_twice_is_recorded_unread_not_stored(tmp_path, monkeypatch):
    (tmp_path / "book.env").write_text("")
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    always_empty = api.collect

    def collect(bid):
        out = always_empty(bid)
        return {cid: (("ok", "", None) if cid.endswith("_c1s2") else r) for cid, r in out.items()}

    monkeypatch.setattr(batch, "submit", api.submit)
    monkeypatch.setattr(batch, "batch_status", api.status)
    monkeypatch.setattr(batch, "collect", collect)
    batch._LEVELS.clear()
    img = test_scan_cut.page()
    scans = lambda v, p: img
    batch.add_pages([(1, 8, "scan of page 8")], scans)
    for _ in range(6):
        batch.advance(scans)
    p = supplied.read(1, 8)["provenance"]
    assert p["unread"] == {"c1s2": "empty"} and p["readings"]["c1s2"] == ""
