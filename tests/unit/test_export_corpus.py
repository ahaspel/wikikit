"""The corpus loader's contract: a damaged payload SURFACES, never vanishes.

The behaviour these lock down is the whole point of the module — every phase used
to `except: continue` past a corrupt article, which ships it stale (no xrefs, no
rendered_html) or drops it from the public download bundle with every downstream
count still looking right.
"""
import json

import pytest

from britannica.export.corpus import (
    CorpusLoadError, load_corpus, write_corpus, write_payload)


def _write(d, name, payload):
    p = d / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_loads_articles_and_skips_sidecars(tmp_path):
    _write(tmp_path, "01-0001-aaa.json", {"id": 1, "body": "x"})
    _write(tmp_path, "01-0002-bbb.json", {"id": 2, "body": "y"})
    _write(tmp_path, "index.json", [{"filename": "01-0001-aaa.json"}])
    _write(tmp_path, "contributors.json", [])
    payloads, failures = load_corpus(tmp_path)
    assert failures == []
    assert {p.name for p in payloads} == {"01-0001-aaa.json", "01-0002-bbb.json"}


def test_unparseable_payload_raises_not_skipped(tmp_path):
    _write(tmp_path, "01-0001-aaa.json", {"id": 1, "body": "x"})
    (tmp_path / "01-0002-bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CorpusLoadError) as exc:
        load_corpus(tmp_path)
    assert "01-0002-bad.json" in str(exc.value)


def test_missing_required_field_is_a_failure(tmp_path):
    _write(tmp_path, "01-0001-aaa.json", {"body": "no id here"})
    with pytest.raises(CorpusLoadError) as exc:
        load_corpus(tmp_path)
    assert "missing field(s): id" in str(exc.value)


def test_every_failure_is_reported_not_just_the_first(tmp_path):
    for i in range(3):
        (tmp_path / f"01-000{i}-bad.json").write_text("{", encoding="utf-8")
    _, failures = load_corpus(tmp_path, strict=False)
    assert len(failures) == 3


def test_non_strict_returns_failures_without_raising(tmp_path):
    _write(tmp_path, "01-0001-aaa.json", {"id": 1, "body": "x"})
    (tmp_path / "01-0002-bad.json").write_text("{", encoding="utf-8")
    payloads, failures = load_corpus(tmp_path, strict=False)
    assert len(payloads) == 1 and len(failures) == 1


def test_require_is_configurable(tmp_path):
    _write(tmp_path, "01-0001-aaa.json", {"id": 1})     # no body
    payloads, failures = load_corpus(tmp_path, require=("id",))
    assert failures == [] and len(payloads) == 1


def test_round_trip_write_matches_canonical_form(tmp_path):
    p = _write(tmp_path, "01-0001-aaa.json", {"id": 1, "body": "x"})
    payloads, _ = load_corpus(tmp_path)
    payloads[p]["body"] = "changed — ünicode ✓"
    assert write_corpus(payloads) == 1
    reloaded, _ = load_corpus(tmp_path)
    assert reloaded[p]["body"] == "changed — ünicode ✓"
    # canonical on-disk form: indent=2, unescaped non-ASCII
    text = p.read_text(encoding="utf-8")
    assert "\n  " in text and "ünicode" in text


def test_write_payload_writes_one(tmp_path):
    p = tmp_path / "solo.json"
    write_payload(p, {"id": 9, "body": "b"})
    assert json.loads(p.read_text(encoding="utf-8"))["id"] == 9
