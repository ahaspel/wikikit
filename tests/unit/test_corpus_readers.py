"""The two corpus readers are TOTAL — the engine behaviour the corpus-read
ratchet (test_corpus_read_ratchet.py) exists to protect.

`export.corpus.load_corpus` and `source_pages.load_pages` apply their own
exclusion rule and RAISE on a payload they cannot read, because a file missing
its fields is a FAILURE, not a silent skip.  If either became lenient, the
ratchet would be enforcing a rule that no longer buys anything.
"""
from __future__ import annotations

import inspect


def test_both_readers_still_refuse_to_skip():
    from wikikit.export.corpus import load_corpus
    from wikikit.source_pages import load_pages

    for fn, name in ((load_corpus, "load_corpus"), (load_pages, "load_pages")):
        src = inspect.getsource(fn)
        assert "failures.append" in src, "%s no longer records failures" % name
        assert "raise" in src, "%s no longer raises on failure — the skip is back" % name


def test_the_raw_reader_applies_corrections():
    """Corrections are part of READING, not something each caller re-applies.
    `corrections.py` names three stages that "must re-apply" them and warns that a
    stage which forgets "silently no-ops corrections on its path" — the reader is
    what makes that unforgettable ([[feedback_corrections_json]])."""
    from wikikit.source_pages import load_pages
    assert "apply_corrections" in inspect.getsource(load_pages)
