"""No literal may BECOME a second implementation.

The count of duplicated rule-encoding literals may fall freely. It may not rise
without someone deliberately accepting it:

    uv run python tools/diagnostics/dup_constants.py --accept

Why a ratchet rather than a rule: every duplicate found in this codebase was found
by a human reading code, usually after it had shipped a defect. `tests/unit/
test_marker_emitters.py` catches the grammars someone thought to enumerate; this
catches the ones nobody did, without needing to know in advance which constant is
about to be copied. It is the same move the leak oracle made — stop relying on
anyone noticing, and make the unnoticed case fail.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "diagnostics"))

from dup_constants import BASELINE, collect, collect_symbols  # noqa: E402


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_no_symbol_has_become_a_second_implementation():
    """The class the literal ratchet cannot see.

    `_balanced_end` exists three times with three incompatible signatures and
    shares no literal with itself; `parse_img_meta` and `_link_display` twice
    each. A shadow implementation almost always keeps the obvious name, and that
    is the only handle available without comparing behaviour.
    """
    syms = collect_symbols()
    known = set(_baseline().get("symbols", []))
    new = sorted(set(syms) - known)
    detail = "\n".join(
        f"  {n}()\n" + "\n".join(f"      {loc}" for loc in syms[n]) for n in new)
    assert not new, (
        f"{len(new)} function name(s) now defined in more than one library "
        f"module:\n{detail}\nIf it is the same job, give it one owner and import "
        "it. If the collision is genuinely coincidental, run "
        "tools/diagnostics/dup_constants.py --accept.")


def test_no_literal_has_joined_the_duplicated_set():
    dups = collect()
    known = set(_baseline().get("literals", []))
    new = sorted(set(dups) - known)
    detail = "\n".join(
        f"  {v!r}\n" + "\n".join(f"      {loc}" for loc in dups[v]) for v in new)
    assert not new, (
        f"{len(new)} literal(s) became a second implementation:\n{detail}\n"
        "Give it ONE owner and import it. If the duplication is genuinely "
        "intended, run tools/diagnostics/dup_constants.py --accept.")


def test_the_baseline_is_not_vacuous():
    """A ratchet against an empty or missing baseline would pass forever."""
    assert BASELINE.exists(), "baseline file missing"
    base = _baseline()
    assert len(base.get("literals", [])) > 50, "literal baseline implausibly small"
    assert len(base.get("symbols", [])) > 5, "symbol baseline implausibly small"
    assert len(collect()) > 50, "collector found almost nothing — check its filters"
