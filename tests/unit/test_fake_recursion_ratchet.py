"""No pattern may recognise nesting by writing the levels out.

The campaign named this class at the start — "16 fake-recursion regexes -> one
shared balanced scanner" — with `fake_recursion_audit.py` as its measure and
`fake_recursion_audit = 0` in the proof-of-done.  The measure was never written,
so the class ran unmeasured for the whole campaign while every class that DID
have a scoreboard closed and stayed closed.

What it cost while nobody was counting: CARNIVORA rendered seven figures with no
legend under any of them.  The caption pattern spelled two levels of `{{...}}`
and EB1911 writes figure legends three deep, so the caption came back EMPTY and
the producer, correctly, emitted a bare image for it.  Every gate was green.  It
was found by a reader opening the page — which is the discovery channel this
ratchet exists to replace.

THE TEST IS THE TOOL.  It asserts on `fake_recursion_audit.audit()` rather than
re-implementing the detection, because a ratchet with its own private copy of the
rule is exactly the shadow path the audit hunts ([[feedback_tune_dont_fork]]).

FIXED_SHAPE findings are deliberately not asserted on: a pattern naming one known
composite fails to MATCH rather than truncating, and an unmatched template leaks
visibly instead of silently ([[feedback_honesty_surface_failures]]).  Gating them
would make this noisy, and a noisy gate gets ignored — which is how the EPUB's
missing-image log line went unread for months.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "diagnostics" / "fake_recursion_audit.py"
EXCEPTIONS = ROOT / "data" / "fake_recursion_exceptions.json"

GATED = {"TRUNCATING", "ENUMERATED", "UNREADABLE"}


def _audit_module():
    spec = importlib.util.spec_from_file_location("fake_recursion_audit", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_pattern_enumerates_nesting_depth():
    mod = _audit_module()
    allowed = json.loads(EXCEPTIONS.read_text(encoding="utf-8")) if EXCEPTIONS.exists() else {}
    findings = [f for f in mod.audit() if f["kind"] in GATED]
    unacknowledged = [f for f in findings if mod.key_for(f) not in allowed]

    detail = "\n".join(
        f"  {f['kind']}  {f['path']}:{f['line']}\n"
        f"      {f['pattern'][:110]}\n"
        f"      key: {mod.key_for(f)}"
        for f in unacknowledged)
    assert not unacknowledged, (
        f"{len(unacknowledged)} pattern(s) enumerate nesting depth:\n{detail}\n"
        "Call wikitext.template_end() / wikitext.split_top_pipes() — a balanced "
        "walk has no depth to exceed. If it genuinely cannot bite, acknowledge it "
        f"in {EXCEPTIONS.name} with the evidence (how many instances exist, how "
        "many nest).")


def test_every_acknowledgement_still_matches_a_pattern():
    """A stale acknowledgement is a licence nobody is using.

    The key hashes the PATTERN, so editing a pattern revokes its acknowledgement
    on purpose — which only works if a key that no longer matches anything is
    noticed rather than accumulating as a permanent free pass.
    """
    mod = _audit_module()
    if not EXCEPTIONS.exists():
        return
    allowed = json.loads(EXCEPTIONS.read_text(encoding="utf-8"))
    live = {mod.key_for(f) for f in mod.audit()}
    stale = sorted(set(allowed) - live)
    assert not stale, (
        f"{len(stale)} acknowledgement(s) in {EXCEPTIONS.name} no longer match any "
        f"pattern:\n" + "\n".join(f"  {k}\n      {allowed[k][:100]}" for k in stale) +
        "\nThe pattern was fixed or moved — delete the entry.")
