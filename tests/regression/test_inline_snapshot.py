"""Snapshot regression test for the Python inline engine (``decode_inline``).

``tests/snapshots/inline/inline_ref.json`` is a battery of ``{input, output}``
records — one marker-string input and its decoded HTML.  For each, re-run
``decode_inline(input, escape=True)`` (the cell/caption path: escape text, then
decode markers) and assert it reproduces the recorded output.

The golden was adjudicated byte-identical to the retired viewer's
``decodeInlineMarkers`` (verify_inline, 37/37); ``decode_inline`` is the sole
inline decoder now, so this is a self-snapshot guarding future changes.

To rebaseline after an intended change: rewrite each record's ``output`` as
``decode_inline(record["input"], escape=True)`` (see ``_rebaseline``), then
adjudicate the git diff.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from britannica.render.inline import decode_inline


REF_PATH = Path("tests/snapshots/inline/inline_ref.json")


def _cases() -> list[tuple[str, str]]:
    if not REF_PATH.exists():
        return []
    return [(r["input"], r["output"]) for r in json.loads(REF_PATH.read_text(encoding="utf-8"))]


def _first_divergence(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


@pytest.mark.parametrize("input_str,expected", _cases(),
                         ids=lambda v: (v[:40] if isinstance(v, str) else ""))
def test_inline_snapshot(input_str, expected):
    got = decode_inline(input_str, escape=True)
    if got != expected:
        i = _first_divergence(got, expected)
        pytest.fail(
            f"inline decode diverged @ char {i} for input {input_str[:56]!r}\n"
            f"  expected: ...{expected[max(0, i - 12):i + 40]!r}\n"
            f"  actual:   ...{got[max(0, i - 12):i + 40]!r}"
        )


def _rebaseline() -> None:
    """Rewrite the golden outputs from ``decode_inline`` (the source of truth).

        python -c "from tests.regression.test_inline_snapshot import _rebaseline; _rebaseline()"
    """
    records = json.loads(REF_PATH.read_text(encoding="utf-8"))
    for r in records:
        r["output"] = decode_inline(r["input"], escape=True)
    REF_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
