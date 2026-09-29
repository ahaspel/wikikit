"""Every tool's imports must still resolve — a corpse is not an instrument.

The instrument and its history are `wikikit.audits.tool_imports`; this
repository's tool directories and checked packages are
tests/ledgers/tool_imports.json.
"""
from __future__ import annotations

from pathlib import Path

from wikikit.audits import ledger
from wikikit.audits.tool_imports import broken

ROOT = Path(__file__).resolve().parents[2]


def test_every_tool_still_resolves_its_imports():
    cfg = ledger(ROOT, "tool_imports")
    found = broken(ROOT, cfg["tool_dirs"], cfg["packages"])
    assert not found, (
        "these tools import names that no longer exist — they crash before they "
        "can report anything:\n  " + "\n  ".join(found)
        + "\nFix the import or DELETE the tool; a diagnostic that cannot run is "
          "worse than none, because docs and habit keep counting it as coverage.")
