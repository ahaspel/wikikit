"""No module-level name may BECOME assigned-and-never-read.

The count of unread module-level constants may fall freely. It may not rise
without someone deliberately accepting it:

    uv run python tools/diagnostics/unread_constants.py --accept

Why a ratchet rather than a rule: the seven found in
`populate_classified_toc.py` on 2026-08-24 were inert for months, and inert is
not harmless — the module's docstring described the dead path they belonged to,
and told a reader the classified TOC was built from hand-marked boundaries.  It
is not.  Nothing failed; the only guard was somebody remembering, and the whole
argument for a ratchet is that "I usually remember" is the same shape as a
comment saying *don't forget*.

The baseline is a record of what was already there, not an endorsement of it.
`RENDERED_MARKER_OPENS` in `markers.py` is in it: a tuple assigned, never read,
and referred to by three separate comments as though it were live.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "diagnostics"))

from unread_constants import BASELINE, collect  # noqa: E402


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_no_constant_has_become_unread():
    base = _baseline()
    found = collect()

    new: list[str] = []
    for path, names in sorted(found.items()):
        known = set(base.get(path, ()))
        for n in names:
            if n not in known:
                new.append(f"{path}::{n}")

    assert not new, (
        f"{len(new)} module-level name(s) are now assigned and never read:\n    "
        + "\n    ".join(new)
        + "\n  Delete them, or use them.  An unread constant is not harmless: it "
          "answers\n  searches and anchors comments on behalf of code that no "
          "longer runs.\n  If it is deliberate, run "
          "tools/diagnostics/unread_constants.py --accept"
    )


def test_the_baseline_does_not_name_things_that_are_now_used():
    """The ratchet must ratchet.  When an entry gets deleted or wired up, it has
    to leave the baseline, or the baseline slowly becomes permission."""
    base = _baseline()
    found = collect()
    stale: list[str] = []
    for path, names in sorted(base.items()):
        live = set(found.get(path, ()))
        for n in names:
            if n not in live:
                stale.append(f"{path}::{n}")
    assert not stale, (
        f"{len(stale)} baseline entr(y/ies) no longer unread — fixed, good:\n    "
        + "\n    ".join(stale)
        + "\n  Run tools/diagnostics/unread_constants.py --accept to bank the win."
    )
