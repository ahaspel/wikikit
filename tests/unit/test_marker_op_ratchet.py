"""The sweeper ratchet: a marker string-op outside its owner is a regression.

The instrument and its history are `wikikit.audits.marker_ops`; this
repository's owners, globs and anchor are tests/ledgers/marker_ops.json.  A new
hit means one of two things:

  * the op belongs in a producer/decoder that already owns the marker —
    move it (the usual verdict); or
  * the file genuinely IS a new owner — add it to the ledger with a reason,
    where the diff reviewer can see the claim being made.
"""
from __future__ import annotations

from pathlib import Path

from wikikit.audits import ledger
from wikikit.audits.marker_ops import js_hits, python_hits

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ledger(ROOT, "marker_ops")
OWNERS = LEDGER["owners"]


def test_marker_ops_only_in_owners():
    hits = python_hits(ROOT, LEDGER["py_globs"])
    # Guard on the guard: a broken glob would pass vacuously.
    assert LEDGER["anchor"] in hits, \
        f"scanner found nothing in {LEDGER['anchor']} — the scan itself is broken"
    strays = {f: lines for f, lines in hits.items() if f not in OWNERS}
    assert not strays, (
        "marker string-op outside the owner ledger — a new sweeper, or a new "
        "owner to claim explicitly:\n" + "\n".join(
            f"  {f}: lines {lines}" for f, lines in sorted(strays.items()))
        + "\nMove the op into the marker's producer/decoder, or add the file "
          "to tests/ledgers/marker_ops.json with its reason.")


def test_owner_ledger_carries_no_ghosts():
    """An owner whose file no longer operates on markers (or no longer exists)
    is a stale claim — prune it, so the ledger stays the truth."""
    hits = python_hits(ROOT, LEDGER["py_globs"])
    ghosts = [f for f in OWNERS if f not in hits]
    assert not ghosts, (
        "ledger owners with no marker ops left — remove them:\n  " + "\n  ".join(ghosts))


def test_viewer_js_has_no_marker_regexes():
    """The viewer decodes NOTHING — rendered_html comes baked, and the last
    JS marker regexes (ftCleanMarkup, formatPreview, renderImg's alt sweep)
    guarded provably-plain inputs.  Zero is the contract
    ([[feedback_viewer_no_regex]])."""
    hits = js_hits(ROOT, LEDGER["js_globs"])
    assert not hits, (
        "marker regex in viewer JS — the viewer is mechanical; decode "
        "belongs in Python:\n" + "\n".join(
            f"  {f}: lines {lines}" for f, lines in sorted(hits.items())))
