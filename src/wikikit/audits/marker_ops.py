"""The sweeper ratchet's instrument: a marker string-op outside its owner.

The J-campaign closed the preprocess chain and the K-series closed the edges
(2026-08-15/16): every string-op on marker tokens in shipping code lives in the
marker's own producer, a sanctioned decoder, or a reader — and the viewer JS has
NONE.  A line that both names a marker token (`«` literal or `\\u00ab` escape)
and performs a string operation (`re.sub` / `.replace` / `re.compile` in
Python; a regex-method call in JS) is a hit.  The unit of judgment is the FILE —
either a file owns marker work or it does not; line numbers churn.

Each repository keeps its own ledger (tests/ledgers/marker_ops.json): the
OWNERS it grants, the globs it scans, and an ANCHOR — a file that must show up
in the scan, so a broken glob fails instead of passing vacuously.
`tools/diagnostics` and tests are out of scope on purpose: audit code reads
markers by trade, and a diagnostic that transforms is caught by what it ships
— nothing.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

PY_OP = re.compile(r"re\.sub\(|\.replace\(|re\.compile\(")
JS_OP = re.compile(r"(?:replace|match|split)\s*\(\s*/")


def marker_line(line: str) -> bool:
    return "«" in line or "\\u00ab" in line


def _marker_op_lines(root: Path, globs, op_re) -> dict[str, list[int]]:
    hits: dict[str, list[int]] = {}
    for pat in globs:
        for f in glob.glob(str(root / pat), recursive=True):
            if "__pycache__" in f:
                continue
            rel = str(Path(f).relative_to(root)).replace("\\", "/")
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if marker_line(line) and op_re.search(line):
                        hits.setdefault(rel, []).append(i)
    return hits


def python_hits(root: Path, globs) -> dict[str, list[int]]:
    """Python files under `globs` with a marker string-op, file -> lines."""
    return _marker_op_lines(root, globs, PY_OP)


def js_hits(root: Path, globs) -> dict[str, list[int]]:
    """Viewer JS/HTML under `globs` with a marker regex, file -> lines."""
    return _marker_op_lines(root, globs, JS_OP)
