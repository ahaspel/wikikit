"""Each corpus is read through its ONE reader — the ratchet.

The instrument and its history are `wikikit.audits.corpus_reads`; this
repository's honest exceptions (collection -> {file: why it reads the directory
itself, and how it stays honest}) are tests/ledgers/corpus_reads.json.  The
readers' own behaviour — they refuse to skip, the raw one applies corrections —
is tested with the engine, in test_corpus_readers.py.
"""
from __future__ import annotations

from pathlib import Path

from wikikit.audits import ledger
from wikikit.audits.corpus_reads import problems, stale

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ledger(ROOT, "corpus_reads")


def test_each_corpus_is_read_through_its_one_reader():
    found = problems(ROOT, LEDGER)
    assert not found, (
        "\n\n".join(found) +
        "\n\nEither read through the one reader — which applies the exclusion rule "
        "and RAISES on an unreadable payload — or add the file to that "
        "collection's ledger with the reason it can be trusted to state its own "
        "coverage.")


def test_no_ledger_carries_ghosts():
    """A ledger entry for a file that no longer reads the directory is a stale
    claim — the same rot as a doc asserting a net that does not run."""
    found = stale(ROOT, LEDGER)
    assert not found, "stale ledger entries:\n  " + "\n  ".join(found)
