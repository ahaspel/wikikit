"""Supplied pages — a book's own transcriptions from its scans — enter by the
same door as fetched pages, and replace a fetched page only where it is
missing, empty or not proofread (user, 2026-10-02) — or found bad as data,
whatever its level (user, 2026-10-03)."""
import json

import pytest

from wikikit.source_pages import SourceLoadError, load_pages, prefer


def page(level, text):
    return {"raw_text": f'<noinclude><pagequality level="{level}" user="x" /></noinclude>{text}'}


@pytest.mark.parametrize("fetched_level, fetched_text, wins", [
    (None, None, "supplied"),          # Wikisource has no page
    (0, "", "supplied"),               # "without text"
    (1, "Tub latb Rbt.", "supplied"),  # raw OCR, not proofread
    (2, "problematic", "fetched"),     # a person has been here; flag, don't replace
    (3, "proofread", "fetched"),
    (4, "validated", "fetched"),
])
def test_which_page_a_book_reads(fetched_level, fetched_text, wins):
    fetched = None if fetched_level is None else page(fetched_level, fetched_text)
    supplied = page(1, "our transcription")
    assert prefer(fetched, supplied) is (supplied if wins == "supplied" else fetched)
    assert prefer(fetched, None) is fetched


@pytest.mark.parametrize("fetched_level", [2, 3, 4])
def test_a_page_found_bad_is_replaced_proofread_or_not(fetched_level):
    """User, 2026-10-03: "if we can determine that the data is lousy, we
    replace it, proofread or not" — the supplied page says why."""
    fetched = page(fetched_level, "{| class=sortable |J.G.A. .. J.G. Aʟɢᴇʀ.")
    supplied = {**page(1, "our transcription"),
                "provenance": {"replaces": "the list reader parses 8 of 36 rows"}}
    assert prefer(fetched, supplied) is supplied


@pytest.mark.parametrize("fetched_level, fetched_text, wins", [
    (None, None, "supplied"),          # nothing else: an incomplete page beats none
    (0, "", "supplied"),               # nor does an empty one
    (1, "Tub latb Rbt.", "fetched"),   # raw OCR with all its text beats a page with a hole
    (3, "proofread", "fetched"),
])
def test_an_incomplete_page_never_replaces_text(fetched_level, fetched_text, wins):
    """A strip the model refused leaves our page level 2, text missing; it
    replaced complete OCR pages and shipped the hole (2026-10-10)."""
    fetched = None if fetched_level is None else page(fetched_level, fetched_text)
    supplied = page(2, "our transcription, a strip missing")
    assert prefer(fetched, supplied) is (supplied if wins == "supplied" else fetched)
    found_bad = {**supplied, "provenance": {"replaces": "found lousy"}}
    assert prefer(fetched, found_bad) is (found_bad if wins == "supplied" else fetched)


@pytest.mark.parametrize("ours_level, fetched_level, fetched_text, wins", [
    (1, 1, "Tub latb Rbt.", "fetched"),   # unchecked: waits behind raw OCR
    (3, 1, "Tub latb Rbt.", "supplied"),  # proofread at the desk: in
    (1, 0, "", "supplied"),               # nothing fetched to wait behind
    (1, None, None, "supplied"),
])
def test_a_book_may_hold_its_pages_until_proofread(monkeypatch, ours_level, fetched_level,
                                                   fetched_text, wins):
    """EB1911 (user, 2026-10-15): a whole page replaces Wikisource's text only
    once a person has checked it — found bad or not."""
    import dataclasses
    from wikikit import corpora
    book = dataclasses.replace(corpora.current_corpus(), supplied_replaces_text_at=3)
    monkeypatch.setattr(corpora, "current_corpus", lambda: book)
    fetched = None if fetched_level is None else page(fetched_level, fetched_text)
    supplied = page(ours_level, "our transcription")
    assert prefer(fetched, supplied) is (supplied if wins == "supplied" else fetched)
    found_bad = {**supplied, "provenance": {"replaces": "found lousy"}}
    assert prefer(fetched, found_bad) is (found_bad if wins == "supplied" else fetched)


def write(base, vol, pg, text, source="wikisource"):
    d = base / f"vol_{vol:02d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"vol{vol:02d}-page{pg:04d}.json").write_text(json.dumps(
        {"volume": vol, "page_number": pg, "source": source, "raw_text": text}), encoding="utf-8")


def test_the_loader_lays_supplied_pages_over_fetched_ones(tmp_path):
    raw, sup = tmp_path / "raw", tmp_path / "supplied"
    write(raw, 1, 1, page(3, "proofread")["raw_text"])
    write(raw, 1, 2, page(1, "ocr junk")["raw_text"])
    write(sup, 1, 1, page(1, "ours")["raw_text"], "scan")   # loses to a proofread page
    write(sup, 1, 2, page(1, "ours")["raw_text"], "scan")   # wins over OCR
    write(sup, 1, 3, page(1, "ours")["raw_text"], "scan")   # fills a gap
    got, _ = load_pages(1, raw_dir=raw, supplied_dir=sup)
    assert [(p.page, "ours" in p.text, p.path.parent.parent.name) for p in got] == [
        (1, False, "raw"), (2, True, "supplied"), (3, True, "supplied")]


def test_a_book_with_only_supplied_pages_needs_no_fetched_volume(tmp_path):
    sup = tmp_path / "supplied"
    write(sup, 2, 7, page(1, "ours")["raw_text"], "scan")
    got, failures = load_pages(2, raw_dir=tmp_path / "raw", supplied_dir=sup)
    assert [p.page for p in got] == [7] and failures == []


def test_a_volume_with_neither_is_still_a_failure(tmp_path):
    with pytest.raises(SourceLoadError):
        load_pages(3, raw_dir=tmp_path / "raw", supplied_dir=tmp_path / "supplied")
