"""A marker grammar is written in ONE place, and this fails when it isn't.

The instrument is `wikikit.audits.marker_emitters`; the owners (marker name ->
the module allowed to build it, relative to src/) are
tests/ledgers/marker_emitters.json.
"""
from pathlib import Path

import pytest

from wikikit.audits import ledger
from wikikit.audits.marker_emitters import offenders

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name,owner", sorted(ledger(ROOT, "marker_emitters").items()))
def test_marker_is_built_only_by_its_owner(name, owner):
    found = offenders(ROOT, name, owner)
    assert not found, (
        f"«{name}» is constructed outside {owner}. A marker grammar written in "
        f"more than one place drifts — that is how «LN»'s target/display order "
        f"came to disagree between the producer and the export. Build it through "
        f"the owner's emitter instead:\n  " + "\n  ".join(found))
